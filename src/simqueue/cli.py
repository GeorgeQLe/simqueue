"""Local stdio service and token-safe JSON-stdin CLI."""

import argparse
import json
import sys
from pathlib import Path

from .backend import Attention
from .core import Queue
from .storage import backend_for, detach_idle, initialize


def main():
    parser = argparse.ArgumentParser(description="Coordinate disposable Apple simulator leases")
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / "Library/Application Support/simqueue"
    )
    parser.add_argument("--max-devices", type=int, default=2)
    parser.add_argument(
        "command",
        choices=[
            "serve",
            "inventory",
            "status",
            "sweep",
            "call",
            "storage-init",
            "storage-status",
            "storage-detach",
        ],
    )
    parser.add_argument("tool", nargs="?", help="For call: arguments come from JSON on stdin")
    parser.add_argument("--apply", action="store_true", help="Apply explicit storage provisioning")
    parser.add_argument("--storage-root", type=Path, help="External APFS mount for storage-init")
    args = parser.parse_args()
    q = Queue(args.state_dir, backend_for(args.state_dir), max_devices=args.max_devices)
    if args.command == "serve":
        from .server import make_server

        make_server(q).run()
        return
    try:
        if args.command == "storage-init":
            result = initialize(q, apply=args.apply, external_root=args.storage_root)
        elif args.command == "storage-status":
            result = (
                q.backend.store.status() if hasattr(q.backend, "store") else {"mode": "internal"}
            )
        elif args.command == "storage-detach":
            result = detach_idle(q)
        elif args.command == "inventory":
            result = q.backend.inventory()
        elif args.command in ("status", "sweep"):
            result = q.status(sweep=args.command == "sweep")
        else:
            allowed = {
                "acquire",
                "join",
                "poll",
                "heartbeat",
                "release",
                "boot",
                "track_process",
                "capture",
                "install",
            }
            if args.tool not in allowed:
                parser.error("Unknown tool")
            values = json.load(sys.stdin)
            if "lease_token" in values:
                values["token"] = values.pop("lease_token")
            if args.tool == "heartbeat":
                result = q.poll(**values, renew=True)
            else:
                result = getattr(q, args.tool)(**values)
        print(json.dumps(result, indent=2))
    except (Attention, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
