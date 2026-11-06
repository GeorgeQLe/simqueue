import asyncio

import pytest
from mcp import Client
from test_queue import Backend, Processes

from simqueue.core import Queue
from simqueue.server import make_server


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_sdk_protocol(tmp_path, mode):
    async def check():
        q = Queue(tmp_path / "state", Backend(), Processes(), max_devices=1)
        async with Client(make_server(q), mode=mode) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == {
                "acquire",
                "join",
                "poll",
                "heartbeat",
                "release",
                "boot",
                "track_process",
                "status",
                "sweep",
                "inventory",
                "capture",
                "install",
            }
            invalid = await client.call_tool("release", {"lease_token": "invalid"})
            assert invalid.is_error
            assert "Unknown lease capability" in str(invalid.content)
            result = await client.call_tool(
                "acquire",
                {
                    "owner": "test",
                    "task": "protocol",
                    "device_type": "phone",
                    "runtime": "ios",
                    "share": True,
                },
            )
            assert not result.is_error
            data = result.structured_content
            assert data["state"] == "granted"
            result = await client.call_tool("release", {"lease_token": data["lease_token"]})
            assert result.structured_content["device_state"] == "deleted"

    asyncio.run(check())
