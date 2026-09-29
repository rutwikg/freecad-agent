"""Smoke test: engine + a model + a specific FreeCAD (by RPC port). Optionally shut down mid-turn.

    uv run python tools/smoke.py PORT "prompt" [shutdown_after_s] [backend] [model] [effort]
"""
import json, os, subprocess, sys, time, urllib.request
port, prompt, early = sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else 0
backend = sys.argv[4] if len(sys.argv) > 4 else "ollama"
model = sys.argv[5] if len(sys.argv) > 5 else "qwen3.8:27b"
effort = sys.argv[6] if len(sys.argv) > 6 else "low"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def vram():
    try:
        return [m["name"] for m in json.load(urllib.request.urlopen("http://localhost:11434/api/ps", timeout=3))["models"]]
    except Exception:
        return "local ollama not reachable"
p = subprocess.Popen([sys.executable, "-m", "engine.server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
send = lambda m: (p.stdin.write(json.dumps(m) + "\n"), p.stdin.flush())
send({"type": "hello", "protocol": 1})
send({"type": "session.start", "session": "s", "cwd": os.path.abspath("sandbox/workdir"), "backend": backend,
      "model": model, "effort": effort, "permission_mode": "acceptEdits", "auto_allow": ["mcp__plugin_"],
      "env": {"FREECAD_RPC_PORT": port}})
t0 = time.time(); first = None; asked = 0
for line in p.stdout:
    m = json.loads(line); t = m["type"]; dt = time.time() - t0
    if t in ("text.delta", "thinking.delta"):
        first = first or dt; continue
    if t == "session.mcp": print(f"{dt:5.1f}s mcp {m['servers']}")
    elif t == "session.started":
        print(f"{dt:5.1f}s started"); t0p = time.time(); send({"type": "prompt", "session": "s", "text": prompt})
    elif t == "tool.start": print(f"{dt:5.1f}s tool {m['name'].replace('mcp__plugin_freecad_freecad__','fc.')} {json.dumps(m['input'])[:130]}")
    elif t == "tool.result": print(f"{dt:5.1f}s   -> {'ERR ' if m['is_error'] else ''}{str(m['content'])[:110]!r}")
    elif t == "permission.request": asked += 1; print(f"{dt:5.1f}s PERMISSION ASKED for {m['tool']}"); send({"type": "permission.reply", "request_id": m["request_id"], "decision": "allow"})
    elif t == "turn.end": print(f"{dt:5.1f}s turn.end {m['status']} turns={m.get('turns')} first-token={first and round(first,1)}s asked={asked}"); send({"type": "shutdown"})
    elif t == "error": print(f"{dt:5.1f}s ERROR {m['message'][:200]}")
    if early and time.time() - t0 > early and t != "turn.end":
        print(f"{dt:5.1f}s -> sending shutdown mid-turn (VRAM now: {vram()})"); send({"type": "shutdown"}); early = 0
p.wait(timeout=30)
print(f"engine exit {p.returncode} after {time.time()-t0:.1f}s; VRAM now: {vram()}")
