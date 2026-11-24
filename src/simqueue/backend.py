"""Bounded Apple-tool operations. No caller-supplied executable or device-set path."""

import contextlib
import contextvars
import json
import os
import plistlib
import re
import selectors
import subprocess
import time
from pathlib import Path


class Attention(RuntimeError):
    """The outcome is uncertain; preserve the resource for reconciliation."""


class BootFailure(Attention):
    """Native boot monitoring reported a terminal migration failure."""


class NativeTimeout(Attention):
    """A native command exhausted its time budget."""


BOOT_STATUS = re.compile(r"Status=([0-9]{1,3}),\s*isTerminal=(YES|NO)\b")


def boot_statuses(output):
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return BOOT_STATUS.findall(output or "")


def boot_detail(output):
    text = output.decode(errors="replace")
    matches = list(BOOT_STATUS.finditer(text))
    if not matches:
        return {}
    last = matches[-1]
    status = int(last[1])
    detail = {"native_status": status, "terminal": last[2] == "YES"}
    phase = {2: "data_migration", 3: "migration_failed", 4: "system_app"}.get(status)
    if phase:
        detail["native_phase"] = phase
    if status == 2:
        plugin = re.search(
            r"^\s*Reason:Running plugin (com\.apple\.[\w.-]{1,120})(?=\s)",
            text[last.end() :],
            re.MULTILINE | re.ASCII,
        )
        if plugin:
            detail["migration_plugin"] = plugin[1]
    return detail


CORESIMULATOR_INFO = Path(
    "/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/Resources/Info.plist"
)
EXPECTED_CORESIMULATOR = re.compile(r'^EXPECTED_VERSION="([0-9][0-9.]*)"$', re.MULTILINE)


def _version(text):
    parts = [int(part) for part in text.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def first_launch_pending(simctl_launcher, info_plist=CORESIMULATOR_INFO):
    """Return (installed, expected) CoreSimulator versions when the selected
    Xcode's simctl launcher would start its password-gated first-launch install,
    which blocks every noninteractive simctl call. None when it would not."""
    try:
        match = EXPECTED_CORESIMULATOR.search(Path(simctl_launcher).read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    if not match:
        return None
    expected = match[1]
    try:
        with open(info_plist, "rb") as handle:
            installed = str(plistlib.load(handle).get("CFBundleVersion") or "")
    except (OSError, ValueError, plistlib.InvalidFileException):
        installed = ""
    try:
        if installed and _version(installed) >= _version(expected):
            return None
    except ValueError:
        return None
    return installed or "missing", expected


def wrapper_developer_dir(wrapper):
    """The DEVELOPER_DIR the shared apple-tools wrapper exports, if any."""
    try:
        match = re.search(r"^export DEVELOPER_DIR=(/\S+)$", Path(wrapper).read_text(), re.MULTILINE)
    except OSError:
        return None
    return Path(match[1]) if match else None


class AppleBackend:
    operation_timeout = 120

    def __init__(self):
        self._deadline = contextvars.ContextVar("simqueue-native-deadline", default=None)
        self._boot_observer = contextvars.ContextVar("simqueue-boot-observer", default=None)
        self.wrapper = Path.home() / ".local/bin/apple-tools"
        self.tool_command = (
            [str(self.wrapper), "xcrun"] if self.wrapper.exists() else ["/usr/bin/xcrun"]
        )
        self.device_root = Path.home() / "Library/Developer/CoreSimulator/Devices"
        developer_dir = (
            wrapper_developer_dir(self.wrapper)
            if self.wrapper.exists()
            else Path(os.environ["DEVELOPER_DIR"])
            if os.environ.get("DEVELOPER_DIR")
            else None
        )
        self.simctl_launcher = developer_dir / "usr/bin/simctl" if developer_dir else None
        self.coresimulator_info = CORESIMULATOR_INFO

    def check_toolchain(self):
        """Refuse to start simctl while Xcode's first-launch install is pending.

        After an Xcode update the selected simctl launcher runs
        `xcodebuild -runFirstLaunch`, which waits on an administrator password
        dialog, so every call would hang until its timeout and leave another
        waiting installer behind."""
        if not self.simctl_launcher:
            return
        pending = first_launch_pending(self.simctl_launcher, self.coresimulator_info)
        if pending:
            installed, expected = pending
            developer_dir = self.simctl_launcher.parents[2]
            raise Attention(
                f"Xcode at {developer_dir.parent.parent} needs its first-launch install: "
                f"CoreSimulator {installed} is older than the {expected} it expects. "
                f"An administrator must run: sudo {developer_dir}/usr/bin/xcodebuild "
                "-runFirstLaunch. simqueue does not start the password-gated install itself."
            )

    @contextlib.contextmanager
    def operation_budget(self, seconds=None):
        """Share a native-command budget across inventory, readiness and the operation."""
        previous = self._deadline.get()
        deadline = time.monotonic() + (self.operation_timeout if seconds is None else seconds)
        token = self._deadline.set(min(previous, deadline) if previous is not None else deadline)
        try:
            yield
        finally:
            self._deadline.reset(token)

    @contextlib.contextmanager
    def observe_boot(self, observer):
        token = self._boot_observer.set(observer)
        try:
            yield
        finally:
            self._boot_observer.reset(token)

    def monitor_bootstatus(self, command, timeout, env, observer):
        """Stream only structured native progress, keeping the original command deadline."""
        deadline = time.monotonic() + timeout
        output = bytearray()
        last = {}
        with (
            subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
            ) as process,
            selectors.DefaultSelector() as selector,
        ):
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise subprocess.TimeoutExpired(command, timeout, output=bytes(output))
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    output.extend(chunk)
                    detail = boot_detail(output)
                    if detail and detail != last:
                        observer({"command": "bootstatus", **detail})
                        last = detail
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except BaseException:
                process.kill()
                raise
        return subprocess.CompletedProcess(command, code, output.decode(errors="replace"))

    def run(self, *args, timeout=45):
        deadline = self._deadline.get()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeTimeout("Native operation budget exhausted; outcome uncertain")
            timeout = min(timeout, remaining)
        self.check_toolchain()
        env = dict(os.environ)
        env.pop("SIMULATOR_DEVICE_SET_PATH", None)
        command = [*self.tool_command, "simctl", "--set", str(self.device_root), *args]
        observer = self._boot_observer.get()
        if observer:
            observer({"command": args[0]})
        try:
            p = (
                self.monitor_bootstatus(command, timeout, env, observer)
                if observer and args[0] == "bootstatus"
                else subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
            )
        except subprocess.TimeoutExpired as exc:
            statuses = boot_statuses(exc.output) if args[0] == "bootstatus" else []
            if ("3", "YES") in statuses:
                raise BootFailure(
                    "Native data migration failed; release this disposable device"
                ) from exc
            detail = f"; last native boot status {statuses[-1][0]}" if statuses else ""
            recovery = (
                "keep the lease for the coordinator's same-device readiness check"
                if args[0] in ("boot", "bootstatus")
                else "reconcile before retrying"
            )
            raise NativeTimeout(
                f"simctl {args[0]} exceeded {timeout:.1f}s{detail}; outcome uncertain, {recovery}"
            ) from exc
        except OSError as exc:
            raise Attention("simctl could not start; inspect the local Apple toolchain") from exc
        if p.returncode:
            # Never echo arbitrary process output into a client or audit log.
            raise Attention(f"simctl {args[0]} failed (exit {p.returncode}); inspect locally")
        return p.stdout

    def inventory(self):
        try:
            result = json.loads(self.run("list", "--json"))
            if not all(key in result for key in ("devices", "devicetypes", "runtimes")):
                raise ValueError("Missing inventory fields")
            return result
        except (ValueError, TypeError) as exc:
            raise Attention("Simulator inventory could not be verified") from exc

    def devices(self):
        result = {}
        for runtime, devices in self.inventory()["devices"].items():
            for device in devices:
                result[device["udid"]] = {**device, "runtime": runtime}
        return result

    def validate(self, device_type, runtime):
        inventory = self.inventory()
        if not any(
            r["identifier"] == runtime and r.get("isAvailable") for r in inventory["runtimes"]
        ):
            raise ValueError("Runtime must be an installed, available runtime identifier")
        if not any(t["identifier"] == device_type for t in inventory["devicetypes"]):
            raise ValueError("Unknown device type identifier")

    def create(self, name, device_type, runtime):
        return self.run(
            "create", name, device_type, runtime, timeout=self.operation_timeout
        ).strip()

    def validate_created(self, uuid, runtime):
        """Backend-specific initialization proof for a journal-owned create intent."""

    def boot(self, uuid):
        # Starting a cold device can itself outlast the generic command limit.
        # Both phases still share Queue.boot's single operation deadline.
        self.run("boot", uuid, timeout=self.operation_timeout)
        self.wait_ready(uuid)

    def wait_ready(self, uuid):
        """Require native terminal readiness even when inventory already says Booted."""
        status = self.run("bootstatus", uuid, "-d", timeout=self.operation_timeout)
        # simctl can exit zero for terminal data-migration failure (status 3).
        # Do not echo native output, which can contain arbitrary device labels.
        if ("3", "YES") in boot_statuses(status) or any(
            line.strip() == "Data Migration Failed" for line in status.splitlines()
        ):
            raise BootFailure("Native data migration failed; release this disposable device")
        # Another holder may have shut the device down while bootstatus was waiting.
        if self.devices().get(uuid, {}).get("state") != "Booted":
            raise Attention("Device stopped during readiness check; native readiness unverified")

    def shutdown(self, uuid):
        self.run("shutdown", uuid)

    def delete(self, uuid):
        self.run("delete", uuid)

    def data_exists(self, uuid):
        path = self.device_root / uuid
        return path.exists() or path.is_symlink()


class Processes:
    """Fence PID reuse with the process's start stamp; ambiguous probes hold a lease."""

    def fingerprint(self, pid):
        if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
            raise ValueError("Invalid process ID")
        try:
            p = subprocess.run(
                [
                    "/bin/ps",
                    "-p",
                    str(pid),
                    "-o",
                    "lstart=",
                    "-o",
                    "uid=",
                    "-o",
                    "stat=",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Attention("Process identity probe unavailable") from exc
        if p.returncode == 1 and not p.stdout.strip() and not p.stderr.strip():
            return None
        if p.returncode or not p.stdout.strip():
            raise Attention("Process identity probe unavailable")
        parts = p.stdout.split()
        if len(parts) != 7 or int(parts[5]) != os.getuid():
            raise Attention("Process identity or ownership could not be verified")
        if parts[6].startswith("Z"):
            return None
        return " ".join(parts[:6])

    def alive(self, pid, fingerprint):
        try:
            current = self.fingerprint(pid)
        except Attention:
            return None
        return current is not None and current == fingerprint
