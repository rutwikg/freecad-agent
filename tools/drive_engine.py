"""Headless test driver for engine.server: plays the GUI's role.

    uv run python tools/drive_engine.py [model] ["prompt" ...]
"""
import json, os, subprocess, sys, threading, time
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
p = subprocess.Popen([sys.executable, "-m", "engine.server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=open("runtime/engine_stderr_drive.log", "w", encoding="utf-8"), text=True, encoding="utf-8", bufsize=1)
def send(m): p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
model = sys.argv[1] if len(sys.argv) > 1 else "gemma4:26b"
prompts = sys.argv[2:] or ["Create a file hello.txt containing 'hi from the engine', then read it back and tell me its contents.", "/freecad:ping"]
send({"type": "hello", "protocol": 1})
send({"type": "session.start", "session": "s1", "cwd": os.path.abspath("sandbox/workdir"),
      "backend": "ollama", "model": model, "permission_mode": "default"})
t0 = time.time(); text = []
for line in p.stdout:
    m = json.loads(line); t = m["type"]
    if t == "text.delta": text.append(m["text"]); continue
    if text: print(f"  [text] {''.join(text)!r}"); text = []
    if t == "thinking.delta": continue
    short = {k: v for k, v in m.items() if k not in ("type", "session")}
    if t == "session.catalog":
        c = m["catalog"]; short = {"model": c["model"], "mcp": c["mcp_servers"], "skills": len(c["skills"] or []), "commands": len(c["commands"] or [])}
    print(f"{time.time()-t0:6.1f}s {t}: {json.dumps(short)[:260]}")
    if t == "session.started": send({"type": "prompt", "session": "s1", "text": prompts.pop(0)})
    elif t == "permission.request": send({"type": "permission.reply", "request_id": m["request_id"], "decision": "allow"})
    elif t == "turn.end":
        if prompts: send({"type": "prompt", "session": "s1", "text": prompts.pop(0)})
        else: send({"type": "sessions.list"})
    elif t == "sessions": send({"type": "shutdown"})
p.wait(); print("engine exit", p.returncode)
