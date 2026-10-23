"""The tray's clip editor (tray-widget/clip_browser.py): its local server -
clip listing, Range requests for the video player, path checks, token -
plus how it picks a window and opens the editor (Installer/editor_install.py)."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import clip_browser  # noqa: E402
import clip_edit  # noqa: E402
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


def test_launch_page_and_icon_need_no_token_and_never_leak_it(server):
    port = server.server_address[1]
    assert server.launch_url == f"http://127.0.0.1:{port}/clipem-editor/#tok"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/clipem-editor/", timeout=5) as res:
        page = res.read()
    assert b'location.replace("/" + token + "/")' in page and b"/tok/" not in page and b"#tok" not in page
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/clipem-editor/icon.png", timeout=5) as res:
        assert res.headers["Content-Type"] == "image/png" and res.read(8) == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/clipem-editor/../tok/api/clips", timeout=5)
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/clipem-editor/api/clips", timeout=5)


def test_desktop_entry_gives_the_window_clipem_icon(tmp_path):
    stale = tmp_path / "chrome-127.0.0.1__clipem-editor_-Default.desktop"
    stale.write_text("old browser window entry")
    path = clip_browser.ensure_app_desktop_entry(directory=str(tmp_path))
    assert os.path.basename(path) == "clipem-editor.desktop" and not stale.exists()
    text = open(path, encoding="utf-8").read()
    assert "StartupWMClass=clipem-editor" in text
    assert "NoDisplay=true" in text and text.count("Icon=") == 1 and "ClipEm.png" in text
    mtime = os.path.getmtime(path)
    assert clip_browser.ensure_app_desktop_entry(directory=str(tmp_path)) == path
    assert os.path.getmtime(path) == mtime  # unchanged - not rewritten


def test_window_app_id_matches_the_desktop_entry():
    with open(os.path.join(clip_browser.EDITOR_APP_DIR, "package.json"), encoding="utf-8") as f:
        package = json.load(f)
    assert package["desktopName"] == clip_browser.DESKTOP_ID + ".desktop"
    assert os.path.isfile(os.path.join(clip_browser.EDITOR_APP_DIR, package["main"]))


def test_page_and_clip_list(server):
    status, headers, body = _get(server, "")
    assert status == 200 and headers["Content-Type"].startswith("text/html") and b"editor.js" in body
    status, _headers, body = _get(server, "api/clips")
    data = json.loads(body)
    assert [c["name"] for c in data["clips"]] == ["a.mp4"]
    assert data["clips"][0]["tracks_file"] == "a-tracks.mkv" and data["clips"][0]["duration"] == 12.0


def test_page_and_theme_carry_settings_colors(server, monkeypatch):
    config = {"dark_mode": False, "custom_colors": {"light": {"wave": "#123456"}, "dark": {"wave": "#654321"}}}
    monkeypatch.setattr(clip_browser, "load_run_config", lambda: config)
    _status, _headers, body = _get(server, "")
    assert b'<html lang="en" data-theme="light">' in body
    assert b'<style id="custom-colors">html:root[data-theme] {\n  --wave: #123456;\n}\n</style>' in body
    # Settings saved again: the page's poll sees the change.
    config.update(dark_mode=True)
    theme = json.loads(_get(server, "api/theme")[2])
    assert theme == {"theme": "dark", "css": "html:root[data-theme] {\n  --wave: #654321;\n}\n"}
    config.clear()
    assert json.loads(_get(server, "api/theme")[2]) == {"theme": "dark", "css": ""}


SPLIT_PROBE = """  Duration: 00:00:12.00, start: 0.000000, bitrate: 2000 kb/s
  Stream #0:0: Video: h264 (High), yuv420p(progressive), 1920x1080, 60 fps, 60 tbr, 1k tbn
  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp (default)
      Metadata:
        title           : Mix
  Stream #0:2: Audio: aac (LC), 48000 Hz, stereo, fltp
      Metadata:
        title           : Mic
  Stream #0:3: Audio: aac (LC), 48000 Hz, stereo, fltp
      Metadata:
        title           : Desktop
"""


@pytest.fixture
def editor(server, tmp_path, monkeypatch):
    """The server's editor, probing the fake clips as a 12 s split-tracks clip."""
    editor = clip_browser.Editor(ffmpeg="ffmpeg", edits_dir=str(tmp_path / "edits"))
    probed = []

    def info(source):
        probed.append(os.path.basename(source))
        return clip_browser.clip_edit.parse_probe(SPLIT_PROBE)

    monkeypatch.setattr(editor, "info", info)
    editor._missing = []
    server.editor = editor
    editor.probed = probed
    return editor


def _post(server, path, body):
    return _get(server, path, {"Content-Type": "application/json"}, json.dumps(body).encode())


def test_project_is_loaded_from_the_tracks_copy_and_saved(server, editor):
    status, _h, body = _get(server, "api/project?name=a.mp4")
    data = json.loads(body)
    assert status == 200 and data["source"] == "a-tracks.mkv" and editor.probed == ["a-tracks.mkv"]
    assert data["info"]["split"] and [t["title"] for t in data["info"]["tracks"]] == ["Mix", "Mic", "Desktop"]
    assert data["project"]["keep"] == [[0.0, 12.0]] and data["missing"] == []
    assert data["discord"]["target_mb"] == 20

    project = data["project"]
    project["keep"] = [[1, 4], [6, 20]]
    project["tracks"][1]["regions"] = [{"start": 2, "end": 3, "db": 6}]
    status, _h, body = _post(server, "api/project", {"name": "a.mp4", "project": project})
    assert status == 200 and json.loads(body)["project"]["keep"] == [[1.0, 4.0], [6.0, 12.0]]
    _status, _h, body = _get(server, "api/project?name=a.mp4")
    saved = json.loads(body)["project"]
    assert saved["keep"] == [[1.0, 4.0], [6.0, 12.0]]
    assert saved["tracks"][1]["regions"] == [{"start": 2.0, "end": 3.0, "db": 6.0, "channel": None}]


def test_project_of_a_missing_clip(server, editor):
    assert _get(server, "api/project?name=nope.mp4")[0] == 404
    assert _get(server, "api/project?name=..%2Fa.mp4")[0] == 404
    assert _post(server, "api/project", {"name": "nope.mp4", "project": {}})[0] == 404


def test_export_needs_a_new_enough_ffmpeg(server, editor):
    editor._missing = ["concat", "matroska"]
    status, _h, body = _post(server, "api/export", {"name": "a.mp4", "preset": "full"})
    assert status == 409 and "update ClipEm" in json.loads(body)["error"]
    assert json.loads(_get(server, "api/project?name=a.mp4")[2])["missing"] == ["concat", "matroska"]


def test_export_starts_the_job_with_safe_names(server, editor, monkeypatch):
    started = []
    monkeypatch.setattr(editor.export_job, "start",
                        lambda steps, outputs, total, cleanup=(), on_done=None: started.append((steps, outputs, total)))
    clips = server.folder_fn()
    open(os.path.join(clips, "a-edit.mp4"), "w").close()
    status, _h, _body = _post(server, "api/export", {"name": "a.mp4", "preset": "full"})
    assert status == 200
    steps, outputs, total = started[0]
    assert sorted(os.path.basename(p) for p in outputs.values()) == ["a-edit-2-tracks.mkv", "a-edit-2.mp4"]
    assert all(os.path.basename(p).startswith(".") for p in outputs)  # hidden until done
    assert total == 12.0 and steps[0][0][steps[0][0].index("-i") + 1].endswith("a-tracks.mkv")
    _post(server, "api/export", {"name": "a.mp4", "preset": "discord"})
    assert [os.path.basename(p) for p in started[1][1].values()] == ["a-discord.mp4"]
    status, _h, _body = _post(server, "api/export", {"name": "a.mp4", "preset": "custom",
                                                     "custom": {"height": 2160, "fps": None, "video_kbps": 8000}})
    assert status == 400 and len(started) == 2
    _post(server, "api/export", {"name": "a.mp4", "preset": "discord", "out_name": "a"})
    assert [os.path.basename(p) for p in started[2][1].values()] == ["a-discord.mp4"]  # its own name: suffixed as usual
    _post(server, "api/export", {"name": "a.mp4", "preset": "full", "out_name": "../My: clip.mp4"})
    assert sorted(os.path.basename(p) for p in started[3][1].values()) == ["My clip-tracks.mkv", "My clip.mp4"]
    assert all(os.path.dirname(p) == clips for p in started[3][1].values())


def test_track_routes_check_the_track_number(server, editor):
    assert _get(server, "track/9/a.mp4")[0] == 404
    assert _get(server, "peaks/x/a.mp4")[0] == 404
    assert _get(server, "track/0/missing.mp4")[0] == 404


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
    editor = None
    monkeypatch.setattr(clip_browser.editor_install, "external_editor", lambda: editor)
    monkeypatch.setattr(clip_browser.editor_install, "open_in_editor",
                        lambda editor, path, log=None: opened.append((editor, os.path.basename(path))))
    body = json.dumps({"name": "a.mp4"}).encode()
    status, _h, response = _get(server, "api/open", {"Content-Type": "application/json"}, body)
    assert status == 400 and "Settings" in json.loads(response)["error"]

    editor = "/usr/bin/kdenlive"
    status, _h, response = _get(server, "api/open", {"Content-Type": "application/json"}, body)
    assert status == 200 and json.loads(response)["file"] == "a-tracks.mkv"
    assert opened == [("/usr/bin/kdenlive", "a-tracks.mkv")]


def test_last_export_is_listed_with_its_clip(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    _touch(clips, "a.mp4", b"a", mtime=100)
    _touch(clips, "b.mp4", b"b", mtime=100)
    _touch(clips, "a-edit.mp4", b"e", mtime=200)
    editor = clip_browser.Editor(ffmpeg="ffmpeg", edits_dir=str(tmp_path / "edits"),
                                 exports_path=str(tmp_path / "cache" / "exports.json"))
    editor.record_export(str(clips / "a.mp4"), [str(clips / "a-edit.mp4"), str(clips / "a-edit-tracks.mkv")], now=123)
    editor.record_export(str(clips / "b.mp4"), [str(clips / "b-gone.mp4")], now=5)  # export since deleted
    srv = clip_browser.ClipBrowserServer(lambda: str(clips), token="tok", thumbnailer=FakeThumbnailer(), editor=editor)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        listed = {c["name"]: c["last_export"] for c in json.loads(_get(srv, "api/clips")[2])["clips"]}
    finally:
        srv.shutdown()
        srv.server_close()
    assert listed == {"a.mp4": {"name": "a-edit.mp4", "path": str(clips / "a-edit.mp4"), "time": 123,
                                "in_folder": True},
                      "b.mp4": None, "a-edit.mp4": None}
    assert editor.last_exports(str(tmp_path)) == {}  # another folder's clips


def test_exports_point_back_to_their_clip(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    for name in ("Replay 1.mp4", "Replay 1-edit.mp4", "Replay 1-discord-2.mp4", "Replay 1-1080p60.mp4",
                 "Replay 1-best.mp4", "Funny moment.mp4", "Replay 2.mp4", "Replay 2-edition.mp4", "Lone-edit.mp4"):
        _touch(clips, name, b"x", mtime=100)
    editor = clip_browser.Editor(ffmpeg="ffmpeg", edits_dir=str(tmp_path / "edits"),
                                 exports_path=str(tmp_path / "exports.json"))
    editor.record_export(str(clips / "Replay 2.mp4"), [str(clips / "Funny moment.mp4")], now=1)  # "Export As"
    names = [p.name for p in clips.iterdir()]
    assert editor.export_origins(str(clips), names) == {
        "Replay 1-edit.mp4": "Replay 1.mp4", "Replay 1-discord-2.mp4": "Replay 1.mp4",
        "Replay 1-1080p60.mp4": "Replay 1.mp4", "Funny moment.mp4": "Replay 2.mp4",
    }  # not "-best" or "-edition" (not export names), nor an export whose clip is gone


def test_export_as_goes_where_picked_and_can_be_shown(server, editor, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(editor.export_job, "start", lambda steps, outputs, total, cleanup=(), on_done=None:
                        started.append((outputs, on_done)))
    monkeypatch.setattr(editor, "exports_path", str(tmp_path / "exports.json"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    custom = {"height": None, "fps": None, "video_kbps": 4000}
    status, _h, _b = _post(server, "api/export", {"name": "a.mp4", "preset": "custom", "custom": custom,
                                                  "out_path": str(elsewhere / "My clip")})
    assert status == 200
    outputs, on_done = started[0]
    assert list(outputs.values()) == [str(elsewhere / "My clip.mp4")]
    assert os.path.dirname(next(iter(outputs))) == str(elsewhere)  # the partial is beside it
    for bad in ("relative.mp4", str(tmp_path / "missing" / "x.mp4"), os.path.join(server.folder_fn(), "a.mp4")):
        assert _post(server, "api/export", {"name": "a.mp4", "preset": "custom", "custom": custom,
                                            "out_path": bad})[0] == 400

    (elsewhere / "My clip.mp4").write_bytes(b"x")
    shown = []
    monkeypatch.setattr(clip_browser, "reveal_in_file_manager", shown.append)
    path = str(elsewhere / "My clip.mp4")
    assert _post(server, "api/reveal", {"path": path})[0] == 404  # not one of ours yet
    on_done([path])
    assert _post(server, "api/reveal", {"path": path})[0] == 200 and shown == [path]


def test_export_job_reports_its_files_when_done(tmp_path):
    partial, final = tmp_path / ".x.mp4.partial", tmp_path / "x.mp4"
    partial.write_bytes(b"x")
    done = []
    job = clip_edit.ExportJob()
    job._run([], {str(partial): str(final)}, 1.0, (), on_done=done.append)
    assert job.status["state"] == "done" and done == [[str(final)]]
    assert job.status["sizes"] == [1]  # the page shows the real size


def test_playing_and_revealing_an_export(server, monkeypatch):
    calls = []
    monkeypatch.setattr(clip_browser, "open_with_default_app", lambda path: calls.append(("play", path)))
    monkeypatch.setattr(clip_browser, "reveal_in_file_manager", lambda path: calls.append(("reveal", path)))
    assert _post(server, "api/play", {"name": "a.mp4"})[0] == 200
    assert _post(server, "api/reveal", {"name": "a.mp4"})[0] == 200
    assert [(what, os.path.basename(path)) for what, path in calls] == [("play", "a.mp4"), ("reveal", "a.mp4")]
    assert _post(server, "api/play", {"name": "../a.mp4"})[0] == 404
    assert _post(server, "api/play", {"name": "gone.mp4"})[0] == 404
    assert len(calls) == 2


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

def test_window_command_runs_clipems_own_window_app(monkeypatch):
    monkeypatch.setattr(clip_browser.editor_window, "linux_needs_no_sandbox", lambda root: False)
    cmd = clip_browser.window_command("/app/editor-window/clipem-editor", "http://127.0.0.1:1/x/#t", "Linux")
    assert cmd[:3] == ["/app/editor-window/clipem-editor", clip_browser.EDITOR_APP_DIR,
                       "--clipem-url=http://127.0.0.1:1/x/#t"]
    assert f"--clipem-profile={clip_browser.EDITOR_PROFILE_DIR}" in cmd and "--no-sandbox" not in cmd
    assert any(arg.startswith("--clipem-icon=") and arg.endswith("ClipEm.png") for arg in cmd)
    monkeypatch.setattr(clip_browser.editor_window, "linux_needs_no_sandbox", lambda root: True)
    assert "--no-sandbox" in clip_browser.window_command("/x/clipem-editor", "http://127.0.0.1:1/", "Linux")
    windows = clip_browser.window_command("C:/x/ClipEm Editor.exe", "http://127.0.0.1:1/", "Windows")
    assert windows[-1].endswith("ClipEm.ico") and "--no-sandbox" not in windows


def test_window_runs_as_electron_not_node(monkeypatch):
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    assert "ELECTRON_RUN_AS_NODE" not in clip_browser.window_env()


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


def test_open_in_editor_reports_an_editor_that_dies(tmp_path, monkeypatch):
    monkeypatch.setattr(editor_install, "EDITOR_LOG_PATH", str(tmp_path / "editor.log"))
    exe = _fake_editor(tmp_path, 'echo "cannot open display" >&2\nexit 2\n')
    with pytest.raises(editor_install.EditorError, match="cannot open display"):
        editor_install.open_in_editor(exe, "/clips/a.mkv", system="Linux")



def test_quit_closes_the_window_and_cancels_the_export(server, monkeypatch):
    window = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setitem(clip_browser._window, "proc", window)
    monkeypatch.setattr(clip_browser, "_quitting", threading.Event())
    cancelled = []
    monkeypatch.setattr(server.editor.export_job, "cancel", lambda: cancelled.append(True))
    server.on_quit = lambda: clip_browser.quit_editor(server)
    status, _h, _body = _post(server, "api/quit", {})
    assert status == 200
    assert window.wait(timeout=5) is not None and cancelled == [True]
    assert clip_browser._quitting.is_set()
    assert _get(server, "../api/quit")[0] == 404  # needs the token like every API


def test_delete_moves_the_clip_and_its_copies_to_the_trash(server, editor, monkeypatch):
    clips = server.folder_fn()
    _touch(pathlib.Path(clips), "a-small.mp4", b"small", mtime=102)
    project = editor.project_path(os.path.join(clips, "a.mp4"))
    os.makedirs(os.path.dirname(project), exist_ok=True)
    open(project, "w").close()
    trashed = []
    monkeypatch.setattr(clip_browser, "move_to_trash", lambda path, system=None: trashed.append(path) or os.remove(path))
    status, _h, body = _post(server, "api/delete", {"name": "a.mp4"})
    assert status == 200
    assert sorted(json.loads(body)["files"]) == ["a-small.mp4", "a-tracks.mkv", "a.mp4"]
    assert sorted(os.path.basename(p) for p in trashed) == ["a-small.mp4", "a-tracks.mkv", "a.mp4"]
    assert not os.path.exists(project)
    assert _post(server, "api/delete", {"name": "a.mp4"})[0] == 404
    assert _post(server, "api/delete", {"name": "../a.mp4"})[0] == 404


def test_delete_waits_for_an_export(server, editor, monkeypatch):
    monkeypatch.setattr(editor.export_job, "running", lambda: True)
    assert _post(server, "api/delete", {"name": "a.mp4"})[0] == 409
    assert os.path.exists(os.path.join(server.folder_fn(), "a.mp4"))


def test_linux_trash_follows_the_freedesktop_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for _ in range(2):
        clip = tmp_path / "my clip.mp4"
        clip.write_bytes(b"video")
        clip_browser._trash_linux(str(clip), now=lambda: time.struct_time((2026, 10, 17, 15, 0, 0, 5, 290, 0)))
        assert not clip.exists()
    trash = tmp_path / "data" / "Trash"
    assert sorted(p.name for p in (trash / "files").iterdir()) == ["my clip.2.mp4", "my clip.mp4"]
    info = (trash / "info" / "my clip.mp4.trashinfo").read_text()
    assert info == f"[Trash Info]\nPath={urllib.parse.quote(str(tmp_path / 'my clip.mp4'))}\nDeletionDate=2026-10-17T15:00:00\n"


def test_which_editor_clips_are_edited_in():
    kind, disabled, external = (editor_install.editor_kind, editor_install.editors_disabled,
                                editor_install.external_editor)
    assert kind({}) == "internal" and external({}) is None
    old = {"custom_editor_path": "/usr/bin/kdenlive"}  # set before the choice existed
    assert kind(old) == "external" and external(old) == "/usr/bin/kdenlive"
    chosen_internal = dict(old, clip_editor="internal")
    assert external(chosen_internal) is None  # its path is kept, just not used
    off = dict(old, clip_editor="external", clip_editors_disabled=True)
    assert disabled(off) and external(off) is None
    assert external({"clip_editor": "external", "custom_editor_path": "  "}) is None
