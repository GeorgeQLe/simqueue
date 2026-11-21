"""Local stdio service and token-safe JSON-stdin CLI."""

import argparse
import json
import sys
from pathlib import Path

from .backend import Attention
from .core import Queue
from .storage import backend_for, detach_idle, initialize
from .xctest import XCTestRuns, xcode_test

DEFAULT_STATE = Path.home() / "Library/Application Support/simqueue"


def main():
    parser = argparse.ArgumentParser(description="Coordinate disposable Apple simulator leases")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--max-devices", type=int, help="Capacity for an isolated queue")
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
            "_xctest-worker",
        ],
    )
    parser.add_argument("tool", nargs="?", help="For call: arguments come from JSON on stdin")
    parser.add_argument("--apply", action="store_true", help="Apply explicit storage provisioning")
    parser.add_argument("--storage-root", type=Path, help="External APFS mount for storage-init")
    parser.add_argument("--history", action="store_true", help="Show full queue status")
    args = parser.parse_args()
    default_queue = args.state_dir.absolute() == DEFAULT_STATE and args.max_devices is None
    q = Queue(
        args.state_dir,
        backend_for(args.state_dir),
        max_devices=args.max_devices
        if args.max_devices is not None
        else (1 if default_queue else 2),
        migrate_default_capacity=default_queue,
    )
    if args.command == "serve":
        from .server import make_server

        make_server(q).run()
        return
    runs = XCTestRuns(q)
    try:
        if args.command == "_xctest-worker":
            runs.work(args.tool)
            return
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
            result = q.status(
                sweep=args.command == "sweep", compact=args.command == "status" and not args.history
            )
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
                "xcode-test",
                "run-xctest",
                "xctest-status",
                "xctest-wait",
            }
            if args.tool not in allowed:
                parser.error("Unknown tool")
            values = json.load(sys.stdin)
            if "lease_token" in values:
                values["token"] = values.pop("lease_token")
            if args.tool == "heartbeat":
                result = q.poll(**values, renew=True)
            elif args.tool == "xcode-test":
                xcode_test(q, **values)
                return
            elif args.tool == "run-xctest":
                result = runs.submit(**values)
            elif args.tool == "xctest-status":
                result = runs.status(**values)
            elif args.tool == "xctest-wait":
                result = runs.wait(**values)
            else:
                result = getattr(q, args.tool)(**values)
        print(json.dumps(result, indent=2))
    except (Attention, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
