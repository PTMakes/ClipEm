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


def test_log_files_lists_every_log_by_group(install):
    groups = dict(bug_report.log_files())
    assert [os.path.basename(p) for p in groups["ClipEm logs"]] == ["run.log", "settings.log"]
    assert len(groups["OBS logs"]) == bug_report.OBS_LOG_COUNT + 3  # every one, unlike a report
    assert os.path.basename(groups["OBS logs"][0]) == f"obs-{bug_report.OBS_LOG_COUNT + 2:02}.txt"  # newest first
    assert [os.path.basename(p) for p in groups["OBS crash reports"]] == ["crash.txt"]


def test_log_files_leaves_out_empty_groups(install):
    os.remove(dict(bug_report.log_files())["OBS crash reports"][0])
    assert "OBS crash reports" not in dict(bug_report.log_files())


def test_read_log_masks_the_websocket_password(install):
    path = dict(bug_report.log_files())["ClipEm logs"][0]
    text = bug_report.read_log(path)
    assert PASSWORD not in text and bug_report.REDACTED in text


def test_read_log_shows_the_end_of_a_big_log(tmp_path):
    path = tmp_path / "big.log"
    path.write_text("old\n" * 1000 + "newest line\n")
    text = bug_report.read_log(str(path), max_bytes=100)
    assert text.endswith("newest line\n") and text.startswith("[Showing the last") and "old\nold" in text


def test_read_log_says_when_it_cant(tmp_path):
    assert "Couldn't read" in bug_report.read_log(str(tmp_path / "gone.log"))


def test_log_viewer_reads_each_log_on_its_own(install):
    import tkinter as tk

    import log_viewer

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    dialog = log_viewer.open_log_viewer(root, lambda value: value, lambda path: None)
    assert log_viewer.open_log_viewer(root, lambda value: value, lambda path: None) is dialog  # only one

    def find(widget, kind):
        for child in widget.winfo_children():
            if isinstance(child, kind):
                return child
            found = find(child, kind)
            if found:
                return found
        return None

    files, text = find(dialog, tk.Listbox), find(dialog, tk.Text)
    names = list(files.get(0, "end"))
    assert names[0] == "ClipEm logs" and "  run.log" in names and "OBS logs" in names
    assert PASSWORD not in text.get("1.0", "end")  # run.log, opened first

    files.selection_clear(0, "end")
    files.selection_set(names.index("  settings.log"))
    files.event_generate("<<ListboxSelect>>")
    root.update()
    assert text.get("1.0", "end").strip() == "saved"

    files.selection_clear(0, "end")
    files.selection_set(names.index("OBS logs"))  # a heading opens nothing
    files.event_generate("<<ListboxSelect>>")
    root.update()
    assert text.get("1.0", "end").strip() == "saved"
    root.destroy()
