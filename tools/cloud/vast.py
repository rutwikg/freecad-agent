"""Manage the Vast.ai GPU that serves models to the agent.

    uv run python tools/cloud/vast.py offers [--min-vram 90] [--max-price 1.5]
    uv run python tools/cloud/vast.py add-ssh-key            # once: registers this project's public key
    uv run python tools/cloud/vast.py create OFFER_ID        # STARTS BILLING (asks to confirm)
    uv run python tools/cloud/vast.py status                 # state, $/h, cost so far, model download
    uv run python tools/cloud/vast.py destroy                # STOPS BILLING, deletes the instance

Model, context and SSH key come from the "vast" backend in agent_config.json.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engine import cloud_vast as vast  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "agent_config.json").read_text(encoding="utf-8"))["backends"]["vast"]


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("offers")
    o.add_argument("--min-vram", type=int, default=90)
    o.add_argument("--max-price", type=float)
    sub.add_parser("add-ssh-key")
    c = sub.add_parser("create")
    c.add_argument("offer_id", type=int)
    c.add_argument("--yes", action="store_true")
    sub.add_parser("status")
    d = sub.add_parser("destroy")
    d.add_argument("--yes", action="store_true")
    a = ap.parse_args()

    if a.cmd == "offers":
        for off in vast.search_offers(a.min_vram, a.max_price):
            print(f"{off['id']:<10} {off['gpu']:<20} {off['vram_gb']:>3} GB  ${off['price_h']:.3f}/h  "
                  f"{off['where'][:24]:<24} {off['down_mbps']:>5} Mb/s  rel {off['reliability']}")

    elif a.cmd == "add-ssh-key":
        pub = vast.ssh_key_path(CFG).with_suffix(".pub").read_text(encoding="utf-8").strip()
        print(vast.run("create", "ssh-key", pub, raw=False))

    elif a.cmd == "create":
        existing = vast.instance()
        if existing:
            sys.exit(f"An instance is already recorded ({existing['id']}, {existing.get('actual_status')}). "
                     f"Destroy it first.")
        model, ctx = CFG["models"][0], CFG["context_window"]
        if not confirm(f"Rent offer {a.offer_id} for {model} (context {ctx})? Billing starts now.", a.yes):
            sys.exit("cancelled")
        res = vast.create_instance(a.offer_id, model, ctx)
        print(f"created instance {res['instance_id']}; waiting for it to boot …")
        for _ in range(90):
            inst = vast.instance(res["instance_id"])
            state = inst.get("actual_status") if inst else None
            print(f"  {time.strftime('%H:%M:%S')} {state}")
            if state == "running" and vast.ssh_endpoint(inst):
                break
            time.sleep(10)
        print(json.dumps(vast.status(CFG, with_remote=False), indent=1))

    elif a.cmd == "status":
        print(json.dumps(vast.status(CFG), indent=1))

    elif a.cmd == "destroy":
        st = vast.status(CFG, with_remote=False)
        if not st.get("instance_id"):
            sys.exit("no instance recorded")
        if not confirm(f"Destroy instance {st['instance_id']} (cost so far ${st.get('cost_so_far')})?", a.yes):
            sys.exit("cancelled")
        vast.destroy(st["instance_id"])
        print("destroyed; billing stopped")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
