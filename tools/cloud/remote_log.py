"""Show recent model requests on the Vast instance (time, status, duration) and GPU load.

    uv run python tools/cloud/remote_log.py [lines]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engine import cloud_vast as vast  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
cfg = json.loads((ROOT / "agent_config.json").read_text(encoding="utf-8"))["backends"]["vast"]
n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
inst = vast.instance()
if not inst:
    sys.exit("no instance")
host, port = vast.ssh_endpoint(inst)
cmd = (f"grep -F 'POST     \"/v1/messages?' /root/ollama.log | tail -n {n}; echo; date -u; "
       "nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader")
out = vast.remote(cfg, host, port, cmd, timeout=40)
print("\n".join(line[:140] for line in out.splitlines() if "Welcome" not in line and "Have fun" not in line))
