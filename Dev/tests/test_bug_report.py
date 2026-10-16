"""bug_report.create_bug_report(): which logs go in, and the websocket
password never does."""

import os
import zipfile

import bug_report
import pytest

PASSWORD = "s3cret-ws-password"


@pytest.fixture
def install(tmp_path, monkeypatch):
    log_dir, obs_logs, crashes = (tmp_path / n for n in ("logs", "obs-logs", "crashes"))
    for folder in (log_dir, obs_logs, crashes):
        folder.mkdir()
    (log_dir / "run.log").write_text(f"connecting with password {PASSWORD}\n")
    (log_dir / "settings.log").write_text("saved\n")
    for i in range(bug_report.OBS_LOG_COUNT + 3):
        path = obs_logs / f"obs-{i:02}.txt"
        path.write_text(f"obs log {i}\n")
        os.utime(path, (1000 + i, 1000 + i))
    (crashes / "crash.txt").write_text("boom\n")
    monkeypatch.setattr(bug_report, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(bug_report.obs_config, "OBS_LOG_DIR", str(obs_logs))
    monkeypatch.setattr(bug_report, "OBS_CRASH_DIR", str(crashes))
    monkeypatch.setattr(bug_report, "system_info", lambda: f"ClipEm version: test\npassword={PASSWORD}\n")
    monkeypatch.setattr(
        bug_report.obs_config, "load_obs_websocket_settings", lambda: {"port": 4455, "password": PASSWORD},
    )
    return tmp_path


def test_report_has_logs_and_no_password(install):
    dest = str(install / "report.zip")

    count = bug_report.create_bug_report(dest)

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        contents = {name: zf.read(name).decode() for name in names}
    assert {"system-info.txt", "clipem-logs/run.log", "clipem-logs/settings.log", "obs-crashes/crash.txt"} <= names
    obs_logs = sorted(name for name in names if name.startswith("obs-logs/"))
    # Only the newest OBS logs.
    assert len(obs_logs) == bug_report.OBS_LOG_COUNT
    assert "obs-logs/obs-00.txt" not in names
    assert count == len(names) - 1
    assert all(PASSWORD not in text for text in contents.values())
    assert bug_report.REDACTED in contents["clipem-logs/run.log"]


def test_failed_report_leaves_no_partial_zip(install, monkeypatch):
    dest = str(install / "report.zip")

    def broken():
        raise RuntimeError("nope")

    monkeypatch.setattr(bug_report, "system_info", broken)
    with pytest.raises(RuntimeError):
        bug_report.create_bug_report(dest)
    assert not os.path.exists(dest)
