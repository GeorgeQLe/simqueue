"""Explicit disposable-device investigation of Apple's Contacts migration crashes."""

import argparse
import asyncio
import json
import os
import plistlib
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

from simqueue.backend import AppleBackend
from simqueue.core import Queue
from simqueue.storage import backend_for, detach_idle


def reports_since(start, uuid=None):
    reports = []
    for p in (Path.home() / "Library/Logs/DiagnosticReports").glob("*.ips"):
        if uuid is None and p.stat().st_mtime < start:
            continue
        try:
            data = json.loads(p.read_text().split("\n", 1)[1])
        except (ValueError, IndexError, OSError):
            continue
        if (
            uuid is not None
            and data.get("coalitionName") != f"com.apple.CoreSimulator.SimDevice.{uuid}"
        ):
            continue
        if data.get("parentProc") == "launchd_sim":
            threads = data.get("threads", [])
            thread = threads[data.get("faultingThread", 0)] if threads else {}
            reports.append(
                {
                    "file": str(p),
                    "process": data.get("procName"),
                    "time": data.get("captureTime"),
                    "exception": data.get("exception"),
                    "queue": thread.get("queue"),
                    "frames": [f.get("symbol") for f in thread.get("frames", [])[:6]],
                }
            )
    return reports


async def run(output, runtime, fresh_contacts=False):
    output.mkdir(parents=True, exist_ok=True)
    state = Path.home() / "Library/Application Support/simqueue"
    q = Queue(state, backend_for(state))
    before = await asyncio.to_thread(AppleBackend().devices)
    result = {"runtime": runtime, "started": time.time()}
    token, uid = None, None

    async def call(client, name, **args):
        response = await client.call_tool(name, args)
        if response.is_error:
            raise RuntimeError(str(response.content))
        return response.structured_content

    try:
        async with Client(
            StdioServerParameters(command=str(Path.home() / ".local/bin/simqueue"), args=["serve"]),
            read_timeout_seconds=360,
        ) as client:
            inventory = await call(client, "inventory")
            result["runtime_info"] = [
                {k: v for k, v in r.items() if k != "supportedDeviceTypes"}
                for r in inventory["runtimes"]
            ]
            lease = await call(
                client,
                "acquire",
                owner="migration-investigation",
                task=f"Contacts migration {runtime}",
                device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
                runtime=runtime,
            )
            token, uid = lease["lease_token"], lease["uuid"]
            assert uid and lease["state"] == "granted"
            result["uuid"] = uid
            device = Path(lease["device_set"]) / uid
            result["device_set"] = lease["device_set"]
            # Record metadata only, never export regular devices' contact data.
            ab = device / "data/Library/AddressBook"
            result["fresh_contacts"] = fresh_contacts
            if fresh_contacts and ab.exists():
                assert ab.is_dir() and not ab.is_symlink()
                ab.rename(device / "data/Library/AddressBook.seed-backup")
            result["seed"] = [
                {
                    "name": p.name,
                    "size": p.stat().st_size,
                    "mode": oct(p.stat().st_mode & 0o777),
                    "uid": p.stat().st_uid,
                }
                for p in ab.glob("*")
            ]
            (output / "result.json").write_text(json.dumps(result, indent=2))
            print("Booting", runtime, uid, flush=True)
            result["boot_started"] = time.time()
            await call(client, "boot", lease_token=token)
            result["boot_finished"] = time.time()
            print("Boot complete; recording migration diagnostics", flush=True)
            await asyncio.sleep(15)
            for p in (device / "data/Library/Preferences").glob("*migration*"):
                try:
                    result.setdefault("migration_preferences", {})[p.name] = plistlib.loads(
                        p.read_bytes()
                    )
                except (ValueError, plistlib.InvalidFileException):
                    pass
            result["crashes"] = reports_since(result["started"], uid)
            print("Crashes:", [(r["process"], r["queue"]) for r in result["crashes"]], flush=True)
    finally:
        if token:
            released = await asyncio.to_thread(q.release, token)
            result["release"] = released
            assert released["device_state"] == "deleted", released
            assert uid not in await asyncio.to_thread(q.backend.devices)
            assert not await asyncio.to_thread(q.backend.data_exists, uid)
        try:
            result["detach"] = await asyncio.to_thread(detach_idle, q)
        except RuntimeError as exc:
            result["detach_error"] = str(exc)
        after = await asyncio.to_thread(AppleBackend().devices)
        assert set(before) == set(after) and all(
            before[k]["state"] == after[k]["state"] for k in before
        )
        result["cleanup_verified"] = True
        result["image_detached"] = not os.path.ismount(q.backend.store.mount)
        result["finished"] = time.time()
        (output / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--runtime", default="com.apple.CoreSimulator.SimRuntime.iOS-27-0")
    p.add_argument("--fresh-contacts", action="store_true")
    args = p.parse_args()
    asyncio.run(run(args.output, args.runtime, args.fresh_contacts))
