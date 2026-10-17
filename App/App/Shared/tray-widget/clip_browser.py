"""ClipEm's clip editor: every clip in the clips folder, newest first, in a
sidebar that can be hidden - hover one to play it with sound, move across
it to scrub - and the selected clip in the main pane, with trim handles, a
cut tool, per-track audio editing and full-quality/Discord exports (the
media work is clip_edit.py's). A button also opens the clip in the user's
own editor, if one is set (see Installer/editor_install.py).

Launched as a separate process from the tray's "Open Clip Editor" menu item,
like the settings dialog. The page itself (clip_browser/) is served from a
local HTTP server on 127.0.0.1, every URL carrying a random token, and shown
in ClipEm's own window - nothing the user has installed is needed: ClipEm's
pinned Electron (Installer/editor_window.py, installed by Setup or on first
open) runs the small app in editor-window/, which shows this page with
ClipEm's name and icon and plays clips' H.264/AAC itself. The window closing
ends this process.

The window opens at a fixed launch page (LAUNCH_PATH, the token after its
"#", which never reaches the server) that hands over to the token URL. On
Linux the window's app id is "clipem-editor" (editor-window/package.json),
and ensure_app_desktop_entry() writes the matching desktop entry, so the
taskbar shows ClipEm's icon.

Quitting the tray widget closes the editor too, however it was opened
(the tray menu or the Start menu entry): it POSTs api/quit to the URL in
URL_PATH, which cancels any export and closes the window (see quit_editor()).
SIGTERM does the same.
"""

import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.dirname(SCRIPT_DIR)
INSTALLER_DIR = os.path.join(SHARED_DIR, "Installer")
# Both folders explicitly: Windows' embeddable Python (its ._pth file) doesn't
# put the script's own folder on sys.path, so clip_edit wouldn't be found.
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(1, INSTALLER_DIR)
import clip_edit  # noqa: E402
import editor_install  # noqa: E402
import editor_window  # noqa: E402
import obs_config  # noqa: E402
import obs_install  # noqa: E402
from autostart import desktop_exec_arg  # noqa: E402
from clipem_common import (  # noqa: E402
    APP_DIR,
    acquire_instance_lock,
    exit_with_parent,
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
# The running instance's URL, for a second "Open Clip Editor" to reopen.
URL_PATH = os.path.join(SCRIPT_DIR, ".clip_browser.url")

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".flv")
# Copies clip_reexport.py saves next to a clip - listed with it, not as clips.
COPY_SUFFIXES = {"-small": "small_file", "-tracks": "tracks_file"}
THUMB_WIDTH = 320
MAX_THUMBNAIL_JOBS = 2
CHUNK_BYTES = 1024 * 1024

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

    def start_export(self, folder, clip_path, source, preset, custom=None, out_name=None, solo=None):
        """Starts exporting next to the clip. Returns the job's status.
        custom: {"height", "fps", "video_kbps"} for the "custom" preset.
        out_name: the name typed on the page - used as is when it isn't the
        clip's own name; otherwise the export gets a suffix (-edit, -discord...).
        solo: the track indexes soloed on the page - the mix is just those,
        as it's heard there (solo isn't saved with the project)."""
        info = self.info(source)
        project = self.load_project(clip_path, info)
        if isinstance(solo, list):
            project["solo"] = [index for index in solo if isinstance(index, int) and not isinstance(index, bool)]
        stem = os.path.splitext(os.path.basename(clip_path))[0]
        name = clip_edit.clean_name(out_name)
        exact = name if name and name != stem else None
        if preset == "custom":
            custom = custom or {}
            settings = clip_edit.custom_settings(info, custom.get("height"), custom.get("fps"),
                                                 custom.get("video_kbps"))
            label = f"{settings['height']}p{round(settings['fps']) if settings['fps'] else ''}"
            mp4 = clip_edit.unique_path(folder, exact or f"{stem}-{label}", ".mp4")
            partial = os.path.join(folder, f".{os.path.basename(mp4)}.partial")
            os.makedirs(CACHE_DIR, exist_ok=True)
            steps = clip_edit.two_pass_commands(self.ffmpeg, source, project, info, settings, partial, PASSLOG_PATH)
            outputs, cleanup = {partial: mp4}, [PASSLOG_PATH]
        elif preset == "discord":
            mp4 = clip_edit.unique_path(folder, exact or f"{stem}-discord", ".mp4")
            partial = os.path.join(folder, f".{os.path.basename(mp4)}.partial")
            os.makedirs(CACHE_DIR, exist_ok=True)
            steps = clip_edit.discord_commands(self.ffmpeg, source, project, info, partial, PASSLOG_PATH)
            outputs, cleanup = {partial: mp4}, [PASSLOG_PATH]
        else:
            mp4 = clip_edit.unique_path(folder, exact or f"{stem}-edit", ".mp4")
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
        "custom": clip_edit.custom_options(info),
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Deleting: to the Recycle Bin / Trash, never for good
# ---------------------------------------------------------------------------

def _trash_windows(path):
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=os.path.abspath(path) + "\0",
                         fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result or op.fAnyOperationsAborted:
        raise OSError(f"Windows couldn't move it to the Recycle Bin (error {result:#x}) - is it open somewhere?")


def _trash_macos(path, run=subprocess.run):
    script = ["-e", "on run argv", "-e", 'tell application "Finder" to delete (POSIX file (item 1 of argv))',
              "-e", "end run"]
    result = run(["osascript", *script, os.path.abspath(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise OSError(f"Couldn't move it to the Trash: {result.stderr.strip()}")


def _linux_trash_dir(path):
    """The freedesktop.org trash for path: the home trash if it's on the
    same filesystem, else $topdir/.Trash-$uid on path's own."""
    home_trash = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash")
    os.makedirs(home_trash, exist_ok=True)
    if os.stat(home_trash).st_dev == os.stat(path).st_dev:
        return home_trash
    top = os.path.dirname(os.path.abspath(path))
    while os.path.dirname(top) != top and os.stat(os.path.dirname(top)).st_dev == os.stat(path).st_dev:
        top = os.path.dirname(top)
    return os.path.join(top, f".Trash-{os.getuid()}")


def _trash_linux(path, now=None):
    path = os.path.abspath(path)
    trash = _linux_trash_dir(path)
    files, info = os.path.join(trash, "files"), os.path.join(trash, "info")
    os.makedirs(files, mode=0o700, exist_ok=True)
    os.makedirs(info, mode=0o700, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    number = 1
    while True:
        name = os.path.basename(path) if number == 1 else f"{stem}.{number}{ext}"
        info_path = os.path.join(info, name + ".trashinfo")
        try:
            fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            number += 1
    when = (now or time.localtime)()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"[Trash Info]\nPath={urllib.parse.quote(path)}\n"
                f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S', when)}\n")
    try:
        os.rename(path, os.path.join(files, name))
    except OSError:
        os.remove(info_path)
        raise


def move_to_trash(path, system=None):
    system = system or _SYSTEM
    if system == "Windows":
        _trash_windows(path)
    elif system == "Darwin":
        _trash_macos(path)
    else:
        _trash_linux(path)


def clip_files(folder, name):
    """The clip `name` and the copies listed with it (-tracks.mkv, -small)."""
    clip = next((c for c in list_clips(folder) if c["name"] == name), None)
    if not clip:
        return []
    names = [name] + [clip[key] for key in COPY_SUFFIXES.values() if clip[key]]
    return [path for n in names if (path := resolve_clip(folder, n))]


def delete_clip(folder, name, editor, trash=None, attempts=10):
    """Moves the clip and its copies to the Recycle Bin / Trash, and drops
    its saved edit. Retries for a moment: on Windows, the page that was
    just showing it may still have it open. Returns the file names moved."""
    trash = trash or move_to_trash
    paths = clip_files(folder, name)
    if not paths:
        raise FileNotFoundError("That clip isn't there any more.")
    try:
        os.remove(editor.project_path(paths[0]))
    except OSError:
        pass
    moved = []
    for path in paths:
        for attempt in range(attempts):
            try:
                trash(path)
                break
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.2)
        moved.append(os.path.basename(path))
    return moved


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
        self.on_quit = lambda: None  # api/quit - main() closes the window

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
            elif route == "api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.on_quit, daemon=True).start()
            elif route == "api/export-cancel":
                self.server.editor.export_job.cancel()
                self._json(self.server.editor.export_job.status)
            elif route == "api/delete":
                if self.server.editor.export_job.running():
                    self._json({"error": "An export is running - wait for it to finish first."}, 409)
                    return
                try:
                    moved = delete_clip(folder, body.get("name"), self.server.editor)
                except FileNotFoundError as exc:
                    self._json({"error": str(exc)}, 404)
                    return
                log.info(f"Moved to the trash: {', '.join(moved)}")
                self._json({"files": moved})
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
        try:
            status = editor.start_export(folder, clip, source, body.get("preset"), body.get("custom"),
                                         body.get("out_name"), body.get("solo"))
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        self._json(status)

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

EDITOR_APP_DIR = os.path.join(SCRIPT_DIR, "editor-window")
EDITOR_PROFILE_DIR = os.path.join(CACHE_DIR, "editor-window-profile")
# The window's app id on Linux - package.json's desktopName in EDITOR_APP_DIR.
DESKTOP_ID = "clipem-editor"


def applications_dir():
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data_home, "applications")


def app_desktop_entry():
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=ClipEm Editor",
        f"Exec={desktop_exec_arg(sys.executable)} {desktop_exec_arg(os.path.abspath(__file__))}",
        f"Icon={os.path.join(SCRIPT_DIR, 'ClipEm.png')}",
        f"StartupWMClass={DESKTOP_ID}",
        "NoDisplay=true",
        "Terminal=false",
    ]) + "\n"


def ensure_app_desktop_entry(directory=None):
    """Linux taskbars take a window's icon from the desktop entry named after
    its app id. Hidden from menus (NoDisplay), rewritten if this install has
    moved, and the entries older versions wrote for browser --app windows
    (chrome-127.0.0.1__clipem-editor_-Default.desktop...) are removed."""
    directory = directory or applications_dir()
    path = os.path.join(directory, DESKTOP_ID + ".desktop")
    try:
        for name in os.listdir(directory):
            if name.endswith(f"-127.0.0.1__{LAUNCH_PATH}_-Default.desktop"):
                os.remove(os.path.join(directory, name))
    except OSError:
        pass
    content = app_desktop_entry()
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


def window_command(exe, url, system=None):
    """ClipEm's Electron running its editor-window app at url."""
    system = system or _SYSTEM
    icon = LAUNCH_FILES["icon.ico" if system == "Windows" else "icon.png"]
    cmd = [exe, EDITOR_APP_DIR, f"--clipem-url={url}", f"--clipem-profile={EDITOR_PROFILE_DIR}",
           f"--clipem-icon={icon}"]
    if system == "Linux" and editor_window.linux_needs_no_sandbox(os.path.dirname(exe)):
        cmd.append("--no-sandbox")
    return cmd


def window_env():
    env = dict(os.environ)
    # Set when started from inside another Electron app (e.g. a VS Code
    # terminal) - it would run Electron as plain Node, with no window.
    env.pop("ELECTRON_RUN_AS_NODE", None)
    return env


def ensure_window_runtime():
    """The editor window's executable, downloading it first (a pinned,
    signed-manifest-verified Electron - see Installer/editor_window.py) if
    Setup didn't install it. None if that fails."""
    if editor_window.installed():
        return editor_window.exe_path()
    log.info("The editor window isn't installed - downloading it now.")
    try:
        entry = editor_window.install(obs_install.fetch_verified, log=log.info)
        obs_install.record_installed_sha256(editor_window.SHA256_COMPONENT, entry)
        obs_install.record_local_checksums([editor_window.runtime_dir()], "editor window", log=log.info)
    except Exception as exc:
        log.error(f"Couldn't install the editor window: {exc}")
        return None
    return editor_window.exe_path()


# The window this process is showing, so quit_editor() can close it.
_window = {"proc": None}
_quitting = threading.Event()


def _start_window(exe, url, wait):
    if _SYSTEM == "Linux":
        ensure_app_desktop_entry()
    os.makedirs(EDITOR_PROFILE_DIR, exist_ok=True)
    kwargs = {"creationflags": 0x08000000} if _SYSTEM == "Windows" else {}
    cmd = window_command(exe, url)
    if wait:
        # The window serving this process's page closes if this process dies
        # without closing it (killed, or a lost terminal's SIGHUP).
        cmd.append(f"--clipem-parent-pid={os.getpid()}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=window_env(), **kwargs)
    if wait:
        _window["proc"] = proc
        if _quitting.is_set():  # quit while it was starting
            proc.terminate()
        proc.wait()


def quit_editor(server):
    """Cancels any export and closes the window - main() then returns."""
    if _quitting.is_set():
        return
    _quitting.set()
    log.info("Closing the clip editor (ClipEm is quitting).")
    server.editor.export_job.cancel()
    proc = _window["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def open_window(url):
    """Shows url in ClipEm's own editor window until it's closed. Returns
    False if the window can't be shown (its runtime couldn't be installed)."""
    exe = ensure_window_runtime()
    if not exe:
        return False
    log.info("Opening the clip editor window.")
    _start_window(exe, url, wait=True)
    return True


def reopen_existing():
    """Another clip editor is running: bring its window to the front (the
    window's own single-instance lock hands this launch over to it)."""
    try:
        with open(URL_PATH, "r", encoding="utf-8") as f:
            url = f.read().strip()
    except OSError:
        return
    if editor_window.installed():
        _start_window(editor_window.exe_path(), url, wait=False)


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
    server.on_quit = lambda: quit_editor(server)
    if _SYSTEM != "Windows":
        # Terminated (the tray widget's fallback, or exit_with_parent):
        # close the window and stop the export rather than orphaning them.
        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, lambda _sig, _frame: threading.Thread(
                target=quit_editor, args=(server,), daemon=True).start())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    write_text_atomic(URL_PATH, server.launch_url)
    log.info(f"Clip editor serving {obs_config.load_clip_folder()}")
    try:
        opened = open_window(server.launch_url)
    finally:
        server.editor.export_job.cancel()  # its ffmpeg would outlive us
        server.shutdown()
        try:
            with open(URL_PATH, "r", encoding="utf-8") as f:
                ours = f.read().strip() == server.launch_url
            if ours:  # not a newer editor's, after this one was slow to close
                os.remove(URL_PATH)
        except OSError:
            pass
        release_instance_lock(LOCK_PATH)
    if not opened:
        sys.exit(1)  # the tray widget tells the user


if __name__ == "__main__":
    main()
