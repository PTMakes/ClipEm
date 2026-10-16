"""The tray widget's disk-space and recording-size warnings
(tray-widget/disk_space.py), driven by a fake obs-websocket client."""

import sys
import types
from collections import namedtuple

import pytest
from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # obsws_client's only dependency
sys.path.insert(0, TRAY_WIDGET_DIR)
import disk_space  # noqa: E402
from disk_space import GB  # noqa: E402
from obsws_client import ObsWebSocketError  # noqa: E402

Usage = namedtuple("Usage", "total used free")


class FakeClient:
    def __init__(self, record_dir):
        self.record_dir = record_dir

    def call(self, request_type, request_data=None, timeout=10):
        assert request_type == "GetRecordDirectory"
        if isinstance(self.record_dir, Exception):
            raise self.record_dir
        return {"recordDirectory": self.record_dir}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def monitor(tmp_path):
    notifications, checked = [], []
    clock = Clock()
    free = {"bytes": 500 * GB}

    def usage(path):
        checked.append(path)
        return Usage(1000 * GB, 0, free["bytes"])

    mon = disk_space.DiskSpaceMonitor(notifications.append, fallback_dir=lambda: str(tmp_path), disk_usage=usage, clock=clock,
                                      interval_seconds=60)
    mon.clock, mon.free, mon.checked = clock, free, checked
    client = FakeClient(str(tmp_path))

    enabled = {"on": True}

    def run(writing=True, recording=False, recorded=0, advance=60):
        mon.tick(client, lambda: enabled["on"], writing, recording, recorded)
        clock.now += advance

    mon.enabled = enabled
    return mon, client, notifications, run


def test_plenty_of_space_says_nothing(monitor):
    mon, _client, notifications, run = monitor
    for _ in range(5):
        run()
    assert notifications == []
    assert mon.free_bytes == 500 * GB


def test_low_space_notifies_once_then_rearms(monitor):
    mon, _client, notifications, run = monitor
    mon.free["bytes"] = 15 * GB
    for _ in range(5):
        run()
    assert len(notifications) == 1 and "15.0 GB" in notifications[0]

    mon.free["bytes"] = 21 * GB  # inside the re-arm margin: still counts as warned
    run()
    mon.free["bytes"] = 15 * GB
    run()
    assert len(notifications) == 1

    mon.free["bytes"] = 50 * GB
    run()
    mon.free["bytes"] = 15 * GB
    run()
    assert len(notifications) == 2


def test_critical_space_notifies_and_repeats_only_while_writing(monitor):
    mon, _client, notifications, run = monitor
    mon.free["bytes"] = 3 * GB
    run(advance=disk_space.CRITICAL_REPEAT_SECONDS)
    assert len(notifications) == 1 and "Disk almost full" in notifications[0]

    run(writing=False, advance=disk_space.CRITICAL_REPEAT_SECONDS)
    assert len(notifications) == 1  # nothing being written: no repeat

    run(writing=True)
    assert len(notifications) == 2
    run(writing=True)
    assert len(notifications) == 2  # not again until CRITICAL_REPEAT_SECONDS later


def test_off_checks_nothing_and_warns_again_when_back_on(monitor):
    mon, _client, notifications, run = monitor
    mon.free["bytes"] = 15 * GB
    run()
    assert len(notifications) == 1
    mon.enabled["on"] = False
    run(recording=True, recorded=150 * GB)
    assert len(notifications) == 1 and len(mon.checked) == 1
    mon.enabled["on"] = True
    run()
    assert len(notifications) == 2


def test_only_checks_free_space_every_interval(monitor):
    mon, _client, _notifications, run = monitor
    for _ in range(6):
        run(advance=10)
    assert len(mon.checked) == 1


def test_missing_record_folder_checks_its_parent(monitor, tmp_path):
    mon, client, _notifications, run = monitor
    client.record_dir = str(tmp_path / "not" / "made" / "yet")
    run()
    assert mon.checked == [str(tmp_path)]


def test_falls_back_to_clips_folder_when_obs_cant_say(monitor, tmp_path):
    mon, client, _notifications, run = monitor
    client.record_dir = ObsWebSocketError("GetRecordDirectory failed")
    run()
    assert mon.checked == [str(tmp_path)]


def test_disk_errors_dont_escape(monitor):
    mon, _client, _notifications, run = monitor

    def broken(path):
        raise OSError("gone")

    mon._disk_usage = broken
    run()  # logged, not raised - the poll loop would treat it as a lost connection


def test_recording_over_100gb_notifies_once_per_100gb(monitor):
    _mon, _client, notifications, run = monitor
    run(recording=True, recorded=99 * GB, advance=2)
    assert notifications == []
    run(recording=True, recorded=100 * GB + 1, advance=2)
    run(recording=True, recorded=150 * GB, advance=2)
    assert len(notifications) == 1
    message = notifications[0]
    assert "over 100 GB" in message and "500.0 GB free" in message

    run(recording=True, recorded=201 * GB, advance=2)
    assert len(notifications) == 2 and "over 200 GB" in notifications[1]


def test_new_recording_starts_over(monitor):
    _mon, _client, notifications, run = monitor
    run(recording=True, recorded=120 * GB)
    run(recording=False)
    run(recording=True, recorded=110 * GB)
    assert len(notifications) == 2


def test_format_size():
    assert disk_space.format_size(512 * 1024 ** 2) == "512 MB"
    assert disk_space.format_size(int(1.5 * GB)) == "1.5 GB"
