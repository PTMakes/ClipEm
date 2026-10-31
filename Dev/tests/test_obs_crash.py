"""Restarting OBS after a crash, never after a normal exit (obs_crash.py)."""

import sys

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import obs_crash  # noqa: E402


class Fixture:
    def __init__(self):
        self.now = 0.0
        self.running = False
        self.restarts = 0
        self.notifications = []
        self.slept = []
        self.watch = obs_crash.ObsCrashWatch(self.notifications.append, self.restart, lambda: self.running,
                                             clock=lambda: self.now, sleep=self.slept.append)

    def restart(self):
        self.restarts += 1

    def crash(self):
        self.watch.connected()
        self.watch.check()


@pytest.fixture
def obs():
    return Fixture()


def test_a_crash_is_reported_and_restarted(obs):
    obs.crash()
    assert obs.restarts == 1
    assert len(obs.notifications) == 1 and "stopped unexpectedly" in obs.notifications[0]
    assert "wasn't saved" in obs.notifications[0]


def test_a_normal_exit_restarts_nothing(obs):
    obs.watch.connected()
    obs.watch.exit_started({"eventType": "ExitStarted"})
    obs.watch.check()
    assert obs.restarts == 0 and obs.notifications == [] and obs.slept == []


def test_a_running_obs_isnt_a_crash(obs):
    obs.running = True
    obs.crash()
    assert obs.restarts == 0 and obs.notifications == []


def test_it_waits_before_looking_for_the_process(obs):
    obs.crash()
    assert obs.slept == [obs_crash.PROCESS_GONE_SECONDS]


def test_one_lost_connection_restarts_once(obs):
    obs.crash()
    for _ in range(5):
        obs.watch.check()  # the poll loop, while reconnecting
    assert obs.restarts == 1


def test_never_having_connected_restarts_nothing(obs):
    obs.watch.check()
    assert obs.restarts == 0


def test_it_stops_after_too_many_crashes_and_says_so(obs):
    for _ in range(obs_crash.MAX_RESTARTS + 2):
        obs.crash()
    assert obs.restarts == obs_crash.MAX_RESTARTS
    assert "stopped restarting" in obs.notifications[obs_crash.MAX_RESTARTS]


def test_old_crashes_stop_counting(obs):
    for _ in range(obs_crash.MAX_RESTARTS):
        obs.crash()
    obs.now += obs_crash.RESTART_WINDOW_SECONDS
    obs.crash()
    assert obs.restarts == obs_crash.MAX_RESTARTS + 1


def test_a_clean_exit_flag_doesnt_outlive_the_connection(obs):
    obs.watch.connected()
    obs.watch.exit_started()
    obs.watch.check()
    obs.crash()
    assert obs.restarts == 1


def test_a_failed_restart_says_so(obs):
    def fail():
        raise OSError("no python")

    obs.watch._restart = fail
    obs.crash()
    assert "Couldn't restart OBS (no python)" in obs.notifications[-1]
