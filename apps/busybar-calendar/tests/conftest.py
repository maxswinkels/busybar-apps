import pytest

from busybar_calendar import service


@pytest.fixture(autouse=True)
def no_real_service_files(tmp_path_factory, monkeypatch):
    """Keep tests off this machine's own launchd agent or systemd unit."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(service, "_home", lambda: home)
