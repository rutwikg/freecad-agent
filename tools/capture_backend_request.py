"""Shows exactly what the Claude Code CLI sends to the model backend, without a model.

Starts a dummy HTTP server that records request bodies, points one engine session at
it via ANTHROPIC_BASE_URL, sends "hi", and prints the generation parameters of the
resulting /v1/messages request (model, max_tokens, thinking, ...).

    uv run python tools/capture_backend_request.py [KEY=VALUE env overrides ...]
"""

import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
bodies: list[dict] = []


class Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if self.path.startswith("/v1/messages") and "count_tokens" not in self.path:
            bodies.append(body)
        self.send_response(404)
        self.end_headers()

    do_GET = do_POST

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 11999), Recorder)
threading.Thread(target=server.serve_forever, daemon=True).start()

env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11999"}
# "session:key=value" sets a session.start field (e.g. session:effort=low); plain KEY=VALUE is env.
fields = {a[8:].split("=", 1)[0]: a[8:].split("=", 1)[1] for a in sys.argv[1:] if a.startswith("session:")}
env.update(dict(a.split("=", 1) for a in sys.argv[1:] if not a.startswith("session:")))
p = subprocess.Popen([sys.executable, "-m", "engine.server"], cwd=ROOT, stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")


def send(m):
    p.stdin.write(json.dumps(m) + "\n")
    p.stdin.flush()


send({"type": "hello", "protocol": 1})
send({"type": "session.start", "session": "c", "cwd": str(ROOT / "sandbox" / "workdir"), "backend": "ollama",
      "model": "qwen3.8:27b", "permission_mode": "default", "env": env, **fields})
deadline = time.time() + 60
for line in p.stdout:
    m = json.loads(line)
    if m["type"] == "session.started":
        send({"type": "prompt", "session": "c", "text": "hi"})
    if m["type"] in ("turn.end", "error") or time.time() > deadline:
        break
send({"type": "shutdown"})
p.wait(timeout=20)
server.shutdown()

main = [b for b in bodies if any(
    (isinstance(msg.get("content"), str) and "hi" in msg["content"]) or
    (isinstance(msg.get("content"), list) and any(c.get("text") == "hi" for c in msg["content"] if isinstance(c, dict)))
    for msg in b.get("messages", []))]
print(f"overrides: {env} session fields: {fields}")
if main:
    system = main[0].get("system")
    text = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system or [])
    print("system prompt includes pack instructions:", "Build geometry incrementally" in text)
print(f"{len(bodies)} /v1/messages requests, {len(main)} carrying the prompt")
for b in main[:1]:
    print(json.dumps({k: v for k, v in b.items() if k not in ("messages", "tools", "system")}, indent=1)[:1500])
