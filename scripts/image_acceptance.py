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
from simqueue.storage import ImageStore, backend_for, detach_idle


async def run(output):
    output.mkdir(parents=True, exist_ok=True)
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
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-26-5",
                share=True,
            )
            leases.append(lease["lease_token"])
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
            assert joined["uuid"] == uid
            report["checks"].append("Independent MCP connections share one T9-backed device")
            print("Created T9-backed device; booting", uid, flush=True)
            await call(a, "boot", lease_token=lease["lease_token"])
            print("Boot completed; installing Puzzle Quest", flush=True)
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
            print("App running; screenshot exported:", capture["path"], flush=True)
            q.backend.run("terminate", uid, bundle)
            await asyncio.sleep(1)
            await call(a, "release", lease_token=lease["lease_token"])
            assert uid in q.backend.devices()
            report["checks"].append("First release preserves the other agent's held device")
            released = await call(b, "release", lease_token=joined["lease_token"])
            assert released["device_state"] == "deleted", released
            report["checks"].append("Final release deletes device UUID and directory")
    finally:
        if uid and bundle and uid in q.backend.devices():
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
        report["success"] = len(report["checks"]) == 5
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
    asyncio.run(run(parser.parse_args().output.resolve()))
