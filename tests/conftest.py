import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "toolchain_check: keep the live Xcode first-launch preflight enabled"
    )


@pytest.fixture(autouse=True)
def _no_live_toolchain_check(request, monkeypatch):
    """Tests mock simctl; the host's real Xcode state must not leak into them."""
    if request.node.get_closest_marker("toolchain_check") is None:
        monkeypatch.setattr("simqueue.backend.AppleBackend.check_toolchain", lambda self: None)
