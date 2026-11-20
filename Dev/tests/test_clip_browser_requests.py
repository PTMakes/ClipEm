"""The clip editor's local server (tray-widget/clip_browser.py) given
malformed requests: every one gets an answer, and none ties up a handler."""

import json
import os
import socket
import sys
import threading

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import clip_browser  # noqa: E402


@pytest.fixture
def server(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "a.mp4").write_bytes(b"x")
    srv = clip_browser.ClipBrowserServer(lambda: str(clips), token="tok")
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _raw(server, request, timeout=3):
    """The status line the server answers a raw request with."""
    with socket.create_connection(server.server_address, timeout=timeout) as sock:
        sock.sendall(request)
        try:
            data = sock.recv(256)
        except TimeoutError:
            return "<no answer in time>"
    return data.split(b"\r\n")[0].decode() or "<closed without an answer>"


def _post(route, body, length=None):
    length = str(len(body)) if length is None else length
    return (f"POST /tok/{route} HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n").encode() + body


@pytest.mark.parametrize("route", ["api/asset-clip", "api/rename", "api/project", "api/folder", "api/delete",
                                   "api/export", "api/reveal", "api/open", "api/automix"])
@pytest.mark.parametrize("body", [b"[]", b'"text"', b"5", b"null", b"not json"])
def test_a_body_that_isnt_a_json_object_is_answered(server, route, body):
    status = _raw(server, _post(route, body))
    assert status.startswith("HTTP/1.0 "), status
    assert " 500 " not in status, status  # read as {}: the route's own "missing" answer


@pytest.mark.parametrize("length", ["abc", "-1", "-5", ""])
@pytest.mark.parametrize("route", ["api/rename", "api/asset?name=a.png", "api/asset-clip"])
def test_a_bad_content_length_is_answered_straight_away(server, route, length):
    status = _raw(server, _post(route, b"{}", length=length), timeout=2)
    assert status.startswith("HTTP/1.0 "), status


def test_the_token_must_be_the_whole_first_segment(server):
    assert " 404 " in _raw(server, b"GET /tokx/api/clips HTTP/1.1\r\n\r\n")
    assert " 404 " in _raw(server, b"GET /to/api/clips HTTP/1.1\r\n\r\n")
    assert " 404 " in _raw(server, b"GET /tok HTTP/1.1\r\n\r\n")
    assert " 200 " in _raw(server, b"GET /tok/api/clips HTTP/1.1\r\n\r\n")


def test_media_requests_cant_escape_the_folder(server, tmp_path):
    (tmp_path / "secret.mp4").write_bytes(b"secret")
    for path in ("media/..%2Fsecret.mp4", "media/%2E%2E/secret.mp4", "media/..%5Csecret.mp4",
                 "thumb/..%2Fsecret.mp4", "media/" + str(tmp_path / "secret.mp4").replace("/", "%2F")):
        assert " 404 " in _raw(server, f"GET /tok/{path} HTTP/1.1\r\n\r\n".encode()), path


def test_setting_the_folder_needs_an_absolute_existing_one(server, tmp_path):
    for body in (b'{"path": "relative/dir"}', b'{"path": 5}', b'{"path": "/no/such/folder"}'):
        assert " 400 " in _raw(server, _post("api/folder", body)), body
    other = tmp_path / "other"
    other.mkdir()
    body = json.dumps({"path": str(other)}).encode()
    assert " 200 " in _raw(server, _post("api/folder", body))
    assert server.current_folder() == os.path.normpath(str(other))
    assert " 200 " in _raw(server, _post("api/folder", b'{"path": ""}'))  # back to the clips folder
    assert server.current_folder() == str(tmp_path / "clips")


def test_reveal_and_play_only_open_clips_or_recorded_exports(server, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(clip_browser, "reveal_in_file_manager", opened.append)
    monkeypatch.setattr(clip_browser, "open_with_default_app", opened.append)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    body = json.dumps({"path": str(outside)}).encode()
    for route in ("api/reveal", "api/play", "api/path"):
        assert " 404 " in _raw(server, _post(route, body)), route
    assert opened == []
    assert " 200 " in _raw(server, _post("api/play", b'{"name": "a.mp4"}'))
    assert opened == [os.path.realpath(tmp_path / "clips" / "a.mp4")]
