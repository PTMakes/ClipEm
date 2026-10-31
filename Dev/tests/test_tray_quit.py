"""What the tray does with an OBS it can't reach, as it gives up."""

import sys
import types

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
tray_widget = pytest.importorskip("tray_widget")


@pytest.fixture
def tray(monkeypatch):
    closed, notified = [], []
    app = types.SimpleNamespace(obs_window=types.SimpleNamespace(visible=False), refreshed=[],
                                obs_crash=types.SimpleNamespace(exited_cleanly=True))
    app._refresh_obs_window = lambda: app.refreshed.append(True)
    app._notify = notified.append
    monkeypatch.setattr(tray_widget, "OBS_WINDOW_REPORT_SECONDS", 0)
    monkeypatch.setattr(tray_widget.obs_process, "find_obs_pids", lambda: [42])
    monkeypatch.setattr(tray_widget, "close_obs_window", lambda client=None: closed.append(True))
    return app, closed, notified


def test_a_hidden_obs_is_closed(tray):
    app, closed, notified = tray
    tray_widget.TrayApp._close_unreachable_obs(app)
    assert closed == [True] and notified == []


def test_a_showing_obs_is_left_open_and_the_user_told(tray):
    app, closed, notified = tray
    app.obs_window.visible = True
    tray_widget.TrayApp._close_unreachable_obs(app)
    assert closed == [] and len(notified) == 1 and "still open" in notified[0]


def test_the_window_is_checked_again_after_kdes_script_has_answered(tray):
    app, closed, _notified = tray

    def script_answers():
        app.refreshed.append(True)
        app.obs_window.visible = len(app.refreshed) > 1

    app._refresh_obs_window = script_answers
    tray_widget.TrayApp._close_unreachable_obs(app)
    assert closed == []  # hidden as far as it knew at first


def test_nothing_happens_without_an_obs(tray, monkeypatch):
    app, closed, notified = tray
    monkeypatch.setattr(tray_widget.obs_process, "find_obs_pids", lambda: [])
    tray_widget.TrayApp._close_unreachable_obs(app)
    assert closed == [] and notified == [] and app.refreshed == []


def test_an_obs_that_never_got_going_is_reported(tray, monkeypatch):
    app, closed, notified = tray
    app.obs_crash.exited_cleanly = False
    monkeypatch.setattr(tray_widget.obs_process, "find_obs_pids", lambda: [])
    tray_widget.TrayApp._close_unreachable_obs(app)
    assert closed == [] and len(notified) == 1 and "OBS isn't running" in notified[0]


def test_quitting_from_the_menu_closes_a_showing_obs_too(monkeypatch):
    """Quit is a request to: only giving up asks first."""
    closed = []
    app = types.SimpleNamespace(client=None, connected=False, _quit=lambda icon, item: None)
    monkeypatch.setattr(tray_widget, "close_obs_window", lambda client=None: closed.append(client))
    tray_widget.TrayApp._quit_and_close_obs(app, None, None)
    assert closed == [None]
