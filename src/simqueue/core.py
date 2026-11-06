"""Durable host-local queue. Every operation is serialized across MCP/CLI processes."""

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

from .backend import Attention, Processes


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
    def __init__(self, root, backend, processes=None, clock=time.time, max_devices=2):
        self.root = Path(root).absolute()
        self.backend = backend
        self.processes = processes or Processes()
        self.clock = clock
        if type(max_devices) is not int or not 1 <= max_devices <= 8:
            raise ValueError("Capacity must be 1..8")
        self.capacity = max_devices
        self.state = None

    @contextlib.contextmanager
    def locked(self):
        if self.root.is_symlink():
            raise Attention("State directory must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise Attention("State directory must be owned by this user with mode 0700")
        lock = os.open(
            self.root / "coordinator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
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
            if path.exists():
                try:
                    self.state = json.loads(path.read_text())
                    require(self.state["version"] == 1)
                    require(self.state["capacity"] == self.capacity)
                    require(isinstance(self.state["devices"], dict))
                    require(isinstance(self.state["requests"], dict))
                    require(isinstance(self.state["events"], list))
                    self.validate_state()
                except (ValueError, KeyError, AssertionError, TypeError) as exc:
                    raise Attention(
                        "Invalid state or capacity mismatch; nothing was overwritten"
                    ) from exc
            else:
                self.state = {
                    "version": 1,
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
            self.maintenance(inspect_active=inspect_active)
        except Attention as exc:
            self.state["attention"] = str(exc)
            self.save()
        else:
            self.state.pop("attention", None)
            self.save()

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

    def request(self, token):
        if not isinstance(token, str) or len(token) > 200:
            raise ValueError("Unknown lease capability")
        digest = token_hash(token)
        for request in self.state["requests"].values():
            if secrets.compare_digest(request["token_hash"], digest):
                return request
        raise ValueError("Unknown lease capability")

    def new_request(self, owner, task, ttl, device_type, runtime, share_hash=None, device_id=None):
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
        self.state["sequence"] = r["sequence"]
        self.state["requests"][rid] = r
        self.save({"event": "requested", "request_id": rid})
        return r, token

    def public(self, r):
        d = self.state["devices"].get(r["device_id"], {})
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
            "device_set": str(self.backend.device_root)
            if hasattr(self.backend, "device_root")
            else None,
            "device_state": d.get("state"),
            "device_type": r["device_type"],
            "runtime": r["runtime"],
            "attention": d.get("attention") or self.state.get("attention"),
            "queue_position": next(
                (i + 1 for i, q in enumerate(queued) if q["id"] == r["id"]), None
            ),
        }

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
        if (
            not inspect_active
            and all(d["state"] == "ready" and self.holders(did) for did, d in live)
            and not (pending and len(live) < self.capacity)
        ):
            return
        devices = self.backend.devices()
        for did, d in self.state["devices"].items():
            if d["state"] == "deleted":
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
                if d["state"] == "creating" and actual is not None:
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
        queued = sorted(
            (r for r in self.state["requests"].values() if r["state"] == "queued"),
            key=lambda r: r["sequence"],
        )
        for r in queued:
            if (
                sum(d["state"] != "deleted" for d in self.state["devices"].values())
                >= self.capacity
            ):
                break
            alive = self.processes.alive(r["owner_pid"], r["owner_stamp"])
            if alive is None:
                raise Attention("Queued owner is unverifiable; allocation paused")
            if alive is False:
                r["state"] = "expired"
                self.save()
                continue
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
            }
            self.state["devices"][did] = d
            r["device_id"] = did
            r["state"] = "creating"
            self.save({"event": "create-intent", "device_id": did})
            try:
                self.backend.validate(r["device_type"], r["runtime"])
                created = self.backend.create(d["name"], d["device_type"], d["runtime"])
                uuid.UUID(created)
                d["uuid"] = created
                if self.checked_device(d, self.backend.devices()) is None:
                    raise Attention("Created device is not yet visible; reconcile the intent")
                self.backend.validate_created(created, d["runtime"])
                r["state"] = "granted"
                d["state"] = "ready"
                self.save({"event": "granted", "request_id": r["id"], "device_id": did})
            except (Attention, ValueError) as exc:
                d["attention"] = str(exc)
                self.save({"event": "create-uncertain", "device_id": did})

    def acquire(self, owner, task, device_type, runtime, ttl=900, share=False):
        with self.locked():
            self.backend.validate(device_type, runtime)
            shared = secrets.token_urlsafe(32) if share else None
            r, token = self.new_request(
                owner, task, ttl, device_type, runtime, token_hash(shared) if shared else None
            )
            self.maintain_safely()
            return {**self.public(r), "lease_token": token, "share_token": shared}

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
            self.maintain_safely()
            return self.public(r)

    def release(self, token):
        with self.locked():
            r = self.request(token)
            r["state"] = "released"
            self.save({"event": "released", "request_id": r["id"]})
            self.maintain_safely()
            return self.public(r)

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

    def boot(self, token):
        with self.locked(), self.backend.operation_budget():
            r = self.request(token)
            if r["state"] != "granted":
                raise ValueError("An active lease is required")
            d = self.state["devices"][r["device_id"]]
            a = self.checked_device(d, self.backend.devices())
            if a is None:
                raise Attention("Managed device is missing")
            if a["state"] == "Shutdown":
                self.backend.boot(d["uuid"])
            elif a["state"] == "Booted":
                self.backend.wait_ready(d["uuid"])
            else:
                raise Attention("Managed device is transitioning; native readiness unverified")
            return {**self.public(r), "boot_ready": True}

    def install(self, token, app_path):
        with self.locked(), self.backend.operation_budget():
            r = self.request(token)
            if r["state"] != "granted":
                raise ValueError("An active lease is required")
            if not hasattr(self.backend, "install"):
                raise Attention("Managed install requires the external storage image backend")
            d = self.state["devices"][r["device_id"]]
            actual = self.checked_device(d, self.backend.devices())
            if actual is None or actual["state"] != "Booted":
                raise Attention("Managed device must be booted for install")
            self.backend.wait_ready(d["uuid"])
            return self.backend.install(d["uuid"], app_path)

    def capture(self, token, filename="screenshot.png"):
        with self.locked(), self.backend.operation_budget():
            r = self.request(token)
            if r["state"] != "granted":
                raise ValueError("An active lease is required")
            if not hasattr(self.backend, "capture"):
                raise Attention("Managed capture requires the external storage image backend")
            d = self.state["devices"][r["device_id"]]
            actual = self.checked_device(d, self.backend.devices())
            if actual is None or actual["state"] != "Booted":
                raise Attention("Managed device must be booted for capture")
            self.backend.wait_ready(d["uuid"])
            return self.backend.capture(d["uuid"], r["id"], filename)

    def status(self, sweep=False):
        with self.locked():
            if sweep:
                self.maintain_safely(inspect_active=False)
            return {
                "capacity": self.capacity,
                "storage": self.backend.store.status()
                if hasattr(self.backend, "store")
                else {"mode": "internal"},
                "attention": self.state.get("attention"),
                "requests": [self.public(r) for r in self.state["requests"].values()],
                "devices": [
                    {k: v for k, v in d.items() if k != "share_hash"}
                    for d in self.state["devices"].values()
                ],
                "events": self.state["events"][-20:],
            }
