import base64
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_queue import acquire
from test_queue import setup as queue_setup

from simqueue.backend import Attention
from simqueue.storage import ImageBackend, ImageStore, backend_for, detach_idle, initialize

setup = queue_setup


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    t9 = tmp_path / "t9"
    t9.mkdir()
    image = t9 / "Devices.sparsebundle"
    image.mkdir()
    mount = root / "DeviceVolume"
    mount.mkdir()
    config = {
        "version": 1,
        "image": str(image),
        "mount": str(mount),
        "t9_root": str(t9),
        "t9_uuid": "t9-uuid",
        "volume_uuid": "image-uuid",
    }
    s = ImageStore(root, config)
    mounted = {str(t9), str(mount)}
    monkeypatch.setattr("simqueue.storage.os.path.ismount", lambda p: str(p) in mounted)
    attachment = {
        "image-path": str(image),
        "system-entities": [{"dev-entry": "/dev/disk99", "content-hint": "GUID_partition_scheme"}],
    }

    def run(args, **kwargs):
        if args[:2] == ["/usr/bin/hdiutil", "info"]:
            return {"images": [attachment] if str(mount) in mounted else []}
        if args[:2] == ["/usr/sbin/diskutil", "info"]:
            return (
                {"VolumeUUID": "t9-uuid"}
                if args[-1] == str(t9)
                else {"VolumeUUID": "image-uuid", "MountPoint": str(mount)}
            )
        if args[:3] == ["/usr/sbin/diskutil", "image", "attach"]:
            mounted.add(str(mount))
            return b""
        if args[:2] == ["/usr/sbin/diskutil", "eject"]:
            mounted.discard(str(mount))
            return b""
        if "developer-host" in args[0]:
            return json.dumps({"status": "ready", "authority": {"verdict": "verified"}}).encode()
        raise AssertionError(args)

    command = Mock(side_effect=run)
    monkeypatch.setattr("simqueue.storage.command", command)
    return s, mounted, attachment, command


def test_existing_mount_and_backing_identity(store):
    s, _, _, command = store
    s.ensure()
    assert not any("attach" in c.args[0] for c in command.call_args_list)


def test_missing_t9_never_mounts_or_falls_back(store):
    s, mounted, _, command = store
    mounted.remove(str(s.t9))
    with pytest.raises(Attention, match="T9 is not mounted"):
        s.ensure()
    command.assert_not_called()


def test_wrong_volume_or_attachment_refused(store):
    s, _, attachment, _ = store
    s.config["volume_uuid"] = "wrong-image"
    with pytest.raises(Attention, match="identity changed"):
        s.ensure()
    s.config["volume_uuid"] = "image-uuid"
    attachment["image-path"] = "/another/image"
    with pytest.raises(Attention, match="configured image"):
        s.ensure()


def test_mount_and_detach_only_own_image(store):
    s, mounted, _, command = store
    mounted.remove(str(s.mount))
    s.ensure()
    assert str(s.mount) in mounted
    assert s.detach()["detached"]
    calls = [c.args[0] for c in command.call_args_list]
    assert ["/usr/sbin/diskutil", "eject", "/dev/disk99"] in calls
    assert not any("force" in value for args in calls for value in args)


def test_nonempty_unmounted_directory_is_preserved(store):
    s, mounted, _, command = store
    mounted.remove(str(s.mount))
    (s.mount / "keep.txt").write_text("data")
    with pytest.raises(Attention, match="not empty"):
        s.ensure()
    assert not any("attach" in c.args[0] for c in command.call_args_list)


def test_bad_config_never_selects_internal_backend(tmp_path):
    (tmp_path / "storage.json").write_text("{broken")
    with pytest.raises(Attention, match="fallback refused"):
        backend_for(tmp_path)


def test_live_lease_prevents_detach_and_reconfiguration(setup):
    q, b, _, _ = setup
    a = acquire(q)
    b.store = Mock()
    with pytest.raises(Attention, match="ownership prevents"):
        detach_idle(q)
    with pytest.raises(Attention, match="Active requests"):
        initialize(q, apply=True)
    b.store.detach.assert_not_called()
    q.release(a["lease_token"])


def test_capture_stages_on_image_then_exports_to_t9(store):
    s, _, _, _ = store
    b = ImageBackend(s)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aEnEAAAAASUVORK5CYII="
    )

    def screenshot(*args, **kwargs):
        output = Path(args[-1])
        assert output.is_relative_to(s.mount)
        output.write_bytes(png)

    b.run = screenshot
    result = b.capture("owned-uuid", "owned-request", "test.png")
    assert Path(result["path"]).read_bytes() == png
    assert Path(result["path"]).is_relative_to(s.t9)
    assert (result["width"], result["height"]) == (1, 1)
    with pytest.raises(ValueError):
        b.capture("owned-uuid", "owned-request", "../outside.png")


def test_backend_namespace_change_is_fenced(setup):
    q, b, _, _ = setup
    a = acquire(q)
    b.device_root = Path("/another/device-set")
    with pytest.raises(Attention, match="Device set changed"):
        q.release(a["lease_token"])
    assert not b.deleted


def test_install_stages_on_image_and_removes_only_own_copy(store, monkeypatch):
    import plistlib

    s, _, _, _ = store
    app = s.t9 / "Example.app"
    app.mkdir()
    (app / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleSupportedPlatforms": ["iPhoneSimulator"],
                "CFBundleIdentifier": "com.example.test",
            }
        )
    )
    backend = ImageBackend(s)
    calls = []

    def install(*args, **kwargs):
        assert Path(args[2]).is_relative_to(s.mount / "Imports")
        assert (Path(args[2]) / "Info.plist").exists()
        calls.append(args)

    monkeypatch.setattr(backend, "run", install)
    assert backend.install("test-uuid", str(app))["bundle_id"] == "com.example.test"
    assert calls and not list((s.mount / "Imports").iterdir())
    assert app.exists()


def test_interrupted_setup_cannot_fall_back_internally(tmp_path):
    (tmp_path / "storage-setup-intent.json").write_text("{}")
    with pytest.raises(Attention, match="setup is incomplete"):
        backend_for(tmp_path)


@pytest.mark.parametrize(
    "runtime",
    [
        "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
        "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
    ],
)
def test_only_new_devices_receive_fresh_contacts_workaround(store, monkeypatch, runtime):
    from simqueue.backend import AppleBackend

    s, _, _, _ = store
    backend = ImageBackend(s)
    existing = backend.device_root / "regular" / "data/Library/AddressBook"
    existing.mkdir(parents=True)
    (existing / "user-data").write_text("preserve")

    def create(self, name, device_type, runtime):
        seed = self.device_root / "new-device" / "data/Library/AddressBook"
        seed.mkdir(parents=True)
        (seed / "sample-db").write_text("sample")
        return "new-device"

    monkeypatch.setattr(AppleBackend, "create", create)
    uid = backend.create("agent-simqueue-new", "phone", runtime)
    new = backend.device_root / uid
    assert not (new / "data/Library/AddressBook").exists()
    assert (new / ".simqueue-original-contacts/sample-db").read_text() == "sample"
    assert (
        json.loads((new / ".simqueue-initialization.json").read_text())["contacts"]
        == "empty-on-first-boot"
    )
    assert (existing / "user-data").read_text() == "preserve"


def test_other_runtimes_keep_their_standard_contacts_seed(store, monkeypatch):
    from simqueue.backend import AppleBackend

    s, _, _, _ = store
    backend = ImageBackend(s)
    seed = backend.device_root / "new-device" / "data/Library/AddressBook"
    seed.mkdir(parents=True)
    (seed / "sample").write_text("standard")
    monkeypatch.setattr(AppleBackend, "create", lambda *args: "new-device")
    backend.create("agent-simqueue-new", "phone", "com.apple.CoreSimulator.SimRuntime.iOS-28-0")
    assert (seed / "sample").read_text() == "standard"
    assert not (backend.device_root / "new-device/.simqueue-initialization.json").exists()


def test_fresh_contacts_symlink_is_preserved_and_refused(store, monkeypatch):
    from simqueue.backend import AppleBackend

    s, _, _, _ = store
    backend = ImageBackend(s)
    outside = s.t9 / "regular-contacts"
    outside.mkdir()
    (outside / "user-data").write_text("preserve")
    seed = backend.device_root / "new-device/data/Library/AddressBook"
    seed.parent.mkdir(parents=True)
    seed.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(AppleBackend, "create", lambda *args: "new-device")
    with pytest.raises(Attention, match="unverifiable"):
        backend.create("agent-simqueue-new", "phone", "com.apple.CoreSimulator.SimRuntime.iOS-26-5")
    assert seed.is_symlink() and (outside / "user-data").read_text() == "preserve"


def test_install_timeout_preserves_stage_without_claiming_success(store, monkeypatch):
    import plistlib

    s, _, _, _ = store
    app = s.t9 / "Uncertain.app"
    app.mkdir()
    (app / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleSupportedPlatforms": ["iPhoneSimulator"],
                "CFBundleIdentifier": "com.example.uncertain",
            }
        )
    )
    backend = ImageBackend(s)

    def incomplete(*args, **kwargs):
        raise Attention("Install completion was not acknowledged")

    monkeypatch.setattr(backend, "run", incomplete)
    with pytest.raises(Attention, match="not acknowledged"):
        backend.install("owned-uuid", str(app))
    stages = list((s.mount / "Imports").glob("install-*/Uncertain.app"))
    assert len(stages) == 1
    assert (stages[0] / "Info.plist").read_bytes() == (app / "Info.plist").read_bytes()
    assert app.exists()
