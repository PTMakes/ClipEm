"""The tray's Clip Browser (tray-widget/clip_browser.py): its local server -
clip listing, Range requests for the video player, path checks, token -
plus how it picks a window and opens editors (Installer/editor_install.py)."""

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import clip_browser  # noqa: E402
import editor_install  # noqa: E402


def _touch(folder, name, data=b"x", mtime=None):
    path = folder / name
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# Listing and paths
# ---------------------------------------------------------------------------

def test_clips_newest_first_with_their_copies_attached(tmp_path):
    _touch(tmp_path, "2026-10-01 10-00-00.mp4", mtime=100)
    _touch(tmp_path, "2026-10-01 10-00-00-tracks.mkv", mtime=101)
    _touch(tmp_path, "2026-10-01 10-00-00-small.mp4", mtime=102)
    _touch(tmp_path, "2026-10-02 10-00-00.mp4", mtime=200)
    _touch(tmp_path, "2026-10-02 10-00-00-small.mp4.partial", mtime=201)  # still being written
    _touch(tmp_path, "orphan-small.mp4", mtime=50)  # its clip was deleted
    _touch(tmp_path, ".hidden.mp4", mtime=300)
    _touch(tmp_path, "notes.txt", mtime=300)
    (tmp_path / "folder.mp4").mkdir()

    clips = clip_browser.list_clips(str(tmp_path))
    assert [c["name"] for c in clips] == ["2026-10-02 10-00-00.mp4", "2026-10-01 10-00-00.mp4", "orphan-small.mp4"]
    assert clips[1]["tracks_file"] == "2026-10-01 10-00-00-tracks.mkv"
    assert clips[1]["small_file"] == "2026-10-01 10-00-00-small.mp4"
    assert clips[0]["tracks_file"] is None and clips[0]["small_file"] is None


def test_missing_folder_lists_nothing(tmp_path):
    assert clip_browser.list_clips(str(tmp_path / "nope")) == []


def test_resolve_clip_refuses_anything_outside_the_folder(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    _touch(clips, "a.mp4")
    _touch(tmp_path, "secret.mp4")
    os.symlink(tmp_path / "secret.mp4", clips / "link.mp4")
    assert clip_browser.resolve_clip(str(clips), "a.mp4") == os.path.realpath(clips / "a.mp4")
    for bad in ("../secret.mp4", str(tmp_path / "secret.mp4"), "link.mp4", "sub/a.mp4", "..\\secret.mp4",
                ".", "", None, "missing.mp4"):
        assert clip_browser.resolve_clip(str(clips), bad) is None, bad


def test_the_editor_gets_the_tracks_copy_when_there_is_one(tmp_path):
    _touch(tmp_path, "a.mp4")
    _touch(tmp_path, "a-tracks.mkv")
    _touch(tmp_path, "b.mp4")
    assert clip_browser.file_to_edit(str(tmp_path), "a.mp4").endswith("a-tracks.mkv")
    assert clip_browser.file_to_edit(str(tmp_path), "b.mp4").endswith("b.mp4")
    assert clip_browser.file_to_edit(str(tmp_path), "../b.mp4") is None


@pytest.mark.parametrize("header, expected", [
    (None, None),
    ("", None),
    ("bytes=0-", (0, 999)),
    ("bytes=100-199", (100, 199)),
    ("bytes=900-5000", (900, 999)),
    ("bytes=-100", (900, 999)),
    ("bytes=-5000", (0, 999)),
    ("bytes=10-20, 30-40", (10, 20)),
    ("items=0-1", None),
])
def test_parse_range(header, expected):
    assert clip_browser.parse_range(header, 1000) == expected


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=500-100", "bytes=-0"])
def test_unsatisfiable_ranges(header):
    with pytest.raises(clip_browser.RangeNotSatisfiable):
        clip_browser.parse_range(header, 1000)


def test_duration_from_ffmpeg_output():
    assert clip_browser.parse_duration("  Duration: 00:01:02.50, start: 0.000") == 62.5
    assert clip_browser.parse_duration("garbage") is None


def test_thumbnail_cache_key_changes_with_the_file(tmp_path):
    path = str(tmp_path / "a.mp4")
    assert clip_browser.thumb_key(path, 1.0) != clip_browser.thumb_key(path, 2.0)
    assert clip_browser.thumb_key(path, 1.0) == clip_browser.thumb_key(path, 1.0)


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class FakeThumbnailer:
    def info(self, path):
        return {"duration": 12.0}

    def thumbnail(self, path):
        return None


@pytest.fixture
def server(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    _touch(clips, "a.mp4", bytes(range(256)) * 4, mtime=100)
    _touch(clips, "a-tracks.mkv", b"mkv", mtime=101)
    srv = clip_browser.ClipBrowserServer(lambda: str(clips), token="tok", thumbnailer=FakeThumbnailer())
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(server, path, headers=None, data=None):
    request = urllib.request.Request(server.url + path, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(request, timeout=5) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_every_request_needs_the_token(server):
    port = server.server_address[1]
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/clips", timeout=5)
    assert err.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/wrong/api/clips", timeout=5)


def test_page_and_clip_list(server):
    status, headers, body = _get(server, "")
    assert status == 200 and headers["Content-Type"].startswith("text/html") and b"app.js" in body
    status, _headers, body = _get(server, "api/clips")
    data = json.loads(body)
    assert [c["name"] for c in data["clips"]] == ["a.mp4"]
    assert data["clips"][0]["tracks_file"] == "a-tracks.mkv" and data["clips"][0]["duration"] == 12.0


def test_media_supports_ranges(server):
    status, headers, body = _get(server, "media/a.mp4")
    assert status == 200 and len(body) == 1024 and headers["Accept-Ranges"] == "bytes"
    status, headers, body = _get(server, "media/a.mp4", {"Range": "bytes=10-19"})
    assert status == 206 and body == bytes(range(10, 20))
    assert headers["Content-Range"] == "bytes 10-19/1024"
    status, headers, _body = _get(server, "media/a.mp4", {"Range": "bytes=5000-"})
    assert status == 416 and headers["Content-Range"] == "bytes */1024"


def test_media_refuses_paths_outside_the_folder(server):
    assert _get(server, "media/..%2F..%2Fetc%2Fpasswd")[0] == 404
    assert _get(server, "media/missing.mp4")[0] == 404
    assert _get(server, "thumb/a.mp4")[0] == 404  # no ffmpeg in this fake


def test_opening_an_editor(server, monkeypatch):
    opened = []
    monkeypatch.setattr(clip_browser.editor_install, "find_losslesscut", lambda: "/opt/LosslessCut/losslesscut")
    monkeypatch.setattr(clip_browser.editor_install, "custom_editor_path", lambda: None)
    monkeypatch.setattr(clip_browser.editor_install, "open_in_editor",
                        lambda editor, path, log=None: opened.append((editor, os.path.basename(path))))
    body = json.dumps({"name": "a.mp4", "editor": "losslesscut"}).encode()
    status, _h, response = _get(server, "api/open", {"Content-Type": "application/json"}, body)
    assert status == 200 and json.loads(response)["file"] == "a-tracks.mkv"
    assert opened == [("/opt/LosslessCut/losslesscut", "a-tracks.mkv")]

    body = json.dumps({"name": "a.mp4", "editor": "custom"}).encode()
    status, _h, response = _get(server, "api/open", {"Content-Type": "application/json"}, body)
    assert status == 400 and "Settings" in json.loads(response)["error"]


def test_install_job_reports_progress_and_errors():
    done = threading.Event()

    def fake_install(log):
        log("Downloading LosslessCut...")
        done.wait(5)
        raise RuntimeError("network down")

    job = clip_browser.InstallJob(install=fake_install)
    job.start()
    job.start()  # a second click while it runs does nothing
    done.set()
    for _ in range(100):
        if job.status["state"] != "running":
            break
        threading.Event().wait(0.02)
    assert job.status["state"] == "error" and "network down" in job.status["message"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_real_thumbnail_and_duration(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=red:s=640x360:d=2",
                    "-c:v", "libx264", str(clip)], check=True)
    thumbs = clip_browser.Thumbnailer(ffmpeg=shutil.which("ffmpeg"), cache_dir=str(tmp_path / "thumbs"))
    assert thumbs.info(str(clip)) == {}
    jpg = thumbs.thumbnail(str(clip))
    assert jpg and open(jpg, "rb").read(2) == b"\xff\xd8"
    assert abs(thumbs.info(str(clip))["duration"] - 2.0) < 0.1
    assert thumbs.thumbnail(str(clip)) == jpg  # cached


# ---------------------------------------------------------------------------
# Window and editors
# ---------------------------------------------------------------------------

def test_app_browser_on_linux_prefers_chromium_family():
    on_path = {"brave-browser": "/usr/bin/brave-browser"}
    assert clip_browser.find_app_browser("Linux", which=on_path.get) == "/usr/bin/brave-browser"
    assert clip_browser.find_app_browser("Linux", which=lambda name: None) is None


def test_app_browser_command_has_own_profile_and_allows_hover_sound():
    cmd = clip_browser.app_browser_command("/usr/bin/chromium", "http://127.0.0.1:1/t/", "/tmp/profile")
    assert cmd[:3] == ["/usr/bin/chromium", "--app=http://127.0.0.1:1/t/", "--user-data-dir=/tmp/profile"]
    assert "--autoplay-policy=no-user-gesture-required" in cmd


def test_editor_label_and_command():
    assert editor_install.editor_label("C:\\Program Files\\Resolve\\Resolve.exe") == "Resolve"
    assert editor_install.editor_label("/Applications/DaVinci Resolve.app") == "DaVinci Resolve"
    assert editor_install.editor_label("/opt/kdenlive.AppImage") == "kdenlive"
    assert editor_install.editor_command("/Applications/Kdenlive.app", "/c/a.mkv", "Darwin") == \
        ["open", "-a", "/Applications/Kdenlive.app", "/c/a.mkv"]
    assert editor_install.editor_command("/usr/bin/kdenlive", "/c/a.mkv", "Linux") == ["/usr/bin/kdenlive", "/c/a.mkv"]


def _fake_editor(tmp_path, script):
    exe = tmp_path / "editor"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    return str(exe)


def test_open_in_editor_retries_without_sandbox_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(editor_install, "EDITOR_LOG_PATH", str(tmp_path / "editor.log"))
    marker = tmp_path / "args"
    exe = _fake_editor(tmp_path, f'''if [ "$1" = "--no-sandbox" ]; then echo "$@" > "{marker}"; exit 0; fi
echo "The SUID sandbox helper binary was found, but is not configured correctly" >&2
exit 1
''')
    logs = []
    editor_install.open_in_editor(exe, "/clips/a.mkv", log=logs.append, system="Linux")
    for _ in range(100):
        if marker.exists():
            break
        threading.Event().wait(0.02)
    assert marker.read_text().strip() == "--no-sandbox /clips/a.mkv"
    assert "--no-sandbox" in logs[0]


def test_open_in_editor_reports_an_editor_that_dies(tmp_path, monkeypatch):
    monkeypatch.setattr(editor_install, "EDITOR_LOG_PATH", str(tmp_path / "editor.log"))
    exe = _fake_editor(tmp_path, 'echo "cannot open display" >&2\nexit 2\n')
    with pytest.raises(editor_install.EditorError, match="cannot open display"):
        editor_install.open_in_editor(exe, "/clips/a.mkv", system="Linux")


def test_find_losslesscut_inside_its_wrapper_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(editor_install, "APP_DIR", str(tmp_path))
    assert editor_install.find_losslesscut("Linux") is None
    exe = tmp_path / "Linux" / "LosslessCut" / "LosslessCut-linux-x64" / "losslesscut"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert editor_install.find_losslesscut("Linux") == str(exe)


def test_install_losslesscut_unpacks_into_place(tmp_path, monkeypatch):
    import tarfile

    import obs_install

    monkeypatch.setattr(editor_install, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(obs_install, "INSTALLED_VERSIONS_PATH", str(tmp_path / "installed_versions.json"))
    source = tmp_path / "src" / "LosslessCut-linux-x64"
    source.mkdir(parents=True)
    (source / "losslesscut").write_text("#!/bin/sh\n")

    def fake_fetch(platform_key, artifact, dest, log=print):
        assert (platform_key, artifact) == ("linux", "losslesscut")
        with tarfile.open(dest, "w:bz2") as tf:
            tf.add(source, arcname="LosslessCut-linux-x64")
        return {"url": "https://github.com/mifi/lossless-cut/releases/download/v3.69.0/LosslessCut-linux-x64.tar.bz2"}

    monkeypatch.setattr(obs_install, "fetch_verified", fake_fetch)
    exe = editor_install.install_losslesscut(log=lambda m: None, system="Linux")
    assert exe == str(tmp_path / "Linux" / "LosslessCut" / "LosslessCut-linux-x64" / "losslesscut")
    assert os.access(exe, os.X_OK)
    assert sorted(os.listdir(tmp_path / "Linux")) == ["LosslessCut"]  # no staging folder or archive left
    assert json.loads((tmp_path / "installed_versions.json").read_text()) == {"losslesscut": "3.69.0"}
