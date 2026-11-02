"""What's on the captured screen, on Windows (tray-widget/win_windows.py). The
Win32 calls themselves need Windows; what's decided from them is tested here."""

import logging
import sys

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import win_windows  # noqa: E402

SCREEN = (0, 0, 1920, 1080)
SECOND = (1920, 0, 3840, 1080)
TITLED = win_windows.WS_CAPTION | 0x00010000
BORDERLESS = 0x90000000


def row(exe="game.exe", style=BORDERLESS, rect=SCREEN, monitor=SCREEN, hwnd=1, title="Nice Name"):
    return {"hwnd": hwnd, "pid": 5, "exe": exe, "title": title, "style": style, "rect": rect, "monitor_rect": monitor,
            "screen": "ID"}


@pytest.mark.parametrize("style, rect, expected", [
    (BORDERLESS, SCREEN, True),  # borderless, or exclusive, fullscreen
    (TITLED, (-8, -8, 1928, 1048), False),  # maximized, overhanging
    (TITLED, (-8, -8, 1928, 1088), False),  # maximized with the taskbar hidden
    (BORDERLESS, (0, 0, 1920, 1000), False),  # short of the bottom
    (BORDERLESS, (-10, -10, 1930, 1090), True),  # bigger than the monitor
])
def test_fullscreen_is_covering_the_monitor_with_no_title_bar(style, rect, expected):
    assert win_windows.is_fullscreen(style, rect, SCREEN) is expected


def test_a_window_on_the_other_monitor_isnt_covering_this_one():
    assert not win_windows.covers(SCREEN, SECOND)


def test_a_window_neither_focused_nor_fullscreen_isnt_reported():
    assert win_windows.as_report(row(style=TITLED, rect=(0, 0, 100, 100)), foreground=2) is None


def test_a_report_looks_like_kdes():
    report = win_windows.as_report(row(), foreground=1)
    assert report == {"pid": 5, "app": "game", "title": "Nice Name", "screen": "ID", "screen_name": "",
                      "fullscreen": True, "active": True, "use_title": True}


def test_a_focused_windowed_app_keeps_its_own_name():
    assert not win_windows.as_report(row(style=TITLED, rect=(0, 0, 9, 9)), foreground=1)["use_title"]


def test_a_fullscreen_browser_isnt_named_after_the_page():
    assert not win_windows.as_report(row(exe="chrome.exe"), foreground=1)["use_title"]


def test_a_process_windows_wont_name_goes_by_its_title():
    report = win_windows.as_report(row(exe=""), foreground=1)
    assert report["app"] == win_windows.UNKNOWN_APP and report["use_title"]


class Capture:
    def __init__(self, kind):
        self.name, self.kind = "Screen", kind


class Client:
    def __init__(self, settings):
        self.settings = settings

    def call(self, request, data):
        assert request == "GetInputSettings" and data == {"inputName": "Screen"}
        return {"inputSettings": self.settings}


def test_the_captured_monitor_is_the_captures_id():
    client = Client({"monitor_id": "\\\\?\\DISPLAY#X"})
    assert win_windows.captured_monitor(client, lambda c: Capture("monitor_capture")) == "\\\\?\\DISPLAY#X"


@pytest.mark.parametrize("found, settings", [(None, {}), (Capture("game_capture"), {}),
                                             (Capture("monitor_capture"), {}),
                                             (Capture("monitor_capture"), {"monitor_id": ""})])
def test_no_screen_capture_means_no_captured_monitor(found, settings):
    assert win_windows.captured_monitor(Client(settings), lambda c: found) is None


def reported(*screens):
    return [{"app": f"app{n}", "screen": screen} for n, screen in enumerate(screens)]


def watcher(find_screen, windows, clock=lambda: 0.0):
    return win_windows.WinWindows(logging.getLogger("test"), find_screen, clock=clock, windows=windows)


def test_only_the_captured_monitor_counts_whatever_the_case():
    seen = watcher(lambda: "\\\\?\\display#A", lambda: reported("\\\\?\\DISPLAY#A", "\\\\?\\DISPLAY#B"))
    assert [w["app"] for w in seen.on_captured_screen()] == ["app0"]


def test_a_monitor_can_be_captured_by_its_gdi_name():
    # What a real Windows 11 install's capture had saved as its monitor_id.
    windows = [{"app": "here", "screen": "\\?\DISPLAY#A", "screen_name": "\\.\DISPLAY1"},
               {"app": "there", "screen": "\\?\DISPLAY#B", "screen_name": "\\.\DISPLAY2"}]
    seen = watcher(lambda: "\\.\DISPLAY1", lambda: windows)
    assert [w["app"] for w in seen.on_captured_screen()] == ["here"]


def test_every_monitor_counts_when_the_captured_one_isnt_known():
    assert len(watcher(lambda: None, lambda: reported("A", "B")).on_captured_screen()) == 2


def test_obs_going_away_doesnt_stop_clip_names():
    def find():
        raise ConnectionError("gone")

    assert len(watcher(find, lambda: reported("A", "B")).on_captured_screen()) == 2


def test_failing_to_list_windows_is_said_once():
    messages = []

    class Log:
        warning = staticmethod(messages.append)

    def broken():
        raise OSError("no")

    seen = win_windows.WinWindows(Log, lambda: None, windows=broken)
    assert seen.on_captured_screen() == [] and seen.on_captured_screen() == []
    assert len(messages) == 1


def test_the_captured_monitor_isnt_asked_for_every_time():
    lookups, now = [], [0.0]
    seen = watcher(lambda: lookups.append(1), lambda: reported("A"), clock=lambda: now[0])
    seen.on_captured_screen()
    seen.on_captured_screen()
    assert len(lookups) == 1
    now[0] = win_windows.CAPTURED_SCREEN_CACHE_SECONDS
    seen.on_captured_screen()
    assert len(lookups) == 2
