import logging
import os
import shutil
import subprocess
import sys
import types

import obs_process
import pytest
from conftest import TRAY_WIDGET_DIR

log = logging.getLogger("test")


class FakeObsWebSocket:
    """Answers Get*Status / Stop* like obs-websocket: outputs start active
    and go inactive once stopped."""

    def __init__(self, replay_active=True, record_active=False):
        self.active = {"ReplayBuffer": replay_active, "Record": record_active}
        self.calls = []

    def call(self, request_type):
        self.calls.append(request_type)
        output = request_type.replace("Get", "").replace("Stop", "").replace("Status", "")
        if request_type.startswith("Stop"):
            self.active[output] = False
            return {}
        return {"outputActive": self.active[output]}


def test_close_obs_stops_outputs_then_closes(monkeypatch):
    running = [True]
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [1234] if running[0] else [])

    def request_close(_log):
        running[0] = False
        return True

    monkeypatch.setattr(obs_process, "request_obs_close", request_close)
    client = FakeObsWebSocket(replay_active=True, record_active=True)

    assert obs_process.close_obs(log, client=client, timeout_seconds=1)
    assert "StopReplayBuffer" in client.calls and "StopRecord" in client.calls
    assert client.active == {"ReplayBuffer": False, "Record": False}


def test_close_obs_only_stops_active_outputs(monkeypatch):
    running = [True]
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [1234] if running[0] else [])
    monkeypatch.setattr(obs_process, "request_obs_close", lambda _log: running.__setitem__(0, False) or True)
    client = FakeObsWebSocket(replay_active=True, record_active=False)
    obs_process.close_obs(log, client=client, timeout_seconds=1)
    assert "StopRecord" not in client.calls


def test_close_obs_reports_obs_that_wont_exit(monkeypatch):
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [1234])  # never exits
    monkeypatch.setattr(obs_process, "request_obs_close", lambda _log: True)
    assert not obs_process.close_obs(log, timeout_seconds=0.3)


def test_close_obs_when_not_running_does_nothing(monkeypatch):
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [])
    monkeypatch.setattr(obs_process, "request_obs_close", lambda _log: pytest.fail("nothing to close"))
    monkeypatch.setattr(obs_process, "connect_to_obs", lambda _log: pytest.fail("nothing to connect to"))
    assert obs_process.close_obs(log)
    assert obs_process.close_obs_stopping_outputs(log)


def test_close_obs_stopping_outputs_closes_its_connection(monkeypatch):
    running = [True]
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [1234] if running[0] else [])
    monkeypatch.setattr(obs_process, "request_obs_close", lambda _log: running.__setitem__(0, False) or True)
    client = FakeObsWebSocket()
    client.closed = False
    client.close = lambda: setattr(client, "closed", True)
    monkeypatch.setattr(obs_process, "connect_to_obs", lambda _log: client)

    assert obs_process.close_obs_stopping_outputs(log, timeout_seconds=1)
    assert client.closed


@pytest.mark.skipif(sys.platform != "linux", reason="Linux sends SIGTERM to matching /proc entries")
def test_linux_close_request_signals_our_pids(monkeypatch):
    killed = []
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [111, 222])
    monkeypatch.setattr(obs_process.os, "kill", lambda pid, sig: killed.append(pid))
    assert obs_process.request_obs_close(log)
    assert killed == [111, 222]


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc/<pid>/exe")
def test_find_obs_pids_matches_a_real_process_inside_this_install(tmp_path, monkeypatch):
    """A real process whose executable is App/Linux/obs-install/bin/obs
    inside the install counts; the same binary elsewhere doesn't."""
    sleep = shutil.which("sleep")
    ours = tmp_path / "ClipEm" / "App" / "Linux" / "obs-install" / "bin" / "obs"
    theirs = tmp_path / "elsewhere" / "obs"
    for path in (ours, theirs):
        path.parent.mkdir(parents=True)
        shutil.copy(sleep, path)
    monkeypatch.setattr(obs_process, "ROOT_DIR", str(tmp_path / "ClipEm"))

    procs = [subprocess.Popen([str(p), "30"]) for p in (ours, theirs)]
    try:
        found = obs_process.find_obs_pids()
        assert procs[0].pid in found
        assert procs[1].pid not in found
        assert obs_process.obs_running()
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()
    assert procs[0].pid not in obs_process.find_obs_pids()


def test_is_ours_rejects_non_obs_executables_inside_the_install(tmp_path, monkeypatch):
    monkeypatch.setattr(obs_process, "ROOT_DIR", str(tmp_path))
    assert not obs_process._is_ours(str(tmp_path / "App" / "Linux" / "python" / "bin" / "python3"))
    assert not obs_process._is_ours(None)
    assert obs_process._is_ours(os.path.join(str(tmp_path), "App", "Windows", "bin", "64bit", "obs64.exe"))


def test_is_ours_isnt_fooled_by_a_folder_with_the_same_start(tmp_path, monkeypatch):
    monkeypatch.setattr(obs_process, "ROOT_DIR", str(tmp_path / "ClipEm"))
    assert not obs_process._is_ours(str(tmp_path / "ClipEm-old" / "App" / "obs"))
    assert obs_process._is_ours(str(tmp_path / "ClipEm" / "App" / "obs"))


def test_is_ours_knows_the_macos_app(tmp_path, monkeypatch):
    monkeypatch.setattr(obs_process, "ROOT_DIR", str(tmp_path))
    assert obs_process._is_ours(str(tmp_path / "App" / "OBS.app" / "Contents" / "MacOS" / "OBS"))
    assert not obs_process._is_ours(str(tmp_path / "App" / "Other.app" / "Contents" / "MacOS" / "Other"))


@pytest.mark.usefixtures("links")
def test_is_ours_follows_links_out_of_the_install(tmp_path, monkeypatch):
    theirs = tmp_path / "system" / "obs"
    theirs.parent.mkdir()
    theirs.write_text("")
    ours = tmp_path / "ClipEm" / "obs"
    ours.parent.mkdir()
    ours.symlink_to(theirs)
    monkeypatch.setattr(obs_process, "ROOT_DIR", str(tmp_path / "ClipEm"))
    assert not obs_process._is_ours(str(ours))


def test_macos_finds_obs_from_ps(tmp_path, monkeypatch):
    monkeypatch.setattr(obs_process, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(obs_process.sys, "platform", "darwin")
    ours = tmp_path / "App" / "OBS.app" / "Contents" / "MacOS" / "OBS"
    out = f"  12 {ours}\n  34 /Applications/OBS.app/Contents/MacOS/OBS\nnot a pid line\n"
    monkeypatch.setattr(obs_process.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out))
    assert obs_process.find_obs_pids() == [12]


def test_a_failing_process_list_is_no_obs(monkeypatch):
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ps", 5)

    monkeypatch.setattr(obs_process.sys, "platform", "darwin")
    monkeypatch.setattr(obs_process.subprocess, "run", fail)
    assert obs_process.find_obs_pids() == []
    assert obs_process.obs_running() is False


def test_nothing_running_means_no_close_request(monkeypatch):
    monkeypatch.setattr(obs_process.sys, "platform", "linux")
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [])
    monkeypatch.setattr(obs_process.os, "kill", lambda pid, sig: pytest.fail("nothing to signal"))
    assert obs_process.request_obs_close(log) is False


def test_a_close_request_never_raises(monkeypatch):
    def refuse(pid, sig):
        raise PermissionError("not allowed")

    monkeypatch.setattr(obs_process.sys, "platform", "linux")
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [111])
    monkeypatch.setattr(obs_process.os, "kill", refuse)
    assert obs_process.request_obs_close(log) is False


def test_linux_closes_with_sigterm_never_sigkill(monkeypatch):
    sent = []
    monkeypatch.setattr(obs_process.sys, "platform", "linux")
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [111])
    monkeypatch.setattr(obs_process.os, "kill", lambda pid, sig: sent.append(sig))
    assert obs_process.request_obs_close(log) is True
    assert sent == [obs_process.signal.SIGTERM]


def test_macos_asks_obs_to_quit(monkeypatch):
    ran = []
    monkeypatch.setattr(obs_process.sys, "platform", "darwin")
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [])
    monkeypatch.setattr(obs_process.subprocess, "run", lambda cmd, timeout: ran.append(cmd))
    assert obs_process.request_obs_close(log) is True
    assert ran == [["osascript", "-e", 'tell application "OBS" to quit']]


def _fake_windows(monkeypatch, match):
    import ctypes

    posted = []
    user32 = types.SimpleNamespace(PostMessageW=lambda *args: posted.append(args))
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(obs_process.sys, "platform", "win32")
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [111])
    monkeypatch.setattr(obs_process, "_find_main_window_win32", lambda pids: match)
    return posted


def test_windows_closes_obs_with_wm_close(monkeypatch):
    posted = _fake_windows(monkeypatch, (77, 111, "OBS 32.2.2 - Profile"))
    assert obs_process.request_obs_close(log) is True
    assert posted == [(77, 0x0010, 0, 0)]


def test_windows_needs_a_window_to_close(monkeypatch):
    posted = _fake_windows(monkeypatch, None)
    assert obs_process.request_obs_close(log) is False
    assert posted == []


def test_waiting_for_obs_sees_it_go(monkeypatch):
    answers = iter([[1], [1], []])
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: next(answers))
    monkeypatch.setattr(obs_process.time, "sleep", lambda _s: None)
    assert obs_process.wait_for_obs_exit(60) is True


def test_a_refused_close_isnt_waited_for(monkeypatch):
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [1234])
    monkeypatch.setattr(obs_process, "request_obs_close", lambda _log: False)
    monkeypatch.setattr(obs_process, "wait_for_obs_exit", lambda _t: pytest.fail("no close was sent"))
    assert obs_process.close_obs(log) is False


def test_stopping_outputs_gives_up_on_an_output_that_stays(monkeypatch):
    class Stuck(FakeObsWebSocket):
        def call(self, request_type):
            super().call(request_type)
            return {"outputActive": True}

    warnings = []
    monkeypatch.setattr(log, "warning", warnings.append)
    monkeypatch.setattr(obs_process.time, "sleep", lambda _s: None)
    obs_process.stop_obs_outputs(Stuck(), log, timeout_seconds=0.01)
    assert "still active" in warnings[0]


def test_stopping_outputs_never_raises(monkeypatch):
    class Gone:
        def call(self, _request_type):
            raise ConnectionResetError("OBS went away")

    warnings = []
    monkeypatch.setattr(log, "warning", warnings.append)
    obs_process.stop_obs_outputs(Gone(), log)
    assert "closing anyway" in warnings[0]


def test_outputs_are_waited_for_after_being_stopped(monkeypatch):
    class Slow(FakeObsWebSocket):
        polls = 0

        def call(self, request_type):
            if request_type == "GetRecordStatus" and "StopReplayBuffer" in self.calls:
                Slow.polls += 1
                self.active["Record"] = Slow.polls < 3
            return super().call(request_type)

    monkeypatch.setattr(obs_process.time, "sleep", lambda _s: None)
    client = Slow(replay_active=True, record_active=False)
    obs_process.stop_obs_outputs(client, log)
    assert Slow.polls == 3


def _websocket_settings(monkeypatch):
    import obs_config

    monkeypatch.setattr(obs_config, "load_obs_websocket_settings", lambda: {"port": 4455, "password": "pw"})
    monkeypatch.setattr(sys, "path", list(sys.path))  # connect_to_obs adds to it


def test_connecting_uses_the_settings_obs_started_with(monkeypatch):
    _websocket_settings(monkeypatch)
    made = []

    class Client:
        def __init__(self, url, password):
            made.append((url, password))

        def connect(self, timeout):
            made.append(timeout)

    monkeypatch.setitem(sys.modules, "obsws_client", types.SimpleNamespace(ObsWebSocketClient=Client))
    assert isinstance(obs_process.connect_to_obs(log, timeout=3), Client)
    assert made == [("ws://127.0.0.1:4455", "pw"), 3]


def test_not_being_able_to_connect_is_no_client(monkeypatch):
    _websocket_settings(monkeypatch)

    class Client:
        def __init__(self, url, password):
            pass

        def connect(self, timeout):
            raise ConnectionRefusedError("no OBS")

    monkeypatch.setitem(sys.modules, "obsws_client", types.SimpleNamespace(ObsWebSocketClient=Client))
    assert obs_process.connect_to_obs(log) is None


def test_no_client_still_closes_obs(monkeypatch):
    running = [True]
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [1234] if running[0] else [])
    monkeypatch.setattr(obs_process, "request_obs_close", lambda _log: running.__setitem__(0, False) or True)
    monkeypatch.setattr(obs_process, "connect_to_obs", lambda _log: None)
    assert obs_process.close_obs_stopping_outputs(log, timeout_seconds=1) is True


def test_the_connection_is_closed_even_if_closing_obs_fails(monkeypatch):
    client = FakeObsWebSocket()
    client.closed = False
    client.close = lambda: setattr(client, "closed", True)

    def fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [1234])
    monkeypatch.setattr(obs_process, "connect_to_obs", lambda _log: client)
    monkeypatch.setattr(obs_process, "close_obs", fail)
    with pytest.raises(RuntimeError):
        obs_process.close_obs_stopping_outputs(log)
    assert client.closed


def test_kill_obs_forces_each_process(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX signals")
    killed = []
    pids = [111, 222]
    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: list(pids))
    monkeypatch.setattr(obs_process.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(obs_process, "wait_for_obs_exit", lambda timeout: True)
    assert obs_process.kill_obs(log) is True
    assert killed == [(111, obs_process.signal.SIGKILL), (222, obs_process.signal.SIGKILL)]


def test_kill_obs_says_if_it_survives(monkeypatch):
    def refuse(pid, sig):
        raise PermissionError("not allowed")

    monkeypatch.setattr(obs_process, "find_obs_pids", lambda: [111])
    monkeypatch.setattr(obs_process.os, "kill", refuse)
    monkeypatch.setattr(obs_process, "wait_for_obs_exit", lambda timeout: False)
    assert obs_process.kill_obs(log) is False


TRAY_SOURCE = os.path.join(TRAY_WIDGET_DIR, "tray_widget.py")


def test_the_tray_menu_offers_both_ways_to_quit():
    with open(TRAY_SOURCE, encoding="utf-8") as f:
        source = f.read()
    assert 'pystray.MenuItem("Quit ClipEm Only", self._quit_leaving_obs)' in source
    assert 'pystray.MenuItem("Quit ClipEm and OBS", self._quit_and_close_obs)' in source
    assert 'pystray.MenuItem("Restart ClipEm and OBS", self._restart_clipem_and_obs)' in source
    assert "ClipEm Tray" not in source
    assert "obs_process.kill_obs(log)" in source  # one that won't close doesn't stay behind


def test_obs_gets_its_tray_icon_back_when_the_tray_goes():
    """An OBS left running hidden needs a way to be reached."""
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "obs-plugin",
                           "clipem-obs.cpp"), encoding="utf-8") as f:
        plugin = f.read()
    assert "set_tray_icon_visible(listener_seen_ms.load() && !listener_alive());" in plugin
    assert 'register_request(vendor, "Goodbye", request_goodbye, nullptr)' in plugin

