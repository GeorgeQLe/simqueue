"""Explicit macOS acceptance test; creates, boots and deletes owned test devices.

Run with --output <T9-task-directory>. Never part of routine pytest.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from simqueue.backend import AppleBackend
from simqueue.core import Queue


async def run(output):
    output.mkdir(parents=True, exist_ok=True)
    backend = AppleBackend()
    before = backend.devices()
    (output / "before.json").write_text(json.dumps(before, indent=2))
    state = output / "queue"
    args = ["-m", "simqueue.cli", "--state-dir", str(state), "--max-devices", "1", "serve"]
    parameters = StdioServerParameters(command=sys.executable, args=args)
    leases, ids, child = [], [], None
    report = {"checks": [], "owned_uuids": ids, "preexisting_uuids": sorted(before)}

    async def call(client, name, **kwargs):
        result = await client.call_tool(name, kwargs)
        if result.is_error:
            raise RuntimeError(f"MCP {name} returned an error")
        return result.structured_content

    try:
        async with (
            Client(parameters, read_timeout_seconds=180) as a,
            Client(parameters, read_timeout_seconds=180) as b,
        ):
            first = await call(
                a,
                "acquire",
                owner="acceptance-a",
                task="simqueue-acceptance",
                device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-26-5",
                share=True,
            )
            leases.append(first["lease_token"])
            assert first["state"] == "granted", first
            ids.append(first["uuid"])
            print("Created first disposable simulator", flush=True)
            joined = await call(
                b,
                "join",
                share_token=first["share_token"],
                owner="acceptance-b",
                task="simqueue-acceptance",
            )
            leases.append(joined["lease_token"])
            assert joined["uuid"] == first["uuid"]
            queued = await call(
                a,
                "acquire",
                owner="acceptance-queued",
                task="simqueue-acceptance",
                device_type=first["device_type"],
                runtime=first["runtime"],
            )
            leases.append(queued["lease_token"])
            assert queued["state"] == "queued"
            report["checks"].append("two independent MCP servers share one durable FIFO queue")
            await call(a, "boot", lease_token=first["lease_token"])
            assert backend.devices()[first["uuid"]]["state"] == "Booted"
            print("Booted owned simulator; checking shared protection", flush=True)
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(60)"
            )
            await call(a, "track_process", lease_token=first["lease_token"], pid=child.pid)
            await call(a, "release", lease_token=first["lease_token"])
            assert first["uuid"] in backend.devices()
            released = await call(b, "release", lease_token=joined["lease_token"])
            assert released["attention"] and first["uuid"] in backend.devices()
            report["checks"].append("other holder and registered live process both block deletion")
            child.terminate()
            await asyncio.wait_for(child.wait(), timeout=10)
            child = None
            await call(b, "sweep")
            allocated = await call(a, "poll", lease_token=queued["lease_token"])
            assert allocated["state"] == "granted"
            ids.append(allocated["uuid"])
            assert first["uuid"] not in backend.devices()
            assert not backend.data_exists(first["uuid"])
            print("Verified first deletion and queued allocation", flush=True)
            result = await call(a, "release", lease_token=queued["lease_token"])
            assert result["device_state"] == "deleted"
            report["checks"].append(
                "last release shuts down, deletes, verifies data removal, grants next"
            )
    finally:
        if child is not None:
            child.terminate()
            await asyncio.wait_for(child.wait(), timeout=10)
        # Exact journal/capabilities only. Never touch pre-existing UUIDs or shared runtimes.
        q = Queue(state, backend, max_devices=1)
        for token in leases:
            q.release(token)
        final = q.status(sweep=True)
        (output / "final-state.json").write_text(json.dumps(final, indent=2))
        after = backend.devices()
        for uid in ids:
            assert uid not in after and not backend.data_exists(uid), f"Cleanup incomplete: {uid}"
        assert set(before) <= set(after), "A pre-existing device disappeared"
        assert all(before[uid]["state"] == after[uid]["state"] for uid in before)
        report["checks"].append(
            "owned UUIDs and directories absent; pre-existing devices unchanged"
        )
        report["success"] = len(report["checks"]) == 4
        (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output.resolve()))
