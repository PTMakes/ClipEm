"""The tray widget's stuck-screen and black-screen checks
(tray-widget/stuck_screen.py), driven by a fake obs-websocket client."""

import ast
import base64
import glob
import io
import os
import subprocess
import sys
import types

import pytest
from conftest import APP_SHARED_DIR, TRAY_WIDGET_DIR
from PIL import Image

sys.modules.setdefault("websocket", types.ModuleType("websocket"))  # obsws_client's only dependency
sys.path.insert(0, TRAY_WIDGET_DIR)
import stuck_screen  # noqa: E402
from obsws_client import ObsWebSocketError  # noqa: E402

# Not PipeWire on Linux: that has its own tests below.
KIND = stuck_screen.CAPTURE_INPUT_KINDS[-1]


def png(color, size=(16, 9)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


BLACK = png((0, 0, 0))
PICTURE = png((40, 90, 200))


class FakeClient:
    """Just the requests the checks make. `frame` is what the capture shows,
    `program` what OBS's output shows."""

    def __init__(self, scene_items=None, inputs=None, capture_settings=None):
        self.frame = b"frame"
        self.program = PICTURE
        self.scene_items = scene_items if scene_items is not None else [
            {"inputKind": "wasapi_input_capture", "sourceName": "Mic", "sceneItemId": 1, "sceneItemEnabled": True},
            {"inputKind": KIND, "sourceName": "Screen", "sceneItemId": 2, "sceneItemEnabled": True},
        ]
        self.inputs = inputs or {}
        self.capture_settings = capture_settings or {"monitor": 1}
        self.calls = []

    def call(self, request_type, request_data=None, timeout=10):
        self.calls.append((request_type, request_data))
        if request_type == "GetCurrentProgramScene":
            return {"sceneName": "Scene", "currentProgramSceneName": "Scene"}
        if request_type == "GetSceneItemList":
            return {"sceneItems": self.scene_items}
        if request_type == "GetInputList":
            return {"inputs": self.inputs.get(request_data["inputKind"], [])}
        if request_type == "GetSourceScreenshot":
            image = self.program if request_data["sourceName"] == "Scene" else self.frame
            if isinstance(image, Exception):
                raise image
            return {"imageData": "data:image/png;base64," + base64.b64encode(image).decode()}
        if request_type == "GetInputSettings":
            return {"inputSettings": dict(self.capture_settings)}
        if request_type == "GetVideoSettings":
            return {"baseWidth": 1920, "baseHeight": 1080}
        return {}

    def requests(self, request_type):
        return [data for kind, data in self.calls if kind == request_type]

    def screenshots_of(self, name):
        return [d for d in self.requests("GetSourceScreenshot") if d["sourceName"] == name]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def monitor():
    """(monitor, notifications, run(client, frames..., stuck=, black=)) -
    run() feeds one capture frame per check interval."""
    notifications = []
    clock = Clock()
    mon = stuck_screen.StuckScreenMonitor(notifications.append, interval_seconds=10, stuck_checks=3,
                                          black_checks=2, clock=clock)

    def run(client, *frames, stuck=True, black=False):
        for frame in frames:
            client.frame = frame
            mon.tick(client, lambda: stuck, lambda: black)
            clock.now += 10

    mon.clock = clock
    return mon, notifications, run


# --- stuck screen ----------------------------------------------------------

def test_resets_and_notifies_after_stuck_checks_identical_frames(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    run(client, b"a", b"a", b"a")  # baseline + 2 duplicates
    assert notifications == [] and client.requests("SetInputSettings") == []

    run(client, b"a")  # 3rd duplicate in a row
    assert len(notifications) == 1 and "30s" in notifications[0]
    assert client.requests("SetInputSettings") == [
        {"inputName": "Screen", "inputSettings": {"monitor": 1}, "overlay": True}
    ]
    (transform,) = client.requests("SetSceneItemTransform")
    assert transform["sceneName"] == "Scene" and transform["sceneItemId"] == 2
    assert transform["sceneItemTransform"]["boundsWidth"] == 1920
    assert client.requests("SetSceneItemEnabled") == []  # it wasn't hidden

    run(client, b"a", b"a", b"a")
    assert len(client.requests("SetInputSettings")) == 1  # counting starts over after each recovery
    mon.clock.now += stuck_screen.STUCK_BACKOFF_SECONDS[0]
    run(client, b"a", b"a", b"a", b"a")
    assert len(client.requests("SetInputSettings")) == 2


def test_notifies_at_most_once_per_cooldown(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    run(client, *[b"a"] * 4)
    mon.clock.now += stuck_screen.STUCK_BACKOFF_SECONDS[0]
    run(client, *[b"a"] * 4)  # a second recovery
    assert len(client.requests("SetInputSettings")) == 2
    assert len(notifications) == 1
    mon.clock.now += stuck_screen.NOTIFY_COOLDOWN_SECONDS
    run(client, *[b"a"] * 4)
    assert len(notifications) == 2


def test_a_changing_frame_restarts_the_count(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    run(client, b"a", b"a", b"a", b"b", b"b", b"b")
    assert notifications == []
    run(client, b"b")
    assert len(notifications) == 1


def test_only_checks_once_per_interval():
    clock = Clock()
    mon = stuck_screen.StuckScreenMonitor(lambda message: None, interval_seconds=10, clock=clock)
    client = FakeClient()
    for _ in range(5):  # the tray polls every couple of seconds
        mon.tick(client, lambda: True, lambda: False)
        clock.now += 2
    assert len(client.requests("GetSourceScreenshot")) == 1
    mon.tick(client, lambda: True, lambda: False)
    assert len(client.requests("GetSourceScreenshot")) == 2


def test_frames_are_requested_small_and_never_saved(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    run(client, b"a")
    (request,) = client.requests("GetSourceScreenshot")
    assert request == {"sourceName": "Screen", "imageFormat": "png", "imageWidth": stuck_screen.FRAME_WIDTH}
    assert "imageFilePath" not in request  # SaveSourceScreenshot would write a file


# OBS APIs that write a screenshot file. The frontend ones save into the
# recording folder and share one "last screenshot" path with the user's own
# screenshot hotkey - the old OBS script deleted whatever that path pointed
# at, so a user's screenshot taken at the same moment got deleted and the
# script's own was left in the clip folder.
SCREENSHOT_FILE_APIS = {
    "obs_frontend_take_screenshot",
    "obs_frontend_take_source_screenshot",
    "obs_frontend_get_last_screenshot",
    "SaveSourceScreenshot",
    "imageFilePath",
}


def test_nothing_shipped_writes_screenshot_files():
    used = []
    for path in sorted(glob.glob(os.path.join(APP_SHARED_DIR, "**", "*.py"), recursive=True)):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            name = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute)
                    else node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None)
            if name in SCREENSHOT_FILE_APIS:
                used.append(f"{os.path.relpath(path, APP_SHARED_DIR)}:{node.lineno} {name}")
    assert used == []


def test_both_off_takes_no_frames_and_forgets_the_count(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    run(client, b"a", b"a", b"a")
    run(client, b"a", stuck=False, black=False)
    assert len(client.requests("GetSourceScreenshot")) == 3
    run(client, b"a", b"a", b"a")
    assert notifications == []  # the count started over


def test_capture_outside_the_scene_is_reset_without_a_transform(monitor):
    mon, notifications, run = monitor
    client = FakeClient(scene_items=[], inputs={KIND: [{"inputName": "Other Screen"}]})
    run(client, b"a", b"a", b"a", b"a")
    assert len(notifications) == 1
    assert client.requests("SetInputSettings")[0]["inputName"] == "Other Screen"
    assert client.requests("SetSceneItemTransform") == []


def test_no_capture_source_does_nothing(monitor):
    mon, notifications, run = monitor
    client = FakeClient(scene_items=[])
    run(client, b"a", b"a", b"a", b"a")
    assert notifications == [] and client.requests("GetSourceScreenshot") == []


def test_a_failed_request_is_logged_not_raised(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    run(client, b"a", b"a", ObsWebSocketError("GetSourceScreenshot failed"), b"a", b"a")
    assert notifications == []  # the failure restarted the count


def test_an_unexpected_error_is_logged_not_raised(monitor, caplog):
    mon, notifications, run = monitor
    client = FakeClient()
    run(client, KeyError("imageData"))
    assert "Screen check crashed" in caplog.text


def test_logs_what_it_watches_once_and_each_identical_frame(monitor, caplog):
    mon, notifications, run = monitor
    client = FakeClient()
    with caplog.at_level("INFO", logger="tray"):
        run(client, b"a", b"b", b"b")
        run(client, b"a", stuck=False, black=False)
        run(client, b"a")
    messages = [r.getMessage() for r in caplog.records]
    assert sum("capture: 'Screen' in scene 'Scene'" in m for m in messages) == 2  # again after being off
    assert sum("unchanged (1/3" in m for m in messages) == 1
    assert sum("both off" in m for m in messages) == 1


# --- hidden capture --------------------------------------------------------

def test_recovery_shows_a_hidden_capture_again(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    client.scene_items[1]["sceneItemEnabled"] = False
    run(client, b"a", b"a", b"a", b"a")
    assert client.requests("SetSceneItemEnabled") == [
        {"sceneName": "Scene", "sceneItemId": 2, "sceneItemEnabled": True}
    ]


# --- black screen ----------------------------------------------------------

def test_is_black():
    assert stuck_screen.is_black(BLACK)
    assert stuck_screen.is_black(png((3, 2, 4)))  # dithering noise
    assert not stuck_screen.is_black(PICTURE)
    assert not stuck_screen.is_black(png((0, 0, 30)))  # very dark blue isn't black
    image = Image.new("RGB", (16, 9))
    image.putpixel((5, 5), (255, 255, 255))  # e.g. just a cursor
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    assert not stuck_screen.is_black(buffer.getvalue())


def test_black_program_output_recovers_the_capture(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    client.program = BLACK
    client.scene_items[1]["sceneItemEnabled"] = False  # the usual cause: capture hidden
    run(client, b"a", stuck=False, black=True)
    assert notifications == []
    assert client.screenshots_of("Screen") == []  # stuck check is off
    run(client, b"b", stuck=False, black=True)  # 2nd black frame in a row
    assert len(notifications) == 1 and "black" in notifications[0]
    assert client.requests("SetSceneItemEnabled")[0]["sceneItemEnabled"] is True
    assert len(client.requests("SetInputSettings")) == 1


def test_picture_in_between_restarts_the_black_count(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    for program in (BLACK, PICTURE, BLACK, PICTURE):
        client.program = program
        run(client, b"x", stuck=False, black=True)
    assert notifications == [] and client.requests("SetInputSettings") == []


def test_black_with_no_capture_to_recover_still_notifies(monitor):
    mon, notifications, run = monitor
    client = FakeClient(scene_items=[])
    client.program = BLACK
    run(client, b"a", b"a", stuck=False, black=True)
    assert len(notifications) == 1 and "Check the sources in OBS" in notifications[0]
    assert client.requests("SetInputSettings") == []


# --- Setup's display -------------------------------------------------------

class Record:
    """Stands in for run_config.json's "capture_display"."""

    def __init__(self, settings, user_changed=False):
        self.value = {"source_id": KIND, "settings": settings, "user_changed": user_changed}

    def choice(self):
        return stuck_screen.DisplayChoice(lambda: self.value, self._set)

    def _set(self, user_changed):
        self.value["user_changed"] = user_changed


def _recover(capture_settings, display_choice):
    client = FakeClient(capture_settings=capture_settings)
    capture = stuck_screen.Capture("Screen", KIND, "Scene", 2, True)
    stuck_screen.recover_capture_source(client, capture, display_choice)
    return client.requests("SetInputSettings")[0]["inputSettings"]


def test_recovery_points_the_capture_back_at_setups_display():
    record = Record({"monitor": 1, "monitor_id": "DISPLAY#BBB"})
    applied = _recover({"monitor": 0, "monitor_id": "DISPLAY#AAA", "capture_cursor": True}, record.choice())
    assert applied == {"monitor": 1, "monitor_id": "DISPLAY#BBB", "capture_cursor": True}


def test_recovery_keeps_a_display_the_user_picked_in_obs():
    record = Record({"monitor": 1, "monitor_id": "DISPLAY#BBB"})
    record.choice().on_input_settings_changed(
        {"inputName": "Screen", "inputSettings": {"monitor": 0, "monitor_id": "DISPLAY#AAA"}})
    assert record.value["user_changed"] is True
    applied = _recover({"monitor": 0, "monitor_id": "DISPLAY#AAA"}, record.choice())
    assert applied == {"monitor": 0, "monitor_id": "DISPLAY#AAA"}


def test_picking_setups_display_again_in_obs_clears_the_users_choice():
    record = Record({"monitor": 1}, user_changed=True)
    record.choice().on_input_settings_changed({"inputName": "Screen", "inputSettings": {"monitor": 1}})
    assert record.value["user_changed"] is False


def test_other_inputs_settings_changes_are_ignored():
    record = Record({"monitor": 1})
    record.choice().on_input_settings_changed({"inputName": "Mic", "inputSettings": {"device_id": "x"}})
    assert record.value["user_changed"] is False


def test_a_different_kind_of_capture_keeps_its_display():
    record = Record({"monitor": 1})
    record.value["source_id"] = "some_other_capture"
    assert _recover({"monitor": 0}, record.choice()) == {"monitor": 0}


def test_no_setup_record_keeps_the_current_display():
    assert _recover({"monitor": 0}, stuck_screen.DisplayChoice()) == {"monitor": 0}


# --- PipeWire stream death -------------------------------------------------

PIPEWIRE = "pipewire-screen-capture-source"
LOG_START = (
    '11:30:14.184: [pipewire] Created stream 0x5572bc245a20\n'
    '11:30:14.205: [pipewire] Stream 0x5572bc245a20 state: "streaming" (error: none)\n'
)
LOG_DIED = (
    '11:30:41.391: [pipewire] Stream 0x5572bc245a20 state: "paused" (error: none)\n'
    '11:30:41.706: [pipewire] Stream 0x5572bc245a20 state: "error" (error: no more input formats)\n'
    '11:30:41.706: [pipewire] Error id:2 seq:47 res:-32 (Broken pipe): no more input formats\n'
)
LOG_RESTARTED = (
    '11:31:02.100: [pipewire] Created stream 0x5572bc245a20\n'  # same address can come back
    '11:31:02.130: [pipewire] Stream 0x5572bc245a20 state: "streaming" (error: none)\n'
)


LOG_TORN_DOWN = '11:30:46.495: [pipewire] Stream 0x5572bc245a20 state: "unconnected" (error: none)\n'


def test_stream_watch_reports_only_a_currently_dead_stream(tmp_path):
    log_file = tmp_path / "2026-09-26 11-30-12.txt"
    watch = stuck_screen.PipeWireStreamWatch(str(tmp_path))
    assert stuck_screen.PipeWireStreamWatch().dead_stream() is None  # no log dir
    assert watch.dead_stream() is None  # no log yet
    log_file.write_text(LOG_START)
    assert watch.dead_stream() is None
    log_file.write_text(LOG_START + LOG_DIED)
    assert watch.dead_stream() == "error"
    log_file.write_text(LOG_START + LOG_DIED + LOG_TORN_DOWN)  # restarted, no screen picked yet
    assert watch.dead_stream() == "unconnected"
    log_file.write_text(LOG_START + LOG_DIED + LOG_TORN_DOWN + LOG_RESTARTED)
    assert watch.dead_stream() is None


def reload_requests(client):
    """Requests for ClipEm's OBS script to press "Select Monitor" - never
    PressInputPropertiesButton, which runs on obs-websocket's thread."""
    assert client.requests("PressInputPropertiesButton") == []
    return [r for r in client.requests("SetInputSettings")
            if stuck_screen.PIPEWIRE_RELOAD_REQUEST_KEY in r["inputSettings"]]


def pipewire_monitor(tmp_path, black=False, is_locked=lambda: False, input_seconds=lambda: None):
    notifications = []
    clock = Clock()
    mon = stuck_screen.StuckScreenMonitor(
        notifications.append, pipewire_watch=stuck_screen.PipeWireStreamWatch(str(tmp_path)),
        interval_seconds=10, stuck_checks=3, black_checks=2, clock=clock, is_locked=is_locked,
        input_seconds=input_seconds)
    client = FakeClient(scene_items=[
        {"inputKind": PIPEWIRE, "sourceName": "Screen Capture (PipeWire)", "sceneItemId": 2,
         "sceneItemEnabled": True},
    ])

    def tick():
        mon.tick(client, lambda: True, lambda: black)
        clock.now += 10

    return client, notifications, tick


NO_PICTURE = ObsWebSocketError("GetSourceScreenshot failed: Failed to render screenshot.")


def test_a_session_that_never_started_reopens_the_picker(tmp_path):
    """KDE failing to restore the saved screen after a restart: OBS's log has
    no stream state at all, and the source simply has no picture."""
    (tmp_path / "obs.txt").write_text('17:27:22.100: [pipewire] Asking for monitor and window\n')
    client, notifications, tick = pipewire_monitor(tmp_path)
    client.frame = NO_PICTURE
    for _ in range(stuck_screen.PIPEWIRE_NO_PICTURE_SETTLE_SECONDS // 10):
        tick()
    assert reload_requests(client) == []  # a picker may be open, or it's just slow
    tick()
    assert len(reload_requests(client)) == 1
    assert len(notifications) == 1 and "Pick your screen" in notifications[0]
    assert "after sleep, a restart" in notifications[0]
    for _ in range(stuck_screen.PIPEWIRE_RETRY_SECONDS // 10):
        tick()
    assert len(reload_requests(client)) == 2


def test_a_slow_session_that_starts_opens_no_picker(tmp_path):
    (tmp_path / "obs.txt").write_text(LOG_START)
    client, notifications, tick = pipewire_monitor(tmp_path)
    client.frame = NO_PICTURE
    tick()
    tick()
    for number in range(8):
        client.frame = b"picture %d" % number
        tick()
    assert reload_requests(client) == [] and notifications == []


def test_no_picker_opens_while_the_screen_is_locked(tmp_path):
    (tmp_path / "obs.txt").write_text("")
    locked = {"value": True}
    client, notifications, tick = pipewire_monitor(tmp_path, is_locked=lambda: locked["value"])
    client.frame = NO_PICTURE
    for _ in range(10):
        tick()
    assert reload_requests(client) == [] and notifications == []
    locked["value"] = False
    tick()  # unlocked: straight away, the wait is long over
    assert len(reload_requests(client)) == 1


def test_black_output_on_pipewire_opens_the_picker_not_a_useless_restart(tmp_path):
    (tmp_path / "obs.txt").write_text(LOG_START)
    client, notifications, tick = pipewire_monitor(tmp_path, black=True)
    client.program = BLACK
    tick()
    assert reload_requests(client) == []
    tick()
    assert len(reload_requests(client)) == 1 and len(client.requests("SetInputSettings")) == 1
    assert len(notifications) == 1 and "Pick your screen" in notifications[0]
    for _ in range(stuck_screen.PIPEWIRE_RETRY_SECONDS // 10 - 2):  # the picker may be open: leave it
        tick()
    assert len(reload_requests(client)) == 1
    for _ in range(4):
        tick()
    assert len(reload_requests(client)) == 2


def test_black_while_locked_is_left_alone(tmp_path):
    (tmp_path / "obs.txt").write_text(LOG_START)
    client, notifications, tick = pipewire_monitor(tmp_path, black=True, is_locked=lambda: True)
    client.program = BLACK
    for _ in range(6):
        tick()
    assert client.requests("SetInputSettings") == [] and notifications == []


def test_a_dead_pipewire_stream_is_restarted_after_settling_then_retried(tmp_path):
    log_file = tmp_path / "obs.txt"
    log_file.write_text(LOG_START)
    client, notifications, tick = pipewire_monitor(tmp_path)
    tick()
    assert reload_requests(client) == []

    log_file.write_text(LOG_START + LOG_DIED)
    tick()  # displays may still be waking up: the picker would miss some
    assert reload_requests(client) == []
    tick()  # no waiting for STUCK_CHECKS identical frames
    (request,) = reload_requests(client)
    assert request["inputName"] == "Screen Capture (PipeWire)" and request["overlay"] is True
    assert len(notifications) == 1 and "Pick your screen" in notifications[0]

    log_file.write_text(LOG_START + LOG_DIED + LOG_TORN_DOWN)
    for _ in range(stuck_screen.PIPEWIRE_RETRY_SECONDS // 10 - 1):  # the picker may be open: wait
        tick()
    assert len(reload_requests(client)) == 1
    tick()  # still no stream: try again, and say so again
    assert len(reload_requests(client)) == 2
    assert len(notifications) == 2

    log_file.write_text(LOG_START + LOG_DIED + LOG_TORN_DOWN + LOG_RESTARTED)
    tick()
    log_file.write_text(LOG_START + LOG_DIED + LOG_TORN_DOWN + LOG_RESTARTED + LOG_DIED)
    tick()  # died again later: settles again first
    assert len(reload_requests(client)) == 2
    tick()
    assert len(reload_requests(client)) == 3


def test_pipewire_settle_rechecks_early_and_skips_a_stream_that_comes_back(tmp_path):
    """The picker opens PIPEWIRE_SETTLE_SECONDS after the stream died, not a
    whole check interval later - unless the stream came back meanwhile."""
    log_file = tmp_path / "obs.txt"
    log_file.write_text(LOG_START + LOG_DIED)
    notifications = []
    clock = Clock()
    mon = stuck_screen.StuckScreenMonitor(
        notifications.append, pipewire_watch=stuck_screen.PipeWireStreamWatch(str(tmp_path)),
        interval_seconds=60, clock=clock)
    client = FakeClient(scene_items=[
        {"inputKind": PIPEWIRE, "sourceName": "Screen Capture (PipeWire)", "sceneItemId": 2,
         "sceneItemEnabled": True},
    ])
    mon.tick(client, lambda: True, lambda: False)
    clock.now += stuck_screen.PIPEWIRE_SETTLE_SECONDS
    mon.tick(client, lambda: True, lambda: False)  # well before the 60s interval
    assert len(reload_requests(client)) == 1

    log_file.write_text(LOG_START + LOG_DIED + LOG_TORN_DOWN + LOG_RESTARTED + LOG_DIED)
    clock.now += 60
    mon.tick(client, lambda: True, lambda: False)
    log_file.write_text(LOG_START + LOG_DIED + LOG_TORN_DOWN + LOG_RESTARTED + LOG_DIED + LOG_RESTARTED)
    clock.now += stuck_screen.PIPEWIRE_SETTLE_SECONDS
    mon.tick(client, lambda: True, lambda: False)  # came back by itself: no picker
    assert len(reload_requests(client)) == 1


def test_a_capture_with_no_picture_counts_as_stuck(monitor):
    mon, notifications, run = monitor
    client = FakeClient()
    run(client, *[ObsWebSocketError("GetSourceScreenshot failed: Failed to render screenshot.")] * 4)
    assert len(client.requests("SetInputSettings")) == 1


def test_pipewire_without_a_way_to_see_the_mouse_uses_the_ordinary_recovery(tmp_path):
    """GNOME and the like: identical frames count, and no picker opens."""
    (tmp_path / "obs.txt").write_text(LOG_START)
    client, notifications, tick = pipewire_monitor(tmp_path)
    client.frame = b"same"
    for _ in range(8):
        tick()
    assert reload_requests(client) == []
    assert len(client.requests("SetInputSettings")) == 1 and len(notifications) == 1
    assert "restarted the screen capture" in notifications[0]


def test_a_frozen_pipewire_capture_opens_the_picker_when_the_mouse_proves_it(tmp_path):
    (tmp_path / "obs.txt").write_text(LOG_START)
    client, notifications, tick = pipewire_monitor(tmp_path, input_seconds=lambda: 1.0)
    client.frame = b"same"
    for _ in range(4):
        tick()
    assert len(reload_requests(client)) == 1
    assert len(notifications) == 1 and "Pick your screen" in notifications[0]


def test_a_static_pipewire_screen_with_nobody_there_opens_nothing(tmp_path):
    (tmp_path / "obs.txt").write_text(LOG_START)
    client, notifications, tick = pipewire_monitor(tmp_path, input_seconds=lambda: 60.0)
    client.frame = b"same"
    for _ in range(8):
        tick()
    assert client.requests("SetInputSettings") == [] and notifications == []


def test_a_mouse_that_isnt_drawn_in_the_capture_proves_nothing(tmp_path):
    (tmp_path / "obs.txt").write_text(LOG_START)
    client, notifications, tick = pipewire_monitor(tmp_path, input_seconds=lambda: 1.0)
    client.capture_settings = {"ShowCursor": False}
    client.frame = b"same"
    for _ in range(8):
        tick()
    assert client.requests("SetInputSettings") == [] and notifications == []


def _backoff_monitor(input_seconds=lambda: None):
    notifications = []
    clock = Clock()
    mon = stuck_screen.StuckScreenMonitor(notifications.append, interval_seconds=10, stuck_checks=3, clock=clock,
                                          input_seconds=input_seconds)
    client = FakeClient()

    def run(seconds, frame=b"same"):
        for _ in range(seconds // 10):
            client.frame = frame
            mon.tick(client, lambda: True, lambda: False)
            clock.now += 10

    return client, notifications, run


def test_recoveries_that_change_nothing_wait_longer_each_time():
    client, _notifications, run = _backoff_monitor()
    times = []
    done = 0
    for minute_tenths in range(0, 180):
        run(10)
        count = len(client.requests("SetInputSettings"))
        if count > done:
            done = count
            times.append(minute_tenths * 10)
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    assert gaps[:3] == sorted(gaps[:3])
    for gap, wait in zip(gaps, stuck_screen.STUCK_BACKOFF_SECONDS, strict=False):
        assert gap >= wait


def test_a_changed_picture_ends_the_wait():
    client, _notifications, run = _backoff_monitor()
    run(40)
    assert len(client.requests("SetInputSettings")) == 1
    run(10, frame=b"moved")
    run(40)
    assert len(client.requests("SetInputSettings")) == 2  # counted afresh, no wait


def test_waiting_logs_nothing(caplog):
    _client, _notifications, run = _backoff_monitor()
    run(40)
    caplog.clear()
    with caplog.at_level("INFO", logger="tray"):
        run(30)
    assert caplog.records == []


def test_identical_frames_dont_count_while_the_pc_is_idle():
    client, notifications, run = _backoff_monitor(input_seconds=lambda: 120.0)
    run(200)
    assert client.requests("SetInputSettings") == [] and notifications == []


def test_identical_frames_count_while_the_pc_is_in_use():
    client, _notifications, run = _backoff_monitor(input_seconds=lambda: 1.0)
    run(40)
    assert len(client.requests("SetInputSettings")) == 1
    assert reload_requests(client) == []


@pytest.mark.parametrize("output, expected", [("yes\n", True), ("no\n", False), ("", False)])
def test_session_locked_asks_logind(monkeypatch, output, expected):
    monkeypatch.setattr(stuck_screen.sys, "platform", "linux")
    monkeypatch.setattr(stuck_screen.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, output, ""))
    assert stuck_screen.session_locked() is expected


def test_session_locked_is_false_without_loginctl(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("loginctl")

    monkeypatch.setattr(stuck_screen.sys, "platform", "linux")
    monkeypatch.setattr(stuck_screen.subprocess, "run", missing)
    assert stuck_screen.session_locked() is False

