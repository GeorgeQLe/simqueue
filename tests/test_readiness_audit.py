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


@pytest.mark.parametrize("state", ["Booting", "Shutting Down"])
def test_transitional_state_does_not_claim_readiness(tmp_path, state):
    b = SlowBackend()
    q = Queue(tmp_path / "queue", b, max_devices=1)
    lease = q.acquire("audit", "readiness", "phone", "ios")
    try:
        b.items[lease["uuid"]]["state"] = state
        with pytest.raises(Attention, match="transitioning"):
            q.boot(lease["lease_token"])
        assert b.waits == 0
    finally:
        b.items[lease["uuid"]]["state"] = "Shutdown"
        assert q.release(lease["lease_token"])["device_state"] == "deleted"


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
