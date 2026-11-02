"""A dead connection (OBS closed/restarted) must be told apart from one
failed request: the tray widget reconnects on the first, not the second."""

import base64
import hashlib
import json
import sys
import threading
import types

import pytest
from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # websocket-client comes with the tray widget
sys.path.insert(0, TRAY_WIDGET_DIR)
import obsws_client  # noqa: E402
from obsws_client import ObsWebSocketConnectionLost, ObsWebSocketError  # noqa: E402


class FakeSocket:
    """Answers each request with `result`; after close() recv/send fail like
    a socket whose peer went away."""

    def __init__(self, result=True):
        self.result = result
        self.inbox = []
        self.ready = threading.Condition()
        self.closed = False

    def send(self, text):
        if self.closed:
            raise ConnectionResetError("peer closed")
        request = json.loads(text)["d"]
        reply = {"op": obsws_client.OP_REQUEST_RESPONSE, "d": {
            "requestId": request["requestId"],
            "requestStatus": {"result": self.result, "comment": "not available"},
            "responseData": {"outputActive": True},
        }}
        with self.ready:
            self.inbox.append(json.dumps(reply))
            self.ready.notify()

    def recv(self):
        with self.ready:
            self.ready.wait_for(lambda: self.inbox or self.closed, timeout=5)
            if self.inbox:
                return self.inbox.pop(0)
        raise ConnectionResetError("peer closed")

    def close(self):
        with self.ready:
            self.closed = True
            self.ready.notify()


def _connected_client(sock):
    client = obsws_client.ObsWebSocketClient("ws://127.0.0.1:4455", "pw")
    client._ws = sock
    client._identified.set()
    client._reader_thread = threading.Thread(target=client._read_loop, daemon=True)
    client._reader_thread.start()
    return client


def test_working_connection_answers():
    client = _connected_client(FakeSocket())
    assert client.call("GetReplayBufferStatus") == {"outputActive": True}
    assert client.is_connected()


def test_a_refused_request_is_not_a_lost_connection():
    client = _connected_client(FakeSocket(result=False))
    with pytest.raises(ObsWebSocketError) as exc:
        client.call("SaveReplayBuffer")
    assert not isinstance(exc.value, ObsWebSocketConnectionLost)
    assert client.is_connected()


def test_obs_closing_the_connection_is_a_lost_connection():
    sock = FakeSocket()
    client = _connected_client(sock)
    sock.close()  # OBS restarts
    client._reader_thread.join(timeout=5)
    assert not client.is_connected()
    with pytest.raises(ObsWebSocketConnectionLost):
        client.call("GetReplayBufferStatus")


def test_a_failed_send_is_a_lost_connection():
    sock = FakeSocket()
    client = _connected_client(sock)
    sock.closed = True  # send fails before the reader notices
    with pytest.raises(ObsWebSocketConnectionLost):
        client.call("GetReplayBufferStatus", timeout=1)


def _spec_auth(password, salt, challenge):
    """obs-websocket's documented steps, spelled out."""
    secret = base64.b64encode(hashlib.sha256(password.encode() + salt.encode()).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode()).digest()).decode()


class Handshake:
    """What websocket.create_connection returns: answers connect()'s two
    messages from `replies`, then behaves like a closed socket."""

    def __init__(self, *replies):
        self.replies = [json.dumps(r) if isinstance(r, dict) else r for r in replies]
        self.sent = []
        self.closed = False

    def recv(self):
        if self.replies:
            return self.replies.pop(0)
        raise ConnectionResetError("peer closed")

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        self.closed = True


def _serve(monkeypatch, sock):
    opened = []

    def create_connection(url, timeout):
        opened.append((url, timeout))
        return sock

    monkeypatch.setattr(obsws_client.websocket, "create_connection", create_connection, raising=False)
    return opened


HELLO = {"op": obsws_client.OP_HELLO, "d": {"rpcVersion": 1}}
HELLO_AUTH = {"op": obsws_client.OP_HELLO, "d": {"authentication": {"salt": "sAlT", "challenge": "cHaL"}}}
IDENTIFIED = {"op": obsws_client.OP_IDENTIFIED, "d": {"negotiatedRpcVersion": 1}}


def test_the_auth_string_is_what_obs_websocket_expects():
    assert obsws_client._make_auth_string("pässword", "sAlT", "cHaL") == _spec_auth("pässword", "sAlT", "cHaL")


def test_connecting_authenticates_and_subscribes(monkeypatch):
    sock = Handshake(HELLO_AUTH, IDENTIFIED)
    opened = _serve(monkeypatch, sock)
    client = obsws_client.ObsWebSocketClient("ws://127.0.0.1:4455", "pw", event_subscriptions=64)

    client.connect(timeout=3)

    assert opened == [("ws://127.0.0.1:4455", 3)]
    assert sock.sent == [{"op": obsws_client.OP_IDENTIFY, "d": {
        "rpcVersion": obsws_client.RPC_VERSION, "authentication": _spec_auth("pw", "sAlT", "cHaL"),
        "eventSubscriptions": 64}}]


def test_a_server_without_a_password_gets_no_auth(monkeypatch):
    sock = Handshake(HELLO, IDENTIFIED)
    _serve(monkeypatch, sock)
    client = obsws_client.ObsWebSocketClient("ws://x", "pw")
    client.connect()
    client._reader_thread.join(timeout=5)  # the socket is out of messages, so it stops
    assert sock.sent == [{"op": obsws_client.OP_IDENTIFY, "d": {"rpcVersion": obsws_client.RPC_VERSION}}]


def test_anything_but_hello_is_refused(monkeypatch):
    _serve(monkeypatch, Handshake(IDENTIFIED))
    client = obsws_client.ObsWebSocketClient("ws://x", "pw")
    with pytest.raises(ObsWebSocketError, match="Expected Hello"):
        client.connect()
    assert not client.is_connected()


def test_a_wrong_password_is_an_error_not_a_connection(monkeypatch):
    _serve(monkeypatch, Handshake(HELLO_AUTH, {"op": 99}))
    client = obsws_client.ObsWebSocketClient("ws://x", "wrong")
    with pytest.raises(ObsWebSocketError, match="Authentication failed"):
        client.connect()
    assert not client.is_connected() and client._reader_thread is None


def test_calling_before_connecting_says_so():
    with pytest.raises(ObsWebSocketConnectionLost, match="Not connected"):
        obsws_client.ObsWebSocketClient("ws://x", "pw").call("GetVersion")


def test_request_data_is_sent_only_when_there_is_some():
    sent = []

    class Recording(FakeSocket):
        def send(self, text):
            sent.append(json.loads(text)["d"])
            super().send(text)

    client = _connected_client(Recording())
    client.call("GetVersion")
    client.call("TriggerHotkeyByName", {"hotkeyName": "x"})
    assert "requestData" not in sent[0]
    assert sent[1]["requestData"] == {"hotkeyName": "x"} and sent[1]["requestType"] == "TriggerHotkeyByName"
    assert sent[0]["requestId"] != sent[1]["requestId"]


def test_an_unanswered_request_times_out_as_a_failed_request():
    class Silent(FakeSocket):
        def send(self, text):
            pass

    client = _connected_client(Silent())
    with pytest.raises(ObsWebSocketError, match="Timed out") as exc:
        client.call("GetVersion", timeout=0.05)
    assert not isinstance(exc.value, ObsWebSocketConnectionLost)
    assert client._pending == {} and client.is_connected()


def test_a_request_waiting_when_the_connection_goes_fails_at_once():
    class ThenGone(FakeSocket):
        def send(self, text):
            self.close()

    client = _connected_client(ThenGone())
    with pytest.raises(ObsWebSocketConnectionLost, match="Connection lost"):
        client.call("GetVersion", timeout=5)


def test_the_reason_a_request_failed_is_passed_on():
    client = _connected_client(FakeSocket(result=False))
    with pytest.raises(ObsWebSocketError, match="SaveReplayBuffer failed: not available"):
        client.call("SaveReplayBuffer")


def _feed(sock, *messages):
    with sock.ready:
        sock.inbox.extend(json.dumps(m) if isinstance(m, dict) else m for m in messages)
        sock.ready.notify()


def test_events_reach_their_callbacks_even_if_one_fails():
    sock = FakeSocket()
    client = _connected_client(sock)
    seen = threading.Event()
    got = []

    def bad(_data):
        raise RuntimeError("callback bug")

    client.on_event("ReplayBufferSaved", bad)
    client.on_event("ReplayBufferSaved", lambda data: (got.append(data), seen.set()))
    client.on_event("Other", lambda data: got.append("wrong event"))
    _feed(sock, {"op": obsws_client.OP_EVENT, "d": {"eventType": "ReplayBufferSaved", "eventData": {"path": "c.mkv"}}})
    assert seen.wait(5)
    assert got == [{"path": "c.mkv"}]


def test_an_event_without_data_is_an_empty_one():
    sock = FakeSocket()
    client = _connected_client(sock)
    seen = threading.Event()
    got = []
    client.on_event("Ping", lambda data: (got.append(data), seen.set()))
    _feed(sock, {"op": obsws_client.OP_EVENT, "d": {"eventType": "Ping"}})
    assert seen.wait(5) and got == [{}]


def test_junk_on_the_connection_is_skipped():
    sock = FakeSocket()
    client = _connected_client(sock)
    _feed(sock, "", "{not json", {"op": obsws_client.OP_REQUEST_RESPONSE, "d": {"requestId": "nobody's"}})
    assert client.call("GetVersion") == {"outputActive": True}
    assert client.is_connected()


def test_closing_is_quiet_and_ends_the_reader():
    sock = FakeSocket()
    client = _connected_client(sock)
    client.close()
    client._reader_thread.join(timeout=5)
    assert not client._reader_thread.is_alive() and sock.closed and not client.is_connected()


def test_closing_survives_a_socket_that_wont_and_one_never_opened():
    obsws_client.ObsWebSocketClient("ws://x", "pw").close()

    class Stubborn(Handshake):
        def close(self):
            raise OSError("already gone")

    client = obsws_client.ObsWebSocketClient("ws://x", "pw")
    client._ws = Stubborn()
    client.close()


class FlakyClient:
    """Stands in for ObsWebSocketClient: fails `failures` connects."""

    failures = 0
    made = []

    def __init__(self, url, password, event_subscriptions=None):
        self.made.append((url, password, event_subscriptions))

    def connect(self):
        if FlakyClient.failures:
            FlakyClient.failures -= 1
            raise ConnectionRefusedError("OBS isn't up")


@pytest.fixture
def flaky(monkeypatch):
    FlakyClient.made = []
    monkeypatch.setattr(obsws_client, "ObsWebSocketClient", FlakyClient)
    slept = []
    monkeypatch.setattr(obsws_client.time, "sleep", slept.append)
    return slept


def test_retrying_keeps_going_until_obs_is_up(flaky):
    FlakyClient.failures = 2
    logs = []
    client = obsws_client.connect_with_retry("ws://x", "pw", 8, retry_seconds=3, log=logs.append)
    assert isinstance(client, FlakyClient)
    assert FlakyClient.made == [("ws://x", "pw", 8)] * 3
    assert flaky == [3, 3] and len(logs) == 2


def test_retrying_gives_up_when_told_to(flaky):
    FlakyClient.failures = 100
    with pytest.raises(ObsWebSocketError, match="within 0s"):
        obsws_client.connect_with_retry("ws://x", "pw", timeout_seconds=0, log=lambda _m: None)
    assert flaky == []


def test_protocol_numbers_are_obs_websockets():
    assert (obsws_client.OP_HELLO, obsws_client.OP_IDENTIFY, obsws_client.OP_IDENTIFIED) == (0, 1, 2)
    assert (obsws_client.OP_EVENT, obsws_client.OP_REQUEST, obsws_client.OP_REQUEST_RESPONSE) == (5, 6, 7)
    assert obsws_client.RPC_VERSION == 1


def test_a_connection_made_the_real_way_works(monkeypatch):
    class StaysOpen(FakeSocket):
        def send(self, text):
            if json.loads(text)["op"] == obsws_client.OP_REQUEST:
                super().send(text)

    sock = StaysOpen()
    sock.inbox += [json.dumps(HELLO), json.dumps(IDENTIFIED)]
    _serve(monkeypatch, sock)
    client = obsws_client.ObsWebSocketClient("ws://x", "pw")
    client.connect()
    try:
        assert client.is_connected()
        assert client.call("GetVersion") == {"outputActive": True}
    finally:
        client.close()


def test_an_answer_without_a_status_is_a_failure():
    class NoStatus(FakeSocket):
        def send(self, text):
            reply = {"op": obsws_client.OP_REQUEST_RESPONSE, "d": {"requestId": json.loads(text)["d"]["requestId"]}}
            with self.ready:
                self.inbox.append(json.dumps(reply))
                self.ready.notify()

    with pytest.raises(ObsWebSocketError, match="unknown error"):
        _connected_client(NoStatus()).call("GetVersion")


def test_a_failed_send_leaves_nothing_waiting():
    sock = FakeSocket()
    client = _connected_client(sock)
    sock.closed = True
    with pytest.raises(ObsWebSocketConnectionLost):
        client.call("GetVersion", timeout=1)
    assert client._pending == {}


def test_closing_on_purpose_isnt_logged_as_a_lost_connection(caplog):
    sock = FakeSocket()
    client = _connected_client(sock)
    with caplog.at_level("WARNING", logger="tray"):
        client.close()
        client._reader_thread.join(timeout=5)
    assert "connection closed" not in caplog.text
