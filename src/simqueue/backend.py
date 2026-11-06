"""Bounded Apple-tool operations. No caller-supplied executable or device-set path."""

import contextlib
import contextvars
import json
import os
import subprocess
import time
from pathlib import Path


class Attention(RuntimeError):
    """The outcome is uncertain; preserve the resource for reconciliation."""


class AppleBackend:
    operation_timeout = 120

    def __init__(self):
        self._deadline = contextvars.ContextVar("simqueue-native-deadline", default=None)
        self.wrapper = Path.home() / ".local/bin/apple-tools"
        self.tool_command = (
            [str(self.wrapper), "xcrun"] if self.wrapper.exists() else ["/usr/bin/xcrun"]
        )
        self.device_root = Path.home() / "Library/Developer/CoreSimulator/Devices"

    @contextlib.contextmanager
    def operation_budget(self):
        """Share a native-command budget across inventory, readiness and the operation."""
        previous = self._deadline.get()
        deadline = time.monotonic() + self.operation_timeout
        token = self._deadline.set(min(previous, deadline) if previous is not None else deadline)
        try:
            yield
        finally:
            self._deadline.reset(token)

    def run(self, *args, timeout=45):
        deadline = self._deadline.get()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Attention("Native operation budget exhausted; outcome uncertain, reconcile")
            timeout = min(timeout, remaining)
        env = dict(os.environ)
        env.pop("SIMULATOR_DEVICE_SET_PATH", None)
        try:
            p = subprocess.run(
                [*self.tool_command, "simctl", "--set", str(self.device_root), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise Attention(
                f"simctl {args[0]} exceeded {timeout:.1f}s; outcome uncertain, reconcile before retrying"
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
        return self.run("create", name, device_type, runtime).strip()

    def validate_created(self, uuid, runtime):
        """Backend-specific initialization proof for a journal-owned create intent."""

    def boot(self, uuid):
        self.run("boot", uuid)
        self.wait_ready(uuid)

    def wait_ready(self, uuid):
        """Require native terminal readiness even when inventory already says Booted."""
        self.run("bootstatus", uuid, timeout=self.operation_timeout)
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
                ["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "uid=", "-o", "stat="],
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
