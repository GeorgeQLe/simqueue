"""Shared host admission for the cold-boot phase (developer-host heavy-slot).

A cold simulator boot and a FormSeed3D Blender render share one host slot. The
slot is held by a `developer-host heavy-slot hold` helper process: it reports
`waiting`/`acquired`/`timeout` as JSON lines and releases when told to, when
its stdin closes, or when it dies (the kernel drops its lock). simqueue takes
the slot outside the coordinator lock and outside any native operation budget,
so the shared wait never counts toward the boot timeout.

The slot is advisory: if the helper is missing, fails or stops answering, the
boot proceeds without it. Only an explicit bounded-wait timeout refuses the
boot (HostSlotBusy), before any native command runs.
"""

import contextlib
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

from .backend import Attention

DEFAULT_MAX_WAIT = 900
DECISION_GRACE = 15
RELEASE_GRACE = 10
RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class HostSlotBusy(Attention):
    """The shared heavy-phase slot stayed busy past the bounded wait; no boot was attempted."""


def max_wait_from_environment(environ=None):
    raw = (environ if environ is not None else os.environ).get(
        "SIMQUEUE_HOST_SLOT_MAX_WAIT_SECONDS", ""
    )
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_WAIT
    return value if 0 < value <= 86400 else DEFAULT_MAX_WAIT


def default_launcher():
    if sys.platform != "darwin":
        return None
    if os.environ.get("SIMQUEUE_HOST_SLOT", "").lower() in ("0", "off", "false", "no"):
        return None
    override = os.environ.get("DEVELOPER_HOST_LAUNCHER", "")
    # An absolute override selects a trial developer-host release; otherwise the installed one.
    launcher = (
        Path(override) if os.path.isabs(override) else Path.home() / ".local/bin/developer-host"
    )
    return launcher if launcher.is_file() and os.access(launcher, os.X_OK) else None


class HostSlot:
    """Callable `slot(run_id, progress)` returning a context manager around the cold boot."""

    def __init__(self, launcher=None, max_wait=None, clock=time.monotonic):
        self.launcher = launcher
        self.max_wait = max_wait if max_wait is not None else max_wait_from_environment()
        self.clock = clock

    def __call__(self, run_id, progress=None):
        return self.hold(run_id, progress)

    @contextlib.contextmanager
    def hold(self, run_id, progress=None):
        launcher = self.launcher if self.launcher is not None else default_launcher()
        report = progress or (lambda detail: None)
        if launcher is None or not RUN_ID.fullmatch(str(run_id)):
            yield {"host_slot": "unavailable"}
            return
        started = self.clock()
        try:
            helper = subprocess.Popen(
                [
                    str(launcher),
                    "heavy-slot",
                    "hold",
                    "--source",
                    "simqueue",
                    "--phase",
                    "cold-boot",
                    "--run-id",
                    str(run_id),
                    "--max-wait",
                    str(self.max_wait),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            report({"host_slot": "unavailable"})
            yield {"host_slot": "unavailable"}
            return
        try:
            decision = self._decide(helper, started, report)
            if decision["state"] == "timeout":
                report({"host_slot": "timeout", "reason": decision.get("reason")})
                raise HostSlotBusy(
                    f"Host heavy-phase slot stayed busy for {self.max_wait:g}s "
                    f"({decision.get('reason', 'busy')}); boot was not attempted. "
                    "Retry later or inspect `developer-host heavy-slot status`."
                )
            if decision["state"] == "acquired":
                detail = {"host_slot": "acquired", "wait_ms": decision.get("wait_ms", 0)}
            else:
                detail = {"host_slot": "unavailable"}
            report(detail)
            yield detail
        finally:
            self._finish(helper)

    def _decide(self, helper, started, report):
        """Read helper messages until it acquires, times out or stops answering."""
        deadline = started + self.max_wait + DECISION_GRACE
        buffer = b""
        last_reason = None
        stream = helper.stdout
        while True:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                state = message.get("state")
                if state == "waiting" and message.get("reason") != last_reason:
                    last_reason = message.get("reason")
                    report(
                        {
                            "host_slot": "waiting",
                            "reason": last_reason,
                            "wait_ms": message.get("waited_ms", 0),
                        }
                    )
                elif state in ("acquired", "timeout"):
                    return message
                elif state in ("cancelled", "released", "error"):
                    return {"state": "unavailable"}
            remaining = deadline - self.clock()
            if remaining <= 0:
                return {"state": "unavailable"}
            ready, _, _ = select.select([stream], [], [], min(remaining, 5))
            if not ready:
                continue
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                return {"state": "unavailable"}
            buffer += chunk

    @staticmethod
    def _finish(helper):
        try:
            helper.stdin.write(b"release\n")
            helper.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            helper.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            helper.wait(RELEASE_GRACE)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper.wait()
        finally:
            helper.stdout.close()


def null_slot(run_id, progress=None):
    return contextlib.nullcontext({"host_slot": "unused"})
