"""APFS image connection point. Backing bytes remain on a verified T9 volume.

No privacy changes, privileged helper, physical-disk formatting or force ejects.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .backend import AppleBackend, Attention


def command(args, *, plist=False, timeout=60):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        if result.returncode:
            raise Attention(f"Storage command failed: {Path(args[0]).name} {args[1]}")
        return plistlib.loads(result.stdout) if plist else result.stdout
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise Attention("Storage command unavailable or result unverifiable") from exc


def atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix="storage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ImageStore:
    def __init__(self, state_root, config):
        self.root = Path(state_root).absolute()
        self.config = config
        try:
            if config["version"] != 1:
                raise ValueError("Unsupported storage config")
            self.image = Path(config["image"])
            self.mount = Path(config["mount"])
            self.t9 = Path(config["t9_root"])
            if not all(p.is_absolute() for p in (self.image, self.mount, self.t9)):
                raise ValueError("Absolute paths required")
            if not self.image.is_relative_to(self.t9) or self.mount.is_relative_to(self.t9):
                raise ValueError("Image must be on T9; mount point must be internal")
            if not config["t9_uuid"] or not config["volume_uuid"]:
                raise ValueError("Storage identities missing")
        except (KeyError, TypeError, ValueError) as exc:
            raise Attention("Invalid storage configuration; internal fallback refused") from exc

    @classmethod
    def load(cls, root):
        path = Path(root) / "storage.json"
        if path.is_symlink():
            raise Attention("Storage config must not be a symlink")
        if not path.exists():
            return None
        try:
            return cls(root, json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            raise Attention("Storage config unreadable; internal fallback refused") from exc

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.root / "storage.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 8
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Attention("Storage connection busy; retry")
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def backing(self):
        if not os.path.ismount(self.t9) or self.t9.is_symlink():
            raise Attention("T9 is not mounted; internal fallback refused")
        info = command(["/usr/sbin/diskutil", "info", "-plist", str(self.t9)], plist=True)
        if info.get("VolumeUUID") != self.config["t9_uuid"]:
            raise Attention("T9 volume identity changed; storage access refused")
        if self.image.is_symlink() or not self.image.is_dir():
            raise Attention("Configured T9 sparsebundle is missing or replaced")
        if self.image.resolve() != self.image or self.image.stat().st_dev != self.t9.stat().st_dev:
            raise Attention("Image backing is outside the verified T9 filesystem")

    def attachment(self):
        images = command(["/usr/bin/hdiutil", "info", "-plist"], plist=True).get("images", [])
        matches = [image for image in images if image.get("image-path") == str(self.image)]
        if len(matches) > 1:
            raise Attention("Multiple image attachments; preserve and inspect")
        return matches[0] if matches else None

    def verify_mount(self):
        if not os.path.ismount(self.mount) or self.mount.is_symlink():
            raise Attention("Expected image volume is not mounted")
        attachment = self.attachment()
        if attachment is None:
            raise Attention("Mounted path does not belong to the configured image")
        info = command(["/usr/sbin/diskutil", "info", "-plist", str(self.mount)], plist=True)
        if info.get("VolumeUUID") != self.config["volume_uuid"] or info.get("MountPoint") != str(
            self.mount
        ):
            raise Attention("Mounted image identity changed; operation refused")
        return attachment

    def ensure(self):
        with self.locked():
            self.backing()
            if not os.path.ismount(self.mount):
                if self.mount.exists() and (self.mount.is_symlink() or any(self.mount.iterdir())):
                    raise Attention("Unmounted destination is not empty; mounting refused")
                if self.attachment() is not None:
                    raise Attention("Configured image attached elsewhere; preserve and inspect")
                check = json.loads(
                    command(
                        [
                            str(Path.home() / ".local/bin/developer-host"),
                            "storage",
                            "--format",
                            "json",
                            "--checker-protocol",
                            "json",
                        ]
                    )
                )
                if (
                    check.get("status") != "ready"
                    or check.get("authority", {}).get("verdict") != "verified"
                ):
                    raise Attention("Developer storage readiness could not be verified")
                command(
                    [
                        "/usr/sbin/diskutil",
                        "image",
                        "attach",
                        "--nobrowse",
                        "--mountPoint",
                        str(self.mount),
                        str(self.image),
                    ],
                    timeout=120,
                )
            self.verify_mount()

    def detach(self):
        with self.locked():
            self.backing()
            attachment = self.attachment()
            if attachment is None:
                if os.path.ismount(self.mount):
                    raise Attention("Unrelated volume occupies the mount point")
                return {"detached": True}
            self.verify_mount()
            entities = attachment.get("system-entities", [])
            roots = [
                e["dev-entry"]
                for e in entities
                if re.fullmatch(r"(?:/dev/)?disk\d+", e.get("dev-entry", ""))
                and e.get("content-hint") == "GUID_partition_scheme"
            ]
            if len(roots) != 1:
                raise Attention("Cannot identify this image's root device; detach refused")
            for attempt in range(3):
                try:
                    command(["/usr/sbin/diskutil", "eject", roots[0]])
                    break
                except Attention:
                    if attempt == 2:
                        raise Attention(
                            "Image remains busy; preserved without forcing eject"
                        ) from None
                    time.sleep(1)
            if os.path.ismount(self.mount) or self.attachment() is not None:
                raise Attention("Image detach could not be verified")
            return {"detached": True}

    def status(self):
        return {
            "mode": "t9-apfs-image",
            "image": str(self.image),
            "mount": str(self.mount),
            "device_set": str(self.mount / "Devices"),
            "volume_uuid": self.config["volume_uuid"],
            "mounted": os.path.ismount(self.mount),
        }


class ImageBackend(AppleBackend):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.device_root = store.mount / "Devices"

    def run(self, *args, timeout=45):
        self.store.ensure()
        self.device_root.mkdir(mode=0o700, exist_ok=True)
        return super().run(*args, timeout=timeout)

    def create(self, name, device_type, runtime):
        uuid = super().create(name, device_type, runtime)
        # Only a device just created by this call may have its sample Contacts seed
        # moved. Never apply to existing devices, at boot, or on runtime upgrades.
        if runtime in {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
            "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
        }:
            device = self.device_root / uuid
            seed = device / "data/Library/AddressBook"
            if device.is_symlink() or seed.is_symlink() or not seed.is_dir():
                raise Attention("Fresh Contacts seed is unverifiable; preserve device")
            if not seed.resolve().is_relative_to(device.resolve()):
                raise Attention("Fresh Contacts seed escapes the new device; preserve device")
            saved = device / ".simqueue-original-contacts"
            if saved.exists() or saved.is_symlink():
                raise Attention("Fresh Contacts backup already exists; preserve device")
            seed.rename(saved)
            atomic_json(
                device / ".simqueue-initialization.json",
                {
                    "version": 1,
                    "runtime": runtime,
                    "contacts": "empty-on-first-boot",
                    "reason": "Bundled sample Contacts triggers CNContactPosterMigrator null-array crash",
                    "preserved_seed": saved.name,
                },
            )
        return uuid

    def data_exists(self, uuid):
        self.store.ensure()
        return super().data_exists(uuid)

    def boot(self, uuid):
        self.run("boot", uuid)
        self.run("bootstatus", uuid, "-b", timeout=300)

    def install(self, uuid, app_path):
        source = Path(app_path).expanduser().resolve(strict=True)
        if not source.is_dir() or source.suffix != ".app":
            raise ValueError("Provide an existing simulator .app directory")
        info = plistlib.loads((source / "Info.plist").read_bytes())
        if "iPhoneSimulator" not in info.get("CFBundleSupportedPlatforms", []):
            raise ValueError("App must target iPhoneSimulator")
        bundle = info.get("CFBundleIdentifier")
        if not isinstance(bundle, str) or not bundle:
            raise ValueError("App bundle identifier missing")
        self.store.ensure()
        imports = self.store.mount / "Imports"
        if imports.is_symlink():
            raise Attention("Image import directory must not be a symlink")
        imports.mkdir(mode=0o700, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="install-", dir=imports))
        app = stage / source.name
        shutil.copytree(source, app, symlinks=False)
        # Cold iOS 27 installs can finish in installd while the bridge still waits
        # for LaunchServices registration. Keep a finite bound, but allow the same
        # five-minute startup budget as bootstatus; never infer success from files.
        self.run("install", uuid, str(app), timeout=300)
        shutil.rmtree(stage)
        return {"installed": True, "bundle_id": bundle, "uuid": uuid}

    def capture(self, uuid, request_id, filename):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.png", filename):
            raise ValueError("Use a simple PNG filename, without directory components")
        self.store.ensure()
        stage_root = self.store.mount / "Exports"
        if stage_root.is_symlink():
            raise Attention("Image export directory must not be a symlink")
        stage_root.mkdir(mode=0o700, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="capture-", dir=stage_root))
        source = stage / filename
        self.run("io", uuid, "screenshot", str(source))
        with source.open("rb") as data:
            header = data.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            raise Attention("Screenshot producer returned an invalid PNG; preserved")
        destination = self.store.t9 / "Artifacts/simqueue-captures" / request_id
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            not destination.resolve().is_relative_to(self.store.t9)
            or destination.stat().st_dev != self.store.t9.stat().st_dev
        ):
            raise Attention("Capture export must remain on verified T9 storage")
        target = destination / filename
        if target.exists():
            raise ValueError("Capture destination exists; choose another filename")
        temporary = destination / (filename + ".partial")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        shutil.rmtree(stage)
        return {
            "path": str(target),
            "sha256": digest,
            "width": int.from_bytes(header[16:20]),
            "height": int.from_bytes(header[20:24]),
        }


def backend_for(root):
    store = ImageStore.load(root)
    if store is None and (Path(root) / "storage-setup-intent.json").exists():
        raise Attention("Storage setup is incomplete; inspect intent before continuing")
    return ImageBackend(store) if store else AppleBackend()


def initialize(queue, *, apply=False):
    """Explicit provisioning only. Never replace an image or reconfigure live leases."""
    existing = ImageStore.load(queue.root)
    if existing:
        return existing.status()
    t9 = Path.home() / "Developer"
    image = t9 / "SimulatorStorage/simqueue.sparsebundle"
    mount = queue.root / "DeviceVolume"
    if not apply:
        return {
            "dry_run": True,
            "image": str(image),
            "mount": str(mount),
            "maximum_size": "64 GB sparse",
            "apply_required": True,
        }
    with queue.locked():
        if any(d["state"] != "deleted" for d in queue.state["devices"].values()) or any(
            r["state"] in ("queued", "creating", "granted")
            for r in queue.state["requests"].values()
        ):
            raise Attention("Active requests exist; storage reconfiguration refused")
        check = json.loads(
            command(
                [
                    str(Path.home() / ".local/bin/developer-host"),
                    "storage",
                    "--format",
                    "json",
                    "--checker-protocol",
                    "json",
                ]
            )
        )
        if (
            check.get("status") != "ready"
            or check.get("authority", {}).get("verdict") != "verified"
            or not os.path.ismount(t9)
        ):
            raise Attention("T9 readiness could not be verified")
        t9_info = command(["/usr/sbin/diskutil", "info", "-plist", str(t9)], plist=True)
        if image.exists() or image.is_symlink():
            raise Attention("Image path already exists; adoption/overwrite refused")
        if mount.exists() and (mount.is_symlink() or any(mount.iterdir())):
            raise Attention("Mount destination already contains data")
        image.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        intent = {
            "image": str(image),
            "mount": str(mount),
            "t9_root": str(t9),
            "t9_uuid": t9_info["VolumeUUID"],
            "version": 1,
        }
        atomic_json(queue.root / "storage-setup-intent.json", intent)
        command(
            [
                "/usr/sbin/diskutil",
                "image",
                "create",
                "blank",
                "--format",
                "UDSB",
                "--size",
                "64g",
                "--fs",
                "APFS",
                "--volumeName",
                "Simqueue Devices",
                str(image),
            ],
            timeout=120,
        )
        command(
            [
                "/usr/sbin/diskutil",
                "image",
                "attach",
                "--nobrowse",
                "--mountPoint",
                str(mount),
                str(image),
            ],
            timeout=120,
        )
        info = command(["/usr/sbin/diskutil", "info", "-plist", str(mount)], plist=True)
        config = {**intent, "volume_uuid": info["VolumeUUID"]}
        store = ImageStore(queue.root, config)
        store.backing()
        store.verify_mount()
        # Fence already-instantiated clients from silently using the old device set.
        queue.state["backend_key"] = str(mount / "Devices")
        queue.save({"event": "storage-configured"})
        atomic_json(queue.root / "storage.json", config)
        (queue.root / "storage-setup-intent.json").unlink()
        return store.status()


def detach_idle(queue):
    store = getattr(queue.backend, "store", None)
    if store is None:
        return {"detached": True, "mode": "internal"}
    with queue.locked():
        if any(d["state"] != "deleted" for d in queue.state["devices"].values()) or any(
            r["state"] in ("queued", "creating", "granted")
            for r in queue.state["requests"].values()
        ):
            raise Attention("Active or uncertain device ownership prevents image detach")
        if os.path.ismount(store.mount) and queue.backend.devices():
            raise Attention("Image still contains simulator devices; detach refused")
        return store.detach()
