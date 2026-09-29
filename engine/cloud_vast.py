"""Vast.ai GPU instances running Ollama, reached only through an SSH tunnel.

Used by tools/cloud/vast.py (command line) and by the engine (tunnel, status, cost guard).
The current instance is remembered in runtime/cloud/vast.json.

Ollama on the instance listens on 127.0.0.1 only; nothing is exposed publicly.
Everything that costs money (create) is explicit; nothing here starts an instance on its own.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "runtime" / "cloud" / "vast.json"
KNOWN_HOSTS = ROOT / "runtime" / "cloud" / "known_hosts"
LABEL = "openclaudecode"


def _vastai() -> list[str]:
    exe = Path(sys.executable).parent / ("vastai.exe" if os.name == "nt" else "vastai")
    return [str(exe)] if exe.exists() else [shutil.which("vastai") or "vastai"]


def run(*args: str, raw: bool = True, timeout: float = 90) -> Any:
    cmd = _vastai() + list(args) + (["--raw"] if raw else [])
    # stdin=DEVNULL: inside the engine stdin is the GUI's pipe, and the CLI (which can page
    # through `less` or prompt) would otherwise wait on it until the timeout.
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                       stdin=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if r.returncode != 0:
        raise RuntimeError(f"vastai {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:500]}")
    if not raw:
        return r.stdout
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout


def ssh_key_path(cfg: dict) -> Path:
    return Path(os.path.expanduser(cfg.get("ssh_key", "~/.ssh/openclaudecode_vast_ed25519")))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


_REGIONS = {
    "Europe": "AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LT LU LV MD ME MK MT "
              "NL NO PL PT RO RS RU SE SI SK TR UA",
    "North America": "US CA MX",
    "South America": "AR BO BR CL CO EC PE PY UY VE",
    "Asia": "BD CN HK ID IN JP KH KR KZ LK MN MY NP PH PK SG TH TW UZ VN",
    "Middle East": "AE BH IL IQ IR JO KW LB OM QA SA",  # note: Vast's "SA" is Saudi Arabia
    "Oceania": "AU NZ",
    "Africa": "DZ EG ET GH KE MA NG TN ZA",
}
_COUNTRY_REGION = {cc: region for region, codes in _REGIONS.items() for cc in codes.split()}


def region_of(geolocation: str) -> str:
    """Vast geolocation looks like 'Texas, US' or ', US': the country code is the last part."""
    cc = (geolocation or "").split(",")[-1].strip().upper()
    return _COUNTRY_REGION.get(cc, "Other")


def gpu_class(name: str, vram_gb: int) -> str:
    n = name.upper()
    for key, label in (("B300", "B200 / B300"), ("B200", "B200 / B300"), ("H200", "H200"), ("H100", "H100"),
                       ("A100", "A100"), ("A800", "A100"), ("RTX PRO 6000", "RTX PRO 6000")):
        if key in n:
            return label
    return "48 GB class" if vram_gb <= 64 else "Other"


def search_offers(min_vram_gb: int = 40, max_price: float | None = None, limit: int = 300) -> list[dict]:
    """Single-GPU offers that can hold the model, cheapest first, with region and GPU class."""
    q = (f"gpu_ram>={min_vram_gb} num_gpus=1 reliability>0.97 inet_down>=200 disk_space>=100 "
         f"rentable=true verified=true")
    if max_price:
        q += f" dph_total<={max_price}"
    offers = run("search", "offers", q, "-o", "dph", "--limit", str(limit))
    if isinstance(offers, dict) and offers.get("error"):
        raise RuntimeError(f"Vast: {offers.get('msg')}")
    # CMP cards are mining GPUs with crippled compute and PCIe: poor for LLMs despite their VRAM.
    offers = [o for o in offers if not o["gpu_name"].upper().startswith("CMP")]
    return [{"id": o["id"], "gpu": o["gpu_name"], "gpu_class": gpu_class(o["gpu_name"], round(o["gpu_ram"] / 1024)),
             "vram_gb": round(o["gpu_ram"] / 1024), "price_h": round(o["dph_total"], 3),
             "where": (o.get("geolocation") or "?").strip(", "), "region": region_of(o.get("geolocation")),
             "down_mbps": round(o.get("inet_down") or 0), "reliability": round(o.get("reliability2") or 0, 3),
             "cuda": o.get("cuda_max_good")} for o in offers]


def onstart_script(model: str) -> str:
    # The ollama/ollama image leaves /root and the authorized_keys Vast writes with modes that
    # sshd's StrictModes rejects ("bad ownership or modes"), so SSH logins fail. Repair them,
    # and keep repairing for a few minutes in case Vast (re)writes the key after this runs.
    fix = ("chown root:root /root /root/.ssh /root/.ssh/authorized_keys 2>/dev/null; chmod 755 /root; "
           "chmod 700 /root/.ssh 2>/dev/null; chmod 600 /root/.ssh/authorized_keys 2>/dev/null")
    return ("bash -c '"
            f"{fix}; (for i in $(seq 1 60); do {fix}; sleep 5; done) >/dev/null 2>&1 & "
            "nohup ollama serve > /root/ollama.log 2>&1 & "
            "for i in $(seq 1 30); do ollama list >/dev/null 2>&1 && break; sleep 2; done; "
            f"ollama pull {model} > /root/pull.log 2>&1 && echo done >> /root/pull.log'")


def create_instance(offer_id: int, model: str, context: int, disk_gb: int = 80) -> dict:
    """Rents the offer. COSTS MONEY from the moment it runs until destroyed."""
    env = (f"-e OLLAMA_HOST=127.0.0.1:11434 -e OLLAMA_CONTEXT_LENGTH={context} "
           f"-e OLLAMA_KV_CACHE_TYPE=q8_0 -e OLLAMA_FLASH_ATTENTION=1 -e OLLAMA_KEEP_ALIVE=-1")
    res = run("create", "instance", str(offer_id), "--image", "ollama/ollama:latest", "--disk", str(disk_gb),
              "--ssh", "--direct", "--label", LABEL, "--env", env, "--onstart-cmd", onstart_script(model))
    if isinstance(res, dict) and res.get("error"):
        raise RuntimeError(f"Vast refused: {res.get('msg')}")
    instance_id = res.get("new_contract") if isinstance(res, dict) else None
    if not instance_id:
        raise RuntimeError(f"unexpected create response: {res!r}")
    save_state({"instance_id": instance_id, "offer_id": offer_id, "model": model, "context": context,
                "created": time.time()})
    return {"instance_id": instance_id}


def instance(instance_id: int | None = None) -> dict | None:
    iid = instance_id or load_state().get("instance_id")
    if not iid:
        return None
    res = run("show", "instances-v1")  # "show instances" is deprecated
    items = res.get("instances", []) if isinstance(res, dict) else (res or [])
    for inst in items:
        if inst.get("id") == iid:
            return inst
    return None


def ssh_endpoint(inst: dict) -> tuple[str, int] | None:
    """Direct SSH (host, port) of the instance, falling back to Vast's SSH proxy."""
    ports = (inst.get("ports") or {}).get("22/tcp")
    if inst.get("public_ipaddr") and ports:
        return inst["public_ipaddr"].strip(), int(ports[0]["HostPort"])
    if inst.get("ssh_host") and inst.get("ssh_port"):
        return inst["ssh_host"], int(inst["ssh_port"])
    return None


def ssh_base(cfg: dict, host: str, port: int) -> list[str]:
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    ssh = os.path.join(os.environ.get("WINDIR", ""), "System32", "OpenSSH", "ssh.exe") if os.name == "nt" else "ssh"
    return [ssh, "-i", str(ssh_key_path(cfg)), "-p", str(port),
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes",
            f"root@{host}"]


def remote(cfg: dict, host: str, port: int, command: str, timeout: float = 30) -> str:
    r = subprocess.run(ssh_base(cfg, host, port) + [command], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return (r.stdout + r.stderr).strip()


def status(cfg: dict, with_remote: bool = True) -> dict:
    """Instance state, price and cost so far; with_remote also asks the box about the model."""
    state = load_state()
    inst = instance()
    if not inst:
        return {"running": False, "instance_id": state.get("instance_id")}
    started = inst.get("start_date") or state.get("created") or time.time()
    hours = max(0.0, (time.time() - started) / 3600)
    price = inst.get("dph_total") or 0
    out = {"running": inst.get("actual_status") == "running", "instance_id": inst["id"],
           "state": inst.get("actual_status"), "gpu": inst.get("gpu_name"), "price_h": round(price, 3),
           "hours": round(hours, 2), "cost_so_far": round(price * hours, 2), "model": state.get("model"),
           "where": inst.get("geolocation")}
    ep = ssh_endpoint(inst)
    if ep:
        out["ssh"] = f"{ep[0]}:{ep[1]}"
    if with_remote and ep and out["running"]:
        try:
            out.update(remote_health(cfg, *ep))
        except Exception as e:
            out["remote_error"] = str(e)
    return out


def remote_health(cfg: dict, host: str, port: int) -> dict:
    """GPU load and memory, watchdog idle counter, models present: one SSH round trip."""
    cmd = ("echo GPU $(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
           "--format=csv,noheader,nounits | head -1 | tr -d ' '); "
           "echo WD $(cat /root/agent-watchdog.log.status 2>/dev/null); "
           "echo MODELS $(OLLAMA_HOST=127.0.0.1:" + str(load_state().get("remote_port", 11434)) +
           " ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | tr '\\n' ' ')")
    out: dict = {}
    for line in remote(cfg, host, port, cmd, timeout=25).splitlines():
        if line.startswith("GPU ") and line.count(",") == 2:
            util, used, total = line[4:].split(",")
            out |= {"gpu_util": int(util), "gpu_mem_used_gb": round(int(used) / 1024, 1),
                    "gpu_mem_total_gb": round(int(total) / 1024)}
        elif line.startswith("WD ") and "idle_min=" in line:
            kv = dict(p.split("=", 1) for p in line[3:].split() if "=" in p)
            out |= {"idle_min": int(kv.get("idle_min", 0)), "watchdog_limit_min": int(kv.get("limit_min", 0))}
        elif line.startswith("MODELS "):
            out["models_present"] = line[7:].split()
    return out


PREPARE_SCRIPT = r"""
set -u
MODEL="__MODEL__"; CTX="__CTX__"
up() { OLLAMA_HOST=127.0.0.1:$1 ollama list >/dev/null 2>&1; }
if ! command -v ollama >/dev/null 2>&1; then
  echo "STEP installing Ollama on the instance"
  command -v curl >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq curl zstd) >/dev/null 2>&1
  curl -fsSL https://ollama.com/install.sh | sh > /root/ollama-install.log 2>&1 || { echo "FAIL Ollama install failed (see /root/ollama-install.log)"; exit 1; }
fi
PORT=""
# Reuse an Ollama on 11434 only if it was started with our context length; a template's
# default context would silently truncate the agent's prompts.
for PID in $(pgrep -f "ollama serve"); do
  if up 11434 && tr '\0' '\n' < /proc/$PID/environ 2>/dev/null | grep -qx "OLLAMA_CONTEXT_LENGTH=$CTX"; then PORT=11434; fi
done
if [ -z "$PORT" ]; then
  if ! up 11435; then
    echo "STEP starting Ollama for the agent (port 11435, context $CTX)"
    OLLAMA_HOST=127.0.0.1:11435 OLLAMA_CONTEXT_LENGTH=$CTX OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_FLASH_ATTENTION=1 \
      OLLAMA_KEEP_ALIVE=-1 nohup ollama serve > /root/ollama-agent.log 2>&1 &
    for i in $(seq 1 40); do up 11435 && break; sleep 1; done
  fi
  PORT=11435
fi
up $PORT || { echo "FAIL Ollama did not start"; exit 1; }
echo "PORT $PORT"
if OLLAMA_HOST=127.0.0.1:$PORT ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
  echo "MODEL present"
elif pgrep -f "ollama pull" >/dev/null 2>&1; then
  echo "MODEL pulling"   # already downloading (e.g. our start-up script after Rent)
else
  echo "MODEL pulling"
  OLLAMA_HOST=127.0.0.1:$PORT nohup sh -c "ollama pull $MODEL && echo done" > /root/pull.log 2>&1 &
fi
"""


WATCHDOG_SCRIPT = r"""
# Safety net for a forgotten GPU: runs on the instance, independent of FreeCAD.
# A minute counts as busy if the GPU is working or a model request arrived; after
# __MINUTES__ idle minutes the instance destroys itself (falls back to stop) with the
# instance-scoped API key Vast pre-installs in the container environment.
N=__MINUTES__; LOG=/root/agent-watchdog.log
eval "$(tr '\0' '\n' < /proc/1/environ | grep -E '^(CONTAINER_API_KEY|CONTAINER_ID|VAST_CONTAINERLABEL)=' | sed 's/^/export /')"
ID="${CONTAINER_ID:-${VAST_CONTAINERLABEL#C.}}"
API=https://console.vast.ai/api/v0/instances/$ID/
last=$(date +%s); seen=""
echo "$(date -u +%FT%TZ) watchdog started: id=$ID limit=${N}min key=$([ -n "$CONTAINER_API_KEY" ] && echo yes || echo no)" >> $LOG
while true; do
  util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  reqs=$(cat /root/ollama*.log 2>/dev/null | grep -c 'POST .*"/v1/messages?')
  now=$(date +%s)
  if [ "${util:-0}" -gt 10 ] || [ "$reqs" != "$seen" ]; then last=$now; seen=$reqs; fi
  idle=$(( (now - last) / 60 ))
  echo "idle_min=$idle limit_min=$N util=${util:-?} requests=$reqs at=$(date -u +%FT%TZ)" > $LOG.status
  if [ "$idle" -ge "$N" ]; then
    echo "$(date -u +%FT%TZ) idle $idle min: destroying" >> $LOG
    curl -s -X DELETE -H "Authorization: Bearer $CONTAINER_API_KEY" "$API" >> $LOG 2>&1; echo >> $LOG
    sleep 60
    echo "$(date -u +%FT%TZ) still here: stopping instead" >> $LOG
    curl -s -X PUT -H "Authorization: Bearer $CONTAINER_API_KEY" -H 'Content-Type: application/json' \
      -d '{"state":"stopped"}' "$API" >> $LOG 2>&1; echo >> $LOG
    sleep 600
  fi
  sleep 60
done
"""


def install_watchdog(cfg: dict, host: str, port: int) -> str:
    """Start the idle watchdog on the instance once (idempotent)."""
    minutes = int(cfg.get("watchdog_minutes", 30))
    body = WATCHDOG_SCRIPT.replace("__MINUTES__", str(minutes))
    script = ("cat > /root/agent-watchdog.sh <<'WATCHDOG_EOF'\n" + body + "\nWATCHDOG_EOF\n"
              "pgrep -f agent-watchdog.sh >/dev/null || nohup bash /root/agent-watchdog.sh >/dev/null 2>&1 &\n"
              "sleep 1; tail -n 1 /root/agent-watchdog.log")
    out = remote_script(cfg, host, port, script, timeout=60)
    lines = [ln for ln in out.splitlines() if "watchdog" in ln or "idle" in ln]
    return lines[-1] if lines else out.strip()[-200:]


def remote_script(cfg: dict, host: str, port: int, script: str, timeout: float = 600) -> str:
    """Run a bash script on the instance (sent on stdin, so no quoting problems)."""
    r = subprocess.run(ssh_base(cfg, host, port) + ["bash", "-s"], input=script, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return r.stdout + r.stderr


def attach(cfg: dict, instance_id: int) -> dict:
    """Connect to an instance the user started (for example on the Vast website): check it is
    running and reachable over SSH, make sure an Ollama with our settings serves the model,
    and remember it. Returns {"ok", "message", "port", "model_state"}."""
    inst = instance(instance_id)
    if not inst:
        return {"ok": False, "message": f"No instance {instance_id} on your Vast account."}
    state = inst.get("actual_status")
    if state != "running":
        return {"ok": False, "message": f"Instance {instance_id} is '{state}'. Wait until it is running."}
    ep = ssh_endpoint(inst)
    if not ep:
        return {"ok": False, "message": "The instance has no SSH port yet. Is it a template with SSH access?"}
    probe = remote(cfg, *ep, "echo SSH_OK", timeout=30)
    if "SSH_OK" not in probe:
        hint = ("SSH refused the key. Check the project key is under Account > SSH Keys on vast.ai. "
                "Instances started from the plain ollama/ollama image have a key-permission problem; "
                "use Rent… in FreeCAD for those.") if "denied" in probe.lower() else probe[-300:]
        return {"ok": False, "message": hint}
    model, ctx = cfg["models"][0], cfg["context_window"]
    out = remote_script(cfg, *ep, PREPARE_SCRIPT.replace("__MODEL__", model).replace("__CTX__", str(ctx)))
    lines = [ln for ln in out.splitlines() if ln.startswith(("STEP", "FAIL", "PORT", "MODEL"))]
    fail = next((ln[5:] for ln in lines if ln.startswith("FAIL")), None)
    if fail:
        return {"ok": False, "message": fail}
    port = next((int(ln.split()[1]) for ln in lines if ln.startswith("PORT")), 11434)
    model_state = next((ln.split()[1] for ln in lines if ln.startswith("MODEL")), "unknown")
    save_state({"instance_id": instance_id, "model": model, "context": ctx, "remote_port": port,
                "attached": True, "created": inst.get("start_date") or time.time()})
    try:
        watchdog = install_watchdog(cfg, *ep)
    except Exception as e:  # the dock's own idle timer still applies
        watchdog = f"watchdog not installed: {e}"
    return {"ok": True, "port": port, "model_state": model_state, "gpu": inst.get("gpu_name"),
            "watchdog": watchdog,
            "message": f"Connected to {inst.get('gpu_name')} ({inst.get('geolocation')}); "
                       + ("model ready" if model_state == "present" else f"downloading {model}…")}


def pull_progress(cfg: dict) -> str:
    """Last line of the model download log on the instance ('' if unknown, 'done' when finished)."""
    inst = instance()
    ep = ssh_endpoint(inst) if inst else None
    if not ep:
        return ""
    raw = remote(cfg, *ep, "tail -c 4000 /root/pull.log 2>/dev/null", timeout=20)
    return summarise_pull(raw)


_UNIT = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
_LAYER = re.compile(r"pulling ([0-9a-f]{6,}):\s+\d+%.*?([\d.]+)\s*([KMGT]?B)\s*/\s*([\d.]+)\s*([KMGT]?B)"
                    r"(?:\s+([\d.]+\s*[KMGT]?B/s))?(?:\s+((?:\d+h)?(?:\d+m)?\d+s))?")


def summarise_pull(raw: str) -> str:
    """Overall progress from `ollama pull` output. The log is a stream of terminal redraws
    (one line per layer, cursor moves in between), so the last line is often a small,
    finished layer; take each layer's latest state and add them up instead."""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw).replace("\r", "\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if any(ln == "done" for ln in lines):
        return "done"
    layers: dict[str, tuple[float, float, str, str]] = {}
    for m in _LAYER.finditer(text):
        done = float(m.group(2)) * _UNIT[m.group(3)]
        total = float(m.group(4)) * _UNIT[m.group(5)]
        layers[m.group(1)] = (done, total, m.group(6) or "", m.group(7) or "")
    tail = lines[-1] if lines else ""
    for phase in ("verifying sha256 digest", "writing manifest", "success"):
        if phase in tail:
            return phase
    if not layers:
        return tail[:120]
    done = sum(d for d, _, _, _ in layers.values())
    total = sum(t for _, t, _, _ in layers.values())
    big = max(layers.values(), key=lambda v: v[1])  # the weights file sets the pace
    eta = f" · ~{big[3]} left" if big[3] and big[3] != "0s" else ""
    speed = f" · {big[2]}" if big[2] else ""
    return f"{100 * done / total:.0f}% · {done / 1e9:.1f}/{total / 1e9:.1f} GB{speed}{eta}"


def destroy(instance_id: int | None = None) -> None:
    """Destroy and VERIFY it is gone before forgetting it: a silent failure would keep billing.
    (The CLI asks its own "are you sure" and aborts without -y.)"""
    iid = instance_id or load_state().get("instance_id")
    if not iid:
        save_state({})
        return
    run("destroy", "instance", str(iid), "-y", raw=False)
    for _ in range(12):
        if instance(iid) is None:
            save_state({})
            return
        time.sleep(5)
    raise RuntimeError(f"instance {iid} still exists after destroy; check the Vast console")


def start(instance_id: int | None = None) -> None:
    """Resume a stopped instance (the model is still on its disk). Fails if the host's GPU
    has been rented by someone else in the meantime."""
    iid = instance_id or load_state().get("instance_id")
    res = run("start", "instance", str(iid))
    if isinstance(res, dict) and res.get("success") is False:
        raise RuntimeError(res.get("msg") or "start refused")


def free_local_port() -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Tunnel:
    """ssh -L local_port -> instance's Ollama, owned by ONE engine.

    Each engine gets its own local port (chosen once, kept across reopens), so closing one
    FreeCAD can never cut off another FreeCAD's chat, and a reopened tunnel keeps the
    address the chat is already using. A fixed port in the config (e.g. for benchmarks)
    is still honoured."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.local_port = int(cfg.get("tunnel", {}).get("local_port") or 0) or free_local_port()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def open(self) -> str:
        if self.alive():
            return "already open"
        inst = instance()
        if not inst or inst.get("actual_status") != "running":
            raise RuntimeError("no running Vast instance (create one with tools/cloud/vast.py)")
        host, port = ssh_endpoint(inst) or (None, None)
        if not host:
            raise RuntimeError("instance has no SSH endpoint yet")
        local = self.local_port
        remote_port = load_state().get("remote_port", 11434)  # 11435 when we run our own Ollama
        cmd = ssh_base(self.cfg, host, port)
        cmd[1:1] = ["-N", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{local}:127.0.0.1:{remote_port}"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=flags)
        return f"tunnel 127.0.0.1:{local} -> {host}:{port}"

    def close(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
