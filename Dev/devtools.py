"""ClipEm's own editor window - the pinned Electron (Chromium) Setup installs,
see App/App/Shared/Installer/editor_window.py - driven over the DevTools
protocol, for what needs the editor page in a real browser: its tests
(tests/test_editor_page.py) and its screenshot (screenshot_editor.py). Never
a browser of the system's: what's tested and shown is what users run.

The window is ClipEm's Electron running its editor-window app, as
clip_browser.py starts it, with its own profile and DevTools on a free port.
Headless on Linux (Ozone's headless platform); on Windows it opens on the
desktop, so it needs the logged-in session (Dev/test-windows.sh runs there).

Found as CLIPEM_EDITOR_WINDOW, this checkout's (App/App/<platform>/
editor-window - Setup run from it), else on Windows the installed ClipEm's
(the Windows VM tests a copy of the checkout, next to an install).
Standard library only."""

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
TRAY_WIDGET_DIR = os.path.join(os.path.dirname(DEV_DIR), "App", "App", "Shared", "tray-widget")
if TRAY_WIDGET_DIR not in sys.path:
    sys.path.insert(0, TRAY_WIDGET_DIR)

import clip_browser  # noqa: E402 - and through it, Installer/editor_window.py

START_SECONDS = 20


class DevTools:
    """Just enough of the DevTools protocol (over a WebSocket, standard
    library only) to run JavaScript in a page."""

    def __init__(self, ws_url):
        url = urllib.parse.urlparse(ws_url)
        self.sock = socket.create_connection((url.hostname, url.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {url.path} HTTP/1.1\r\nHost: {url.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(1)
        assert b" 101 " in response.split(b"\r\n")[0]
        self.next_id = 0

    def _send(self, text):
        data = text.encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        if len(data) < 126:
            header += bytes([0x80 | len(data)])
        elif len(data) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(data))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("DevTools closed")
            data += chunk
        return data

    def _receive(self):
        message = b""
        while True:
            first, second = self._exact(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._exact(8))[0]
            message += self._exact(length)
            if first & 0x80:
                return json.loads(message)

    def call(self, method, **params):
        """A DevTools method's result."""
        self.next_id += 1
        self._send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = self._receive()
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]

    def js(self, expression, gesture=False):
        """The value of a JavaScript expression (awaited if it's a promise).
        gesture: run as if the user did it (what the clipboard needs)."""
        result = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True,
                           userGesture=gesture)
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return result["result"].get("value")

    def wait_for(self, expression, seconds=15):
        deadline = time.monotonic() + seconds
        while True:
            value = self.js(expression)
            if value or time.monotonic() > deadline:
                return value
            time.sleep(0.1)

    def close(self):
        self.sock.close()


def editor_window_path():
    """ClipEm's editor window executable (see the top), or None."""
    found = [os.environ.get("CLIPEM_EDITOR_WINDOW"), clip_browser.editor_window.exe_path()]
    if sys.platform == "win32":
        found.append(os.path.expandvars(r"%LOCALAPPDATA%\ClipEm\App\Windows\editor-window\ClipEm Editor.exe"))
    return next((path for path in found if path and os.path.isfile(path)), None)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_editor_window(url, profile_dir, exe=None, extra_args=()):
    """(process, DevTools on its page): the editor window showing url (the
    server's ClipBrowserServer.url). profile_dir: its own - the window's
    single-instance lock is its profile's, and the user's may be open.
    extra_args: more Chromium switches.
    Raises RuntimeError if there's no editor window, or it doesn't start."""
    exe = exe or editor_window_path()
    if not exe:
        raise RuntimeError("ClipEm's editor window isn't installed - run Setup, or set CLIPEM_EDITOR_WINDOW.")
    port = _free_port()
    cmd = [arg if not arg.startswith("--clipem-profile=") else f"--clipem-profile={profile_dir}"
           for arg in clip_browser.window_command(exe, url)]
    cmd += [f"--remote-debugging-port={port}", "--mute-audio", *extra_args]
    if sys.platform.startswith("linux"):
        # No display, and no GPU then: without --disable-gpu, video frames and
        # some canvases never reach a screenshot.
        cmd += ["--ozone-platform=headless", "--disable-gpu"]
    proc = subprocess.Popen(cmd, env=clip_browser.window_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + START_SECONDS
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as res:
                targets = [t for t in json.load(res) if t["type"] == "page"]
            if targets:
                break
        except OSError:
            pass
        if time.monotonic() > deadline or proc.poll() is not None:
            stop(proc)
            raise RuntimeError("ClipEm's editor window didn't start.")
        time.sleep(0.2)
    tools = DevTools(targets[0]["webSocketDebuggerUrl"])
    if "Electron/" not in tools.js("navigator.userAgent"):
        tools.close()
        stop(proc)
        raise RuntimeError("That isn't ClipEm's editor window (no Electron in its user agent).")
    return proc, tools


def stop(proc):
    proc.kill()
    proc.wait()
