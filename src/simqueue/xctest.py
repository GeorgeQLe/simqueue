"""Run a built XCTest plan on a lease owned by a detached local worker."""

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .backend import Attention
from .core import ROUTINE_RUNTIME, Queue, label
from .storage import atomic_json

# The child cannot begin XCTest until its PID has been registered with the lease.
TRACKED_EXEC = (
    "import os, sys; "
    "sys.stdin.buffer.read(1) == b'1' or sys.exit(1); "
    "os.execv(sys.argv[1], sys.argv[1:])"
)
MAX_WAIT_SECONDS = 900
MCP_WAIT_SECONDS = 45


def xcode_options(arguments):
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        raise ValueError("Provide xcodebuild arguments as a JSON array of strings")
    valued = {"-xctestrun", "-resultBundlePath", "-derivedDataPath"}
    paths = {}
    options = iter(arguments)
    for option in options:
        if option.startswith(("-only-testing:", "-skip-testing:")) and option.split(":", 1)[1]:
            continue
        if option not in valued or option in paths:
            raise ValueError("Use XCTest plan, output paths and only/skip-testing options")
        value = Path(next(options, ""))
        if not value.is_absolute():
            raise ValueError("XCTest plan and output paths must be absolute")
        paths[option] = value
    if not {"-xctestrun", "-resultBundlePath"} <= paths.keys():
        raise ValueError("Provide -xctestrun and -resultBundlePath")
    return paths


def verify_xcode(paths):
    checker = Path.home() / ".local/bin/developer-host"
    try:
        profile = json.loads(
            subprocess.check_output(
                [str(checker), "storage", "--format", "json", "--checker-protocol", "json"],
                timeout=35,
            )
        )
        root = Path(profile["resources"]["DEVELOPER_STORAGE_ROOT"]["resolvedPath"])
        if profile["authority"]["verdict"] != "verified" or not os.path.ismount(root):
            raise ValueError
        for option, path in paths.items():
            if option == "-xctestrun":
                continue
            resolved = path.resolve()
            if root not in resolved.parents or resolved.parent.stat().st_dev != root.stat().st_dev:
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise Attention(
            "XCTest output paths must have existing parents on verified developer storage"
        ) from exc
    wrapper = Path.home() / ".local/bin/apple-tools"
    if not wrapper.is_file():
        raise Attention("The shared apple-tools wrapper is required")
    return wrapper


def xcode_command(wrapper, lease, arguments):
    return [
        str(wrapper),
        "xcodebuild",
        "test-without-building",
        *arguments,
        "-destination",
        f"id={lease['uuid']}",
        "-parallel-testing-enabled",
        "NO",
        *lease["xcodebuild_args"],
    ]


def xcode_test(queue, token, arguments):
    """Run with a caller-managed lease, preserving the original CLI contract."""
    wrapper = verify_xcode(xcode_options(arguments))
    lease = queue.poll(token, renew=True)
    if lease["state"] != "granted" or lease["operation"] or not lease["device_set"]:
        raise Attention("An idle active lease is required")
    queue.track_process(token, os.getpid())
    if not queue.boot(token)["boot_ready"]:
        raise Attention("The leased simulator is no longer ready for testing")
    os.environ["SIMULATOR_DEVICE_SET_PATH"] = lease["device_set"]
    os.execv(wrapper, xcode_command(wrapper, lease, arguments))


class XCTestRuns:
    def __init__(self, queue):
        self.queue = queue
        self.directory = queue.root / "xctest-runs"

    def path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Run ID must be 32 lowercase hexadecimal characters")
        return self.directory / (run_id + ".json")

    def read(self, run_id):
        path = self.path(run_id)
        try:
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Attention("XCTest run directory must be owned by this user with mode 0700")
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as file:
                return json.load(file)
        except FileNotFoundError:
            raise ValueError("Unknown XCTest run ID") from None

    def submit(self, owner, task, device_type, arguments, runtime=ROUTINE_RUNTIME, run_id=None):
        xcode_options(arguments)
        if run_id is None:
            run_id = uuid.uuid4().hex
        path = self.path(run_id)
        request = {
            "owner": label(owner),
            "task": label(task),
            "device_type": device_type,
            "runtime": runtime,
            "arguments": arguments,
        }
        with self.queue.locked(read_only=True):
            self.directory.mkdir(mode=0o700, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Attention("XCTest run directory must be owned by this user with mode 0700")
            if path.exists() or path.is_symlink():
                record = self.read(run_id)
                if record["request"] != request:
                    raise ValueError("Run ID already belongs to another XCTest plan")
                if record["phase"] != "submitted":
                    return {"run_id": run_id, "phase": record["phase"]}
            else:
                now = time.time()
                atomic_json(
                    path,
                    {
                        "run_id": run_id,
                        "request": request,
                        "submitted_at": now,
                        "phase": "submitted",
                        "phase_started_at": now,
                    },
                )
                self.queue.observe("enqueue", run_id=run_id, reason="preparing")
            # The worker claims the record under the same lock. Resubmitting a
            # lost acknowledgement cannot give two workers a lease for this ID.
            command = [sys.executable, "-m", "simqueue.cli", "--state-dir", str(self.queue.root)]
            if not self.queue.migrate_default_capacity:
                command += ["--max-devices", str(self.queue.capacity)]
            child = subprocess.Popen(
                [*command, "_xctest-worker", run_id],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            threading.Thread(target=child.wait, daemon=True).start()
        return {"run_id": run_id, "phase": "submitted"}

    def status(self, run_id):
        record = self.read(run_id)
        result = {key: val for key, val in record.items() if key not in {"request", "worker"}}
        result.update(owner=record["request"]["owner"], task=record["request"]["task"])
        now = result.get("finished_at", time.time())
        result["elapsed_seconds"] = round(max(0, now - result["submitted_at"]), 1)
        result["phase_elapsed_seconds"] = round(max(0, now - result["phase_started_at"]), 1)
        worker = record.get("worker")
        if (
            worker
            and record["phase"] != "finished"
            and self.queue.processes.alive(worker["pid"], worker["stamp"]) is False
        ):
            result.update(
                phase="interrupted",
                error="XCTest worker exited; consult cleanup state before resubmitting",
            )
        if result["phase"] == "submitted" and result["elapsed_seconds"] > 30:
            result["attention"] = "Worker has not started; resubmit with the same run ID"
        request_id = record.get("request_id")
        if request_id and (record["phase"] != "finished" or record["cleanup"] == "pending"):
            lease = self.queue.request_status(request_id)
            if lease:
                if result["phase"] == "queued":
                    result["queue_position"] = lease["queue_position"]
                    if lease["state"] == "creating":
                        result["phase"] = "creating"
                result["device_state"] = lease["device_state"]
                result["device_uuid"] = lease["uuid"]
                if lease["attention"]:
                    result[
                        "cleanup_attention"
                        if result["phase"] in {"finished", "interrupted", "cleaning_up"}
                        else "attention"
                    ] = lease["attention"]
                if lease["device_state"] == "deleted":
                    result["cleanup"] = "verified"
                elif lease["state"] in {"released", "expired"} and lease["device_state"] is None:
                    result["cleanup"] = "no_device"
                elif result["phase"] == "interrupted":
                    result["cleanup"] = "pending"
        return result

    def wait(self, run_id, timeout_seconds=MCP_WAIT_SECONDS):
        """Wait on the run journal without owning the worker or its lease."""
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_WAIT_SECONDS:
            raise ValueError(f"XCTest wait must be between 1 and {MAX_WAIT_SECONDS} seconds")
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = self.status(run_id)
            phase = status["phase"]
            if phase == "finished":
                ready = status.get("cleanup") in {"verified", "no_device"}
                return {"wait_result": "finished" if ready else "attention", **status}
            if phase == "interrupted" or (phase == "submitted" and status.get("attention")):
                return {"wait_result": "attention", **status}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"wait_result": "timeout", "run_id": run_id, "phase": phase}
            time.sleep(min(2, remaining))

    def work(self, run_id):
        with self.queue.locked(read_only=True):
            record = self.read(run_id)
            if record["phase"] != "submitted":
                return
            pid = os.getpid()
            stamp = self.queue.processes.fingerprint(pid)
            if stamp is None:
                raise Attention("Cannot establish XCTest worker identity")
            record["worker"] = {"pid": pid, "stamp": stamp}

            def update(*, phase=None, **fields):
                if phase is not None and phase != record["phase"]:
                    record.update(phase=phase, phase_started_at=time.time())
                record.update(fields)
                atomic_json(self.path(run_id), record)

            update(phase="preparing")

        token = None
        stopping = threading.Event()
        sampling_stopped = threading.Event()
        heartbeat = None

        def sample():
            peer = Queue(
                self.queue.root,
                self.queue.backend,
                max_devices=self.queue.capacity,
                migrate_default_capacity=self.queue.migrate_default_capacity,
            )
            while not sampling_stopped.wait(30):
                try:
                    with peer.locked(read_only=True):
                        peer.observe("sample", run_id=run_id)
                except (Attention, OSError):
                    continue

        if self.queue.migrate_default_capacity:
            threading.Thread(target=sample, daemon=True).start()

        def requested(request_id, capability):
            nonlocal token
            token = capability
            update(phase="queued", request_id=request_id)

        def renew():
            peer = Queue(
                self.queue.root,
                self.queue.backend,
                max_devices=self.queue.capacity,
                migrate_default_capacity=self.queue.migrate_default_capacity,
            )
            while not stopping.wait(30):
                try:
                    if peer.poll(token, renew=True)["state"] != "granted":
                        return
                except (Attention, OSError):
                    # A transient lock/storage error must not stop future renewals.
                    continue

        def boot_progress(detail):
            boot = record["boot"]
            attempt = detail.get("attempt", boot["attempt"])
            snapshot = (
                detail
                if "native_status" in detail
                else {
                    **boot.get(attempt, {}),
                    **{k: v for k, v in detail.items() if k != "attempt"},
                }
            )
            if attempt != boot["attempt"] or snapshot != boot.get(attempt):
                update(boot={**boot, "attempt": attempt, attempt: snapshot})

        def mark(phase):
            # Submission-to-phase timing for queue telemetry (best effort).
            if not self.queue.migrate_default_capacity:
                return
            try:
                with self.queue.locked(read_only=True):
                    self.queue.observe(
                        "phase",
                        run_id=run_id,
                        phase=phase,
                        run_ms=max(0, round((time.time() - record["submitted_at"]) * 1000)),
                    )
            except (Attention, OSError):
                pass

        cleanup = "no_device"
        try:
            arguments = record["request"]["arguments"]
            paths = xcode_options(arguments)
            wrapper = verify_xcode(paths)
            result_path = paths["-resultBundlePath"]
            if not paths["-xctestrun"].is_file():
                raise ValueError("Built .xctestrun plan is missing")
            if result_path.exists() or result_path.is_symlink():
                raise ValueError("XCTest result destination already exists")

            lease = self.queue.acquire(
                **{
                    key: record["request"][key]
                    for key in ("owner", "task", "device_type", "runtime")
                },
                run_id=run_id,
                on_request=requested,
            )
            while lease["state"] in {"queued", "creating"}:
                if lease["attention"]:
                    raise Attention(lease["attention"])
                time.sleep(3)
                lease = self.queue.poll(token, renew=True)
            if lease["state"] != "granted" or not lease["device_set"]:
                raise Attention("Could not acquire a simulator for XCTest")

            heartbeat = threading.Thread(target=renew, daemon=True)
            heartbeat.start()
            update(
                phase="booting",
                device_uuid=lease["uuid"],
                boot={"attempt": "initial"},
            )
            mark("booting")
            if not self.queue.boot(token, progress=boot_progress)["boot_ready"]:
                raise Attention("The leased simulator did not reach verified readiness")

            if result_path.exists() or result_path.is_symlink():
                raise ValueError("XCTest result destination already exists")
            log_path = result_path.with_name(f"simqueue-{run_id}.log")
            with log_path.open("xb") as log:
                update(phase="testing", log_path=str(log_path))
                mark("testing")
                command = [
                    sys.executable,
                    "-c",
                    TRACKED_EXEC,
                    *xcode_command(wrapper, lease, arguments),
                ]
                env = {**os.environ, "SIMULATOR_DEVICE_SET_PATH": lease["device_set"]}
                with subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                ) as test:
                    try:
                        self.queue.track_process(token, test.pid)
                        test.stdin.write(b"1")
                    finally:
                        test.stdin.close()
                    exit_code = test.wait()
            produced = result_path.is_dir() and not result_path.is_symlink()
            update(
                outcome=("passed" if exit_code == 0 else "failed") if produced else "xcode_error",
                xcode_exit_code=exit_code,
                **(
                    {"result_bundle": str(result_path)}
                    if produced
                    else {"error": "Xcode did not produce an XCTest result bundle"}
                ),
            )
        except (Attention, OSError, ValueError) as exc:
            phase = record["phase"]
            update(
                outcome="xcode_error" if phase == "testing" else "not_run",
                failed_phase=phase,
                error=str(exc),
                error_kind=type(exc).__name__,
                **(
                    {
                        "native_log_directory": str(
                            Path.home() / "Library/Logs/CoreSimulator" / record["device_uuid"]
                        )
                    }
                    if phase == "booting"
                    else {}
                ),
            )
        finally:
            stopping.set()
            if heartbeat is not None:
                heartbeat.join()
            if token is not None:
                cleanup = "pending"
                update(phase="cleaning_up", cleanup=cleanup)
                try:
                    released = self.queue.release(token)
                    if released["device_state"] == "deleted":
                        cleanup = "verified"
                    elif released["device_state"] is None:
                        cleanup = "no_device"
                    if released["attention"]:
                        update(cleanup_attention=released["attention"])
                    update(device_state=released["device_state"], device_uuid=released["uuid"])
                except (Attention, OSError, ValueError) as exc:
                    update(cleanup_attention=str(exc))
            if "outcome" not in record:
                update(outcome="interrupted")
            sampling_stopped.set()
            finished = time.time()
            update(phase="finished", cleanup=cleanup, finished_at=finished)
            if self.queue.migrate_default_capacity:
                try:
                    with self.queue.locked(read_only=True):
                        self.queue.observe(
                            "finish",
                            run_id=run_id,
                            outcome=record["outcome"],
                            cleanup=cleanup,
                            run_ms=max(0, round((finished - record["submitted_at"]) * 1000)),
                            **{
                                key: record[field]
                                for key, field in (
                                    ("failed_phase", "failed_phase"),
                                    ("reason", "error_kind"),
                                )
                                if field in record
                            },
                        )
                except (Attention, OSError):
                    pass
