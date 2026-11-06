"""Local stdio service and token-safe JSON-stdin CLI."""

import argparse
import json
import sys
from pathlib import Path

from .backend import AppleBackend, Attention
from .core import Queue


def main():
    parser = argparse.ArgumentParser(description="Coordinate disposable Apple simulator leases")
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / "Library/Application Support/simqueue"
    )
    parser.add_argument("--max-devices", type=int, default=2)
    parser.add_argument("command", choices=["serve", "inventory", "status", "sweep", "call"])
    parser.add_argument("tool", nargs="?", help="For call: arguments come from JSON on stdin")
    args = parser.parse_args()
    q = Queue(args.state_dir, AppleBackend(), max_devices=args.max_devices)
    if args.command == "serve":
        from .server import make_server

        make_server(q).run()
        return
    try:
        if args.command == "inventory":
            result = q.backend.inventory()
        elif args.command in ("status", "sweep"):
            result = q.status(sweep=args.command == "sweep")
        else:
            allowed = {"acquire", "join", "poll", "heartbeat", "release", "boot", "track_process"}
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
