"""Benchmark model variants on the Vast instance: writing (decode) and reading (prefill) speed.

Talks to the instance's Ollama through its own SSH tunnel on 127.0.0.1:11501, so it does not
disturb the engine's tunnel. Refuses to run while the GPU is busy.

    uv run python tools/cloud/bench.py qwen3.8:27b-q8_0 qwen3.8:27b qwen3.8:27b-mtp-q4_K_M
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engine import cloud_vast as vast  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "agent_config.json").read_text(encoding="utf-8"))["backends"]["vast"]
PORT = 11501
BASE = f"http://127.0.0.1:{PORT}"

WRITE_PROMPT = ("Write a complete FreeCAD Python script (no explanations, code only) that builds a cast "
                "steel bracket: a 400x150x30 mm base plate with R20 corners, four 17.3 mm through holes, "
                "a vertical web with a 20 mm fillet to the plate, two ribs, and a boss with a 30 mm bore. "
                "Use PartDesign, name every feature, and recompute at the end.")
READ_PROMPT = ("Summarise the following engineering log in one sentence.\n\n" +
               "Load case LC{i}: Fx = {i}00 N, Fy = 0 N, Fz = -{i}50 N applied at the boss; mesh size 4 mm; "
               "max von Mises 1{i}3 MPa at the web-to-plate fillet; displacement 0.{i}2 mm.\n" * 1) * 1


def api(path, payload=None, timeout=900):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def gpu_busy(host, port) -> str | None:
    out = vast.remote(CFG, host, port, "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits")
    util = [int(x) for x in out.split() if x.isdigit()]
    return f"GPU at {util[-1]}%" if util and util[-1] > 15 else None


def pull(model):
    have = {m["name"] for m in api("/api/tags")["models"]}
    if model in have:
        return 0.0
    t = time.time()
    req = urllib.request.Request(BASE + "/api/pull", data=json.dumps({"model": model}).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        for line in r:
            status = json.loads(line).get("status", "")
            if status == "success":
                break
    return time.time() - t


def run(model, prompt, num_predict):
    return api("/api/generate", {"model": model, "prompt": prompt, "stream": False, "think": False,
                                 "options": {"num_predict": num_predict, "temperature": 0}})


def main(models):
    inst = vast.instance()
    host, port = vast.ssh_endpoint(inst)
    busy = gpu_busy(host, port)
    if busy:
        sys.exit(f"Not benchmarking: {busy} (someone is using the model). Try again when idle.")
    cfg = dict(CFG, tunnel={"local_port": PORT})
    tunnel = vast.Tunnel(cfg)
    print(tunnel.open())
    try:
        for _ in range(40):
            try:
                api("/api/version", timeout=3)
                break
            except Exception:
                time.sleep(0.5)
        long_read = "".join(READ_PROMPT.replace("{i}", str(i % 9 + 1)) for i in range(160))
        rows = []
        for model in models:
            secs = pull(model)
            if secs:
                print(f"pulled {model} in {secs:.0f}s")
            warm = run(model, "Say OK.", 4)  # load into VRAM
            tag = f"[run {model} {time.time():.0f}] "  # different prompt per run: no prefix-cache reuse
            w = run(model, tag + WRITE_PROMPT, 600)
            r = run(model, tag + long_read, 1)
            rows.append((model, warm.get("load_duration", 0) / 1e9,
                         w["eval_count"] / (w["eval_duration"] / 1e9),
                         r["prompt_eval_count"], r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9)))
            print(f"{model}: write {rows[-1][2]:.1f} tok/s, read {rows[-1][4]:.0f} tok/s "
                  f"({rows[-1][3]} tokens), load {rows[-1][1]:.1f}s")
        print("\n| model | writing (tok/s) | reading (tok/s) | load (s) |\n|---|---|---|---|")
        for m, load, wr, n, rd in rows:
            print(f"| {m} | {wr:.1f} | {rd:.0f} | {load:.1f} |")
        ps = api("/api/ps")["models"]
        print("\nloaded now:", [(m["name"], round(m["size_vram"] / 1e9, 1)) for m in ps])
    finally:
        tunnel.close()


if __name__ == "__main__":
    main(sys.argv[1:] or ["qwen3.8:27b-q8_0", "qwen3.8:27b", "qwen3.8:27b-mtp-q4_K_M"])
