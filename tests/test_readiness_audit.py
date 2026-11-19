import pytest
from test_queue import Backend

from simqueue.backend import Attention
from simqueue.core import Queue


class SlowBackend(Backend):
    def __init__(self):
        super().__init__()
        self.readiness = False
        self.waits = 0
        self.install_calls = 0
        self.capture_calls = 0

    def wait_ready(self, uid):
        self.waits += 1
        if not self.readiness:
            raise Attention("native readiness not complete")

    def install(self, uid, path):
        self.install_calls += 1
        return {"installed": True}

    def capture(self, uid, request_id, filename):
        self.capture_calls += 1
        return {"captured": True}


def test_timeout_then_booted_cannot_bypass_readiness(tmp_path):
    b = SlowBackend()
    q = Queue(tmp_path / "queue", b, max_devices=1)
    lease = q.acquire("audit", "readiness", "phone", "ios")
    token = lease["lease_token"]
    try:
        with pytest.raises(Attention, match="not complete"):
            q.boot(token)
        assert b.items[lease["uuid"]]["state"] == "Booted"
        with pytest.raises(Attention, match="not complete"):
            q.boot(token)
        with pytest.raises(Attention, match="not complete"):
            q.install(token, "/unused.app")
        with pytest.raises(Attention, match="not complete"):
            q.capture(token)
        assert b.waits == 4
        assert b.install_calls == b.capture_calls == 0
        b.readiness = True
        q.boot(token)
        assert q.install(token, "/unused.app")["installed"]
        assert q.capture(token)["captured"]
    finally:
        assert q.release(token)["device_state"] == "deleted"


def test_shutting_down_does_not_claim_readiness(tmp_path):
    b = SlowBackend()
    q = Queue(tmp_path / "queue", b, max_devices=1)
    lease = q.acquire("audit", "readiness", "phone", "ios")
    try:
        b.items[lease["uuid"]]["state"] = "Shutting Down"
        with pytest.raises(Attention, match="transitioning"):
            q.boot(lease["lease_token"])
        assert b.waits == 0
    finally:
        b.items[lease["uuid"]]["state"] = "Shutdown"
        assert q.release(lease["lease_token"])["device_state"] == "deleted"


def test_booting_device_waits_on_the_same_native_boot(tmp_path):
    b = SlowBackend()
    q = Queue(tmp_path / "queue", b, max_devices=1)
    lease = q.acquire("audit", "existing native boot", "phone", "ios")
    token = lease["lease_token"]
    b.items[lease["uuid"]]["state"] = "Booting"
    try:
        with pytest.raises(Attention, match="native readiness not complete"):
            q.boot(token)
        assert b.waits == 1
        assert b.items[lease["uuid"]]["state"] == "Booting"
    finally:
        assert q.release(token)["device_state"] == "deleted"


def test_recovered_creation_requires_initialization_proof(tmp_path):
    class IncompleteBackend(Backend):
        initialized = False

        def create(self, *args):
            super().create(*args)
            raise Attention("Interrupted initialization")

        def validate_created(self, uid, runtime):
            if not self.initialized:
                raise Attention("Initialization unverified")

    b = IncompleteBackend()
    q = Queue(tmp_path / "queue", b, max_devices=1)
    lease = q.acquire("audit", "initialization", "phone", "ios")
    try:
        result = q.poll(lease["lease_token"])
        assert result["state"] == "creating"
        assert result["attention"]
        assert len(b.items) == 1
        b.initialized = True
        assert q.poll(lease["lease_token"])["state"] == "granted"
    finally:
        assert q.release(lease["lease_token"])["device_state"] == "deleted"


def test_released_incomplete_creation_can_still_be_cleaned(tmp_path):
    class IncompleteBackend(Backend):
        def create(self, *args):
            super().create(*args)
            raise Attention("Interrupted initialization")

        def validate_created(self, uid, runtime):
            raise Attention("Initialization unverified")

    b = IncompleteBackend()
    q = Queue(tmp_path / "queue", b, max_devices=1)
    lease = q.acquire("audit", "initialization", "phone", "ios")
    assert q.release(lease["lease_token"])["device_state"] == "deleted"
    assert not b.items


def test_native_budget_is_shared_and_restored(monkeypatch):
    from unittest.mock import Mock

    from simqueue.backend import AppleBackend

    clock = [100.0]
    monkeypatch.setattr("simqueue.backend.time.monotonic", lambda: clock[0])
    run = Mock(return_value=Mock(returncode=0, stdout=""))
    monkeypatch.setattr("simqueue.backend.subprocess.run", run)
    backend = AppleBackend()
    with backend.operation_budget():
        backend.run("bootstatus", "owned", timeout=120)
        assert run.call_args.kwargs["timeout"] == 120
        clock[0] += 90
        backend.run("install", "owned", "/unused", timeout=300)
        assert run.call_args.kwargs["timeout"] == 30
        clock[0] += 31
        with pytest.raises(Attention, match="budget exhausted"):
            backend.run("install", "owned", "/unused")
        assert run.call_count == 2
    backend.run("list", "--json")
    assert run.call_args.kwargs["timeout"] == 45


def test_native_readiness_does_not_succeed_after_concurrent_shutdown(monkeypatch):
    from simqueue.backend import AppleBackend

    backend = AppleBackend()
    monkeypatch.setattr(backend, "run", lambda *args, **kwargs: "Finished")
    monkeypatch.setattr(backend, "devices", lambda: {"owned": {"state": "Shutdown"}})
    with pytest.raises(Attention, match="stopped during readiness"):
        backend.wait_ready("owned")


def test_cold_boot_uses_one_budget_including_slow_native_start(monkeypatch):
    import json
    from unittest.mock import Mock

    from simqueue.backend import AppleBackend

    clock = [100.0]
    monkeypatch.setattr("simqueue.backend.time.monotonic", lambda: clock[0])
    calls = []

    def native(command, **kwargs):
        args = command[command.index("simctl") + 3 :]
        calls.append((args, kwargs["timeout"]))
        if args[0] == "boot":
            clock[0] += 60  # A valid start that exceeded the old 45s limit.
        elif args[0] == "bootstatus":
            clock[0] += 30
        devices = {"ios": [{"udid": "owned", "state": "Booted"}]}
        return Mock(
            returncode=0, stdout=json.dumps({"devices": devices, "devicetypes": [], "runtimes": []})
        )

    monkeypatch.setattr("simqueue.backend.subprocess.run", native)
    backend = AppleBackend()
    backend.operation_timeout = 300
    with backend.operation_budget():
        backend.boot("owned")
    assert calls == [
        (["boot", "owned"], 300),
        (["bootstatus", "owned", "-d"], 240),
        (["list", "--json"], 45),
    ]


@pytest.mark.parametrize(
    "output",
    [
        "[date] Status=3, isTerminal=YES, Elapsed=02:00.\n\tData Migration Failed\n",
        "[date] Status=3, isTerminal=YES, Elapsed=02:00.\n",
        "\tData Migration Failed\n",
    ],
)
def test_native_zero_exit_migration_failure_is_not_ready(monkeypatch, output):
    from simqueue.backend import AppleBackend, BootFailure

    backend = AppleBackend()
    monkeypatch.setattr(backend, "run", lambda *args, **kwargs: output)
    monkeypatch.setattr(backend, "devices", lambda: {"owned": {"state": "Booted"}})
    with pytest.raises(BootFailure, match="migration failed"):
        backend.wait_ready("owned")


@pytest.mark.parametrize("first_operation", ["boot", "install", "capture"])
def test_migration_failure_survives_new_client_and_blocks_work(tmp_path, first_operation):
    from simqueue.backend import BootFailure

    class FailedMigration(SlowBackend):
        def wait_ready(self, uid):
            self.waits += 1
            if self.waits == 1:
                raise BootFailure("Native data migration failed")
            # Later native calls can return "already booted" instead of failure.

    backend = FailedMigration()
    root = tmp_path / "queue"
    q = Queue(root, backend)
    lease = q.acquire("audit", "failed migration", "phone", "ios")
    token = lease["lease_token"]
    backend.items[lease["uuid"]]["state"] = "Booted"
    try:
        args = (token, "/unused.app") if first_operation == "install" else (token,)
        with pytest.raises(BootFailure, match="migration failed"):
            getattr(q, first_operation)(*args)
        q = Queue(root, backend)
        q.poll(token, renew=True)
        for operation, args in [
            (q.boot, (token,)),
            (q.install, (token, "/unused.app")),
            (q.capture, (token,)),
        ]:
            with pytest.raises(BootFailure, match="migration failed"):
                operation(*args)
        assert backend.waits == 1
        assert backend.install_calls == backend.capture_calls == 0
    finally:
        assert q.release(token)["device_state"] == "deleted"


@pytest.mark.parametrize("command", ["boot", "bootstatus", "install"])
def test_timeout_recovery_guidance_does_not_expose_native_output(monkeypatch, command):
    import subprocess

    from simqueue.backend import AppleBackend

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 45, output="private native output")

    monkeypatch.setattr("simqueue.backend.subprocess.run", timeout)
    with pytest.raises(Attention) as caught:
        AppleBackend().run(command, "owned")
    assert "outcome uncertain" in str(caught.value)
    assert "private native output" not in str(caught.value)
    assert ("keep the lease" in str(caught.value)) == (command != "install")


def test_lease_supplies_single_xcode_device_set_argument(tmp_path):
    backend = Backend()
    backend.device_root = tmp_path / "Device Volume" / "Devices"
    q = Queue(tmp_path / "queue", backend)
    lease = q.acquire("audit", "xcode discovery", "phone", "ios")
    try:
        assert lease["device_set"] == str(backend.device_root)
        assert lease["xcodebuild_args"] == [f"-DVTSimulatorSetLocation={backend.device_root}"]
        assert q.poll(lease["lease_token"])["xcodebuild_args"] == lease["xcodebuild_args"]
    finally:
        assert q.release(lease["lease_token"])["device_state"] == "deleted"


@pytest.mark.parametrize("recovers", [True, False])
def test_native_timeout_rechecks_same_uuid_once(tmp_path, recovers):
    from simqueue.backend import NativeTimeout

    class TimedBoot(Backend):
        def __init__(self):
            super().__init__()
            self.calls = []

        def boot(self, uid):
            self.calls.append(("boot", uid))
            self.items[uid]["state"] = "Booted"
            raise NativeTimeout("native boot timed out")

        def wait_ready(self, uid):
            self.calls.append(("recheck", uid))
            if not recovers:
                raise NativeTimeout("native recheck timed out")

    backend = TimedBoot()
    q = Queue(tmp_path / "queue", backend)
    lease = q.acquire("audit", "same UUID", "phone", "ios")
    token, uid = lease["lease_token"], lease["uuid"]
    try:
        if recovers:
            assert q.boot(token)["boot_ready"]
        else:
            with pytest.raises(Attention, match="after one recheck"):
                q.boot(token)
            assert "native boot timed out" in q.poll(token, renew=True)["boot_error"]
            assert "native recheck timed out" in q.status()["events"][-1]["boot_error"]
            with pytest.raises(Attention, match="after a recheck"):
                Queue(q.root, backend).boot(token)
        assert backend.calls == [("boot", uid), ("recheck", uid)]
    finally:
        assert q.release(token)["device_state"] == "deleted"


def test_boot_timeout_reports_status_without_native_text(monkeypatch):
    import subprocess

    from simqueue.backend import AppleBackend, BootFailure, NativeTimeout

    output = b"Status=2, isTerminal=NO, Elapsed=00:01.\nprivate device text"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 45, output=output)

    monkeypatch.setattr("simqueue.backend.subprocess.run", timeout)
    backend = AppleBackend()
    with pytest.raises(NativeTimeout, match="last native boot status 2") as caught:
        backend.wait_ready("owned")
    assert "private device text" not in str(caught.value)
    output = b"Status=3, isTerminal=YES, Elapsed=00:02.\nprivate device text"
    with pytest.raises(BootFailure, match="migration failed"):
        backend.wait_ready("owned")
