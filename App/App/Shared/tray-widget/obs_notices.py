"""The tray's side of clipem-obs, ClipEm's OBS plugin (Dev/obs-plugin/,
loaded by run.py through OBS_PLUGINS_PATH).

The plugin keeps OBS hidden: OBS has no tray icon of its own, and everything
OBS would have shown the user comes here as a notice instead, to be shown as
a ClipEm notification:

- dialog: a dialog OBS opened while hidden. One that only says OK was
  already closed by the plugin ("dismissed"); one that asks something is
  still open in OBS, so the notification says to answer it with Show OBS.
- output: the replay buffer, recording or stream stopped with an error.
- overload: OBS is skipping frames.
- log: an error in OBS's log (a Python traceback from a ClipEm script is one
  notice). These can come in bursts, so at most one notification per
  LOG_NOTIFY_COOLDOWN_SECONDS, the rest counted into the next one.

Notices come as obs-websocket "VendorEvent"s, and "Hello" (sent every
HELLO_SECONDS, which also tells the plugin someone is listening) returns any
this connection missed - e.g. errors from before the tray connected. Every
notice is logged to tray.log in full.

Also how "Show OBS"/"Hide OBS" work when the plugin is there: it shows or
hides OBS's window itself and reports when that changes (WindowVisibility).
Without the plugin (an older install, macOS), obs_window.py's per-platform
ways are used instead.
"""

import logging
import sys
import threading
import time

from obsws_client import ObsWebSocketConnectionLost, ObsWebSocketError

log = logging.getLogger("tray")

VENDOR_NAME = "ClipEm"  # the plugin's VENDOR_NAME
HELLO_SECONDS = 10  # the plugin counts the tray as gone after 30 s without one
LOG_NOTIFY_COOLDOWN_SECONDS = 60
MAX_NOTIFICATION_CHARS = 240

_LOG_LEVELS = {"error": logging.ERROR, "warning": logging.WARNING}


def _shorten(text):
    text = " ".join(text.split())
    return text if len(text) <= MAX_NOTIFICATION_CHARS else text[: MAX_NOTIFICATION_CHARS - 3] + "..."


def notification_text(notice):
    """What the user is told about a notice."""
    kind, title, text = notice.get("kind"), notice.get("title", ""), notice.get("text", "")
    if kind == "dialog":
        if notice.get("needs_answer"):
            return _shorten(f"OBS needs an answer: {title}. {text}") + " Use Show OBS in the tray menu to answer it."
        if text.startswith("OBS opened"):
            return _shorten(text) + " Use Show OBS in the tray menu to see it."
        return _shorten(f"OBS: {title}. {text}" if title else f"OBS: {text}")
    if kind == "log":
        first = text.strip().splitlines()[0] if text.strip() else "unknown error"
        return _shorten(f"OBS reported an error: {first}")
    return _shorten(text)


class ObsPlugin:
    """Driven from the tray's connect (attach) and poll loop (tick)."""

    def __init__(self, notify, on_window_change=None, clock=time.monotonic):
        self._notify = notify
        self._on_window_change = on_window_change
        self._clock = clock
        self._lock = threading.Lock()
        self.present = False
        self.window_visible = False
        self._session = ""
        self._last_id = 0
        self._last_hello = None
        self._log_notified_at = None
        self._log_held = 0

    def attach(self, client):
        """After each (re)connect: listens for the plugin's events and says
        Hello straight away, catching up on anything already reported."""
        client.on_event("VendorEvent", self._on_vendor_event)
        self.present = False
        self._last_hello = None
        try:
            self.tick(client)
        except ObsWebSocketError as exc:  # the poll loop's next tick finds out too
            log.warning(f"Couldn't say Hello to OBS's ClipEm plugin: {exc}")

    def forget(self):
        """The connection is gone - and with it, maybe, OBS."""
        self.present = False

    def tick(self, client):
        now = self._clock()
        if self._last_hello is not None and now - self._last_hello < HELLO_SECONDS:
            self._flush_held_log_errors(now)
            return
        self._last_hello = now
        try:
            reply = self._call(client, "Hello", {"after_id": self._last_id, "session": self._session})
        except ObsWebSocketConnectionLost:
            raise
        except ObsWebSocketError as exc:
            if self.present:
                log.warning(f"OBS's ClipEm plugin stopped answering ({exc}).")
            elif self._last_id == 0 and not self._session:
                log.info(f"OBS's ClipEm plugin isn't loaded ({exc}) - OBS's own notifications won't reach ClipEm.")
            self.present = False
            return
        if not self.present:
            log.info(f"OBS's ClipEm plugin is loaded (version {reply.get('plugin_version')}).")
        self.present = True
        with self._lock:
            if reply.get("session", "") != self._session:  # OBS restarted: its ids start over
                self._session = reply.get("session", "")
                self._last_id = 0
        self._set_window_visible(bool(reply.get("window_visible", False)))
        for notice in reply.get("notices", []):
            self._handle(notice)
        self._flush_held_log_errors(now)

    def set_window_visible(self, client, visible):
        """Shows or hides OBS's window. True if the plugin took the request."""
        if not self.present:
            return False
        if visible and sys.platform == "win32":
            _let_obs_take_foreground()
        try:
            reply = self._call(client, "SetWindowVisible", {"visible": bool(visible)})
        except ObsWebSocketError as exc:
            log.warning(f"Couldn't {'show' if visible else 'hide'} OBS through its plugin: {exc}")
            return False
        return bool(reply.get("ok", False))

    def goodbye(self, client):
        """The tray is quitting and leaving OBS running: the plugin gives OBS
        its own tray icon back. Without this it does so 30 s later."""
        if not self.present:
            return
        try:
            self._call(client, "Goodbye", {})
        except ObsWebSocketError as exc:
            log.warning(f"Couldn't tell OBS's plugin the tray is quitting: {exc}")

    @staticmethod
    def _call(client, request_type, data):
        response = client.call(
            "CallVendorRequest", {"vendorName": VENDOR_NAME, "requestType": request_type, "requestData": data}
        )
        return response.get("responseData", {}) or {}

    def _on_vendor_event(self, event):
        if event.get("vendorName") != VENDOR_NAME:
            return
        data = event.get("eventData", {}) or {}
        if event.get("eventType") == "Notice":
            self.present = True
            self._handle(data)
        elif event.get("eventType") == "WindowVisibility":
            self._set_window_visible(bool(data.get("visible", False)))

    def _set_window_visible(self, visible):
        changed = visible != self.window_visible
        self.window_visible = visible
        if changed and self._on_window_change:
            self._on_window_change(visible)

    def _handle(self, notice):
        try:
            notice_id = int(notice.get("id", 0))
        except (TypeError, ValueError):
            return
        with self._lock:
            session = notice.get("session", "")
            if session != self._session:  # OBS restarted: its ids start over
                self._session = session
                self._last_id = 0
            if notice_id <= self._last_id:
                return  # already had it (an event and a Hello can both bring it)
            self._last_id = notice_id

        kind = notice.get("kind", "")
        repeats = notice.get("repeats", 1)
        log.log(
            _LOG_LEVELS.get(notice.get("level"), logging.INFO),
            f"OBS {kind} notice{f' (x{repeats})' if repeats and repeats > 1 else ''}: "
            f"{notice.get('title', '')}: {notice.get('text', '')}"
            f"{' [closed by the plugin]' if notice.get('dismissed') else ''}",
        )
        if kind == "log":
            self._log_error(notice)
        else:
            # Held-back log errors are most likely this same failure, logged
            # just before OBS showed it - not worth a notification of their own.
            with self._lock:
                self._log_held = 0
            self._notify(notification_text(notice))

    def _log_error(self, notice):
        now = self._clock()
        with self._lock:
            if self._log_notified_at is not None and now - self._log_notified_at < LOG_NOTIFY_COOLDOWN_SECONDS:
                self._log_held += 1
                return
            self._log_notified_at = now
            held, self._log_held = self._log_held, 0
        message = notification_text(notice)
        if held:
            message += f" (and {held} more before it - see OBS's log)"
        self._notify(message)

    def _flush_held_log_errors(self, now):
        with self._lock:
            if not self._log_held or now - self._log_notified_at < LOG_NOTIFY_COOLDOWN_SECONDS:
                return
            held, self._log_held = self._log_held, 0
            self._log_notified_at = now
        self._notify(f"OBS reported {held} more error{'s' if held > 1 else ''} - see OBS's log.")


def _let_obs_take_foreground():
    """Windows only lets the process the user just clicked bring a window
    to the front - the tray, here. Lets OBS do it for its own window."""
    try:
        import ctypes

        import obs_process

        for pid in obs_process.find_obs_pids():
            ctypes.windll.user32.AllowSetForegroundWindow(pid)
    except Exception as exc:
        log.info(f"AllowSetForegroundWindow for OBS failed: {exc}")
