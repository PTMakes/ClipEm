"""The tray widget's wait for a missing clips folder
(tray-widget/clip_folder.py), driven by a fake obs-websocket client."""

import sys
import types

import pytest
from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # obsws_client's only dependency
sys.path.insert(0, TRAY_WIDGET_DIR)
import clip_folder  # noqa: E402
from obsws_client import ObsWebSocketError  # noqa: E402


class FakeClient:
    def __init__(self, record_dir):
        self.record_dir = record_dir
        self.calls = []
        self.start_error = None

    def call(self, request_type, request_data=None, timeout=10):
        self.calls.append(request_type)
        if request_type == "GetRecordDirectory":
            return {"recordDirectory": self.record_dir}
        if request_type == "StartReplayBuffer":
            if self.start_error:
                raise self.start_error
            return {}
        raise AssertionError(request_type)


@pytest.fixture
def setup(tmp_path):
    folder = tmp_path / "pCloudDrive" / "Clips"
    notifications = []
    watch = clip_folder.ClipFolderWatch(notifications.append)
    return watch, FakeClient(str(folder)), folder, notifications


def test_folder_present_does_nothing(setup):
    watch, client, folder, notifications = setup
    folder.mkdir(parents=True)
    watch.tick(client, replay_active=False)
    assert watch.missing_dir is None
    assert notifications == []
    assert "StartReplayBuffer" not in client.calls


def test_waits_for_missing_folder_then_starts_replay_buffer(setup):
    watch, client, folder, notifications = setup
    watch.tick(client, replay_active=False)
    watch.tick(client, replay_active=False)
    assert watch.missing_dir == str(folder)
    assert len(notifications) == 1 and "isn't available" in notifications[0]
    assert not folder.exists()  # never created on its own

    folder.mkdir(parents=True)  # the drive mounted
    watch.tick(client, replay_active=False)
    assert client.calls.count("StartReplayBuffer") == 1
    assert watch.missing_dir is None
    assert "replay buffer started" in notifications[-1]

    watch.tick(client, replay_active=True)
    assert client.calls.count("StartReplayBuffer") == 1


def test_no_auto_start_if_replay_buffer_was_running(setup):
    watch, client, folder, notifications = setup
    watch.tick(client, replay_active=True)
    assert "saving clips will fail" in notifications[0]
    folder.mkdir(parents=True)
    watch.tick(client, replay_active=False)  # e.g. the user stopped it meanwhile
    assert "StartReplayBuffer" not in client.calls
    assert notifications[-1] == "The clips folder is available again."


def test_user_started_replay_buffer_cancels_auto_start(setup):
    watch, client, folder, _notifications = setup
    watch.tick(client, replay_active=False)
    watch.tick(client, replay_active=True)
    folder.mkdir(parents=True)
    watch.tick(client, replay_active=False)
    assert "StartReplayBuffer" not in client.calls


def test_start_failure_is_reported(setup):
    watch, client, folder, notifications = setup
    client.start_error = ObsWebSocketError("nope")
    watch.tick(client, replay_active=False)
    folder.mkdir(parents=True)
    watch.tick(client, replay_active=False)
    assert "didn't start" in notifications[-1]


def test_create_missing_dir_creates_and_starts(setup):
    watch, client, folder, _notifications = setup
    watch.tick(client, replay_active=False)
    watch.create_missing_dir(client, replay_active=False)
    assert folder.is_dir()
    assert client.calls.count("StartReplayBuffer") == 1
    assert watch.missing_dir is None


def test_falls_back_when_obs_has_no_directory(tmp_path):
    notifications = []
    missing = str(tmp_path / "missing")
    watch = clip_folder.ClipFolderWatch(notifications.append, fallback_dir=lambda: missing)
    watch.tick(FakeClient(""), replay_active=False)
    assert watch.missing_dir == missing


def test_deferred_start_when_folder_is_already_there(tmp_path):
    """run.py started OBS without the replay buffer, and the drive mounted
    before the tray widget's first check."""
    folder = tmp_path / "Clips"
    folder.mkdir()
    notifications = []
    watch = clip_folder.ClipFolderWatch(notifications.append, deferred=True)
    watch.forget()  # a (re)connect keeps it
    client = FakeClient(str(folder))
    watch.tick(client, replay_active=False)
    watch.tick(client, replay_active=False)
    assert client.calls.count("StartReplayBuffer") == 1
    assert "replay buffer started" in notifications[-1]


def test_deferred_start_is_dropped_once_replay_buffer_runs(tmp_path):
    folder = tmp_path / "Clips"
    folder.mkdir()
    watch = clip_folder.ClipFolderWatch(lambda message: None, deferred=True)
    client = FakeClient(str(folder))
    watch.tick(client, replay_active=True)
    watch.tick(client, replay_active=False)
    assert "StartReplayBuffer" not in client.calls
