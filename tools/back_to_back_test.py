"""Regression test for "a turn is already running": sends a second prompt the instant the
first turn ends, as a user clicking Send right away would.

    uv run python tools/back_to_back_test.py [backend] [model]
"""
import json
import os
import subprocess
import sys
import time

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
backend = sys.argv[1] if len(sys.argv) > 1 else "vast"
model = sys.argv[2] if len(sys.argv) > 2 else "qwen3.8:27b-q8_0"
prompts = ["Reply with just the word one.", "Reply with just the word two."]
p = subprocess.Popen([sys.executable, "-m", "engine.server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)


def send(m):
    p.stdin.write(json.dumps(m) + "\n")
    p.stdin.flush()


send({"type": "hello", "protocol": 1})
send({"type": "session.start", "session": "s", "cwd": os.path.abspath("sandbox/workdir"), "backend": backend,
      "model": model, "effort": "off", "permission_mode": "default"})
t0, text = time.time(), ""
for line in p.stdout:
    m = json.loads(line)
    t, dt = m["type"], time.time() - t0
    if t == "text.delta":
        text += m["text"]
    elif t == "session.started":
        print(f"{dt:5.1f}s started")
        send({"type": "prompt", "session": "s", "text": prompts.pop(0)})
    elif t == "turn.end":
        print(f"{dt:5.1f}s turn.end {m['status']} reply={text.strip()!r}")
        text = ""
        if prompts:
            send({"type": "prompt", "session": "s", "text": prompts.pop(0)})  # immediately
        else:
            send({"type": "shutdown"})
    elif t == "context":
        print(f"{dt:5.1f}s context {m.get('used')}/{m.get('max')}")
    elif t == "error":
        print(f"{dt:5.1f}s ERROR {m['message']}")
p.wait(timeout=30)
print("exit", p.returncode)
