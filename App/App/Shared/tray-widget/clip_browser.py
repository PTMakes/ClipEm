"""ClipEm's Clip Browser: every clip in the clips folder, newest first, in
a sidebar - hover one to play it with sound, move across it to scrub - with
the latest clip open in the main pane and buttons to open it in LosslessCut
or the user's own editor (see Installer/editor_install.py).

Launched as a separate process from the tray's "Clip Browser..." menu item,
like the settings dialog. The page itself (clip_browser/) is served from a
local HTTP server on 127.0.0.1, every URL carrying a random token, and shown
in an app-style window:

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
import editor_install  # noqa: E402
import obs_config  # noqa: E402
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
UI_FILES = {"": "index.html", "index.html": "index.html", "app.js": "app.js", "app.css": "app.css"}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".flv": "video/x-flv",
    ".jpg": "image/jpeg",
}
CACHE_DIR = os.path.join(APP_DIR, "cache")
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
BROWSER_PROFILE_DIR = os.path.join(CACHE_DIR, "clip-browser-profile")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".clip_browser.lock")
# The running instance's URL, for a second "Clip Browser..." to reopen.
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


# ---------------------------------------------------------------------------
# LosslessCut download, in the background (the page polls its status)
# ---------------------------------------------------------------------------

class InstallJob:
    def __init__(self, install=editor_install.install_losslesscut):
        self._install = install
        self._lock = threading.Lock()
        self.status = {"state": "idle", "message": ""}

    def start(self):
        with self._lock:
            if self.status["state"] == "running":
                return
            self.status = {"state": "running", "message": "Starting download..."}
        threading.Thread(target=self._run, daemon=True).start()

    def _progress(self, message):
        log.info(message)
        self.status = {"state": "running", "message": str(message).strip()}

    def _run(self):
        try:
            self._install(log=self._progress)
            self.status = {"state": "done", "message": "LosslessCut installed."}
        except Exception as exc:
            log.exception("LosslessCut install failed")
            self.status = {"state": "error", "message": f"Couldn't install LosslessCut: {exc}"}


def editors_info():
    custom = editor_install.custom_editor_path()
    return {
        "losslesscut": {
            "supported": editor_install.losslesscut_supported(),
            "installed": bool(editor_install.find_losslesscut()) if editor_install.losslesscut_supported() else False,
            "download_mb": editor_install.LOSSLESSCUT_DOWNLOAD_MB.get(_SYSTEM),
        },
        "custom": {"label": editor_install.editor_label(custom)} if custom else None,
    }


def file_to_edit(folder, clip):
    """The "-tracks.mkv" copy when there is one - editors get every audio
    track - else the clip itself."""
    info = next((c for c in list_clips(folder) if c["name"] == clip), None)
    return resolve_clip(folder, (info and info["tracks_file"]) or clip)


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

    def __init__(self, folder_fn, token=None, thumbnailer=None, install_job=None, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.folder_fn = folder_fn
        self.token = token or secrets.token_urlsafe(24)
        self.thumbnailer = thumbnailer or Thumbnailer()
        self.install_job = install_job or InstallJob()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/{self.token}/"


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

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(min(length, 64 * 1024)) or b"{}")
        except ValueError:
            return {}

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
        elif route == "api/install-status":
            self._json(self.server.install_job.status)
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
            elif route == "api/reveal":
                path = resolve_clip(folder, body.get("name"))
                if not path:
                    self._json({"error": "That clip isn't there any more."}, 404)
                    return
                reveal_in_file_manager(path)
                self._json({"ok": True})
            elif route == "api/install-losslesscut":
                self.server.install_job.start()
                self._json(self.server.install_job.status)
            else:
                self._send(404, b"Not found", "text/plain")
        except Exception as exc:
            log.exception(f"{route} failed")
            self._json({"error": str(exc)}, 500)

    def _open_editor(self, folder, body):
        path = file_to_edit(folder, body.get("name"))
        if not path:
            self._json({"error": "That clip isn't there any more."}, 404)
            return
        if body.get("editor") == "custom":
            editor = editor_install.custom_editor_path()
            if not editor:
                self._json({"error": "No editor is set - choose one in ClipEm's Settings."}, 400)
                return
        else:
            editor = editor_install.find_losslesscut()
            if not editor:
                self._json({"error": "LosslessCut isn't installed yet."}, 400)
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
        "--no-first-run", "--no-default-browser-check", "--window-size=1280,800",
    ]


def open_window(url):
    """Shows url. Returns True once the window has closed, False if it went
    to the default browser (no way to know when that tab closes)."""
    try:
        import webview
    except ImportError:
        webview = None
    if webview is not None:
        try:
            webview.create_window("ClipEm Clips", url, width=1280, height=800)
            webview.start()
            return True
        except Exception as exc:
            log.warning(f"pywebview couldn't open a window ({exc}) - trying a browser.")
    browser = find_app_browser()
    if browser:
        log.info(f"Opening the Clip Browser in {browser} (app mode).")
        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        subprocess.run(app_browser_command(browser, url), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    log.info("No Chromium-family browser found - opening the Clip Browser in the default browser.")
    webbrowser.open(url)
    return False


def reopen_existing():
    """Another Clip Browser is running: show its page again."""
    try:
        with open(URL_PATH, "r", encoding="utf-8") as f:
            url = f.read().strip()
    except OSError:
        return
    browser = find_app_browser()
    if browser:
        # Same profile: Chromium hands this to the running window's process.
        subprocess.Popen(app_browser_command(browser, url), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        log.info("Clip Browser already open; showing it again.")
        reopen_existing()
        return
    server = ClipBrowserServer(obs_config.load_clip_folder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    write_text_atomic(URL_PATH, server.url)
    log.info(f"Clip Browser serving {obs_config.load_clip_folder()}")
    try:
        if not open_window(server.url):
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
