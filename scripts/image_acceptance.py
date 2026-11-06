"""Explicit host acceptance: run Puzzle Quest through the installed T9 image MCP."""

import argparse
import asyncio
import json
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

from simqueue.backend import AppleBackend
from simqueue.core import Queue
from simqueue.storage import ImageStore, atomic_json, backend_for, detach_idle


async def run(output, runtime):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    capabilities = output / "private-capabilities.json"
    if capabilities.exists():
        raise RuntimeError("Unfinished cleanup capabilities exist; recover that run first")
    root = Path.home() / "Library/Application Support/simqueue"
    internal = AppleBackend()
    before = internal.devices()
    store = ImageStore.load(root)
    if store is None:
        raise RuntimeError("Configure the T9 image backend first")
    report = {"started_at": time.time(), "checks": [], "storage": store.status()}
    (output / "before.json").write_text(json.dumps(before, indent=2))
    parameters = StdioServerParameters(
        command=str(Path.home() / ".local/bin/simqueue"), args=["serve"]
    )
    leases, uid, bundle, stage = [], None, None, None
    q = Queue(root, backend_for(root))

    async def call(client, name, **kwargs):
        deadline = time.monotonic() + 120
        while True:
            result = await client.call_tool(name, kwargs)
            if not result.is_error:
                return result.structured_content
            message = " ".join(getattr(c, "text", "") for c in result.content)
            if "busy; retry" not in message or time.monotonic() >= deadline:
                raise RuntimeError(f"MCP {name} returned an error: {message}")
            await asyncio.sleep(2)

    try:
        async with (
            Client(parameters, read_timeout_seconds=360) as a,
            Client(parameters, read_timeout_seconds=360) as b,
        ):
            lease = await call(
                a,
                "acquire",
                owner="image-acceptance-a",
                task="T9 Puzzle Quest acceptance",
                device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
                runtime=runtime,
                share=True,
            )
            leases.append(lease["lease_token"])
            atomic_json(capabilities, {"lease_tokens": leases, "uuid": lease["uuid"]})
            if lease["state"] != "granted":
                raise RuntimeError("External simulator allocation failed or queued")
            uid = lease["uuid"]
            report["uuid"] = uid
            report["device_set"] = lease["device_set"]
            assert Path(lease["device_set"]) == store.mount / "Devices"
            assert not (internal.device_root / uid).exists()
            (output / "result.json").write_text(json.dumps(report, indent=2))
            joined = await call(
                b,
                "join",
                share_token=lease["share_token"],
                owner="image-acceptance-b",
                task="T9 Puzzle Quest shared protection",
            )
            leases.append(joined["lease_token"])
            atomic_json(capabilities, {"lease_tokens": leases, "uuid": uid})
            assert joined["uuid"] == uid
            report["checks"].append("Independent MCP connections share one T9-backed device")
            print("Created T9-backed device; booting", uid, flush=True)
            await call(a, "boot", lease_token=lease["lease_token"])
            print("Boot completed; verifying Contacts and installing Puzzle Quest", flush=True)
            device = Path(lease["device_set"]) / uid
            initialization = json.loads((device / ".simqueue-initialization.json").read_text())
            assert initialization["contacts"] == "empty-on-first-boot"
            report["initialization"] = initialization
            q.backend.run("launch", uid, "com.apple.MobileAddressBook")
            await asyncio.sleep(4)
            report["contacts_capture"] = await call(
                a, "capture", lease_token=lease["lease_token"], filename="contacts.png"
            )
            q.backend.run("terminate", uid, "com.apple.MobileAddressBook")
            bundle = "com.explecorp.combothronepuzzlequest"
            source = None
            for device in before:
                base = internal.device_root / device / "data/Containers/Bundle/Application"
                for candidate in base.glob("*/*.app"):
                    info = plistlib.loads((candidate / "Info.plist").read_bytes())
                    if info.get("CFBundleIdentifier") == bundle:
                        source = candidate
                        break
                if source:
                    break
            if source is None:
                raise RuntimeError("Existing Puzzle Quest simulator build not found")
            installed = await call(
                a, "install", lease_token=lease["lease_token"], app_path=str(source)
            )
            assert installed["bundle_id"] == bundle
            launched = q.backend.run("launch", uid, bundle)
            pid = int(launched.strip().rsplit(":", 1)[1])
            await call(a, "track_process", lease_token=lease["lease_token"], pid=pid)
            await asyncio.sleep(12)
            process = await asyncio.to_thread(
                subprocess.run,
                ["/bin/ps", "-p", str(pid), "-o", "comm="],
                capture_output=True,
                text=True,
                check=False,
            )
            assert process.returncode == 0 and str(store.mount / "Devices" / uid) in process.stdout
            report["app"] = {"bundle_id": bundle, "pid": pid, "source": str(source)}
            report["checks"].append("App installed, launched and remained running from image data")
            capture = await call(
                a, "capture", lease_token=lease["lease_token"], filename="puzzle-quest.png"
            )
            assert Path(capture["path"]).exists() and capture["width"] > 0 and capture["height"] > 0
            report["capture"] = capture
            from investigate_migration import reports_since

            report["migration_crashes"] = [
                r
                for r in reports_since(report["started_at"], uid)
                if r["process"] == "com.apple.migrationpluginwrapper"
            ]
            assert not report["migration_crashes"], report["migration_crashes"]
            print("App running; screenshot exported:", capture["path"], flush=True)
            q.backend.run("terminate", uid, bundle)
            await asyncio.sleep(1)
            await call(a, "release", lease_token=lease["lease_token"])
            assert uid in q.backend.devices()
            report["checks"].append("First release preserves the other agent's held device")
            released = await call(b, "release", lease_token=joined["lease_token"])
            assert released["device_state"] == "deleted", released
            report["checks"].append("Final release deletes device UUID and directory")
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        try:
            # Do not put a fallible inventory probe before releasing leases. An
            # unresponsive service must leave recoverable capabilities on T9.
            if uid and bundle:
                try:
                    q.backend.run("terminate", uid, bundle)
                except RuntimeError:
                    pass
                await asyncio.sleep(1)
            for token in leases:
                q.release(token)
            if uid:
                assert uid not in q.backend.devices() and not q.backend.data_exists(uid)
            if stage and stage.exists():
                shutil.rmtree(stage)
            report["final_queue"] = q.status(sweep=True)
            if not any(d["state"] != "deleted" for d in report["final_queue"]["devices"]):
                report["detach"] = detach_idle(q)
                assert not os.path.ismount(store.mount)
            after = internal.devices()
            assert set(after) == set(before) and all(
                after[k]["state"] == before[k]["state"] for k in before
            )
            report["checks"].append(
                "Internal devices unchanged; test data deleted; image cleanly ejected"
            )
            report["cleanup_verified"] = True
            if capabilities.exists():
                capabilities.unlink()
        except Exception as exc:
            report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            report["success"] = (
                len(report["checks"]) == 5
                and not report.get("failure")
                and not report.get("cleanup_error")
            )
            report["finished_at"] = time.time()
            (output / "result.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "success": report["success"],
                "checks": report["checks"],
                "capture": report.get("capture"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", default="com.apple.CoreSimulator.SimRuntime.iOS-26-5")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.runtime))
