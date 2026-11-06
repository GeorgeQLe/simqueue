import struct
import zlib
from pathlib import Path

import pytest
from test_storage import store as storage_fixture

store = storage_fixture

from simqueue.backend import Attention
from simqueue.png import validate_png
from simqueue.storage import ImageBackend


def png(pixels=b"\x00\x11\x22\x33", width=1, height=1):
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize(
    "data", [png()[:24], png()[:-1], png(b"\x05\x11\x22\x33"), png(b"\x00"), png(width=0)]
)
def test_invalid_png_never_exports(store, data):
    s, _, _, _ = store
    backend = ImageBackend(s)
    backend.run = lambda *args, **kwargs: Path(args[-1]).write_bytes(data)
    with pytest.raises(Attention, match="invalid PNG"):
        backend.capture("owned", "request", "capture.png")
    assert not (s.external_root / "Artifacts/simqueue-captures/request/capture.png").exists()
    assert list((s.mount / "Exports").glob("capture-*/capture.png"))


def test_export_does_not_overwrite_partial_symlink(store):
    s, _, _, _ = store
    destination = s.external_root / "Artifacts/simqueue-captures/request"
    destination.mkdir(parents=True)
    unrelated = s.external_root / "keep.txt"
    unrelated.write_text("preserve")
    (destination / "capture.png.partial").symlink_to(unrelated)
    backend = ImageBackend(s)
    backend.run = lambda *args, **kwargs: Path(args[-1]).write_bytes(png())
    result = backend.capture("owned", "request", "capture.png")
    assert unrelated.read_text() == "preserve"
    assert validate_png(Path(result["path"])) == (1, 1)


def test_capture_refuses_symlink_destination(store):
    s, _, _, _ = store
    destination = s.external_root / "Artifacts/simqueue-captures/request"
    destination.mkdir(parents=True)
    missing = s.external_root / "missing.png"
    (destination / "capture.png").symlink_to(missing)
    backend = ImageBackend(s)
    backend.run = lambda *args, **kwargs: Path(args[-1]).write_bytes(png())
    with pytest.raises(ValueError, match="destination exists"):
        backend.capture("owned", "request", "capture.png")
    assert not missing.exists()
    assert (destination / "capture.png").is_symlink()


def test_png_checksum_checked(tmp_path):
    path = tmp_path / "bad.png"
    data = bytearray(png())
    data[29] ^= 1
    path.write_bytes(data)
    with pytest.raises(ValueError, match="checksum"):
        validate_png(path)


def test_image_recovery_rejects_missing_initialization_marker(store):
    s, _, _, _ = store
    backend = ImageBackend(s)
    device = backend.device_root / "owned"
    seed = device / "data/Library/AddressBook"
    seed.mkdir(parents=True)
    (seed / "sample").write_text("preserve")
    with pytest.raises(Attention, match="initialization is unverified"):
        backend.validate_created("owned", "com.apple.CoreSimulator.SimRuntime.iOS-26-3")
    assert (seed / "sample").read_text() == "preserve"


def test_initialization_io_failure_remains_recoverable(store, monkeypatch):
    from simqueue.backend import AppleBackend

    s, _, _, _ = store
    backend = ImageBackend(s)
    seed = backend.device_root / "owned" / "data/Library/AddressBook"
    seed.mkdir(parents=True)
    (seed / "sample").write_text("preserve")
    monkeypatch.setattr(AppleBackend, "create", lambda *args: "owned")

    def unavailable(*args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(Path, "rename", unavailable)
    with pytest.raises(Attention, match="initialization interrupted"):
        backend.create(
            "agent-simqueue-owned", "phone", "com.apple.CoreSimulator.SimRuntime.iOS-26-3"
        )
    assert (seed / "sample").read_text() == "preserve"
