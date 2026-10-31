"""The tray's side of ClipEm's OBS plugin (tray-widget/obs_notices.py),
driven by a fake obs-websocket client standing in for the plugin."""

import sys
import types

from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # obsws_client's only dependency
sys.path.insert(0, TRAY_WIDGET_DIR)
import obs_notices  # noqa: E402
from obsws_client import ObsWebSocketConnectionLost, ObsWebSocketError  # noqa: E402


def notice(notice_id, kind="dialog", session="s1", **fields):
    return {"id": notice_id, "session": session, "kind": kind, "level": "error", "title": "Title",
            "text": "Text", "needs_answer": False, "dismissed": False, "repeats": 1, **fields}


class FakePlugin:
    """obs-websocket with the plugin's vendor requests; loaded=False is an OBS without it."""

    def __init__(self, loaded=True):
        self.loaded = loaded
        self.notices = []
        self.window_visible = False
        self.calls = []
        self.callbacks = {}

    def on_event(self, event_type, callback):
        self.callbacks.setdefault(event_type, []).append(callback)

    def send(self, event_type, data):
        for cb in self.callbacks.get("VendorEvent", []):
            cb({"vendorName": "ClipEm", "eventType": event_type, "eventData": data})

    def call(self, request_type, request_data=None, timeout=10):
        assert request_type == "CallVendorRequest"
        assert request_data["vendorName"] == "ClipEm"
        self.calls.append((request_data["requestType"], request_data["requestData"]))
        if not self.loaded:
            raise ObsWebSocketError("CallVendorRequest failed: No vendor was found by that name.")
        data = request_data["requestData"]
        if request_data["requestType"] == "Hello":
            after = data["after_id"] if data["session"] == "s1" else 0
            reply = {"plugin_version": 1, "session": "s1", "window_visible": self.window_visible,
                     "notices": [n for n in self.notices if n["id"] > after]}
        elif request_data["requestType"] == "SetWindowVisible":
            self.window_visible = data["visible"]
            reply = {"ok": True}
        else:
            reply = {}
        return {"vendorName": "ClipEm", "requestType": request_data["requestType"], "responseData": reply}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(plugin=None):
    shown, windows, clock = [], [], Clock()
    tracker = obs_notices.ObsPlugin(shown.append, on_window_change=windows.append, clock=clock)
    plugin = plugin or FakePlugin()
    return tracker, plugin, shown, windows, clock


def test_attach_catches_up_on_notices_from_before_the_tray_connected():
    tracker, plugin, shown, _, _ = make()
    plugin.notices = [notice(1, kind="output", text="The replay buffer stopped: the disk is full.")]
    tracker.attach(plugin)
    assert tracker.present
    assert shown == ["The replay buffer stopped: the disk is full."]


def test_a_notice_from_an_event_and_again_from_hello_is_shown_once():
    tracker, plugin, shown, _, clock = make()
    tracker.attach(plugin)
    plugin.notices = [notice(1, kind="overload", text="Skipping frames")]
    plugin.send("Notice", plugin.notices[0])
    clock.now += obs_notices.HELLO_SECONDS
    tracker.tick(plugin)
    assert shown == ["Skipping frames"]


def test_hello_is_only_sent_every_hello_seconds():
    tracker, plugin, _, _, clock = make()
    tracker.attach(plugin)
    tracker.tick(plugin)
    clock.now += obs_notices.HELLO_SECONDS - 1
    tracker.tick(plugin)
    assert [c[0] for c in plugin.calls] == ["Hello"]
    clock.now += 1
    tracker.tick(plugin)
    assert [c[0] for c in plugin.calls] == ["Hello", "Hello"]
    assert plugin.calls[-1][1] == {"after_id": 0, "session": "s1"}


def test_an_obs_restart_starts_the_ids_over():
    tracker, plugin, shown, _, _ = make()
    tracker.attach(plugin)
    plugin.send("Notice", notice(5, kind="output", text="first OBS"))
    plugin.send("Notice", notice(1, kind="output", text="second OBS", session="s2"))
    assert shown == ["first OBS", "second OBS"]


def test_dialog_texts():
    assert obs_notices.notification_text(notice(1, title="Replay Buffer", text="Disk full.", dismissed=True)) == \
        "OBS: Replay Buffer. Disk full."
    asking = obs_notices.notification_text(notice(1, title="Restart", text="Restart OBS now?", needs_answer=True))
    assert asking.startswith("OBS needs an answer: Restart. Restart OBS now?")
    assert "Show OBS" in asking
    opened = obs_notices.notification_text(notice(1, title="Settings", text='OBS opened "Settings".'))
    assert opened.startswith('OBS opened "Settings".') and "Show OBS" in opened


def test_long_texts_are_cut_short():
    text = obs_notices.notification_text(notice(1, kind="output", text="x" * 1000))
    assert len(text) == obs_notices.MAX_NOTIFICATION_CHARS and text.endswith("...")


def test_log_errors_are_held_back_after_one_then_counted():
    tracker, plugin, shown, _, clock = make()
    tracker.attach(plugin)
    plugin.send("Notice", notice(1, kind="log", text="Traceback (most recent call last):\n  File ..."))
    plugin.send("Notice", notice(2, kind="log", text="another"))
    plugin.send("Notice", notice(3, kind="log", text="and another"))
    assert shown == ["OBS reported an error: Traceback (most recent call last):"]
    clock.now += obs_notices.LOG_NOTIFY_COOLDOWN_SECONDS
    tracker.tick(plugin)
    assert shown[1:] == ["OBS reported 2 more errors - see OBS's log."]
    clock.now += obs_notices.LOG_NOTIFY_COOLDOWN_SECONDS
    plugin.send("Notice", notice(4, kind="log", text="later"))
    assert shown[2:] == ["OBS reported an error: later"]


def test_held_log_errors_are_dropped_when_obs_then_shows_the_failure():
    tracker, plugin, shown, _, clock = make()
    tracker.attach(plugin)
    plugin.send("Notice", notice(1, kind="log", text="startup error"))
    plugin.send("Notice", notice(2, kind="log", text="Recording stopped because of bad output path"))
    plugin.send("Notice", notice(3, kind="dialog", title="Bad File Path", text="Check the path.", dismissed=True))
    clock.now += obs_notices.LOG_NOTIFY_COOLDOWN_SECONDS
    tracker.tick(plugin)
    assert shown == ["OBS reported an error: startup error", "OBS: Bad File Path. Check the path."]


def test_other_notices_are_never_held_back():
    tracker, plugin, shown, _, _ = make()
    tracker.attach(plugin)
    plugin.send("Notice", notice(1, kind="log", text="log"))
    plugin.send("Notice", notice(2, kind="output", text="stopped"))
    plugin.send("Notice", notice(3, kind="output", text="stopped again"))
    assert shown == ["OBS reported an error: log", "stopped", "stopped again"]


def test_other_vendors_events_are_ignored():
    tracker, plugin, shown, _, _ = make()
    tracker.attach(plugin)
    for cb in plugin.callbacks["VendorEvent"]:
        cb({"vendorName": "SomeoneElse", "eventType": "Notice", "eventData": notice(1)})
    assert shown == []


def test_window_visibility_comes_from_the_plugin():
    tracker, plugin, _, windows, _ = make()
    plugin.window_visible = True
    tracker.attach(plugin)
    plugin.send("WindowVisibility", {"visible": False})
    plugin.send("WindowVisibility", {"visible": False})
    assert windows == [True, False]
    assert tracker.window_visible is False


def test_show_and_hide_go_through_the_plugin():
    tracker, plugin, _, _, _ = make()
    tracker.attach(plugin)
    assert tracker.set_window_visible(plugin, True)
    assert plugin.calls[-1] == ("SetWindowVisible", {"visible": True})


def test_goodbye_tells_the_plugin_the_tray_is_going():
    tracker, plugin, _, _, _ = make()
    tracker.attach(plugin)
    tracker.goodbye(plugin)
    assert plugin.calls[-1] == ("Goodbye", {})


def test_goodbye_without_the_plugin_asks_nothing():
    tracker, plugin, _, _, _ = make(FakePlugin(loaded=False))
    tracker.attach(plugin)
    tracker.goodbye(plugin)
    assert [c[0] for c in plugin.calls] == ["Hello"]


def test_without_the_plugin_nothing_is_shown_and_show_falls_back():
    tracker, plugin, shown, _, _ = make(FakePlugin(loaded=False))
    tracker.attach(plugin)
    assert not tracker.present
    assert not tracker.set_window_visible(plugin, True)
    assert [c[0] for c in plugin.calls] == ["Hello"]  # SetWindowVisible never tried
    assert shown == []


def test_a_lost_connection_still_reaches_the_poll_loop():
    tracker, plugin, _, _, clock = make()
    tracker.attach(plugin)

    def lost(*args, **kwargs):
        raise ObsWebSocketConnectionLost("gone")

    plugin.call = lost
    clock.now += obs_notices.HELLO_SECONDS
    try:
        tracker.tick(plugin)
    except ObsWebSocketConnectionLost:
        pass
    else:
        raise AssertionError("tick() swallowed a lost connection")
