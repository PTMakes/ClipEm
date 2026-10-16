"""ClipEm's clip editor: every clip in the clips folder, newest first, in a
sidebar that can be hidden - hover one to play it with sound, move across
it to scrub - and the selected clip in the main pane, with trim handles, a
cut tool, per-track audio editing and full-quality/Discord exports (the
media work is clip_edit.py's). A button also opens the clip in the user's
own editor, if one is set (see Installer/editor_install.py).

Launched as a separate process from the tray's "Edit Clips..." menu item,
like the settings dialog. The page itself (clip_browser/) is served from a
local HTTP server on 127.0.0.1, every URL carrying a random token, and shown
in an app-style window:

The window opens at a fixed launch page (LAUNCH_PATH, the token after its
"#", which never reaches the server) that hands over to the token URL: a
Chromium --app window's app_id comes from the URL it opened with, and a
fixed one lets the desktop entry written by ensure_app_desktop_entry() give
it ClipEm's icon in the taskbar on Linux. The page's favicon (the logo)
covers Windows and X11.

- pywebview, if it happens to be installed;
- otherwise a Chromium-family browser in --app mode (Edge ships with every
  Windows 10/11; Chromium/Chrome/Brave/Vivaldi on Linux and macOS) with its
  own profile, so the window closing ends this process, and with autoplay
  allowed, so hover sound works without clicking first;
- otherwise the default browser, served until the tray widget exits.
"""

import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.dirname(SCRIPT_DIR)
INSTALLER_DIR = os.path.join(SHARED_DIR, "Installer")
sys.path.insert(0, INSTALLER_DIR)
import clip_edit  # noqa: E402
import editor_install  # noqa: E402
import obs_config  # noqa: E402
from autostart import desktop_exec_arg  # noqa: E402
from clipem_common import (  # noqa: E402
    APP_DIR,
    acquire_instance_lock,
    exit_with_parent,
    pid_alive,
    release_instance_lock,
    setup_logging,
    write_text_atomic,
)

log = logging.getLogger("clip_browser")

UI_DIR = os.path.join(SCRIPT_DIR, "clip_browser")
UI_FILES = {"": "index.html", "index.html": "index.html", "browser.js": "browser.js", "editor.js": "editor.js",
            "app.css": "app.css"}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".flv": "video/x-flv",
    ".jpg": "image/jpeg", ".m4a": "audio/mp4", ".png": "image/png", ".ico": "image/x-icon",
}
CACHE_DIR = os.path.join(APP_DIR, "cache")
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
EDITS_DIR = os.path.join(CACHE_DIR, "edits")
TRACK_AUDIO_DIR = os.path.join(CACHE_DIR, "track-audio")
PEAKS_DIR = os.path.join(CACHE_DIR, "peaks")
PASSLOG_PATH = os.path.join(CACHE_DIR, "x264-pass")
BROWSER_PROFILE_DIR = os.path.join(CACHE_DIR, "clip-browser-profile")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".clip_browser.lock")
LAUNCH_PATH = "clipem-editor"
# Public (no token): the launch page and the logo it and the editor use.
LAUNCH_FILES = {"icon.png": os.path.join(SCRIPT_DIR, "ClipEm.png"),
                "icon.ico": os.path.join(SCRIPT_DIR, "ClipEm.ico")}
LAUNCH_PAGE = b"""<!doctype html>
<meta charset="utf-8">
<title>ClipEm Editor</title>
<link rel="icon" href="icon.png">
<script>
  const token = location.hash.slice(1);
  if (/^[A-Za-z0-9_-]+$/.test(token)) location.replace("/" + token + "/");
</script>
"""
# Which app_id prefix each Chromium-family browser gives --app windows.
APP_ID_PREFIXES = {
    "chromium": "chrome", "chromium-browser": "chrome", "google-chrome": "chrome", "google-chrome-stable": "chrome",
    "brave": "brave", "brave-browser": "brave", "microsoft-edge": "msedge", "microsoft-edge-stable": "msedge",
    "vivaldi": "vivaldi", "vivaldi-stable": "vivaldi",
}
# The running instance's URL, for a second "Edit Clips..." to reopen.
URL_PATH = os.path.join(SCRIPT_DIR, ".clip_browser.url")

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".flv")
# Copies clip_reexport.py saves next to a clip - listed with it, not as clips.
COPY_SUFFIXES = {"-small": "small_file", "-tracks": "tracks_file"}
THUMB_WIDTH = 320
MAX_THUMBNAIL_JOBS = 2
CHUNK_BYTES = 1024 * 1024
PARENT_CHECK_SECONDS = 5

_SYSTEM = platform.system()


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

def list_clips(folder):
    """[{name, mtime, size, tracks_file, small_file}], newest first. A
    "-small"/"-tracks" copy is listed under its clip; one whose clip is gone
    is listed as a clip itself."""
    try:
        entries = [e for e in os.scandir(folder) if e.is_file()]
    except OSError:
        return []
    videos = {}
    for entry in entries:
        stem, ext = os.path.splitext(entry.name)
        if entry.name.startswith(".") or ext.lower() not in VIDEO_EXTENSIONS:
            continue  # includes the ".partial" files of copies still being written
        videos[entry.name] = (stem, entry.stat())

    stems = {stem: name for name, (stem, _stat) in videos.items()}
    clips = {}
    copies = []
    for name, (stem, stat) in videos.items():
        suffix = next((s for s in COPY_SUFFIXES if stem.endswith(s) and stem[: -len(s)] in stems), None)
        if suffix:
            copies.append((stems[stem[: -len(suffix)]], COPY_SUFFIXES[suffix], name))
        else:
            clips[name] = {"name": name, "mtime": stat.st_mtime, "size": stat.st_size,
                           "tracks_file": None, "small_file": None}
    for parent, key, name in copies:
        if parent in clips:
            clips[parent][key] = name
    return sorted(clips.values(), key=lambda c: (c["mtime"], c["name"]), reverse=True)


def resolve_clip(folder, name):
    """The full path of the file `name` in folder, or None if it isn't a
    plain file name of a file inside it (no paths, no symlinks out)."""
    if not name or name != os.path.basename(name) or "/" in name or "\\" in name or name.startswith("."):
        return None
    root = os.path.realpath(folder)
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root or not os.path.isfile(path):
        return None
    return path


class RangeNotSatisfiable(Exception):
    pass


def parse_range(header, size):
    """(start, end) inclusive for an HTTP Range header, or None to send the
    whole file. Only the first range of a multi-range request is honoured."""
    if not header:
        return None
    match = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*(?:,.*)?", header)
    if not match or (not match.group(1) and not match.group(2)):
        return None
    first, last = match.groups()
    if not first:  # suffix: the last N bytes
        length = int(last)
        if length == 0:
            raise RangeNotSatisfiable()
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise RangeNotSatisfiable()
    return start, end


# ---------------------------------------------------------------------------
# Thumbnails (and durations, which ffmpeg prints while making them)
# ---------------------------------------------------------------------------

def find_ffmpeg():
    exe = "ffmpeg.exe" if _SYSTEM == "Windows" else "ffmpeg"
    platform_dir = {"Windows": "Windows", "Linux": "Linux"}.get(_SYSTEM)
    if platform_dir:
        bundled = os.path.join(APP_DIR, platform_dir, "ffmpeg", "bin", exe)
        if os.path.isfile(bundled):
            return bundled
    return shutil.which("ffmpeg")


def thumb_key(path, mtime):
    return hashlib.sha1(f"{os.path.abspath(path)}|{mtime}".encode()).hexdigest()


def parse_duration(ffmpeg_stderr):
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", ffmpeg_stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


class Thumbnailer:
    """Makes each clip's thumbnail once (cached under THUMB_DIR by path and
    mtime) with ffmpeg, at most MAX_THUMBNAIL_JOBS at a time."""

    def __init__(self, ffmpeg=None, cache_dir=THUMB_DIR, run=subprocess.run):
        self._ffmpeg = ffmpeg if ffmpeg is not None else find_ffmpeg()
        self._cache_dir = cache_dir
        self._run = run
        self._slots = threading.Semaphore(MAX_THUMBNAIL_JOBS)

    def paths(self, clip_path):
        key = thumb_key(clip_path, os.path.getmtime(clip_path))
        return os.path.join(self._cache_dir, key + ".jpg"), os.path.join(self._cache_dir, key + ".json")

    def info(self, clip_path):
        """{"duration": seconds or None} once the thumbnail exists, else {}."""
        _jpg, meta = self.paths(clip_path)
        try:
            with open(meta, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def thumbnail(self, clip_path):
        """The thumbnail's path, making it if needed; None if ffmpeg can't."""
        jpg, meta = self.paths(clip_path)
        if os.path.isfile(jpg):
            return jpg
        if not self._ffmpeg:
            return None
        with self._slots:
            if os.path.isfile(jpg):
                return jpg
            os.makedirs(self._cache_dir, exist_ok=True)
            partial = jpg + ".partial.jpg"
            duration = None
            for seek in ("1", "0"):  # a clip under a second long has no frame at 1s
                result = self._run(
                    [self._ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", seek, "-i", clip_path,
                     "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-2", partial],
                    capture_output=True, text=True, **_no_window_kwargs(),
                )
                duration = duration or parse_duration(result.stderr)
                if result.returncode == 0 and os.path.isfile(partial) and os.path.getsize(partial):
                    os.replace(partial, jpg)
                    write_text_atomic(meta, json.dumps({"duration": duration}))
                    return jpg
            if os.path.exists(partial):
                os.remove(partial)
            log.info(f"No thumbnail for {clip_path}: {result.stderr.strip()[-300:]}")
            return None


def _no_window_kwargs():
    return {"creationflags": 0x08000000} if _SYSTEM == "Windows" else {}


def editors_info():
    custom = editor_install.custom_editor_path()
    return {
        "custom": {"label": editor_install.editor_label(custom)} if custom else None,
    }


def file_to_edit(folder, clip):
    """The "-tracks.mkv" copy when there is one - editors get every audio
    track - else the clip itself."""
    info = next((c for c in list_clips(folder) if c["name"] == clip), None)
    return resolve_clip(folder, (info and info["tracks_file"]) or clip)


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

class Editor:
    """The editor's per-clip state: what each source file holds (probed once
    per file version), saved projects, and the one export that can run."""

    def __init__(self, ffmpeg=None, edits_dir=EDITS_DIR, track_dir=TRACK_AUDIO_DIR, peaks_dir=PEAKS_DIR):
        self.ffmpeg = ffmpeg if ffmpeg is not None else find_ffmpeg()
        self.edits_dir, self.track_dir, self.peaks_dir = edits_dir, track_dir, peaks_dir
        self.export_job = clip_edit.ExportJob(log=log.warning)
        self._info = {}
        self._missing = None
        self._lock = threading.Lock()

    def missing_features(self):
        if not self.ffmpeg:
            return ["ffmpeg"]
        if self._missing is None:
            self._missing = clip_edit.missing_features(self.ffmpeg, run=subprocess.run)
        return self._missing

    def info(self, source):
        key = (source, os.path.getmtime(source))
        with self._lock:
            cached = self._info.get(key)
        if cached is None:
            cached = clip_edit.probe(self.ffmpeg, source)
            with self._lock:
                self._info[key] = cached
        return cached

    def _key(self, path):
        return thumb_key(path, os.path.getmtime(path))

    def project_path(self, clip_path):
        return os.path.join(self.edits_dir, self._key(clip_path) + ".json")

    def load_project(self, clip_path, info):
        try:
            with open(self.project_path(clip_path), "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = None
        return clip_edit.normalize_project(saved, info) if saved else clip_edit.default_project(info)

    def save_project(self, clip_path, info, project):
        project = clip_edit.normalize_project(project, info)
        os.makedirs(self.edits_dir, exist_ok=True)
        write_text_atomic(self.project_path(clip_path), json.dumps(project))
        return project

    def track_audio(self, source, index):
        info = self.info(source)
        path = os.path.join(self.track_dir, f"{self._key(source)}-{index}.m4a")
        return clip_edit.track_audio(self.ffmpeg, source, info, index, path)

    def peaks(self, source, index):
        path = os.path.join(self.peaks_dir, f"{self._key(source)}-{index}.json")
        return clip_edit.track_peaks(self.ffmpeg, source, index, path)

    def start_export(self, folder, clip_path, source, preset):
        """Starts exporting next to the clip. Returns the job's status."""
        info = self.info(source)
        project = self.load_project(clip_path, info)
        stem = os.path.splitext(os.path.basename(clip_path))[0]
        if preset == "discord":
            mp4 = clip_edit.unique_path(folder, f"{stem}-discord", ".mp4")
            partial = os.path.join(folder, f".{os.path.basename(mp4)}.partial")
            os.makedirs(CACHE_DIR, exist_ok=True)
            steps = clip_edit.discord_commands(self.ffmpeg, source, project, info, partial, PASSLOG_PATH)
            outputs, cleanup = {partial: mp4}, [PASSLOG_PATH]
        else:
            mp4 = clip_edit.unique_path(folder, f"{stem}-edit", ".mp4")
            mp4_stem = os.path.splitext(os.path.basename(mp4))[0]
            mkv = os.path.join(folder, f"{mp4_stem}-tracks.mkv")
            mp4_partial = os.path.join(folder, f".{os.path.basename(mp4)}.partial")
            mkv_partial = os.path.join(folder, f".{os.path.basename(mkv)}.partial")
            steps = clip_edit.full_quality_commands(self.ffmpeg, source, project, info, mkv_partial, mp4_partial)
            outputs = {mp4_partial: mp4}
            if len(info["audio"]) > 1:
                outputs[mkv_partial] = mkv
            cleanup = []
        log.info(f"Exporting {os.path.basename(clip_path)} ({preset}) to {', '.join(outputs.values())}")
        self.export_job.start(steps, outputs, clip_edit.kept_duration(project), cleanup=cleanup)
        return self.export_job.status


def project_payload(clip, source, info, project, missing):
    return {
        "clip": clip,
        "source": os.path.basename(source),
        "info": {
            "duration": info["duration"], "width": info["width"], "height": info["height"], "fps": info["fps"],
            "split": clip_edit.is_split_layout(info),
            "tracks": [{"index": t["index"], "title": clip_edit.track_title(info, t["index"])} for t in info["audio"]],
        },
        "project": project,
        "discord": clip_edit.discord_settings(info, clip_edit.kept_duration(project)),
        "missing": missing,
    }


def reveal_in_file_manager(path):
    if _SYSTEM == "Windows":
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif _SYSTEM == "Darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class ClipBrowserServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, folder_fn, token=None, thumbnailer=None, editor=None, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.folder_fn = folder_fn
        self.token = token or secrets.token_urlsafe(24)
        self.thumbnailer = thumbnailer or Thumbnailer()
        self.editor = editor or Editor()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/{self.token}/"

    @property
    def launch_url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/{LAUNCH_PATH}/#{self.token}"


class Handler(BaseHTTPRequestHandler):
    server: ClipBrowserServer

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    # -- helpers --------------------------------------------------------------

    def _route(self):
        """The path after the token, or None (and a 404 sent) without it."""
        path = urllib.parse.urlparse(self.path).path
        prefix = f"/{self.server.token}/"
        if not path.startswith(prefix):
            self._send(404, b"Not found", "text/plain")
            return None
        return urllib.parse.unquote(path[len(prefix):])

    def _send(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _query(self, key):
        values = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get(key)
        return values[0] if values else None

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(min(length, 1024 * 1024)) or b"{}")
        except ValueError:
            return {}

    def _clip_and_source(self, folder, name):
        """(clip path, file to edit) - the "-tracks.mkv" copy when there is
        one - or (None, None), with a 404 sent, if the clip is gone."""
        clip = resolve_clip(folder, name)
        source = file_to_edit(folder, name) if clip else None
        if not source:
            self._json({"error": "That clip isn't there any more."}, 404)
            return None, None
        return clip, source

    def _track_route(self, folder, route):
        """For "<kind>/<n>/<clip name>": (source, n), or None with a 404 sent."""
        _kind, _sep, rest = route.partition("/")
        number, _sep, name = rest.partition("/")
        source = file_to_edit(folder, name) if number.isdigit() and resolve_clip(folder, name) else None
        if not source or int(number) >= len(self.server.editor.info(source)["audio"]):
            self._send(404, b"Not found", "text/plain")
            return None
        return source, int(number)

    def _send_file(self, path, cache=False):
        size = os.path.getsize(path)
        content_type = CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        try:
            byte_range = parse_range(self.headers.get("Range"), size)
        except RangeNotSatisfiable:
            self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            return
        start, end = byte_range or (0, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if byte_range else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the player seeked elsewhere and dropped this request

    # -- routes ---------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith(f"/{LAUNCH_PATH}/"):
            name = path[len(LAUNCH_PATH) + 2:]
            if name == "":
                self._send(200, LAUNCH_PAGE, "text/html; charset=utf-8")
            elif name in LAUNCH_FILES:
                self._send_file(LAUNCH_FILES[name], cache=True)
            else:
                self._send(404, b"Not found", "text/plain")
            return
        route = self._route()
        if route is None:
            return
        folder = self.server.folder_fn()
        if route in UI_FILES:
            self._send_file(os.path.join(UI_DIR, UI_FILES[route]))
        elif route == "api/clips":
            clips = list_clips(folder)
            for clip in clips:
                path = os.path.join(folder, clip["name"])
                clip["duration"] = self.server.thumbnailer.info(path).get("duration")
            self._json({"folder": folder, "clips": clips})
        elif route == "api/editors":
            self._json(editors_info())
        elif route == "api/project":
            self._get_project(folder)
        elif route == "api/export-status":
            self._json(self.server.editor.export_job.status)
        elif route.startswith(("track/", "peaks/")):
            try:
                found = self._track_route(folder, route)
                if found and route.startswith("track/"):
                    self._send_file(self.server.editor.track_audio(*found), cache=True)
                elif found:
                    self._json(self.server.editor.peaks(*found))
            except Exception as exc:
                log.exception(f"{route} failed")
                self._json({"error": str(exc)}, 500)
        elif route.startswith("media/"):
            path = resolve_clip(folder, route[len("media/"):])
            if path:
                self._send_file(path)
            else:
                self._send(404, b"Not found", "text/plain")
        elif route.startswith("thumb/"):
            path = resolve_clip(folder, route[len("thumb/"):])
            thumb = self.server.thumbnailer.thumbnail(path) if path else None
            if thumb:
                self._send_file(thumb, cache=True)
            else:
                self._send(404, b"No thumbnail", "text/plain")
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        route = self._route()
        if route is None:
            return
        folder = self.server.folder_fn()
        body = self._read_json()
        try:
            if route == "api/open":
                self._open_editor(folder, body)
            elif route == "api/project":
                clip, source = self._clip_and_source(folder, body.get("name"))
                if clip:
                    editor = self.server.editor
                    info = editor.info(source)
                    project = editor.save_project(clip, info, body.get("project"))
                    self._json({"project": project,
                                "discord": clip_edit.discord_settings(info, clip_edit.kept_duration(project))})
            elif route == "api/export":
                self._export(folder, body)
            elif route == "api/export-cancel":
                self.server.editor.export_job.cancel()
                self._json(self.server.editor.export_job.status)
            elif route == "api/reveal":
                path = resolve_clip(folder, body.get("name"))
                if not path:
                    self._json({"error": "That clip isn't there any more."}, 404)
                    return
                reveal_in_file_manager(path)
                self._json({"ok": True})
            else:
                self._send(404, b"Not found", "text/plain")
        except Exception as exc:
            log.exception(f"{route} failed")
            self._json({"error": str(exc)}, 500)

    def _get_project(self, folder):
        clip, source = self._clip_and_source(folder, self._query("name"))
        if not clip:
            return
        editor = self.server.editor
        try:
            info = editor.info(source)
        except Exception as exc:
            self._json({"error": f"Can't edit this clip: {exc}"}, 422)
            return
        project = editor.load_project(clip, info)
        self._json(project_payload(os.path.basename(clip), source, info, project, editor.missing_features()))

    def _export(self, folder, body):
        clip, source = self._clip_and_source(folder, body.get("name"))
        if not clip:
            return
        editor = self.server.editor
        missing = editor.missing_features()
        if missing:
            self._json({"error": "ClipEm's ffmpeg is too old to export edits - update ClipEm "
                                 f"(missing: {', '.join(missing)})."}, 409)
            return
        if editor.export_job.running():
            self._json({"error": "An export is already running."}, 409)
            return
        self._json(editor.start_export(folder, clip, source, body.get("preset")))

    def _open_editor(self, folder, body):
        path = file_to_edit(folder, body.get("name"))
        if not path:
            self._json({"error": "That clip isn't there any more."}, 404)
            return
        editor = editor_install.custom_editor_path()
        if not editor:
            self._json({"error": "No editor is set - choose one in ClipEm's Settings."}, 400)
            return
        log.info(f"Opening {path} in {editor}")
        editor_install.open_in_editor(editor, path, log=log.info)
        self._json({"ok": True, "file": os.path.basename(path)})


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

def _windows_browser_paths():
    roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    relative = (
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
    )
    return [os.path.join(root, rel) for rel in relative for root in roots if root]


MACOS_BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
LINUX_BROWSER_NAMES = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave", "brave-browser",
    "microsoft-edge", "microsoft-edge-stable", "vivaldi", "vivaldi-stable",
)


def find_app_browser(system=None, which=shutil.which, isfile=os.path.isfile):
    """A Chromium-family browser that can show the page as an app window."""
    system = system or _SYSTEM
    if system == "Windows":
        return next((p for p in _windows_browser_paths() if isfile(p)), None)
    if system == "Darwin":
        return next((p for p in MACOS_BROWSER_PATHS if isfile(p)), None)
    return next((found for name in LINUX_BROWSER_NAMES if (found := which(name))), None)


def app_browser_command(browser, url, profile_dir=BROWSER_PROFILE_DIR):
    return [
        browser, f"--app={url}", f"--user-data-dir={profile_dir}",
        # Hover-to-play has sound from the first hover, no click needed.
        "--autoplay-policy=no-user-gesture-required",
        "--no-first-run", "--no-default-browser-check", "--window-size=1440,900",
    ]


def app_id(browser):
    """The Wayland app_id (X11: WM_CLASS) of the --app window `browser`
    opens at the launch page, or None for a browser not in APP_ID_PREFIXES."""
    name = os.path.basename(browser).lower().removesuffix(".exe")
    prefix = APP_ID_PREFIXES.get(name)
    return f"{prefix}-127.0.0.1__{LAUNCH_PATH}_-Default" if prefix else None


def applications_dir():
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data_home, "applications")


def app_desktop_entry(window_id):
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=ClipEm Editor",
        f"Exec={desktop_exec_arg(sys.executable)} {desktop_exec_arg(os.path.abspath(__file__))}",
        f"Icon={os.path.join(SCRIPT_DIR, 'ClipEm.png')}",
        f"StartupWMClass={window_id}",
        "NoDisplay=true",
        "Terminal=false",
    ]) + "\n"


def ensure_app_desktop_entry(browser, directory=None):
    """Linux taskbars pick a window's icon from the desktop entry named after
    its app_id - without one, the editor shows the browser's icon. Hidden
    from menus (NoDisplay), and rewritten if this install has moved."""
    window_id = app_id(browser)
    if not window_id:
        return None
    directory = directory or applications_dir()
    path = os.path.join(directory, window_id + ".desktop")
    content = app_desktop_entry(window_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == content:
                return path
    except OSError:
        pass
    try:
        os.makedirs(directory, exist_ok=True)
        write_text_atomic(path, content)
    except OSError as exc:
        log.warning(f"Couldn't write {path} for the editor's taskbar icon: {exc}")
        return None
    return path


def _open_app_browser(browser, url, wait):
    if _SYSTEM == "Linux":
        ensure_app_desktop_entry(browser)
    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    cmd = app_browser_command(browser, url)
    if wait:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_window(url):
    """Shows url. Returns True once the window has closed, False if it went
    to the default browser (no way to know when that tab closes)."""
    try:
        import webview
    except ImportError:
        webview = None
    if webview is not None:
        try:
            webview.create_window("ClipEm Editor", url, width=1440, height=900)
            try:
                webview.start(icon=LAUNCH_FILES["icon.png"])
            except TypeError:  # pywebview without icon support
                webview.start()
            return True
        except Exception as exc:
            log.warning(f"pywebview couldn't open a window ({exc}) - trying a browser.")
    browser = find_app_browser()
    if browser:
        log.info(f"Opening the clip editor in {browser} (app mode).")
        _open_app_browser(browser, url, wait=True)
        return True
    log.info("No Chromium-family browser found - opening the clip editor in the default browser.")
    webbrowser.open(url)
    return False


def reopen_existing():
    """Another clip editor is running: show its page again."""
    try:
        with open(URL_PATH, "r", encoding="utf-8") as f:
            url = f.read().strip()
    except OSError:
        return
    browser = find_app_browser()
    if browser:
        # Same profile: Chromium hands this to the running window's process.
        _open_app_browser(browser, url, wait=False)
    else:
        webbrowser.open(url)


def parent_pid_from_args(argv):
    """The tray widget passes --parent-pid <its pid> when it opens this."""
    if "--parent-pid" in argv:
        index = argv.index("--parent-pid")
        if index + 1 < len(argv) and argv[index + 1].isdigit():
            return int(argv[index + 1])
    return None


def main():
    setup_logging("clip_browser")
    parent_pid = parent_pid_from_args(sys.argv[1:])
    exit_with_parent(parent_pid)
    if not acquire_instance_lock(LOCK_PATH):
        log.info("Clip editor already open; showing it again.")
        reopen_existing()
        return
    server = ClipBrowserServer(obs_config.load_clip_folder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    write_text_atomic(URL_PATH, server.launch_url)
    log.info(f"Clip editor serving {obs_config.load_clip_folder()}")
    try:
        if not open_window(server.launch_url):
            while parent_pid is None or pid_alive(parent_pid):
                time.sleep(PARENT_CHECK_SECONDS)
    finally:
        server.shutdown()
        try:
            os.remove(URL_PATH)
        except OSError:
            pass
        release_instance_lock(LOCK_PATH)


if __name__ == "__main__":
    main()
