"""input_activity.py: how long ago the PC was used, as stuck_screen.py asks."""

import json
import logging
import math
import re
import subprocess
import sys

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import input_activity  # noqa: E402

log = logging.getLogger("test")
BUS = "org.freedesktop.StatusNotifierItem-99-1"


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def activity(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    loaded = []
    monkeypatch.setattr(input_activity.obs_window, "load_kwin_script",
                        lambda source, name: loaded.append((source, name)) or "/tmp/script.js")
    unloaded = []
    monkeypatch.setattr(input_activity.obs_window, "unload_kwin_script",
                        lambda name, path=None: unloaded.append((name, path)))
    clock = Clock()
    screen = {"name": "DP-1", "lookups": 0}

    def find_screen():
        screen["lookups"] += 1
        return screen["name"]

    return input_activity.InputActivity(log, clock=clock, find_screen=find_screen), clock, loaded, unloaded, screen


def test_unknown_until_the_script_runs(activity):
    tracker, *_rest = activity
    assert tracker.seconds_since_input() is None


def test_a_mouse_that_hasnt_moved_is_idle_not_unknown(activity):
    tracker, *_rest = activity
    tracker.start(BUS)
    assert tracker.seconds_since_input() == math.inf


def test_seconds_since_the_mouse_moved(activity):
    tracker, clock, *_rest = activity
    tracker.start(BUS)
    tracker.mouse_moved("DP-1")
    clock.now += 7
    assert tracker.seconds_since_input() == 7


def test_another_screen_doesnt_count_even_at_the_same_size(activity):
    tracker, *_rest = activity
    tracker.start(BUS)
    tracker.mouse_moved("DP-2")
    tracker.mouse_moved("HDMI-A-1")
    assert tracker.seconds_since_input() == math.inf


def test_unknown_when_the_captured_screen_cant_be_told(activity):
    tracker, _clock, _loaded, _unloaded, screen = activity
    screen["name"] = None  # a window capture, or no pw-dump
    tracker.start(BUS)
    tracker.mouse_moved("DP-1")
    assert tracker.seconds_since_input() is None


def test_the_captured_screen_is_looked_up_once_a_minute(activity):
    tracker, clock, _loaded, _unloaded, screen = activity
    tracker.start(BUS)
    for _ in range(5):
        tracker.seconds_since_input()
    assert screen["lookups"] == 1
    clock.now += input_activity.CAPTURED_SCREEN_CACHE_SECONDS
    tracker.seconds_since_input()
    assert screen["lookups"] == 2


def test_the_script_loads_once_and_unloads(activity):
    tracker, _clock, loaded, unloaded, _screen = activity
    tracker.start(None)  # the tray's D-Bus name isn't known yet
    assert loaded == []
    tracker.start(BUS)
    tracker.start(BUS)
    assert len(loaded) == 1 and loaded[0][1] == input_activity.KWIN_SCRIPT_NAME
    tracker.stop()
    assert unloaded == [(input_activity.KWIN_SCRIPT_NAME, "/tmp/script.js")]
    assert tracker.seconds_since_input() is None


def test_a_script_that_wont_load_is_tried_once_and_unknown(activity, monkeypatch):
    tracker, *_rest = activity
    calls = []
    monkeypatch.setattr(input_activity.obs_window, "load_kwin_script", lambda source, name: calls.append(1))
    tracker.start(BUS)
    tracker.start(BUS)
    assert len(calls) == 1 and tracker.seconds_since_input() is None


def test_other_desktops_dont_get_a_script(activity, monkeypatch):
    tracker, _clock, loaded, *_rest = activity
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    tracker.start(BUS)
    assert loaded == [] and tracker.seconds_since_input() is None


def test_windows_asks_the_system(activity, monkeypatch):
    tracker, *_rest = activity
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(input_activity, "_windows_seconds_since_input", lambda: 3.5)
    assert tracker.seconds_since_input() == 3.5


def test_kwin_script_stops_listening_between_reports():
    script = input_activity.kwin_mouse_script(BUS)
    assert BUS in script and "'MouseMoved', on.name" in script
    assert f"pause.interval = {input_activity.REPORT_SECONDS * 1000}" in script
    assert "cursorPosChanged.disconnect(moved)" in script and "pause.singleShot = true" in script


def test_the_mouse_position_goes_nowhere():
    """Only used inline to find the screen: never kept, sent or logged."""
    script = input_activity.kwin_mouse_script(BUS)
    assert script.count("cursorPos)") == 1 and "workspace.screenAt(workspace.cursorPos)" in script
    assert "print(" not in script and "console." not in script
    assert not re.search(r"\.(x|y)\b", script)
    (call,) = re.findall(r"callDBus\((.*?)\);", script)
    assert call.endswith("'MouseMoved', on.name")


def _dump(*nodes):
    return json.dumps([{"id": node_id, "info": {"props": props}} for node_id, props in nodes])


def _pipewire(stream, driver=164):
    return _dump(
        (164, {"media.class": "Stream/Output/Video", "media.name": stream, "node.name": "kwin_wayland"}),
        (159, {"media.class": "Stream/Input/Video", "media.name": "OBS Studio", "media.role": "Screen",
               "node.driver-id": driver, "node.name": "obs"}),
        (200, {"media.name": "kwin-screencast-DP-2", "node.name": "kwin_wayland"}),  # somebody else's
    )


def _run_returning(output):
    return lambda *a, **k: subprocess.CompletedProcess(a, 0, output, "")


@pytest.mark.parametrize("stream, expected", [
    ("kwin-screencast-DP-1", "DP-1"),
    ("kwin-screencast-HDMI-A-1", "HDMI-A-1"),
    ("kwin-screencast-org.kde.konsole", None),  # a window
    ("something-else", None),
])
def test_captured_screen_comes_from_obss_stream(stream, expected):
    assert input_activity.captured_screen(_run_returning(_pipewire(stream))) == expected


def test_captured_screen_is_unknown_without_a_capture_or_pw_dump():
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("pw-dump")

    assert input_activity.captured_screen(missing) is None
    assert input_activity.captured_screen(_run_returning("not json")) is None
    assert input_activity.captured_screen(_run_returning(_pipewire("kwin-screencast-DP-1", driver=999))) is None
