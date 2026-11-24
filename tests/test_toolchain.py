import plistlib

import pytest

from simqueue.backend import AppleBackend, Attention, first_launch_pending, wrapper_developer_dir


def launcher(tmp_path, expected):
    path = tmp_path / "Xcode.app/Contents/Developer/usr/bin/simctl"
    path.parent.mkdir(parents=True)
    path.write_text(f'#!/bin/bash\nEXPECTED_VERSION="{expected}"\nexec simctl "$@"\n')
    return path


def framework(tmp_path, version):
    path = tmp_path / "Info.plist"
    with open(path, "wb") as handle:
        plistlib.dump({"CFBundleVersion": version} if version else {}, handle)
    return path


@pytest.mark.parametrize(
    ("installed", "expected", "pending"),
    [
        ("1174.9.2", "1174.21", ("1174.9.2", "1174.21")),
        ("1174.21", "1174.21", None),
        ("1174.21.0", "1174.21", None),
        ("1175", "1174.21", None),
        (None, "1174.21", ("missing", "1174.21")),
    ],
)
def test_first_launch_pending_compares_versions_numerically(tmp_path, installed, expected, pending):
    assert (
        first_launch_pending(launcher(tmp_path, expected), framework(tmp_path, installed))
        == pending
    )


def test_first_launch_check_ignores_launchers_without_an_expected_version(tmp_path):
    plain = tmp_path / "simctl"
    plain.write_text('#!/bin/sh\nexec simctl "$@"\n')
    assert first_launch_pending(plain, framework(tmp_path, "1")) is None
    assert first_launch_pending(tmp_path / "absent", framework(tmp_path, "1")) is None


def test_wrapper_developer_dir(tmp_path):
    wrapper = tmp_path / "apple-tools"
    wrapper.write_text(
        '#!/bin/sh\nexport DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer\nexec "$@"\n'
    )
    assert str(wrapper_developer_dir(wrapper)) == "/Applications/Xcode.app/Contents/Developer"
    assert wrapper_developer_dir(tmp_path / "absent") is None


@pytest.mark.toolchain_check
def test_run_refuses_simctl_while_first_launch_is_pending(tmp_path, monkeypatch):
    backend = AppleBackend()
    backend.simctl_launcher = launcher(tmp_path, "1174.21")
    backend.coresimulator_info = framework(tmp_path, "1174.9.2")
    started = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: started.append(a))
    with pytest.raises(Attention, match=r"sudo .*/usr/bin/xcodebuild -runFirstLaunch"):
        backend.run("list", "--json")
    assert started == []
