"""Minimal obs-websocket v5 client.

Just enough of the protocol to support the tray widget: connect,
authenticate, send request/response calls, and subscribe to events. Not a
general-purpose client - see https://github.com/obsproject/obs-websocket
/blob/master/docs/generated/protocol.md for the full protocol if you want
to extend this.

Depends only on `websocket-client` (pure Python, no compiled extensions,
so it installs cleanly even into the bundled embeddable Windows Python).
"""

import base64
import hashlib
import json
import logging
import threading
import time

import websocket

OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7
OP_EVENT = 5

RPC_VERSION = 1

log = logging.getLogger("tray")


class ObsWebSocketError(Exception):
    pass


class ObsWebSocketConnectionLost(ObsWebSocketError):
    """The connection itself is gone (e.g. OBS closed or restarted) - as
    opposed to one request failing. The client can't recover; connect a new
    one."""


def _make_auth_string(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsWebSocketClient:
    """A background-threaded obs-websocket connection.

    Call `connect()`, then `call(request_type, request_data)` for
    synchronous request/response, and register callbacks via
    `on_event(event_type, callback)` for push events like
    ReplayBufferStateChanged.
    """

    def __init__(self, url, password, event_subscriptions=None):
        self._url = url
        self._password = password
        self._event_subscriptions = event_subscriptions
        self._ws = None
        self._reader_thread = None
        self._stop = threading.Event()
        self._identified = threading.Event()
        self._pending = {}  # requestId -> {"event": threading.Event, "response": dict}
        self._pending_lock = threading.Lock()
        # websocket-client's send() isn't safe to call from several threads
        # at once (the poll loop and menu callbacks both do).
        self._send_lock = threading.Lock()
        self._request_counter = 0
        self._event_callbacks = {}

    def on_event(self, event_type, callback):
        self._event_callbacks.setdefault(event_type, []).append(callback)

    def connect(self, timeout=10):
        self._ws = websocket.create_connection(self._url, timeout=timeout)
        hello_raw = self._ws.recv()
        hello = json.loads(hello_raw)
        if hello.get("op") != OP_HELLO:
            raise ObsWebSocketError(f"Expected Hello, got: {hello_raw}")

        identify = {"op": OP_IDENTIFY, "d": {"rpcVersion": RPC_VERSION}}
        auth_info = hello["d"].get("authentication")
        if auth_info:
            auth = _make_auth_string(self._password, auth_info["salt"], auth_info["challenge"])
            identify["d"]["authentication"] = auth
        if self._event_subscriptions is not None:
            identify["d"]["eventSubscriptions"] = self._event_subscriptions

        self._ws.send(json.dumps(identify))
        identified_raw = self._ws.recv()
        identified = json.loads(identified_raw)
        if identified.get("op") != OP_IDENTIFIED:
            raise ObsWebSocketError(f"Authentication failed: {identified_raw}")

        self._identified.set()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except Exception as exc:
                if not self._stop.is_set():
                    log.warning(f"obs-websocket connection closed: {exc}")
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            op = msg.get("op")
            if op == OP_REQUEST_RESPONSE:
                req_id = msg["d"]["requestId"]
                with self._pending_lock:
                    pending = self._pending.get(req_id)
                if pending:
                    pending["response"] = msg["d"]
                    pending["event"].set()
            elif op == OP_EVENT:
                event_type = msg["d"]["eventType"]
                event_data = msg["d"].get("eventData", {})
                for cb in self._event_callbacks.get(event_type, []):
                    try:
                        cb(event_data)
                    except Exception:
                        log.exception(f"Error in {event_type} event callback")
        # Connection gone: fail any call still waiting instead of letting it
        # sit out its full timeout.
        self._identified.clear()
        with self._pending_lock:
            for pending in self._pending.values():
                pending["event"].set()

    def call(self, request_type, request_data=None, timeout=10):
        if not self._identified.is_set():
            raise ObsWebSocketConnectionLost("Not connected")

        done = threading.Event()
        with self._pending_lock:
            self._request_counter += 1
            request_id = str(self._request_counter)
            self._pending[request_id] = {"event": done, "response": None}

        payload = {
            "op": OP_REQUEST,
            "d": {"requestType": request_type, "requestId": request_id},
        }
        if request_data:
            payload["d"]["requestData"] = request_data

        try:
            with self._send_lock:
                self._ws.send(json.dumps(payload))
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise ObsWebSocketConnectionLost(f"Couldn't send {request_type}: {exc}") from exc

        if not done.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise ObsWebSocketError(f"Timed out waiting for response to {request_type}")

        with self._pending_lock:
            response = self._pending.pop(request_id)["response"]
        if response is None:
            raise ObsWebSocketConnectionLost(f"Connection lost while waiting for {request_type}")

        status = response.get("requestStatus", {})
        if not status.get("result", False):
            raise ObsWebSocketError(
                f"{request_type} failed: {status.get('comment', 'unknown error')}"
            )
        return response.get("responseData", {})

    def is_connected(self):
        return self._identified.is_set()

    def close(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass


def connect_with_retry(
    url, password, event_subscriptions=None, retry_seconds=5, timeout_seconds=None, log=log.info
):
    """Keeps trying to connect (OBS may still be starting up) until it succeeds.

    If `timeout_seconds` is given and OBS still hasn't accepted a connection
    by then, gives up and raises `ObsWebSocketError` instead of retrying
    forever - callers use this to tell "OBS is just slow to start" apart from
    "OBS has been closed/isn't coming back".
    """
    start = time.monotonic()
    while True:
        client = ObsWebSocketClient(url, password, event_subscriptions)
        try:
            client.connect()
            return client
        except Exception as exc:
            if timeout_seconds is not None and time.monotonic() - start >= timeout_seconds:
                raise ObsWebSocketError(
                    f"Could not connect to OBS within {timeout_seconds}s ({exc}); giving up."
                ) from exc
            log(f"Waiting for OBS ({exc}); retrying in {retry_seconds}s...")
            time.sleep(retry_seconds)
