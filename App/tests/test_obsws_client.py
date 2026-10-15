"""A dead connection (OBS closed/restarted) must be told apart from one
failed request: the tray widget reconnects on the first, not the second."""

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
