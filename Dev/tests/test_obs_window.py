"""Show OBS: everything matched by OBS's PID, never by window title."""

import json
import logging
import sys

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import obs_window  # noqa: E402

log = logging.getLogger("test")
OBS_PID = 4242
BROWSER_PID = 777


class FakeTools:
    """Answers the busctl/wmctrl/xdotool calls obs_window makes, and records
    what it was asked to activate."""

    def __init__(self, sni_items=(), connection_pids=None, wmctrl_windows=None, kwin=False):
        self.sni_items = list(sni_items)
        self.connection_pids = connection_pids or {}
        self.wmctrl_windows = wmctrl_windows  # None: wmctrl isn't installed
        self.kwin = kwin
        self.kwin_scripts = []
        self.activated = []

    def run(self, cmd, timeout=5):
        if cmd[:2] == ["busctl", "--user"] and "RegisteredStatusNotifierItems" in cmd:
            return 0, json.dumps({"type": "v", "data": [{"type": "as", "data": self.sni_items}]})
        if "GetConnectionUnixProcessID" in cmd:
            pid = self.connection_pids.get(cmd[-1])
            return (0, f"u {pid}") if pid else (1, "")
        if "loadScript" in cmd:
            if not self.kwin:
                return None, ""
            with open(cmd[-1], encoding="utf-8") as f:
                self.kwin_scripts.append(f.read())
            return 0, "i 3"
        if "org.kde.kwin.Script" in cmd:
            return 0, ""
        if "unloadScript" in cmd:
            return 0, ""
        if "Activate" in cmd:
            self.activated.append(("sni", cmd[3]))
            return 0, ""
        if cmd[0] == "wmctrl":
            if self.wmctrl_windows is None:
                return None, ""
            if cmd[1] == "-lp":
                return 0, "\n".join(f"{wid}  0 {pid} host {title}" for wid, pid, title in self.wmctrl_windows)
            self.activated.append(("wmctrl", cmd[-1]))
            return 0, ""
        return None, ""  # xdotool: not available


@pytest.fixture
def tools(monkeypatch):
    def install(**kwargs):
        fake = FakeTools(**kwargs)
        monkeypatch.setattr(obs_window, "_run", fake.run)
        monkeypatch.setattr(obs_window.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
        monkeypatch.setattr(obs_window.obs_process, "find_obs_pids", lambda: [OBS_PID])
        return fake

    return install


def test_clicks_obs_own_tray_icon_found_by_pid(tools):
    fake = tools(sni_items=[f"org.kde.StatusNotifierItem-{BROWSER_PID}-1/StatusNotifierItem",
                            f"org.kde.StatusNotifierItem-{OBS_PID}-1/StatusNotifierItem"])
    assert obs_window.show_obs(log)
    assert fake.activated == [("sni", f"org.kde.StatusNotifierItem-{OBS_PID}-1")]


def test_tray_icon_on_a_unique_bus_name_is_resolved_via_dbus(tools):
    fake = tools(sni_items=[":1.87/org/ayatana/NotificationItem/obs", ":1.90/StatusNotifierItem"],
                 connection_pids={":1.87": BROWSER_PID, ":1.90": OBS_PID})
    assert obs_window.show_obs(log)
    assert fake.activated == [("sni", ":1.90")]


def test_never_raises_another_window_titled_obs(tools):
    """The bug: with no OBS tray icon, the old fallback raised any window
    titled "...OBS..." - here a browser tab."""
    fake = tools(wmctrl_windows=[("0x01", BROWSER_PID, "OBS Studio - Firefox")])
    assert not obs_window.show_obs(log)
    assert fake.activated == []


def test_x11_fallback_activates_obs_own_window(tools):
    fake = tools(wmctrl_windows=[("0x01", BROWSER_PID, "OBS docs - Firefox"), ("0x02", OBS_PID, "OBS 32.2.2")])
    assert obs_window.show_obs(log)
    assert fake.activated == [("wmctrl", "0x02")]


def test_obs_not_running(tools, monkeypatch):
    fake = tools(sni_items=[f"org.kde.StatusNotifierItem-{BROWSER_PID}-1/StatusNotifierItem"])
    monkeypatch.setattr(obs_window.obs_process, "find_obs_pids", lambda: [])
    assert not obs_window.show_obs(log)
    assert fake.activated == []


def test_a_visible_x11_window_is_raised_not_toggled_hidden(tools):
    """OBS treats a tray-icon click as show/hide toggle: clicking it while
    OBS's window is up would hide it."""
    fake = tools(sni_items=[f"org.kde.StatusNotifierItem-{OBS_PID}-1/StatusNotifierItem"],
                 wmctrl_windows=[("0x02", OBS_PID, "OBS 32.2.2")])
    assert obs_window.show_obs(log)
    assert fake.activated == [("wmctrl", "0x02")]


def test_kde_leaves_the_tray_click_to_the_kwin_script(tools, monkeypatch):
    """Only KWin can tell whether OBS's window is up, so the script itself
    clicks the tray icon, and only when there's no window."""
    monkeypatch.setattr(obs_window, "KWIN_SCRIPT_LINGER_SECONDS", 0)
    fake = tools(sni_items=[f"org.kde.StatusNotifierItem-{OBS_PID}-1/StatusNotifierItem"], kwin=True)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert obs_window.show_obs(log)
    assert fake.activated == []
    (script,) = fake.kwin_scripts
    assert f"[{OBS_PID}]" in script
    assert f'"org.kde.StatusNotifierItem-{OBS_PID}-1"' in script and "'Activate', 0, 0" in script
    assert script.index("if (found)") < script.index("callDBus")


# --- Show OBS / Hide OBS state ---------------------------------------------

def test_x11_state_follows_obs_own_window(tools, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    fake = tools(wmctrl_windows=[("0x01", BROWSER_PID, "OBS docs - Firefox")])
    state = obs_window.ObsWindowState(log)
    assert not state.refresh([OBS_PID])  # only a browser window titled OBS: still hidden
    fake.wmctrl_windows.append(("0x02", OBS_PID, "OBS 32.2.2"))
    assert state.refresh([OBS_PID]) and state.visible
    assert state.refresh([]) and not state.visible  # OBS closed


def test_wayland_without_kde_remembers_the_last_action(tools, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    tools()
    state = obs_window.ObsWindowState(log)
    state.remember(True)
    assert not state.refresh([OBS_PID]) and state.visible
    state.remember(False)
    assert not state.visible


def test_kde_loads_one_watch_script_per_obs_run(tools, monkeypatch):
    tools()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    calls = []

    def run(cmd, timeout=5):
        calls.append(cmd)
        if "loadScript" in cmd:
            return 0, "i 7"
        return 0, ""

    monkeypatch.setattr(obs_window, "_run", run)
    state = obs_window.ObsWindowState(log)
    state.refresh([OBS_PID], "org.freedesktop.StatusNotifierItem-1-1")
    state.refresh([OBS_PID], "org.freedesktop.StatusNotifierItem-1-1")
    assert sum("loadScript" in c for c in calls) == 1  # not reloaded every poll
    state.reported(True)
    assert state.visible
    state.refresh([OBS_PID + 1], "org.freedesktop.StatusNotifierItem-1-1")  # OBS restarted
    assert sum("loadScript" in c for c in calls) == 2
    state.stop()
    assert calls[-1][-1] == obs_window.KWIN_WATCH_SCRIPT_NAME and "unloadScript" in calls[-1]


def test_kwin_watch_script_reports_to_the_tray_for_obs_pids_only():
    script = obs_window.kwin_watch_script({OBS_PID}, "org.freedesktop.StatusNotifierItem-99-1")
    assert f"var pids = [{OBS_PID}]" in script
    assert '"org.freedesktop.StatusNotifierItem-99-1"' in script and "ObsWindowVisible" in script
    assert "caption" not in script  # never matched by window title


def test_hide_clicks_obs_own_tray_icon(tools):
    fake = tools(sni_items=[f"org.kde.StatusNotifierItem-{OBS_PID}-1/StatusNotifierItem"])
    assert obs_window.hide_obs(log)
    assert fake.activated == [("sni", f"org.kde.StatusNotifierItem-{OBS_PID}-1")]
