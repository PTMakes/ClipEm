"""The clip editor's page itself (tray-widget/clip_browser/*.js), run in a
headless Chromium against the real server - what the Python tests of the
server can't see: which file the page actually plays. Skipped where there's
no Chromium (the Windows VM)."""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)

import clip_browser  # noqa: E402

CHROMIUM = next((shutil.which(name) for name in ("chromium", "chromium-browser", "google-chrome", "chrome")
                 if shutil.which(name)), None)
FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not (CHROMIUM and FFMPEG), reason="needs Chromium and ffmpeg")


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

    def js(self, expression):
        """The value of a JavaScript expression (awaited if it's a promise)."""
        self.next_id += 1
        self._send(json.dumps({"id": self.next_id, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True}}))
        while True:
            message = self._receive()
            if message.get("id") == self.next_id:
                result = message["result"]
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


@pytest.fixture
def page(tmp_path):
    """open(folder) -> DevTools on the editor's page, served from folder."""
    opened = []

    def open_page(folder):
        srv = clip_browser.ClipBrowserServer(lambda: str(folder), token="tok")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        chrome = subprocess.Popen([CHROMIUM, "--headless=new", f"--remote-debugging-port={port}",
                                   f"--user-data-dir={tmp_path / 'profile'}", "--no-first-run", "--mute-audio",
                                   "--autoplay-policy=no-user-gesture-required", srv.url],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opened.append((srv, chrome))
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as res:
                    targets = [t for t in json.load(res) if t["type"] == "page"]
                if targets:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("Chromium didn't start")
            time.sleep(0.2)
        tools = DevTools(targets[0]["webSocketDebuggerUrl"])
        opened.append((None, tools))
        assert tools.wait_for("typeof state !== 'undefined' && state.clips.length > 0"), "the page listed no clips"
        return tools

    yield open_page
    for srv, other in reversed(opened):
        if srv is None:
            other.close()
        else:
            other.kill()
            other.wait()
            srv.shutdown()
            srv.server_close()


def post_clip_save(folder, seconds=8, press=5):
    """A post-clip save as clip_post leaves it: the clip, cut at the press,
    and the "-extra.mkv" running on past it, with every track."""
    extra = folder / "Replay-x-extra.mkv"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s=160x90:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-map", "0:v", "-map", "1:a", "-map", "1:a",
                    "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-metadata", f"CLIPEM_MARK={press:.3f}", "-metadata:s:a:0", "title=Mix",
                    "-metadata:s:a:1", "title=Mic", "-metadata:s:a:2", "title=Desktop", str(extra)], check=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(extra), "-t", str(press), "-map", "0:v", "-map", "0:a:0",
                    "-c", "copy", str(folder / "Replay-x.mp4")], check=True)
    return "Replay-x.mp4"


def test_the_editor_plays_past_a_post_clip_press(tmp_path, page):
    """The timeline runs on to the "-extra.mkv"'s end, so that's what the
    player has to play - the clip itself stops at the press."""
    name = post_clip_save(tmp_path)
    tools = page(tmp_path)
    tools.js(f"select({json.dumps(name)})")
    assert tools.wait_for("ed.info && player.readyState >= 1 && isFinite(player.duration)"), "nothing loaded"
    timeline, mark = tools.js("[ed.info.duration, ed.info.mark]")
    assert mark == pytest.approx(5, abs=0.05) and timeline > 7.5
    assert tools.js("player.duration") == pytest.approx(timeline, abs=0.15)  # the file the editor works on
    reached = tools.js("new Promise((done) => { player.onseeked = () => done(player.currentTime);"
                       " player.currentTime = 7; setTimeout(() => done(player.currentTime), 5000); })")
    assert reached == pytest.approx(7, abs=0.1)


def test_a_plain_clip_plays_itself(tmp_path, page):
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=30:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "Replay-y.mp4")], check=True)
    tools = page(tmp_path)
    tools.js("select('Replay-y.mp4')")
    assert tools.wait_for("player.readyState >= 1 && isFinite(player.duration)")
    assert tools.js("player.currentSrc").endswith("/media/Replay-y.mp4")
