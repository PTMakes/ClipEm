import logging
import os
import shutil
import subprocess
import sys

import obs_process
import pytest

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
