"""bug_report.create_bug_report(): which logs go in, and the websocket
password never does. send_bug_report(): what it uploads, and its errors."""

import io
import os
import urllib.error
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


def _upload_parts(body, content_type):
    """{field name: bytes} of a multipart body."""
    boundary = content_type.split("boundary=")[1].encode()
    parts = {}
    for chunk in body.split(b"--" + boundary)[1:-1]:
        headers, value = chunk.strip(b"\r\n").split(b"\r\n\r\n", 1)
        name = headers.split(b'name="')[1].split(b'"')[0].decode()
        parts[name] = value
    return parts


@pytest.fixture
def temp_zips(install, monkeypatch):
    """The temp zip paths send_bug_report() makes."""
    paths = []
    create = bug_report.create_bug_report

    def recording_create(dest):
        paths.append(dest)
        return create(dest)

    monkeypatch.setattr(bug_report, "create_bug_report", recording_create)
    return paths


def test_send_uploads_description_and_redacted_zip(temp_zips, monkeypatch):
    sent = {}

    def fake_post(url, body, content_type):
        sent.update(url=url, parts=_upload_parts(body, content_type))
        return {"id": "20261017-abcd1234", "issue_url": "https://github.com/PTMakes/ClipEm/issues/1"}

    monkeypatch.setattr(bug_report, "_post", fake_post)

    result = bug_report.send_bug_report("  Clip saved with no audio\n")

    assert result == ("20261017-abcd1234", "https://github.com/PTMakes/ClipEm/issues/1")
    assert sent["url"] == bug_report.BUG_REPORT_URL
    assert sent["parts"]["description"] == b"Clip saved with no audio"
    assert sent["parts"]["version"] and sent["parts"]["platform"]
    with zipfile.ZipFile(io.BytesIO(sent["parts"]["logs"])) as zf:
        assert "clipem-logs/run.log" in zf.namelist()
        assert all(PASSWORD.encode() not in zf.read(name) for name in zf.namelist())
    assert temp_zips and not any(os.path.exists(path) for path in temp_zips)


def test_send_needs_a_description(temp_zips):
    with pytest.raises(bug_report.BugReportError):
        bug_report.send_bug_report("   ")
    assert temp_zips == []


def test_send_refuses_oversized_zip(temp_zips, monkeypatch):
    monkeypatch.setattr(bug_report, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(bug_report, "_post", lambda *args: pytest.fail("uploaded an oversized zip"))

    with pytest.raises(bug_report.BugReportError, match="too large"):
        bug_report.send_bug_report("broken")
    assert not any(os.path.exists(path) for path in temp_zips)


@pytest.mark.parametrize("url", ["http://example.com/report", "ftp://bugs.clipem.app/report"])
def test_send_refuses_insecure_urls(temp_zips, url):
    with pytest.raises(bug_report.BugReportError, match="non-HTTPS"):
        bug_report.send_bug_report("broken", url=url)
    assert not any(os.path.exists(path) for path in temp_zips)


def test_local_http_relay_is_allowed():
    assert bug_report._check_url("http://127.0.0.1:8787/report")


@pytest.mark.parametrize(("code", "body", "message"), [
    (429, b"{}", "try again in a minute"),
    (413, b"{}", "too large"),
    (502, b'{"id": "20261017-abcd1234"}', "20261017-abcd1234"),
    (500, b"not json", "500"),
])
def test_send_http_errors_are_readable(temp_zips, monkeypatch, code, body, message):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "error", {}, io.BytesIO(body))

    monkeypatch.setattr(bug_report.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(bug_report.BugReportError, match=message):
        bug_report.send_bug_report("broken")
    assert not any(os.path.exists(path) for path in temp_zips)


def test_send_network_failure_is_readable(temp_zips, monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(bug_report.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(bug_report.BugReportError, match="Couldn't reach"):
        bug_report.send_bug_report("broken")
