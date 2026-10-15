"""Stuck-screen and black-screen detection for the tray widget.

Every CHECK_INTERVAL_SECONDS it asks OBS (over obs-websocket's
GetSourceScreenshot) for frames and looks at them in memory - frames arrive
as base64 in the websocket response and nothing is written to disk. Two
separate checks, each with its own on/off setting:

- Stuck screen: the screen-capture source's frame is hashed, and only the
  hash kept until the next check. STUCK_CHECKS identical frames in a row
  (a frozen capture) triggers a recovery.
- Black screen: OBS's program output (what gets recorded) is checked for
  pure black. BLACK_CHECKS black frames in a row triggers a recovery.

Recovery: shows the capture source again if it's hidden in the scene,
points it back at the display Setup picked (unless the user has since
picked another one in OBS - see DisplayChoice), re-applies its settings
(which makes OBS reinitialize the capture), refits it to the canvas, and
notifies.

PipeWire (Linux) is different: re-applying its settings only toggles the
cursor, and once its stream has died - e.g. the display was disconnected -
only a new portal session brings it back. So OBS's own log is watched for
the stream going into "error" (see PipeWireStreamWatch), and then the
source's own "Select Monitor" button is pressed PIPEWIRE_SETTLE_SECONDS
later (rather than after STUCK_CHECKS frames) - a display waking from sleep
or reconnecting takes a few seconds to come back, and a picker opened before
then doesn't list it. Not over obs-websocket's PressInputPropertiesButton:
that runs it on obs-websocket's thread, where the new portal session never
gets going. Instead the tray sets PIPEWIRE_RELOAD_REQUEST_KEY in the
source's settings and ClipEm's OBS script (Scripts/stuck_output_check.py)
presses it on OBS's UI thread. That opens the desktop's screen picker. Until a
screen is picked the stream stays "unconnected" (and the source has no
picture at all); the button is pressed again every PIPEWIRE_RETRY_SECONDS
in case the new session didn't start.

Used to be an OBS script (Scripts/stuck_output_check.py) that saved a PNG
screenshot to disk every check and hashed the file.
"""

import base64
import hashlib
import io
import logging
import os
import re
import sys
import time

from obsws_client import ObsWebSocketError
from PIL import Image

log = logging.getLogger("tray")

# OBS input kinds of the built-in screen-capture plugin on each platform
# (stable regardless of what the source is named in the scene). See
# CAPTURE_SOURCE_MONITOR_KEYS in setup.py; Linux lists both the PipeWire
# portal capture (used on current builds) and xshm_input (legacy X11) since
# either could be present.
if sys.platform == "win32":
    CAPTURE_INPUT_KINDS = ("monitor_capture",)
elif sys.platform == "darwin":
    CAPTURE_INPUT_KINDS = ("display_capture",)
else:
    CAPTURE_INPUT_KINDS = ("pipewire-screen-capture-source", "xshm_input")
PIPEWIRE_INPUT_KINDS = ("pipewire-screen-capture-source",)
# Asks ClipEm's OBS script to press the PipeWire source's "Select Monitor"
# button - see the module docstring. Must match the script's RELOAD_REQUEST_KEY.
PIPEWIRE_RELOAD_REQUEST_KEY = "clipem_reload_request"
PIPEWIRE_RETRY_SECONDS = 2 * 60
# How long a stream has to stay dead before the first picker opens, so the
# displays that just woke up / reconnected are all listed in it.
PIPEWIRE_SETTLE_SECONDS = 8
# Stream states (pipewire's pw_stream_state_as_string) that mean no picture
# is coming: "error" when it died, "unconnected" after the old stream is
# torn down and no screen has been picked for a new one yet.
PIPEWIRE_DEAD_STATES = (b"error", b"unconnected")

CHECK_INTERVAL_SECONDS = 10
STUCK_CHECKS = 5  # identical frames in a row before recovering the capture
BLACK_CHECKS = 3  # black program frames in a row before recovering the capture

# Frames are compared downscaled to this width (OBS keeps the aspect
# ratio) - plenty to tell a live capture from a frozen one, and it keeps
# each frame small on the socket. PNG is lossless, so the same pixels
# always hash the same.
FRAME_WIDTH = 640
FRAME_FORMAT = "png"

# "Pure black": no channel of any pixel above this (0-255). Not exactly 0,
# to allow for dithering and rounding in OBS's scaling.
BLACK_MAX_LEVEL = 4

# However often it recovers, notify at most this often - a screen that's
# legitimately static or black (e.g. locked) would otherwise notify every
# minute. Every recovery is still logged.
NOTIFY_COOLDOWN_SECONDS = 10 * 60

# How often a running check logs that it's still going (and how many
# frames it looked at) - otherwise it's silent unless something changes.
HEARTBEAT_SECONDS = 15 * 60

OBS_ALIGN_CENTER = 0  # libobs graphics/graphics.h


class Capture:
    """The screen-capture source, and where it sits in the program scene
    (scene_name/item_id/enabled are None if it isn't in that scene)."""

    def __init__(self, name, kind, scene_name=None, item_id=None, enabled=None):
        self.name = name
        self.kind = kind
        self.scene_name = scene_name
        self.item_id = item_id
        self.enabled = enabled

    def describe(self):
        if self.scene_name is None:
            return f"'{self.name}' (not in the current scene)"
        hidden = "" if self.enabled else " - currently HIDDEN"
        return f"'{self.name}' in scene '{self.scene_name}'{hidden}"


def program_scene_name(client):
    scene = client.call("GetCurrentProgramScene")
    return scene.get("sceneName") or scene.get("currentProgramSceneName")


def find_capture_source(client, scene_name):
    """The screen capture in the program scene, else any screen capture in
    OBS; None if there's no screen capture at all."""
    if scene_name:
        for item in client.call("GetSceneItemList", {"sceneName": scene_name}).get("sceneItems", []):
            if item.get("inputKind") in CAPTURE_INPUT_KINDS:
                return Capture(item["sourceName"], item["inputKind"], scene_name, item["sceneItemId"],
                               bool(item.get("sceneItemEnabled", True)))
    for kind in CAPTURE_INPUT_KINDS:
        inputs = client.call("GetInputList", {"inputKind": kind}).get("inputs", [])
        if inputs:
            return Capture(inputs[0]["inputName"], kind)
    return None


def frame_bytes(client, source_name):
    """The source's (or scene's) current frame as PNG bytes, in memory."""
    shot = client.call(
        "GetSourceScreenshot",
        {"sourceName": source_name, "imageFormat": FRAME_FORMAT, "imageWidth": FRAME_WIDTH},
    )
    # "data:image/png;base64,...."
    _prefix, _sep, encoded = shot["imageData"].partition(",")
    return base64.b64decode(encoded)


def frame_digest(client, source_name):
    return hashlib.sha256(frame_bytes(client, source_name)).hexdigest()


def is_black(png_bytes):
    with Image.open(io.BytesIO(png_bytes)) as image:
        extrema = image.convert("RGB").getextrema()  # ((min, max) per channel)
    return max(high for _low, high in extrema) <= BLACK_MAX_LEVEL


class DisplayChoice:
    """The display Setup pointed the capture at, and whether the user has
    since picked another one in OBS. load() -> obs_config's
    {"source_id", "settings", "user_changed"} record or None;
    set_user_changed(bool) stores the flag. Both default to "nothing
    recorded" (tests, or no Setup record)."""

    def __init__(self, load=lambda: None, set_user_changed=lambda changed: None):
        self._load = load
        self._set_user_changed = set_user_changed

    def wanted_settings(self, capture):
        """Setup's display settings for this capture, or None to keep what it
        has (nothing recorded, a different kind of source, or the user's own
        choice in OBS)."""
        record = self._load()
        if not record or record.get("source_id") != capture.kind:
            return None
        if record.get("user_changed"):
            log.info(f"Keeping '{capture.name}' on the display picked in OBS (not Setup's).")
            return None
        return record["settings"]

    def on_input_settings_changed(self, event):
        """obs-websocket's InputSettingsChanged - sent when a source's
        properties are changed (OBS's Properties dialog included). A display
        different from Setup's is the user's own choice, to be kept; one
        back on Setup's clears that. ClipEm's own SetInputSettings sends this
        too, but only ever with Setup's display or the user's."""
        record = self._load()
        new_settings = event.get("inputSettings") or {}
        if not record or not any(key in new_settings for key in record["settings"]):
            return
        user_changed = any(new_settings.get(key, value) != value for key, value in record["settings"].items())
        if user_changed != bool(record.get("user_changed")):
            where = ("a different display than Setup picked - recovery will keep it there" if user_changed
                     else "the display Setup picked")
            log.info(f"'{event.get('inputName')}' was pointed at {where}.")
            self._set_user_changed(user_changed)


class PipeWireStreamWatch:
    """Reads OBS's own log (the newest file in log_dir) for the PipeWire
    capture's stream state - obs-websocket has no way to ask for it, and a
    dead stream keeps showing its last frame, which from outside looks just
    like a static screen. linux-pipewire logs every state change:
    `[pipewire] Stream 0x... state: "error" (error: ...)`."""

    STATE_LINE = re.compile(rb'\[pipewire\] Stream (0x[0-9a-f]+) state: "([a-z]+)"')
    TAIL_BYTES = 256 * 1024

    def __init__(self, log_dir=None):
        self._log_dir = log_dir

    def dead_stream(self):
        """The latest stream state (e.g. "error") if it means the capture has
        no working stream (PIPEWIRE_DEAD_STATES), else None."""
        if not self._log_dir:
            return None
        try:
            logs = [os.path.join(self._log_dir, name) for name in os.listdir(self._log_dir)
                    if name.endswith(".txt")]
            if not logs:
                return None
            newest = max(logs, key=os.path.getmtime)
            with open(newest, "rb") as f:
                f.seek(0, os.SEEK_END)
                start = max(0, f.tell() - self.TAIL_BYTES)
                f.seek(start)
                tail = f.read()
        except OSError:
            return None
        last = None
        for match in self.STATE_LINE.finditer(tail):
            last = match  # only the most recent state line counts
        if last is None or last.group(2) not in PIPEWIRE_DEAD_STATES:
            return None
        return last.group(2).decode()


def show_capture(client, capture):
    if capture.scene_name is not None and not capture.enabled:
        log.warning(f"'{capture.name}' was hidden in scene '{capture.scene_name}'; showing it again.")
        client.call("SetSceneItemEnabled", {"sceneName": capture.scene_name, "sceneItemId": capture.item_id,
                                            "sceneItemEnabled": True})
        capture.enabled = True


def restart_pipewire_session(client, capture):
    """Has ClipEm's OBS script press the source's "Select Monitor" button: a
    new portal session, which opens the desktop's screen picker."""
    show_capture(client, capture)
    client.call("SetInputSettings", {
        "inputName": capture.name,
        "inputSettings": {PIPEWIRE_RELOAD_REQUEST_KEY: int(time.time())},
        "overlay": True,
    })
    fit_to_canvas(client, capture)


def recover_capture_source(client, capture, display_choice=None):
    """Shows the capture again if it's hidden in the scene, points it back
    at Setup's display (see DisplayChoice), then re-applies its settings -
    this nudges OBS to reinitialize the capture - and refits it to the
    canvas."""
    show_capture(client, capture)
    settings = client.call("GetInputSettings", {"inputName": capture.name})["inputSettings"]
    wanted = display_choice.wanted_settings(capture) if display_choice else None
    if wanted:
        drifted = {key: value for key, value in wanted.items() if settings.get(key) != value}
        if drifted:
            log.warning(f"'{capture.name}' isn't on the display Setup picked; pointing it back ({drifted}).")
            settings.update(wanted)
    client.call("SetInputSettings", {"inputName": capture.name, "inputSettings": settings, "overlay": True})
    fit_to_canvas(client, capture)


def fit_to_canvas(client, capture):
    if capture.scene_name is None:
        return
    video = client.call("GetVideoSettings")
    width, height = video["baseWidth"], video["baseHeight"]
    client.call(
        "SetSceneItemTransform",
        {
            "sceneName": capture.scene_name,
            "sceneItemId": capture.item_id,
            "sceneItemTransform": {
                "positionX": width / 2,
                "positionY": height / 2,
                "rotation": 0,
                "alignment": OBS_ALIGN_CENTER,
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsAlignment": OBS_ALIGN_CENTER,
                "boundsWidth": width,
                "boundsHeight": height,
            },
        },
    )


class StuckScreenMonitor:
    """Call tick(client, stuck_enabled, black_enabled) often (the tray's poll
    loop does, every couple of seconds); it only checks once
    CHECK_INTERVAL_SECONDS have passed. The *_enabled arguments are callables
    (the settings, re-read each check). notify(message) shows the tray
    notification.

    Logs to tray.log whenever its state changes (which checks are on, which
    source it's watching, no source found), every suspicious frame in a row,
    each recovery, and a heartbeat every HEARTBEAT_SECONDS."""

    def __init__(self, notify, display_choice=None, pipewire_watch=None, interval_seconds=CHECK_INTERVAL_SECONDS,
                 stuck_checks=STUCK_CHECKS, black_checks=BLACK_CHECKS, clock=time.monotonic):
        self._notify = notify
        self.display_choice = display_choice or DisplayChoice()
        self._pipewire_watch = pipewire_watch or PipeWireStreamWatch()
        self._last_pipewire_restart = None  # clock() of the last restart while the stream stays dead
        self._pipewire_dead_since = None  # clock() when the stream was first seen dead
        self._interval = interval_seconds
        self._stuck_checks = stuck_checks
        self._black_checks = black_checks
        self._clock = clock
        self._next_check = None
        self._state = None  # the last state logged - see _set_state
        self._frames_checked = 0
        self._next_heartbeat = None
        self._last_notified = None
        self.forget()

    def forget(self):
        """Drops the baseline frame and counts - after a reconnect, a
        recovery, or while off, counting starts fresh."""
        self._source_name = None
        self._last_digest = None
        self._duplicates = 0
        self._black_frames = 0

    def _set_state(self, state, message):
        if state != self._state:
            self._state = state
            log.info(f"Screen checks: {message}")

    def _heartbeat(self, now):
        if self._next_heartbeat is None:
            self._next_heartbeat = now + HEARTBEAT_SECONDS
        elif now >= self._next_heartbeat:
            self._next_heartbeat = now + HEARTBEAT_SECONDS
            log.info(f"Screen checks: still running - {self._frames_checked} frame(s) checked "
                     f"in the last {HEARTBEAT_SECONDS // 60} min.")
            self._frames_checked = 0

    def tick(self, client, stuck_enabled, black_enabled):
        now = self._clock()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + self._interval
        stuck_on, black_on = bool(stuck_enabled()), bool(black_enabled())
        if not (stuck_on or black_on):
            self._set_state("off", "stuck-screen and black-screen detection are both off (Settings).")
            self.forget()
            return
        try:
            self.check(client, stuck_on, black_on)
        except ObsWebSocketError as exc:
            # e.g. the source was just removed, or OBS couldn't render it.
            log.warning(f"Screen check failed: {exc}")
            self._state = None  # log what it's watching again once it recovers
            self.forget()
            return
        except Exception:
            # A bug here must not look like a lost OBS connection to the
            # tray's poll loop (which would drop and reconnect, silently).
            log.exception("Screen check crashed")
            self._state = None
            self.forget()
            return
        self._heartbeat(now)

    def check(self, client, stuck_on, black_on):
        scene_name = program_scene_name(client)
        capture = find_capture_source(client, scene_name)
        checks = []
        if stuck_on:
            checks.append(f"stuck screen ({self._stuck_checks} identical frames)")
        if black_on:
            checks.append(f"black program output ({self._black_checks} black frames)")
        watched = capture.describe() if capture else (
            f"no screen-capture source ({', '.join(CAPTURE_INPUT_KINDS)}) to check or recover"
        )
        self._set_state(
            (stuck_on, black_on, capture and (capture.name, capture.scene_name, capture.enabled)),
            f"every {self._interval:.0f}s, checking for {' and '.join(checks)}; capture: {watched}.",
        )

        if capture is not None and capture.kind in PIPEWIRE_INPUT_KINDS:
            dead = self._pipewire_watch.dead_stream()
            if dead is None:
                self._last_pipewire_restart = None
                self._pipewire_dead_since = None
            else:
                now = self._clock()
                if self._last_pipewire_restart is None:
                    if self._pipewire_dead_since is None:
                        self._pipewire_dead_since = now
                    settle_left = self._pipewire_dead_since + PIPEWIRE_SETTLE_SECONDS - now
                    if settle_left > 0:
                        log.info(f"PipeWire capture: no stream ({dead}) - giving the displays "
                                 f"{settle_left:.0f}s to come back before opening the screen picker.")
                        # Check again as soon as that's up, not a whole interval later.
                        self._next_check = min(self._next_check, now + settle_left)
                        return
                if self._last_pipewire_restart is None or now - self._last_pipewire_restart >= PIPEWIRE_RETRY_SECONDS:
                    retry = self._last_pipewire_restart is not None
                    self._last_pipewire_restart = now
                    self._restart_pipewire(client, capture, dead, retry)
                else:
                    log.info(f"PipeWire capture: no stream ({dead}) - waiting for a screen to be picked.")
                return

        if stuck_on and capture is not None and self._check_stuck(client, capture):
            self._recover(client, capture, "stuck")
            return
        if black_on and scene_name and self._check_black(client, scene_name):
            self._recover(client, capture, "black")

    def _check_stuck(self, client, capture):
        """True once the capture has shown the same frame STUCK_CHECKS times
        in a row."""
        try:
            digest = frame_digest(client, capture.name)
        except ObsWebSocketError as exc:
            # OBS can't render a source that has no picture at all (0x0) - as
            # stuck as it gets, so it counts as an unchanged frame.
            if "render" not in str(exc):
                raise
            log.info(f"Stuck-screen check: '{capture.name}' has no picture ({exc}).")
            digest = "no picture"
        self._frames_checked += 1
        if capture.name != self._source_name or self._last_digest is None:
            self._source_name, self._last_digest, self._duplicates = capture.name, digest, 0
            return False
        if digest != self._last_digest:
            if self._duplicates:
                log.info(f"Stuck-screen check: '{capture.name}' changed again after "
                         f"{self._duplicates} identical frame(s).")
            self._last_digest, self._duplicates = digest, 0
            return False
        self._duplicates += 1
        log.info(f"Stuck-screen check: '{capture.name}' unchanged "
                 f"({self._duplicates}/{self._stuck_checks} identical frames in a row).")
        return self._duplicates >= self._stuck_checks

    def _check_black(self, client, scene_name):
        """True once OBS's program output has been pure black BLACK_CHECKS
        times in a row."""
        black = is_black(frame_bytes(client, scene_name))
        self._frames_checked += 1
        if not black:
            if self._black_frames:
                log.info(f"Black-screen check: program output has picture again after "
                         f"{self._black_frames} black frame(s).")
            self._black_frames = 0
            return False
        self._black_frames += 1
        log.info(f"Black-screen check: program output ('{scene_name}') is black "
                 f"({self._black_frames}/{self._black_checks} in a row).")
        return self._black_frames >= self._black_checks

    def _recover(self, client, capture, reason):
        seconds = (self._stuck_checks if reason == "stuck" else self._black_checks) * self._interval
        self.forget()
        if reason == "stuck":
            problem = f"The screen capture hasn't changed for {seconds:.0f}s"
        else:
            problem = f"OBS's output has been black for {seconds:.0f}s"
        if capture is None:
            log.warning(f"{problem}, but there's no screen-capture source to restart.")
            self._notify_limited(f"{problem}. Check the sources in OBS.")
            return
        log.warning(f"{problem}; recovering {capture.describe()}.")
        recover_capture_source(client, capture, self.display_choice)
        self._notify_limited(
            f"{problem}, so ClipEm restarted the screen capture. "
            "If clips still look wrong, check the capture source in OBS."
        )

    def _restart_pipewire(self, client, capture, state, retry):
        self.forget()
        if retry:
            log.warning(f"PipeWire capture of {capture.describe()} still has no stream ({state}) "
                        f"{PIPEWIRE_RETRY_SECONDS}s after restarting it; starting a new capture session again.")
        else:
            log.warning(f"OBS's log shows the PipeWire stream of {capture.describe()} stopped ({state}), "
                        "e.g. its display was disconnected; starting a new capture session.")
        restart_pipewire_session(client, capture)
        message = ("The screen capture stopped (was a display disconnected?). "
                   "Pick your screen in the dialog that just opened so ClipEm can keep recording.")
        # Not rate-limited, retries included: nothing is recorded until the
        # user picks a screen, and the first one may well have popped up
        # while the display was still disconnected.
        self._last_notified = self._clock()
        self._notify(message)

    def _notify_limited(self, message):
        now = self._clock()
        if self._last_notified is not None and now - self._last_notified < NOTIFY_COOLDOWN_SECONDS:
            log.info(f"Not notifying again yet (last notification {now - self._last_notified:.0f}s ago).")
            return
        self._last_notified = now
        self._notify(message)
