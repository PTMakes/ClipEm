"""The tray's menu actions and its poll loop (tray_widget.TrayApp), against a
fake OBS - the icon itself and the checks it runs are tested on their own."""

import os
import subprocess
import sys
import types

import pytest
from conftest import TRAY_WIDGET_DIR

pytest.importorskip("PIL")
if sys.platform == "linux":
    pytest.importorskip("dbus_next")
else:
    pytest.importorskip("pystray")
sys.path.insert(0, TRAY_WIDGET_DIR)
import tray_widget  # noqa: E402
from obsws_client import ObsWebSocketConnectionLost, ObsWebSocketError  # noqa: E402
from tray_widget import TrayApp  # noqa: E402


class FakeObs:
    """obs-websocket: `answers` by request, or an exception to raise."""

    def __init__(self, **answers):
        self.calls = []
        self.answers = answers
        self.closed = False

    def call(self, request, data=None):
        self.calls.append((request, data) if data else request)
        answer = self.answers.get(request, {})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        self.closed = True


class Recorder:
    """Records every method called on it, in `calls` if one is given."""

    def __init__(self, calls=None, name=""):
        self.calls = [] if calls is None else calls
        self.name = name

    def __getattr__(self, method):
        return lambda *args, **kwargs: self.calls.append(f"{self.name}.{method}")


@pytest.fixture
def app():
    """A TrayApp connected to OBS with the replay buffer on, without what
    __init__ starts."""
    tray = TrayApp.__new__(TrayApp)
    tray.client = FakeObs()
    tray.connected = True
    tray.replay_active = True
    tray.recording_active = False
    tray._children = []
    tray._stopping_children = False
    tray.notes = []
    tray.steps = []
    tray.icon = types.SimpleNamespace(icon=None, title="", notify=lambda message, _title: tray.notes.append(message),
                                      update_menu=lambda: tray.steps.append("update_menu"),
                                      stop=lambda: tray.steps.append("icon.stop"))
    for name in ("obs_plugin", "app_recorder", "stuck_screen", "app_tracks", "mic_check", "obs_crash", "disk_space",
                 "input_activity", "kde_windows", "clip_folder"):
        setattr(tray, name, Recorder(tray.steps, name))
    tray.clip_folder.missing_dir = None
    tray.clip_folder.tick = lambda *_args: None
    tray.obs_plugin.present = True
    tray.obs_window = Recorder(tray.steps, "obs_window")
    tray.obs_crash.exited_cleanly = False
    return tray


@pytest.fixture
def post_clip_off(monkeypatch):
    monkeypatch.setattr(tray_widget.clip_post, "load_settings", lambda: {"enabled": False, "seconds": 10})
    monkeypatch.setattr(tray_widget, "load_run_config", lambda: {})


def test_save_clip_asks_obs_to_save(app, post_clip_off):
    app._save_clip(None, None)
    assert app.client.calls == ["SaveReplayBuffer"] and app.notes == ["Clip saved."]


def test_with_post_clip_on_save_clip_presses_its_hotkey(app, post_clip_off, monkeypatch):
    monkeypatch.setattr(tray_widget.clip_post, "load_settings", lambda: {"enabled": True, "seconds": 7})
    app._save_clip(None, None)
    assert app.client.calls == [("TriggerHotkeyByName", {"hotkeyName": tray_widget.POST_CLIP_HOTKEY})]
    assert app.notes == ["Saving the clip in 7 s."]


def test_the_post_clip_hotkey_name_matches_the_obs_script():
    with open(os.path.join(os.path.dirname(tray_widget.__file__), "..", "Scripts", "post_clip.py"), encoding="utf-8") as f:
        assert f'HOTKEY_NAME = "{tray_widget.POST_CLIP_HOTKEY}"' in f.read()


def test_the_saved_notification_can_be_turned_off(app, post_clip_off, monkeypatch):
    monkeypatch.setattr(tray_widget, "load_run_config", lambda: {"clip_saved_notification": False})
    app._save_clip(None, None)
    assert app.client.calls == ["SaveReplayBuffer"] and app.notes == []


def test_a_failed_save_is_always_shown(app, post_clip_off, monkeypatch):
    monkeypatch.setattr(tray_widget, "load_run_config", lambda: {"clip_saved_notification": False})
    app.client = FakeObs(SaveReplayBuffer=ObsWebSocketError("not active"))
    app._save_clip(None, None)
    assert app.notes == ["Failed to save clip: not active"]


@pytest.mark.parametrize("change", [{"connected": False}, {"client": None}, {"replay_active": False}])
def test_save_clip_needs_a_running_replay_buffer(app, post_clip_off, change):
    client = app.client
    for name, value in change.items():
        setattr(app, name, value)
    app._save_clip(None, None)
    assert client.calls == [] and app.notes == []


@pytest.mark.parametrize("active, request_", [(True, "StopReplayBuffer"), (False, "StartReplayBuffer")])
def test_the_replay_buffer_toggles(app, active, request_):
    app.replay_active = active
    app._toggle_replay_buffer(None, None)
    assert app.client.calls == [request_]


@pytest.mark.parametrize("active, request_", [(True, "StopRecord"), (False, "StartRecord")])
def test_recording_toggles(app, active, request_):
    app.recording_active = active
    app._toggle_recording(None, None)
    assert app.client.calls == [request_]


@pytest.mark.parametrize("toggle", ["_toggle_replay_buffer", "_toggle_recording"])
def test_toggling_while_disconnected_does_nothing(app, toggle):
    app.connected = False
    getattr(app, toggle)(None, None)
    assert app.client.calls == []
    app.connected, app.client = True, None
    getattr(app, toggle)(None, None)


@pytest.mark.parametrize("toggle", ["_toggle_replay_buffer", "_toggle_recording"])
def test_a_refused_toggle_isnt_fatal(app, toggle):
    error = ObsWebSocketError("no")
    app.client = FakeObs(StartReplayBuffer=error, StartRecord=error)
    app.replay_active = False
    getattr(app, toggle)(None, None)


def test_labels_follow_the_state(app):
    assert app._replay_label(None) == "Stop Replay Buffer" and app._recording_label(None) == "Start Recording"
    app.replay_active, app.recording_active = False, True
    assert app._replay_label(None) == "Start Replay Buffer" and app._recording_label(None) == "Stop Recording"
    app.connected = False
    assert "disconnected" in app._replay_label(None) and "disconnected" in app._recording_label(None)
    assert app._can_save_clip(None) is False


def test_save_clips_label_has_the_hotkey(app, monkeypatch):
    monkeypatch.setattr(tray_widget.obs_config, "load_clip_hotkey", lambda: (True, False, False, "F9"))
    monkeypatch.setattr(tray_widget.obs_config, "describe_hotkey", lambda *_k: "Ctrl+F9")
    assert app._save_clip_label(None) == "Save Clip (Ctrl+F9)"
    monkeypatch.setattr(tray_widget.obs_config, "load_clip_hotkey",
                        lambda: (False, False, False, tray_widget.obs_config.NO_HOTKEY))
    assert app._save_clip_label(None) == "Save Clip"

    def broken():
        raise ValueError("broken config")

    monkeypatch.setattr(tray_widget.obs_config, "load_clip_hotkey", broken)
    assert app._save_clip_label(None) == "Save Clip"


def test_a_hidden_recording_item_comes_back_while_recording(app, monkeypatch):
    monkeypatch.setattr(tray_widget, "load_run_config", lambda: {"show_recording_item": False})
    assert not app._recording_shown(None)
    app.recording_active = True
    assert app._recording_shown(None)


def test_a_notification_that_fails_is_only_logged(app):
    def fail(_message, _title):
        raise RuntimeError("no notification service")

    app.icon.notify = fail
    app._notify("hello")


def test_being_disconnected_resets_everything(app, monkeypatch):
    monkeypatch.setattr(tray_widget, "app_icon", lambda dim=False, active=False: ("dim", dim))
    client = app.client
    app.recording_active = True
    app._mark_disconnected("OBS closed")
    assert not app.connected and not app.replay_active and not app.recording_active
    assert client.closed and app.icon.icon == ("dim", True) and app.icon.title == "ClipEm: disconnected"
    assert "obs_plugin.forget" in app.steps and "update_menu" in app.steps


def test_connecting_listens_and_forgets_the_last_connection(app, monkeypatch):
    client = FakeObs()
    client.on_event = lambda event, callback: app.steps.append(("on", event))
    connected = []
    monkeypatch.setattr(tray_widget.obs_config, "load_obs_websocket_settings", lambda: {"port": 1234, "password": "pw"})
    monkeypatch.setattr(tray_widget, "connect_with_retry",
                        lambda url, password, **kwargs: connected.append((url, password, kwargs)) or client)
    app.connected = False
    app.clip_finisher = types.SimpleNamespace(on_replay_saved=None)
    app._connect()
    assert app.connected and app.client is client
    url, password, kwargs = connected[0]
    assert (url, password) == ("ws://127.0.0.1:1234", "pw")
    assert kwargs["timeout_seconds"] == tray_widget.MAX_DISCONNECTED_SECONDS
    assert ("on", "ReplayBufferSaved") in app.steps and ("on", "ExitStarted") in app.steps
    for step in ("obs_crash.connected", "obs_plugin.attach", "stuck_screen.forget", "app_tracks.forget",
                 "clip_folder.forget", "mic_check.forget"):
        assert step in app.steps


class Stop(BaseException):
    """Ends the poll loop: what its sleep raises."""


def _one_round(app, monkeypatch):
    monkeypatch.setattr(tray_widget, "app_icon", lambda dim=False, active=False: ("active", active))
    monkeypatch.setattr(tray_widget, "use_dark_menus", lambda: None)
    monkeypatch.setattr(app, "_open_requested_settings", lambda: app.steps.append("requested_settings"))
    monkeypatch.setattr(app, "_refresh_obs_window", lambda: app.steps.append("refresh_window"))

    def stop(_seconds):
        raise Stop

    monkeypatch.setattr(tray_widget.time, "sleep", stop)
    with pytest.raises(Stop):
        app._poll_loop()


def test_a_poll_shows_what_obs_is_doing(app, monkeypatch):
    app.client = FakeObs(GetReplayBufferStatus={"outputActive": True},
                         GetRecordStatus={"outputActive": True, "outputBytes": 2048})
    monkeypatch.setattr(tray_widget, "format_size", lambda size: f"{size} B")
    _one_round(app, monkeypatch)
    assert app.replay_active and app.recording_active
    assert app.icon.title == "ClipEm: Replay Buffer recording, Recording recording (2048 B)"
    assert app.icon.icon == ("active", True)
    for step in ("stuck_screen.tick", "disk_space.tick", "mic_check.tick", "obs_plugin.tick", "app_tracks.tick"):
        assert step in app.steps


def test_a_poll_of_an_idle_obs(app, monkeypatch):
    app.client = FakeObs(GetReplayBufferStatus={"outputActive": False}, GetRecordStatus={"outputActive": False})
    _one_round(app, monkeypatch)
    assert not app.replay_active and not app.recording_active
    assert app.icon.title == "ClipEm: Replay Buffer idle, Recording idle" and app.icon.icon == ("active", False)


def test_a_lost_connection_reconnects_next_round(app, monkeypatch):
    client = app.client = FakeObs(GetReplayBufferStatus=ObsWebSocketConnectionLost("gone"))
    _one_round(app, monkeypatch)
    assert not app.connected and client.closed


def test_a_refused_request_keeps_the_connection(app, monkeypatch):
    client = app.client = FakeObs(GetReplayBufferStatus=ObsWebSocketError("busy"))
    _one_round(app, monkeypatch)
    assert app.connected and not client.closed


def test_a_bug_in_a_check_counts_as_disconnected_not_a_dead_thread(app, monkeypatch):
    def bug(*_args):
        raise KeyError("bug")

    app.mic_check.tick = bug
    _one_round(app, monkeypatch)
    assert not app.connected


def test_a_clip_folder_going_missing_updates_the_menu(app, monkeypatch):
    def tick(_client, _active):
        app.clip_folder.missing_dir = "/gone"

    app.clip_folder.tick = tick
    _one_round(app, monkeypatch)
    assert app.steps.count("update_menu") == 2


def test_obs_that_cant_be_reached_ends_the_tray(app, monkeypatch):
    app.connected = False

    def fail():
        raise ObsWebSocketError("gave up")

    monkeypatch.setattr(app, "_connect", fail)
    monkeypatch.setattr(app, "_close_unreachable_obs", lambda: app.steps.append("close_unreachable"))
    app._poll_loop()  # returns: nothing left to poll
    assert app.steps.index("close_unreachable") < app.steps.index("icon.stop")


def test_shutting_down_stops_everything(app):
    client = app.client
    app._shutdown("reason")
    assert client.closed and "app_recorder.stop" in app.steps and "icon.stop" in app.steps


@pytest.fixture
def quitting(app, monkeypatch):
    monkeypatch.setattr(tray_widget, "stop_clip_editor", lambda log, timeout=3: False)
    icon = types.SimpleNamespace(stop=lambda: app.steps.append("stopped"))
    return app, icon


def test_quitting_clipem_only_says_goodbye_to_obs(quitting):
    app, icon = quitting
    client = app.client
    app._quit_leaving_obs(icon, None)
    assert "obs_plugin.goodbye" in app.steps and client.closed and app.steps[-1] == "stopped"


def test_quitting_while_disconnected_has_nobody_to_tell(quitting):
    app, icon = quitting
    app.connected = False
    app._quit_leaving_obs(icon, None)
    assert "obs_plugin.goodbye" not in app.steps and app.steps[-1] == "stopped"


@pytest.mark.parametrize("connected", [True, False])
def test_quitting_with_obs_closes_it_through_a_live_connection_only(quitting, monkeypatch, connected):
    app, icon = quitting
    client = app.client
    app.connected = connected
    closed = []
    monkeypatch.setattr(tray_widget, "close_obs_window", closed.append)
    app._quit_and_close_obs(icon, None)
    assert closed == [client if connected else None] and app.steps[-1] == "stopped"


def test_obs_that_wont_close_is_ended_so_none_stays_hidden(monkeypatch):
    killed = []
    monkeypatch.setattr(tray_widget.obs_process, "close_obs", lambda log, client, timeout_seconds: False)
    monkeypatch.setattr(tray_widget.obs_process, "kill_obs", killed.append)
    tray_widget.close_obs_window()
    assert killed == [tray_widget.log]


def test_closing_obs_never_raises(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(tray_widget.obs_process, "close_obs", fail)
    tray_widget.close_obs_window()


def _restart(quitting, monkeypatch, closed=True, editor=False, start=None):
    app, icon = quitting
    order = []
    monkeypatch.setattr(tray_widget.obs_process, "close_obs",
                        closed if callable(closed) else lambda log, client, timeout_seconds: closed)
    monkeypatch.setattr(tray_widget, "stop_clip_editor", lambda log, timeout=3: order.append("editor") or editor)
    monkeypatch.setattr(tray_widget, "release_instance_lock", lambda path: order.append("release_lock"))
    monkeypatch.setattr(tray_widget, "start_clipem", start or (lambda args=(): order.append(("start", args))))
    app._restart_clipem_and_obs(icon, None)
    return order


def test_restarting_hands_the_lock_over_before_starting_the_new_tray(quitting, monkeypatch):
    order = _restart(quitting, monkeypatch)
    assert order[:3] == ["editor", "release_lock", ("start", [])] and quitting[0].steps[-1] == "stopped"


def test_restarting_reopens_the_editor_it_closed(quitting, monkeypatch):
    order = _restart(quitting, monkeypatch, editor=True)
    assert ("start", [tray_widget.REOPEN_EDITOR_FLAG]) in order


def test_nothing_restarts_if_obs_wont_close(quitting, monkeypatch):
    order = _restart(quitting, monkeypatch, closed=False)
    app, _icon = quitting
    assert order == [] and "stopped" not in app.steps and "nothing was restarted" in app.notes[0]


def test_failing_to_close_obs_is_the_same_as_it_not_closing(quitting, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("boom")

    assert _restart(quitting, monkeypatch, closed=fail) == []


def test_a_restart_that_cant_start_clipem_says_so(quitting, monkeypatch):
    def fail(_args=()):
        raise OSError("no python")

    _restart(quitting, monkeypatch, start=fail)
    app, _icon = quitting
    assert "use its shortcut" in app.notes[0] and app.steps[-1] == "stopped"


def _child(running=True, stubborn=False):
    child = Recorder()
    child.poll = lambda: None if running else 0
    if stubborn:
        def wait(timeout):
            raise subprocess.TimeoutExpired("x", timeout)

        child.wait = wait
    else:
        child.wait = lambda timeout: 0
    return child


def test_helpers_are_closed_and_stubborn_ones_killed(app, monkeypatch):
    monkeypatch.setattr(tray_widget, "stop_clip_editor", lambda log, timeout=3: False)
    polite, stubborn, finished = _child(), _child(stubborn=True), _child(running=False)
    app._children = [polite, stubborn, finished]
    app.stop_children(timeout_seconds=0)
    assert polite.calls == [".terminate"] and stubborn.calls == [".terminate", ".kill"] and finished.calls == []
    assert app._children == [] and app._stopping_children


def test_a_helper_that_cant_be_stopped_doesnt_stop_the_rest(app, monkeypatch):
    monkeypatch.setattr(tray_widget, "stop_clip_editor", lambda log, timeout=3: False)
    gone, other = _child(), _child()

    def already_gone():
        raise ProcessLookupError("gone")

    gone.terminate = already_gone
    app._children = [gone, other]
    app.stop_children(timeout_seconds=0)
    assert other.calls == [".terminate"]


def test_a_helper_that_crashes_is_reported_with_its_stderr(app, tmp_path):
    stderr = tmp_path / "settings.stderr.log"
    stderr.write_bytes(b"x" * 100_000 + b"Traceback: the real error")
    app._watch_helper(types.SimpleNamespace(wait=lambda: 1), "settings", str(stderr))
    assert app.notes == ["Settings closed with an error - see logs/tray.log."] and "update_menu" in app.steps


def test_a_helper_that_closes_normally_only_refreshes_the_menu(app, tmp_path):
    app._watch_helper(types.SimpleNamespace(wait=lambda: 0), "settings", str(tmp_path / "gone.log"))
    assert app.notes == [] and app.steps == ["update_menu"]


def test_helpers_being_stopped_arent_crashes(app, tmp_path):
    app._stopping_children = True
    app._watch_helper(types.SimpleNamespace(wait=lambda: -15), "settings", str(tmp_path / "gone.log"))
    assert app.notes == [] and app.steps == []


def test_a_helper_crash_without_a_log_is_still_reported(app, tmp_path):
    app._watch_helper(types.SimpleNamespace(wait=lambda: 1), "settings", str(tmp_path / "gone.log"))
    assert app.notes


def test_a_helper_that_wont_start_is_reported(app, monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise OSError("no python")

    monkeypatch.setattr(tray_widget, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(tray_widget.subprocess, "Popen", fail)
    app._start_helper("/x/settings_dialog.py", "settings")
    assert app.notes == ["Couldn't open settings - see logs/tray.log."] and app._children == []


def test_a_request_from_the_update_window_opens_settings_once(app, monkeypatch, tmp_path):
    request = tmp_path / ".settings.request"
    request.write_text("video,audio\n")
    opened = []
    monkeypatch.setattr(tray_widget, "SETTINGS_REQUEST_PATH", str(request))
    monkeypatch.setattr(app, "_start_helper", lambda script, what, args=(): opened.append(args))
    app._open_requested_settings()
    app._open_requested_settings()
    assert opened == [["--highlight", "video,audio"]] and not request.exists()


def test_giving_up_on_obs_with_none_running_says_so(app, monkeypatch):
    monkeypatch.setattr(tray_widget.obs_process, "find_obs_pids", lambda: [])
    app._close_unreachable_obs()
    assert "OBS isn't running" in app.notes[0]
    app.notes.clear()
    app.obs_crash.exited_cleanly = True
    app._close_unreachable_obs()
    assert app.notes == []


@pytest.mark.parametrize("visible, closed", [(True, False), (False, True)])
def test_giving_up_only_closes_an_obs_nobody_can_see(app, monkeypatch, visible, closed):
    """A hidden one has nothing to quit it from; a window showing is someone's."""
    shut = []
    app.obs_window.visible = visible
    monkeypatch.setattr(tray_widget.obs_process, "find_obs_pids", lambda: [1])
    monkeypatch.setattr(tray_widget, "close_obs_window", lambda: shut.append(True))
    monkeypatch.setattr(tray_widget.time, "sleep", lambda _s: None)
    monkeypatch.setattr(app, "_refresh_obs_window", lambda: None)
    app._close_unreachable_obs()
    assert bool(shut) is closed and bool(app.notes) is visible


@pytest.mark.parametrize("check, name", [
    ("_stuck_check_enabled", "load_stuck_check_enabled"),
    ("_black_check_enabled", "load_black_check_enabled"),
    ("_disk_space_warnings_enabled", "load_disk_space_warnings_enabled"),
    ("_mic_check_enabled", "load_mic_check_enabled"),
])
def test_a_setting_that_cant_be_read_leaves_its_check_on(monkeypatch, check, name):
    monkeypatch.setattr(tray_widget.obs_config, name, lambda: False)
    assert getattr(TrayApp, check)() is False

    def broken():
        raise ValueError("broken config")

    monkeypatch.setattr(tray_widget.obs_config, name, broken)
    assert getattr(TrayApp, check)() is True


def test_a_missing_tray_host_ends_the_tray(app, monkeypatch):
    monkeypatch.setattr(tray_widget.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tray_widget, "tray_host_available", lambda: False)
    app._check_tray_available()
    assert "icon.stop" in app.steps


@pytest.mark.parametrize("reply, code, available", [
    ("boolean true", 0, True), ("boolean false", 0, False), ("boolean false", 1, True), ("", 0, True),
])
def test_the_tray_host_check_only_says_no_when_its_sure(monkeypatch, reply, code, available):
    monkeypatch.setattr(tray_widget.sys, "platform", "linux")
    monkeypatch.setattr(tray_widget.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, code, stdout=reply))
    assert tray_widget.tray_host_available() is available


def test_the_tray_host_check_gives_the_benefit_of_the_doubt(monkeypatch):
    def fail(*_args, **_kwargs):
        raise FileNotFoundError("dbus-send")

    monkeypatch.setattr(tray_widget.sys, "platform", "linux")
    monkeypatch.setattr(tray_widget.subprocess, "run", fail)
    assert tray_widget.tray_host_available() is True
    monkeypatch.setattr(tray_widget.sys, "platform", "win32")
    assert tray_widget.tray_host_available() is True


def test_a_second_tray_exits_at_once(monkeypatch):
    monkeypatch.setattr(tray_widget, "acquire_instance_lock", lambda path: False)
    with pytest.raises(SystemExit) as exc:
        tray_widget.ensure_single_instance()
    assert exc.value.code == 0
    monkeypatch.setattr(tray_widget, "acquire_instance_lock", lambda path: True)
    tray_widget.ensure_single_instance()


def test_the_icon_dims_and_glows():
    plain, dim, active = (tray_widget.app_icon(dim=d, active=a) for d, a in ((False, False), (True, False), (False, True)))
    assert plain.tobytes() != dim.tobytes() and plain.tobytes() != active.tobytes()
