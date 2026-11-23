"""Cold boot takes the shared developer-host heavy slot outside the boot budget."""

import contextlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_queue import Backend

from simqueue.backend import BootFailure, NativeTimeout
from simqueue.core import Queue
from simqueue.host_slot import HostSlot, HostSlotBusy, null_slot

DEVELOPER_HOST = (
    Path.home()
    / "Developer/Projects/web/dev/gblockparty-chromux-v2/tools/developer-host/developer-host"
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class BudgetBackend(Backend):
    """Enforces each native budget against a fake clock and logs the sequence."""

    def __init__(self, clock, events):
        super().__init__()
        self.clock = clock
        self.events = events
        self.deadline = None
        self.boot_error = None

    @contextlib.contextmanager
    def operation_budget(self, seconds=None):
        seconds = 120 if seconds is None else seconds
        self.events.append(("budget", seconds))
        previous, self.deadline = self.deadline, self.clock() + seconds
        try:
            yield
        finally:
            self.deadline = previous

    def boot(self, uid):
        self.events.append("boot")
        if self.clock() > self.deadline:
            raise NativeTimeout("boot budget already spent")
        self.items[uid]["state"] = "Booting"
        if self.boot_error is not None:
            error, self.boot_error = self.boot_error, None
            raise error
        self.items[uid]["state"] = "Booted"

    def wait_ready(self, uid):
        self.events.append("wait_ready")
        if self.clock() > self.deadline:
            raise NativeTimeout("readiness budget already spent")


class FakeSlot:
    def __init__(self, clock, events, wait=0.0, busy=False):
        self.clock, self.events, self.wait, self.busy = clock, events, wait, busy
        self.run_ids = []

    @contextlib.contextmanager
    def __call__(self, run_id, progress=None):
        self.run_ids.append(run_id)
        self.events.append("slot-wait")
        self.clock.now += self.wait  # time spent queued behind a render
        if self.busy:
            raise HostSlotBusy("held-by:formseed3d:render")
        self.events.append("slot-acquired")
        try:
            yield {"host_slot": "acquired"}
        finally:
            self.events.append("slot-released")


def setup(tmp_path, wait=0.0, busy=False):
    clock, events = Clock(), []
    backend = BudgetBackend(clock, events)
    slot = FakeSlot(clock, events, wait=wait, busy=busy)
    queue = Queue(tmp_path / "queue", backend, clock=clock, max_devices=1, host_slot=slot)
    lease = queue.acquire("audit", "host-slot", "phone", "ios", run_id="run-1")
    events.clear()  # device creation is not part of the boot
    return queue, backend, slot, events, lease


def test_slot_wait_is_outside_the_boot_budget(tmp_path):
    # A 500s queue wait would blow the 120s boot budget if it counted toward it.
    queue, _backend, slot, events, lease = setup(tmp_path, wait=500)
    try:
        assert queue.boot(lease["lease_token"])["boot_ready"]
        assert events == [
            ("budget", 30),
            "slot-wait",
            "slot-acquired",
            ("budget", 120),
            "boot",
            "slot-released",
        ]
        assert slot.run_ids == ["run-1"]
    finally:
        assert queue.release(lease["lease_token"])["device_state"] == "deleted"


def test_recheck_runs_inside_the_slot_and_release_follows(tmp_path):
    queue, backend, _slot, events, lease = setup(tmp_path)
    backend.boot_error = NativeTimeout("bootstatus timed out")
    try:
        assert queue.boot(lease["lease_token"])["boot_ready"]
        assert events[events.index("slot-acquired") :] == [
            "slot-acquired",
            ("budget", 120),
            "boot",
            ("budget", 30),
            "wait_ready",
            "slot-released",
        ]
    finally:
        queue.release(lease["lease_token"])


def test_failed_attempt_releases_the_slot(tmp_path):
    queue, backend, _slot, events, lease = setup(tmp_path)
    backend.boot_error = BootFailure("Data Migration Failed")
    try:
        with pytest.raises(BootFailure):
            queue.boot(lease["lease_token"])
        assert events[-1] == "slot-released"
        with queue.locked(read_only=True):
            device = next(iter(queue.state["devices"].values()))
            assert device["boot_failure"] and not device.get("operation")
    finally:
        queue.release(lease["lease_token"])


def test_busy_slot_refuses_before_any_native_boot(tmp_path):
    queue, _backend, slot, events, lease = setup(tmp_path, busy=True)
    token = lease["lease_token"]
    try:
        with pytest.raises(HostSlotBusy):
            queue.boot(token)
        assert "boot" not in events
        with queue.locked(read_only=True):
            device = next(iter(queue.state["devices"].values()))
            assert not device.get("operation")
            assert not device.get("boot_failure") and not device.get("boot_timeout")
        slot.busy = False
        assert queue.boot(token)["boot_ready"]
    finally:
        queue.release(token)


def test_booted_device_and_ordinary_operations_skip_the_slot(tmp_path):
    queue, _backend, _slot, events, lease = setup(tmp_path)
    token = lease["lease_token"]
    try:
        queue.boot(token)
        events.clear()
        assert queue.boot(token)["boot_ready"]  # already booted: readiness check only
        assert "slot-wait" not in events
        assert events == [("budget", 30), ("budget", 120), "wait_ready"]
    finally:
        queue.release(token)


def test_custom_queues_default_to_no_slot(tmp_path):
    assert Queue(tmp_path / "q", Backend()).host_slot is null_slot


# The client against a scripted helper.


def fake_launcher(tmp_path, script):
    path = tmp_path / "developer-host"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


def test_client_reports_wait_then_acquires_and_releases(tmp_path):
    received = tmp_path / "received"
    launcher = fake_launcher(
        tmp_path,
        'echo \'{"state":"waiting","reason":"held-by:formseed3d:render","waited_ms":5}\'\n'
        'echo \'{"state":"acquired","wait_ms":40}\'\n'
        f'cat > "{received}"\n',
    )
    progress = []
    with HostSlot(launcher=launcher, max_wait=5)("run-1", progress.append) as detail:
        assert detail == {"host_slot": "acquired", "wait_ms": 40}
    assert progress == [
        {"host_slot": "waiting", "reason": "held-by:formseed3d:render", "wait_ms": 5},
        {"host_slot": "acquired", "wait_ms": 40},
    ]
    assert received.read_text() == "release\n"


def test_client_timeout_raises_busy(tmp_path):
    launcher = fake_launcher(
        tmp_path, 'echo \'{"state":"timeout","reason":"held-by:formseed3d:render"}\'\nexit 75\n'
    )
    busy = pytest.raises(HostSlotBusy, match="held-by:formseed3d:render")
    with busy, HostSlot(launcher=launcher, max_wait=1)("run-1"):
        pytest.fail("entered the boot without the slot")


@pytest.mark.parametrize("script", ["exit 1\n", "echo not-json\nexit 0\n"])
def test_client_fails_open_when_the_helper_breaks(tmp_path, script):
    with HostSlot(launcher=fake_launcher(tmp_path, script), max_wait=1)("run-1") as detail:
        assert detail == {"host_slot": "unavailable"}
    with HostSlot(launcher=tmp_path / "absent", max_wait=1)("run-1") as detail:
        assert detail == {"host_slot": "unavailable"}


def test_client_bounds_a_silent_helper(tmp_path, monkeypatch):
    monkeypatch.setattr("simqueue.host_slot.DECISION_GRACE", 0)
    launcher = fake_launcher(tmp_path, "exec sleep 30\n")
    started = time.monotonic()
    with HostSlot(launcher=launcher, max_wait=0.5)("run-1") as detail:
        assert detail == {"host_slot": "unavailable"}
    assert time.monotonic() - started < 15


def test_exception_during_boot_still_releases(tmp_path):
    received = tmp_path / "received"
    launcher = fake_launcher(
        tmp_path, f'echo \'{{"state":"acquired","wait_ms":0}}\'\ncat > "{received}"\n'
    )
    with pytest.raises(RuntimeError), HostSlot(launcher=launcher, max_wait=5)("run-1"):
        raise RuntimeError("boot failed")
    assert received.read_text() == "release\n"


# The client against the real developer-host helper (no simulator or Blender).


@pytest.mark.skipif(
    sys.platform != "darwin" or not DEVELOPER_HOST.is_file(), reason="developer-host source"
)
def test_real_helper_excludes_a_render_and_survives_owner_exit(tmp_path, monkeypatch):
    slot_dir = tmp_path / "slot"
    monkeypatch.setenv("DEVELOPER_HOST_HEAVY_SLOT_DIR", str(slot_dir))
    monkeypatch.setenv("DEVELOPER_HOST_QUEUE_METRICS_DIR", str(tmp_path / "metrics"))
    launcher = fake_launcher(tmp_path, f'exec /usr/bin/python3 "{DEVELOPER_HOST}" "$@"\n')
    render = subprocess.Popen(
        [launcher, "heavy-slot", "hold", "--source", "formseed3d", "--phase", "render",
         "--run-id", "render-1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True,
    )  # fmt: skip
    try:
        while json.loads(render.stdout.readline())["state"] != "acquired":
            pass
        progress = []
        slot = HostSlot(launcher=launcher, max_wait=0.6)
        with pytest.raises(HostSlotBusy), slot("boot-1", progress.append):
            pytest.fail("boot entered while the render held the slot")
        assert progress[0]["reason"] == "held-by:formseed3d:render"
        # The render owner dies without releasing: its pipe closes and the slot frees.
        render.kill()
        render.wait()
        with HostSlot(launcher=launcher, max_wait=10)("boot-2", progress.append) as detail:
            assert detail["host_slot"] == "acquired"
            state = subprocess.run(
                [launcher, "heavy-slot", "status"], capture_output=True, text=True, check=True
            )
            assert json.loads(state.stdout)["holder"]["run_id"] == "boot-2"
        state = subprocess.run(
            [launcher, "heavy-slot", "status"], capture_output=True, text=True, check=True
        )
        assert json.loads(state.stdout)["held"] is False
    finally:
        if render.poll() is None:
            render.kill()
            render.wait()
        render.stdin.close()
        render.stdout.close()
        shutil.rmtree(slot_dir, ignore_errors=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="the slot is enabled on macOS only")
def test_launcher_override_selects_a_trial_release(tmp_path, monkeypatch):
    from simqueue.host_slot import default_launcher

    trial = fake_launcher(tmp_path, "exit 0\n")
    monkeypatch.delenv("SIMQUEUE_HOST_SLOT", raising=False)
    monkeypatch.setenv("DEVELOPER_HOST_LAUNCHER", str(trial))
    assert default_launcher() == trial
    monkeypatch.setenv("DEVELOPER_HOST_LAUNCHER", str(tmp_path / "absent"))
    assert default_launcher() is None  # a broken override never falls back to the live install
    monkeypatch.setenv("SIMQUEUE_HOST_SLOT", "off")
    monkeypatch.setenv("DEVELOPER_HOST_LAUNCHER", str(trial))
    assert default_launcher() is None
