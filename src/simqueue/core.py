"""Durable host-local queue with serialized ownership and per-device operations."""

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
import uuid
from pathlib import Path

from .backend import Attention, BootFailure, NativeTimeout, Processes
from .host_slot import HostSlot, null_slot
from .resource_log import observe

ROUTINE_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-5"


class BootReadinessUncertain(Attention):
    """The bounded same-device recheck could not establish readiness."""


def require(condition):
    if not condition:
        raise ValueError("Invalid state")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Owner and task labels must contain 1..200 characters")
    return value


class Queue:
    def __init__(
        self,
        root,
        backend,
        processes=None,
        clock=time.time,
        max_devices=2,
        migrate_default_capacity=False,
        host_slot=None,
    ):
        self.root = Path(root).absolute()
        self.backend = backend
        self.processes = processes or Processes()
        self.clock = clock
        if type(max_devices) is not int or not 1 <= max_devices <= 8:
            raise ValueError("Capacity must be 1..8")
        self.capacity = max_devices
        self.migrate_default_capacity = migrate_default_capacity
        # The shared developer-host cold-boot/render slot guards the default
        # installed queue only; fixtures and custom state directories opt in.
        if host_slot is None:
            host_slot = HostSlot() if migrate_default_capacity else null_slot
        self.host_slot = host_slot
        self.state = None

    @contextlib.contextmanager
    def locked(self, read_only=False, legacy_sweep=False):
        if self.root.is_symlink():
            raise Attention("State directory must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise Attention("State directory must be owned by this user with mode 0700")
        lock = os.open(
            self.root / "coordinator.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            end = time.monotonic() + 8
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise Attention("Coordinator busy; retry with the original lease token")
                    time.sleep(0.05)
            path = self.root / "state.json"
            if path.is_symlink():
                raise Attention("State file must not be a symlink")
            migrate = False
            if path.exists():
                try:
                    self.state = json.loads(path.read_text())
                    require(self.state["version"] in (1, 2))
                    require(isinstance(self.state["devices"], dict))
                    require(isinstance(self.state["requests"], dict))
                    require(isinstance(self.state["events"], list))
                    self.validate_state()
                    migrate = self.state["version"] == 1
                    legacy_live = (
                        migrate
                        and self.migrate_default_capacity
                        and (
                            any(d["state"] != "deleted" for d in self.state["devices"].values())
                            or any(
                                r["state"] not in ("released", "expired")
                                for r in self.state["requests"].values()
                            )
                        )
                    )
                    if legacy_live:
                        if not read_only and not legacy_sweep:
                            raise Attention(
                                "Previous simqueue clients are still active; wait for their verified cleanup"
                            )
                        migrate = False
                    if self.state["capacity"] != self.capacity:
                        require(
                            self.migrate_default_capacity
                            and self.state["capacity"] == 2
                            and self.capacity == 1
                        )
                        if (
                            not read_only
                            and all(d["state"] == "deleted" for d in self.state["devices"].values())
                            and all(
                                r["state"] in ("released", "expired")
                                for r in self.state["requests"].values()
                            )
                        ):
                            self.state["capacity"] = self.capacity
                            migrate = True
                except (ValueError, KeyError, AssertionError, TypeError) as exc:
                    raise Attention(
                        "Invalid state or capacity mismatch; nothing was overwritten"
                    ) from exc
            else:
                self.state = {
                    "version": 2,
                    "capacity": self.capacity,
                    "devices": {},
                    "requests": {},
                    "events": [],
                }
            namespace = str(getattr(self.backend, "device_root", "test-backend"))
            previous = self.state.get("backend_key")
            if previous is None and any(
                d["state"] != "deleted" for d in self.state["devices"].values()
            ):
                expected = str(Path.home() / "Library/Developer/CoreSimulator/Devices")
                if namespace not in (expected, "test-backend"):
                    raise Attention("Legacy ownership journal requires its original device set")
            if previous is not None and previous != namespace:
                raise Attention(
                    "Device set changed; restart this client with the configured backend"
                )
            self.state["backend_key"] = namespace
            self.prune()
            if migrate and not read_only:
                self.state["version"] = 2
                self.save()
            yield
        finally:
            self.state = None
            os.close(lock)

    def validate_state(self):
        # Ownership must be provable before an external command is permitted.
        for did, d in self.state["devices"].items():
            require(uuid.UUID(hex=did).hex == did == d["id"])
            require(d["name"] == "agent-simqueue-" + did)
            require(d["state"] in ("creating", "ready", "deleting", "deleted"))
            require(d["request_id"] in self.state["requests"])
            require(type(d.get("boot_failure", False)) is bool)
            require(type(d.get("boot_timeout", False)) is bool)
            if (operation := d.get("operation")) is not None:
                require(self.state["version"] == 2)
                require(
                    (operation["kind"] == "create" and d["state"] == "creating")
                    or (
                        operation["kind"] in ("boot", "install", "capture")
                        and d["state"] == "ready"
                    )
                )
                require(type(operation["pid"]) is int and operation["pid"] > 0)
                require(isinstance(operation["stamp"], str) and bool(operation["stamp"]))
                require(type(operation["started"]) in (float, int))
            if d["uuid"] is not None:
                uuid.UUID(d["uuid"])
        for rid, r in self.state["requests"].items():
            require(uuid.UUID(hex=rid).hex == rid == r["id"])
            require(r["state"] in ("queued", "creating", "granted", "released", "expired"))
            require(r["device_id"] is None or r["device_id"] in self.state["devices"])
            require(isinstance(r["token_hash"], str) and len(r["token_hash"]) == 64)
            require(isinstance(r["owner_stamp"], str))
            require(type(r["owner_pid"]) is int)
            require(isinstance(r["processes"], list))
            require(type(r["expires"]) in (int, float))
            require(isinstance(r["runtime"], str) and isinstance(r["device_type"], str))

    def maintain_safely(self, inspect_active=True):
        try:
            intent = self.maintenance(inspect_active=inspect_active)
        except Attention as exc:
            self.state["attention"] = str(exc)
            self.save()
        else:
            self.state.pop("attention", None)
            self.save()
            return intent

    def prune(self):
        cutoff = self.clock() - 7 * 86400
        removable = {
            did
            for did, d in self.state["devices"].items()
            if d["state"] == "deleted"
            and all(
                r["expires"] < cutoff
                for r in self.state["requests"].values()
                if r["device_id"] == did
            )
        }
        self.state["requests"] = {
            rid: r
            for rid, r in self.state["requests"].items()
            if not (
                r["state"] in ("released", "expired")
                and r["expires"] < cutoff
                and (r["device_id"] is None or r["device_id"] in removable)
            )
        }
        for did in removable:
            del self.state["devices"][did]

    def save(self, event=None):
        if event:
            self.state["events"].append({"at": self.clock(), **event})
            self.state["events"] = self.state["events"][-500:]
        fd, name = tempfile.mkstemp(prefix="state-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.state, f, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.root / "state.json")
            parent = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(name):
                os.unlink(name)

        if event and self.migrate_default_capacity:
            self.observe_transition(event)

    def capacity_counts(self):
        return {
            "capacity": self.state["capacity"],
            "queued": sum(r["state"] == "queued" for r in self.state["requests"].values()),
            "active": sum(d["state"] != "deleted" for d in self.state["devices"].values()),
        }

    def observe(self, event, **fields):
        if self.migrate_default_capacity:
            observe(event, **self.capacity_counts(), **fields)

    def observe_transition(self, event):
        kind = event["event"]
        if kind not in {"requested", "create-intent", "released", "cleanup-verified"}:
            return
        device = self.state["devices"].get(event.get("device_id"))
        request_id = event.get("request_id") or (device and device["request_id"])
        request = self.state["requests"].get(request_id)
        if request is None:
            return
        managed = request.get("run_id")
        run_id = managed or request_id
        if kind == "create-intent":
            wait_ms = max(0, round((self.clock() - request["created"]) * 1000))
            self.observe("admit", run_id=run_id, wait_ms=wait_ms)
        elif not managed and kind == "requested" and request["state"] == "queued":
            self.observe("enqueue", run_id=run_id, reason="waiting-for-device")
        elif not managed and kind == "released" and request["device_id"] is None:
            self.observe("cancel", run_id=run_id)
        elif not managed and kind == "cleanup-verified":
            self.observe("finish", run_id=run_id, cleanup="verified")

    def request(self, token):
        if not isinstance(token, str) or len(token) > 200:
            raise ValueError("Unknown lease capability")
        digest = token_hash(token)
        for request in self.state["requests"].values():
            if secrets.compare_digest(request["token_hash"], digest):
                return request
        raise ValueError("Unknown lease capability")

    def new_request(
        self, owner, task, ttl, device_type, runtime, share_hash=None, device_id=None, run_id=None
    ):
        if type(ttl) is not int or not 60 <= ttl <= 86400:
            raise ValueError("TTL must be 60..86400 seconds")
        owner, task = label(owner), label(task)
        stamp = self.processes.fingerprint(os.getpid())
        if stamp is None:
            raise Attention("Cannot identify this requester process")
        token = secrets.token_urlsafe(32)
        rid = uuid.uuid4().hex
        r = {
            "id": rid,
            "owner": owner,
            "task": task,
            "token_hash": token_hash(token),
            "created": self.clock(),
            "expires": self.clock() + ttl,
            "ttl": ttl,
            "owner_pid": os.getpid(),
            "owner_stamp": stamp,
            "processes": [],
            "device_type": device_type,
            "runtime": runtime,
            "share_hash": share_hash,
            "state": "granted" if device_id else "queued",
            "device_id": device_id,
            "sequence": self.state.get("sequence", 0) + 1,
        }
        if run_id is not None:
            r["run_id"] = run_id
        self.state["sequence"] = r["sequence"]
        self.state["requests"][rid] = r
        self.save({"event": "requested", "request_id": rid})
        return r, token

    def public(self, r):
        d = self.state["devices"].get(r["device_id"], {})
        device_set = str(self.backend.device_root) if hasattr(self.backend, "device_root") else None
        held = r["state"] == "granted" and r["expires"] <= self.clock()
        queued = [q for q in self.state["requests"].values() if q["state"] == "queued"]
        queued.sort(key=lambda q: q["sequence"])
        return {
            "request_id": r["id"],
            "state": "stale-held" if held else r["state"],
            "owner": r["owner"],
            "task": r["task"],
            "expires": r["expires"],
            "uuid": d.get("uuid"),
            "device_set": device_set,
            "xcodebuild_args": [f"-DVTSimulatorSetLocation={device_set}"] if device_set else [],
            "device_state": d.get("state"),
            "device_type": r["device_type"],
            "runtime": r["runtime"],
            "attention": d.get("attention") or self.state.get("attention"),
            "operation": self.public_operation(d),
            "boot_error": d.get("boot_error"),
            **({"run_id": r["run_id"]} if "run_id" in r else {}),
            "queue_position": next(
                (i + 1 for i, q in enumerate(queued) if q["id"] == r["id"]), None
            ),
        }

    def public_operation(self, device):
        operation = device.get("operation")
        if operation is None:
            return None
        return {"kind": operation["kind"], "started": operation["started"]}

    def holders(self, device_id):
        return [
            r
            for r in self.state["requests"].values()
            if r["device_id"] == device_id and r["state"] == "granted"
        ]

    def checked_device(self, d, devices):
        actual = devices.get(d.get("uuid"))
        if actual is not None and any(
            actual.get(k) != d[v]
            for k, v in [
                ("name", "name"),
                ("runtime", "runtime"),
                ("deviceTypeIdentifier", "device_type"),
            ]
        ):
            raise Attention("Managed device identity changed; deletion refused")
        return actual

    def maintenance(self, inspect_active=True):
        now = self.clock()
        for r in self.state["requests"].values():
            if r["state"] in ("queued", "granted", "creating") and r["expires"] <= now:
                try:
                    dead = self.processes.alive(r["owner_pid"], r["owner_stamp"]) is False
                except Attention:
                    dead = False
                if r["state"] == "queued" or dead:
                    r["state"] = "expired"
        self.save()
        if not any(d["state"] != "deleted" for d in self.state["devices"].values()) and not any(
            r["state"] in ("queued", "creating", "granted") for r in self.state["requests"].values()
        ):
            return
        live = [(did, d) for did, d in self.state["devices"].items() if d["state"] != "deleted"]
        pending = any(r["state"] == "queued" for r in self.state["requests"].values())
        running = [(did, d) for did, d in live if d.get("operation")]
        if running:
            # A slow native command can also stall inventory for the whole device set.
            owners = [self.inspect_operation(did, device) for did, device in running]
            if any(owners):
                return
            for did, d in running:
                if d["operation"]["kind"] == "create":
                    # The producer died. Reconcile its original identity from inventory.
                    d.pop("operation")
                    self.save({"event": "create-owner-gone", "device_id": did})
        if (
            not inspect_active
            and all(d["state"] == "ready" and self.holders(did) for did, d in live)
            and not (pending and len(live) < self.state["capacity"])
        ):
            return
        devices = self.backend.devices() if live else {}
        for did, d in self.state["devices"].items():
            if d["state"] == "deleted":
                continue
            if d.get("operation"):
                continue
            try:
                if not d.get("uuid"):
                    matches = [
                        a
                        for a in devices.values()
                        if a["name"] == d["name"]
                        and a["runtime"] == d["runtime"]
                        and a["deviceTypeIdentifier"] == d["device_type"]
                    ]
                    if len(matches) != 1:
                        raise Attention("Uncertain create intent; preserve and reconcile")
                    d["uuid"] = matches[0]["udid"]
                    # Identifying a UUID does not prove post-create initialization finished.
                    self.save({"event": "create-identified", "device_id": did})
                actual = self.checked_device(d, devices)
                if d["state"] == "creating":
                    if actual is None:
                        raise Attention("Created UUID is not yet visible; reconcile the intent")
                    request = self.state["requests"][d["request_id"]]
                    if request["state"] == "creating":
                        self.backend.validate_created(d["uuid"], d["runtime"])
                        d["state"] = "ready"
                        request["state"] = "granted"
                        self.save({"event": "create-reconciled", "device_id": did})
                if self.holders(did):
                    if actual is None:
                        raise Attention("A leased device disappeared; no replacement was created")
                    d.pop("attention", None)
                    continue
                associated = [
                    p
                    for r in self.state["requests"].values()
                    if r["device_id"] == did
                    for p in r["processes"]
                ]
                if any(self.processes.alive(p["pid"], p["stamp"]) is not False for p in associated):
                    raise Attention("Associated process is live or unverifiable; device retained")
                # Journal deletion before any external side effect. Last release is durable already.
                d["state"] = "deleting"
                self.save({"event": "cleanup-started", "device_id": did})
                if actual is not None:
                    if actual["state"] != "Shutdown":
                        self.backend.shutdown(d["uuid"])
                    current = self.checked_device(d, self.backend.devices())
                    if current is not None and current["state"] != "Shutdown":
                        raise Attention("Device is not shut down; deletion refused")
                    self.backend.delete(d["uuid"])
                after = self.backend.devices()
                if d["uuid"] in after or self.backend.data_exists(d["uuid"]):
                    raise Attention("Device or data directory remains after deletion")
                d["state"] = "deleted"
                d.pop("attention", None)
                self.save({"event": "cleanup-verified", "device_id": did})
            except Attention as exc:
                d["attention"] = str(exc)
                self.save({"event": "attention", "device_id": did})
        # A v1 client can still be using this journal during legacy cleanup.
        if self.state["version"] == 1 or any(
            d["state"] == "creating" for d in self.state["devices"].values()
        ):
            return
        queued = sorted(
            (r for r in self.state["requests"].values() if r["state"] == "queued"),
            key=lambda r: r["sequence"],
        )
        for r in queued:
            if (
                sum(d["state"] != "deleted" for d in self.state["devices"].values())
                >= self.state["capacity"]
            ):
                break
            alive = self.processes.alive(r["owner_pid"], r["owner_stamp"])
            if alive is None:
                raise Attention("Queued owner is unverifiable; allocation paused")
            if alive is False:
                r["state"] = "expired"
                self.save()
                continue
            pid = os.getpid()
            stamp = self.processes.fingerprint(pid)
            if stamp is None:
                raise Attention("Cannot identify this create operation owner")
            did = uuid.uuid4().hex
            d = {
                "id": did,
                "name": "agent-simqueue-" + did,
                "uuid": None,
                "state": "creating",
                "request_id": r["id"],
                "device_type": r["device_type"],
                "runtime": r["runtime"],
                "share_hash": r["share_hash"],
                "operation": {
                    "kind": "create",
                    "pid": pid,
                    "stamp": stamp,
                    "started": now,
                },
            }
            self.state["devices"][did] = d
            r["device_id"] = did
            r["state"] = "creating"
            self.save({"event": "create-intent", "device_id": did})
            return d.copy()

    def create_reserved(self, intent):
        if intent is None:
            return
        did = intent["id"]
        created = None
        error = None
        try:
            with self.backend.operation_budget():
                result = self.backend.create(
                    intent["name"], intent["device_type"], intent["runtime"]
                )
                uuid.UUID(result)
                created = result
                intent["uuid"] = created
                if self.checked_device(intent, self.backend.devices()) is None:
                    raise Attention("Created device is not yet visible; reconcile the intent")
                self.backend.validate_created(created, intent["runtime"])
        except (Attention, ValueError) as exc:
            error = str(exc)
        with self.locked():
            device = self.state["devices"][did]
            require(device["operation"] == intent["operation"])
            request = self.state["requests"][device["request_id"]]
            device.pop("operation")
            device["uuid"] = created
            if error is not None:
                device["attention"] = error
                self.save({"event": "create-uncertain", "device_id": did})
            elif request["state"] == "creating":
                request["state"] = "granted"
                device["state"] = "ready"
                self.save({"event": "granted", "request_id": request["id"], "device_id": did})
            else:
                self.save({"event": "create-finished-after-release", "device_id": did})

    def inspect_operation(self, device_id, device):
        operation = device.get("operation")
        if operation is None:
            return
        try:
            alive = self.processes.alive(operation["pid"], operation["stamp"])
        except Attention:
            alive = None
        if alive is not True:
            message = (
                "Native operation owner is gone or unverifiable; device retained for inspection"
            )
            if device.get("attention") != message:
                device["attention"] = message
                self.save({"event": "operation-uncertain", "device_id": device_id})
        return alive is not False

    def acquire(
        self,
        owner,
        task,
        device_type,
        runtime=ROUTINE_RUNTIME,
        ttl=900,
        share=False,
        *,
        run_id=None,
        on_request=None,
    ):
        # Validation can call simctl as well. Keep it outside the journal lock.
        if on_request is not None and not callable(on_request):
            raise ValueError("Invalid request observer")
        self.backend.validate(device_type, runtime)
        with self.locked():
            shared = secrets.token_urlsafe(32) if share else None
            request, token = self.new_request(
                owner,
                task,
                ttl,
                device_type,
                runtime,
                token_hash(shared) if shared else None,
                run_id=run_id,
            )
            if on_request is not None:
                on_request(request["id"], token)
            intent = self.maintain_safely(inspect_active=False)
        self.create_reserved(intent)
        with self.locked(read_only=True):
            return {
                **self.public(self.request(token)),
                "lease_token": token,
                "share_token": shared,
            }

    def join(self, share_token, owner, task, ttl=900):
        with self.locked():
            digest = token_hash(share_token)
            d = next(
                (
                    d
                    for d in self.state["devices"].values()
                    if d.get("share_hash") == digest and d["state"] == "ready"
                ),
                None,
            )
            if d is None or not any(r["expires"] > self.clock() for r in self.holders(d["id"])):
                raise ValueError("Unknown or inactive sharing capability")
            if any(device.get("operation") for device in self.state["devices"].values()):
                raise Attention("A native operation is in progress; join after it completes")
            if self.checked_device(d, self.backend.devices()) is None:
                raise Attention("Shared device is missing")
            r, token = self.new_request(
                owner, task, ttl, d["device_type"], d["runtime"], device_id=d["id"]
            )
            self.save({"event": "joined", "request_id": r["id"], "device_id": d["id"]})
            return {**self.public(r), "lease_token": token}

    def poll(self, token, renew=False):
        with self.locked():
            r = self.request(token)
            if renew and r["state"] in ("queued", "granted", "creating"):
                r["expires"] = self.clock() + r["ttl"]
                self.save()
                if r["state"] == "granted":
                    return self.public(r)
            intent = self.maintain_safely(inspect_active=False)
        self.create_reserved(intent)
        with self.locked(read_only=True):
            return self.public(self.request(token))

    def release(self, token):
        with self.locked():
            r = self.request(token)
            r["state"] = "released"
            self.save({"event": "released", "request_id": r["id"]})
            intent = self.maintain_safely(inspect_active=False)
        self.create_reserved(intent)
        with self.locked(read_only=True):
            return self.public(self.request(token))

    def track_process(self, token, pid):
        with self.locked():
            r = self.request(token)
            if r["state"] != "granted":
                raise ValueError("Only an active holder can register a process")
            stamp = self.processes.fingerprint(pid)
            if stamp is None:
                raise ValueError("Process does not exist")
            entry = {"pid": pid, "stamp": stamp}
            if entry not in r["processes"]:
                r["processes"].append(entry)
                self.save({"event": "process-tracked", "request_id": r["id"]})
            return {"tracked": True, "pid": pid}

    @contextlib.contextmanager
    def device_operation(self, token, kind):
        with self.locked():
            request = self.request(token)
            if request["state"] != "granted":
                raise ValueError("An active lease is required")
            device = self.state["devices"][request["device_id"]]
            if device.get("operation"):
                raise Attention(
                    "Device operation already in progress or unresolved; check lease status"
                )
            if device.get("boot_failure"):
                raise BootFailure("Native data migration failed; release this disposable device")
            if device.get("boot_timeout"):
                raise Attention(
                    "Boot readiness remains uncertain after a recheck; inspect logs and release"
                )
            pid = os.getpid()
            stamp = self.processes.fingerprint(pid)
            if stamp is None:
                raise Attention("Cannot identify this native operation owner")
            operation = {
                "kind": kind,
                "pid": pid,
                "stamp": stamp,
                "started": self.clock(),
            }
            device["operation"] = operation
            device_id, request_id = device["id"], request["id"]
            self.save({"event": "operation-started", "device_id": device_id, "kind": kind})

        event = "operation-finished"
        boot_error = None
        try:
            yield device, request_id
        except BootFailure as exc:
            event = "boot-failed"
            boot_error = str(exc)
            raise
        except BootReadinessUncertain as exc:
            event = "boot-recheck-failed"
            boot_error = str(exc)
            raise
        except Exception:
            event = "operation-error"
            raise
        finally:
            with self.locked():
                current = self.state["devices"][device_id]
                require(current["operation"] == operation)
                current.pop("operation")
                if event == "boot-failed":
                    current["boot_failure"] = True
                if event == "boot-recheck-failed":
                    current["boot_timeout"] = True
                entry = {
                    "event": event,
                    "device_id": device_id,
                    "kind": kind,
                    "elapsed_seconds": round(max(0, self.clock() - operation["started"]), 1),
                }
                if boot_error is not None:
                    current["boot_error"] = boot_error
                    entry["boot_error"] = boot_error
                self.save(entry)

    def boot(self, token, progress=None):
        observe = self.backend.observe_boot(progress) if progress else contextlib.nullcontext()
        with self.device_operation(token, "boot") as (device, request_id), observe:
            # Decide whether this is a cold boot before taking the shared host
            # slot. The slot wait runs outside every native budget, so it never
            # counts toward the boot timeout; the boot and its same-device
            # recheck then run inside the slot, which is released when the
            # simulator is ready or the attempt ends.
            try:
                with self.backend.operation_budget(seconds=30):
                    current = self.checked_device(device, self.backend.devices())
            except NativeTimeout:
                current = None  # inventory stalled: assume a cold boot follows
            else:
                if current is None:
                    raise Attention("Managed device is missing")
            cold = current is None or current["state"] in ("Shutdown", "Booting")
            if cold:
                with self.locked(read_only=True):
                    request = self.state["requests"][request_id]
                    run_id = request.get("run_id") or request_id
                slot = self.host_slot(run_id, progress)
            else:
                slot = contextlib.nullcontext()
            with slot:
                self.boot_attempt(device, progress)
        with self.locked():
            request = self.state["requests"][request_id]
            return {**self.public(request), "boot_ready": request["state"] == "granted"}

    def boot_attempt(self, device, progress):
        """One boot under the native budget plus at most one same-device recheck."""
        try:
            with self.backend.operation_budget():
                actual = self.checked_device(device, self.backend.devices())
                if actual is None:
                    raise Attention("Managed device is missing")
                if actual["state"] == "Shutdown":
                    self.backend.boot(device["uuid"])
                elif actual["state"] in ("Booting", "Booted"):
                    self.backend.wait_ready(device["uuid"])
                else:
                    raise Attention("Managed device is transitioning; native readiness unverified")
        except NativeTimeout as initial:
            if progress:
                progress({"attempt": "recheck"})
            try:
                with self.backend.operation_budget(seconds=30):
                    actual = self.checked_device(device, self.backend.devices())
                    if actual is None or actual["state"] not in (
                        "Booted",
                        "Booting",
                    ):
                        raise Attention("Managed device is not booting or booted")
                    self.backend.wait_ready(device["uuid"])
            except BootFailure:
                raise
            except Attention as exc:
                raise BootReadinessUncertain(
                    f"Boot readiness remains uncertain after one recheck. Initial wait: {initial}. "
                    f"Recheck: {exc}. Inspect CoreSimulator logs for this UUID and release the lease."
                ) from exc

    def booted_operation(self, token, kind, argument):
        with (
            self.device_operation(token, kind) as (device, request_id),
            self.backend.operation_budget(),
        ):
            operation = getattr(self.backend, kind, None)
            if operation is None:
                raise Attention(f"Managed {kind} requires the external storage image backend")
            actual = self.checked_device(device, self.backend.devices())
            if actual is None or actual["state"] != "Booted":
                raise Attention(f"Managed device must be booted for {kind}")
            self.backend.wait_ready(device["uuid"])
            if kind == "capture":
                return operation(device["uuid"], request_id, argument)
            return operation(device["uuid"], argument)

    def install(self, token, app_path):
        return self.booted_operation(token, "install", app_path)

    def capture(self, token, filename="screenshot.png"):
        return self.booted_operation(token, "capture", filename)

    def request_status(self, request_id):
        with self.locked(read_only=True):
            request = self.state["requests"].get(request_id)
            return self.public(request) if request is not None else None

    def status(self, sweep=False, compact=False):
        if sweep:
            with self.locked(legacy_sweep=True):
                intent = self.maintain_safely(inspect_active=False)
            self.create_reserved(intent)
        with self.locked(read_only=True):
            summary = {
                "capacity": self.state["capacity"],
                "storage": self.backend.store.status()
                if hasattr(self.backend, "store")
                else {"mode": "internal"},
                "attention": self.state.get("attention"),
            }
            if sweep and self.migrate_default_capacity:
                counts = self.capacity_counts()
                if counts["active"] or counts["queued"]:
                    observe("sample", **counts)
            if compact:
                active = sorted(
                    (
                        r
                        for r in self.state["requests"].values()
                        if r["state"] in ("queued", "creating", "granted")
                    ),
                    key=lambda r: r["sequence"],
                )
                devices = self.state["devices"].values()
                return {
                    **summary,
                    "active_count": len(active),
                    "queued_count": sum(r["state"] == "queued" for r in active),
                    "requests": [
                        {
                            **{
                                key: value
                                for key, value in self.public(r).items()
                                if key not in {"device_set", "xcodebuild_args", "expires"}
                                and value is not None
                            },
                            "elapsed_seconds": round(max(0, self.clock() - r["created"]), 1),
                        }
                        for r in active[:20]
                    ],
                    "device_attention": [
                        {"uuid": d.get("uuid"), "attention": d["attention"]}
                        for d in devices
                        if d.get("attention") and d["state"] != "deleted"
                    ],
                }
            return {
                **summary,
                "requests": [self.public(r) for r in self.state["requests"].values()],
                "devices": [
                    {
                        **{k: v for k, v in d.items() if k not in ("share_hash", "operation")},
                        "operation": self.public_operation(d),
                    }
                    for d in self.state["devices"].values()
                ],
                "events": self.state["events"][-20:],
            }
