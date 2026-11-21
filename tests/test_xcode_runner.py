"""Verify the Xcode entry point uses the lease's custom device set."""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from mcp import Client, StdioServerParameters

from simqueue.xctest import XCTestRuns, xcode_test


@pytest.mark.parametrize(
    "status",
    [
        {"phase": "submitted", "attention": "Worker has not started"},
        {"phase": "interrupted", "cleanup": "pending"},
        {"phase": "finished", "outcome": "passed", "cleanup": "pending"},
    ],
)
def test_wait_surfaces_attention_without_claiming_cleanup(tmp_path, status):
    runs = XCTestRuns(Mock(root=tmp_path))
    runs.status = Mock(return_value=status)
    assert runs.wait("run") == {"wait_result": "attention", **status}


def test_xcode_runner_targets_only_the_lease(tmp_path, monkeypatch):
    tools = tmp_path / ".local/bin"
    tools.mkdir(parents=True)
    (tools / "apple-tools").touch()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SIMULATOR_DEVICE_SET_PATH", "/old/set")
    monkeypatch.setattr(os.path, "ismount", lambda path: path == tmp_path)
    monkeypatch.setattr(
        "simqueue.xctest.subprocess.check_output",
        lambda *args, **kwargs: json.dumps(
            {
                "resources": {"DEVELOPER_STORAGE_ROOT": {"resolvedPath": str(tmp_path)}},
                "authority": {"verdict": "verified"},
            }
        ),
    )
    device_set = str(tmp_path / "Device Volume/Devices")
    queue = Mock()
    queue.poll.return_value = {
        "state": "granted",
        "uuid": "test-uuid",
        "operation": None,
        "device_set": device_set,
        "xcodebuild_args": [f"-DVTSimulatorSetLocation={device_set}"],
    }
    queue.boot.return_value = {"boot_ready": True}
    execv = Mock()
    monkeypatch.setattr(os, "execv", execv)
    args = [
        "-xctestrun",
        str(tmp_path / "test.xctestrun"),
        "-resultBundlePath",
        str(tmp_path / "result.xcresult"),
    ]
    xcode_test(queue, "private-capability", args)
    assert os.environ["SIMULATOR_DEVICE_SET_PATH"] == device_set
    assert execv.call_args.args == (
        tools / "apple-tools",
        [
            str(tools / "apple-tools"),
            "xcodebuild",
            "test-without-building",
            *args,
            "-destination",
            "id=test-uuid",
            "-parallel-testing-enabled",
            "NO",
            f"-DVTSimulatorSetLocation={device_set}",
        ],
    )
    queue.track_process.assert_called_once_with("private-capability", os.getpid())
    with pytest.raises(ValueError, match="Use XCTest plan"):
        xcode_test(queue, "private-capability", [*args, "-destination", "id=other"])
    execv.assert_called_once()


@pytest.fixture
def simulated_host(tmp_path, monkeypatch):
    """Exercise real worker processes and the Apple tool boundary with a fake host."""
    home = tmp_path / "home"
    tools = home / ".local/bin"
    tools.mkdir(parents=True)
    storage = next(path for path in tmp_path.resolve().parents if os.path.ismount(path))
    checker = tools / "developer-host"
    checker.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "if sys.argv[1] == 'queue-metrics':\n"
        "    with (Path.home() / 'metrics.jsonl').open('a') as stream:\n"
        "        stream.write(sys.stdin.read() + '\\n')\n"
        "    sys.exit(0)\n"
        "print("
        + repr(
            json.dumps(
                {
                    "resources": {"DEVELOPER_STORAGE_ROOT": {"resolvedPath": str(storage)}},
                    "authority": {"verdict": "verified"},
                }
            )
        )
        + ")\n"
    )
    checker.chmod(0o700)
    wrapper = tools / "apple-tools"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""\
        import json
        import os
        import sys
        import time
        import uuid
        from pathlib import Path

        args = sys.argv[1:]
        root = Path.home()
        if args[0] == "xcodebuild":
            assert args[1] == "test-without-building"
            uid = args[args.index("-destination") + 1].removeprefix("id=")
            path = Path(os.environ["SIMULATOR_DEVICE_SET_PATH"])
            assert (path / uid).is_dir()
            assert f"-DVTSimulatorSetLocation={path}" in args
            (root / "xcode-started").touch()
            while not (root / "finish-xcode").exists():
                time.sleep(.03)
            Path(args[args.index("-resultBundlePath") + 1]).mkdir()
            print("fake XCTest passed")
            sys.exit(0)

        assert args[:3] == ["xcrun", "simctl", "--set"]
        device_set = Path(args[3])
        command, *values = args[4:]
        inventory = root / "inventory.json"
        devices = json.loads(inventory.read_text()) if inventory.exists() else {}
        if command == "list":
            print(json.dumps({
                "devices": {"ios": list(devices.values())},
                "devicetypes": [{"identifier": "phone"}],
                "runtimes": [{"identifier": "ios", "isAvailable": True}],
            }))
            sys.exit(0)
        if command == "create":
            name, device_type, _ = values
            uid = str(uuid.uuid4()).upper()
            (device_set / uid).mkdir(parents=True)
            devices[uid] = {
                "udid": uid, "name": name, "state": "Shutdown",
                "deviceTypeIdentifier": device_type,
            }
            print(uid)
        else:
            uid = values[0]
            if command == "boot":
                devices[uid]["state"] = "Booted"
            elif command == "bootstatus":
                if (root / "stall").exists():
                    print("Status=2, isTerminal=NO", flush=True)
                    print("  Waiting on Data Migration", flush=True)
                    print("  Reason:Running plugin com.apple.locationd.migrator (example)", flush=True)
                    print("private native fixture detail", flush=True)
                    time.sleep(60)
                else:
                    print("Status=0, isTerminal=YES")
                sys.exit(0)
            elif command == "shutdown":
                devices[uid]["state"] = "Shutdown"
            elif command == "delete":
                (device_set / uid).rmdir()
                del devices[uid]
            else:
                raise AssertionError(command)
        inventory.write_text(json.dumps(devices))
    """)
    )
    wrapper.chmod(0o700)
    # Short deadlines apply to the fake host subprocesses only.
    (tmp_path / "sitecustomize.py").write_text(
        "from simqueue.backend import AppleBackend\n"
        "_budget = AppleBackend.operation_budget\n"
        "AppleBackend.operation_budget = lambda self, seconds=None: "
        "_budget(self, 1.5 if seconds is not None else 2.5)\n"
    )
    package = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(map(str, [tmp_path, package])))
    plan = tmp_path / "test.xctestrun"
    plan.touch()
    result = tmp_path / "result.xcresult"
    options = {
        "run_id": "12345678123456781234567812345678",
        "owner": "test",
        "task": "one-command test",
        "device_type": "phone",
        "runtime": "ios",
        "arguments": ["-xctestrun", str(plan), "-resultBundlePath", str(result)],
    }
    return home, options, ["-m", "simqueue.cli", "--state-dir", str(tmp_path / "queue")]


async def result_of(client, tool, values):
    result = await client.call_tool(tool, values)
    assert not result.is_error, result.content
    return result.structured_content


async def wait_for_run(client, run_id, predicate):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = await result_of(client, "xctest_status", {"run_id": run_id})
        if predicate(result):
            return result
        await asyncio.sleep(0.08)
    raise AssertionError(f"Run did not reach expected state: {result}")


def fake_server(commands):
    return StdioServerParameters(
        command=sys.executable,
        args=[*commands, "serve"],
        env={"PYTHONPATH": os.environ["PYTHONPATH"], "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_managed_run_survives_mcp_disconnect(simulated_host):
    home, options, commands = simulated_host
    # Use the normal queue entry point in the isolated fake home to exercise telemetry.
    commands = commands[:2]
    runner = fake_server(commands)

    async def run():
        try:
            async with Client(runner) as client:
                submitted = await result_of(client, "run_xctest", options)
                assert submitted["run_id"] == options["run_id"]
                await wait_for_run(client, submitted["run_id"], lambda s: s["phase"] == "testing")
            # Closing the first stdio server while the fake XCTest is blocked
            # leaves the worker and its registered XCTest grandchild alive.
            async with Client(runner) as client:
                repeated = await result_of(client, "run_xctest", options)
                assert repeated["run_id"] == submitted["run_id"]
                assert repeated["phase"] == "testing"
                active = await result_of(client, "status", {})
                assert [r["run_id"] for r in active["requests"]] == [submitted["run_id"]]
                wait_args = {"run_id": submitted["run_id"], "timeout_seconds": 1}
                assert await result_of(client, "xctest_wait", wait_args) == {
                    "wait_result": "timeout",
                    "run_id": submitted["run_id"],
                    "phase": "testing",
                }
                waiter = asyncio.create_task(
                    result_of(client, "xctest_wait", {**wait_args, "timeout_seconds": 10})
                )
                assert (await result_of(client, "status", {}))["requests"]
                (home / "finish-xcode").touch()
                result = await waiter
                assert result["wait_result"] == "finished"
                assert result["outcome"] == "passed" and result["xcode_exit_code"] == 0
                assert Path(result["result_bundle"]).is_dir()
                assert Path(result["log_path"]).read_text() == "fake XCTest passed\n"
                assert result["cleanup"] == "verified" and result["device_state"] == "deleted"
                assert "worker" not in result and "arguments" not in result
                assert (await result_of(client, "status", {}))["requests"] == []
        finally:
            (home / "finish-xcode").touch()

    asyncio.run(run())
    assert json.loads((home / "inventory.json").read_text()) == {}
    assert not any((home / "Library/Developer/CoreSimulator/Devices").iterdir())

    cli = subprocess.run(
        [sys.executable, *commands, "call", "xctest-wait"],
        input=json.dumps({"run_id": options["run_id"], "timeout_seconds": 900}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(cli.stdout)["cleanup"] == "verified"

    metrics = home / "metrics.jsonl"
    deadline = time.monotonic() + 3
    rows = []
    while time.monotonic() < deadline:
        if metrics.exists():
            rows = [json.loads(line) for line in metrics.read_text().splitlines()]
            if len(rows) >= 3:
                break
        time.sleep(0.03)
    by_event = {row["event"]: row for row in rows}
    assert set(by_event) == {"enqueue", "admit", "finish"}
    assert all(row["run_id"] == options["run_id"] for row in rows)
    assert by_event["admit"]["active"] == 1
    assert by_event["finish"]["active"] == 0
    assert by_event["finish"]["outcome"] == "passed"
    assert by_event["finish"]["cleanup"] == "verified"
    assert not any("lease_token" in row or "arguments" in row for row in rows)


def test_managed_run_identifies_boot_stall_and_cleans_up(simulated_host):
    home, options, commands = simulated_host
    (home / "stall").touch()
    runner = fake_server(commands)

    async def run():
        async with Client(runner) as client:
            await result_of(client, "run_xctest", options)
            result = await wait_for_run(
                client, options["run_id"], lambda s: s["phase"] == "finished"
            )
            assert result["outcome"] == "not_run" and result["failed_phase"] == "booting"
            assert "after one recheck" in result["error"]
            assert result["boot"]["attempt"] == "recheck"
            for attempt in ("initial", "recheck"):
                assert result["boot"][attempt] == {
                    "command": "bootstatus",
                    "native_status": 2,
                    "native_phase": "data_migration",
                    "migration_plugin": "com.apple.locationd.migrator",
                    "terminal": False,
                }
            assert "private native fixture detail" not in json.dumps(result)
            assert result["cleanup"] == "verified" and result["device_state"] == "deleted"

    asyncio.run(run())
    assert not (home / "xcode-started").exists()
    assert json.loads((home / "inventory.json").read_text()) == {}
