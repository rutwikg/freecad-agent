"""Regression test: one engine closing must not cut off another engine's cloud chat, and a
dead tunnel must come back by itself.

Two engines (like two FreeCAD windows) each start a cloud session and send a short prompt.
Engine A is shut down; engine B must still answer. Then B's ssh tunnel process is killed;
B's keep-alive must reopen it and the next prompt must still answer.

    uv run python tools/cloud/tunnel_isolation_test.py
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
MODEL = json.loads((ROOT / "agent_config.json").read_text(encoding="utf-8"))["backends"]["vast"]["models"][0]


class Engine:
    def __init__(self, name):
        self.name = name
        self.p = subprocess.Popen([sys.executable, "-m", "engine.server"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                  encoding="utf-8", bufsize=1)
        self.events, self.text = [], ""
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"type": "hello", "protocol": 1})
        self.send({"type": "session.start", "session": "s", "cwd": str(ROOT / "sandbox" / "workdir"),
                   "backend": "vast", "model": MODEL, "effort": "off", "permission_mode": "default"})

    def _read(self):
        for line in self.p.stdout:
            m = json.loads(line)
            if m["type"] == "text.delta":
                self.text += m["text"]
            self.events.append(m)

    def send(self, m):
        self.p.stdin.write(json.dumps(m) + "\n")
        self.p.stdin.flush()

    def wait(self, kind, timeout=240):
        start = len(self.events)
        t = time.time()
        while time.time() - t < timeout:
            for m in self.events[start:]:
                if m["type"] == kind or m["type"] == "error":
                    return m
            time.sleep(0.2)
        return {"type": "timeout"}

    def ask(self, prompt):
        self.text = ""
        t = time.time()
        self.send({"type": "prompt", "session": "s", "text": prompt})
        end = self.wait("turn.end")
        return end.get("status") or end.get("message") or end["type"], self.text.strip()[:40], round(time.time() - t, 1)

    def close(self):
        self.send({"type": "shutdown"})
        self.p.wait(timeout=30)


def tunnel_pids():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
                          "? { $_.CommandLine -match 'ExitOnForwardFailure' } | "
                          "% { \"$($_.ProcessId) $($_.CommandLine)\" }"], capture_output=True, text=True)
    return [line.split()[0] for line in out.stdout.splitlines() if line.strip()]


a, b = Engine("A"), Engine("B")
print("A started:", a.wait("session.started")["type"], "| B started:", b.wait("session.started")["type"])
print("tunnels open:", len(tunnel_pids()))
print("A asks:", a.ask("Reply with just the word alpha."))
print("B asks:", b.ask("Reply with just the word bravo."))
a.close()
print("A closed; tunnels left:", len(tunnel_pids()))
print("B after A closed:", b.ask("Reply with just the word charlie."))
for pid in tunnel_pids():
    subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
print("killed B's tunnel; waiting for keep-alive …")
time.sleep(15)
print("tunnels after keep-alive:", len(tunnel_pids()))
print("B after tunnel restart:", b.ask("Reply with just the word delta."))
b.close()
print("done; tunnels left:", len(tunnel_pids()))
