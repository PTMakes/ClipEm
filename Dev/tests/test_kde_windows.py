"""What's on the captured screen, on KDE (tray-widget/kde_windows.py)."""

import json
import logging
import sys

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import kde_windows  # noqa: E402


def window(app="game", screen="DP-1", **changes):
    return {"pid": 1, "app": app, "title": "A title", "screen": screen, "fullscreen": False, "active": False,
            **changes}


def watcher(screen="DP-1", clock=lambda: 0.0):
    return kde_windows.KdeWindows(logging.getLogger("test"), clock=clock, find_screen=lambda: screen)


def test_only_the_captured_screen_counts():
    seen = watcher()
    seen.windows_changed(json.dumps([window("here"), window("there", screen="DP-2")]))
    assert [w["app"] for w in seen.on_captured_screen()] == ["here"]


def test_every_screen_counts_when_the_captured_one_isnt_known():
    seen = watcher(screen=None)
    seen.windows_changed(json.dumps([window("here"), window("there", screen="DP-2")]))
    assert len(seen.on_captured_screen()) == 2


def test_the_captured_screen_isnt_looked_up_every_time():
    lookups, now = [], [0.0]
    seen = kde_windows.KdeWindows(logging.getLogger("test"), clock=lambda: now[0],
                                  find_screen=lambda: lookups.append(1))
    seen.windows_changed(json.dumps([window()]))
    seen.on_captured_screen()
    seen.on_captured_screen()
    assert len(lookups) == 1
    now[0] = kde_windows.CAPTURED_SCREEN_CACHE_SECONDS
    seen.on_captured_screen()
    assert len(lookups) == 2


def test_no_windows_needs_no_lookup():
    seen = kde_windows.KdeWindows(logging.getLogger("test"), find_screen=lambda: 1 / 0)
    assert seen.on_captured_screen() == []


@pytest.mark.parametrize("text", ["not json", "{}", "[1, {}]", '[{"app": ""}]', '[{"app": 3}]'])
def test_a_bad_report_is_nothing(text):
    assert kde_windows.parse_windows(text) == []


def test_it_only_runs_on_kde(monkeypatch):
    monkeypatch.setattr(kde_windows.sys, "platform", "linux")
    loaded = []
    monkeypatch.setattr(kde_windows.obs_window, "load_kwin_script", lambda source, name: loaded.append(name) or "/x.js")
    monkeypatch.setattr(kde_windows.obs_window, "unload_kwin_script", lambda name, path=None: loaded.append(path))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    seen = watcher()
    seen.start("bus")
    assert loaded == []
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    seen = watcher()
    seen.start(None)  # the tray has no D-Bus name yet
    seen.start("bus")
    seen.start("bus")
    assert loaded == [kde_windows.KWIN_SCRIPT_NAME]
    seen.windows_changed(json.dumps([window()]))
    seen.stop()
    assert loaded[-1] == "/x.js" and seen.on_captured_screen() == []


def test_the_script_reports_to_the_tray():
    script = kde_windows.kwin_windows_script("org.freedesktop.StatusNotifierItem-9-1")
    assert '"org.freedesktop.StatusNotifierItem-9-1"' in script and "'WindowsChanged'" in script
    assert "fullScreen" in script and "windowActivated" in script and "w.output.name" in script
