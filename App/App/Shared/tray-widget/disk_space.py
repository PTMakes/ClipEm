"""Disk-space and recording-size warnings for the tray widget.

Two checks, both driven from the tray's poll loop:

- Free space: every CHECK_INTERVAL_SECONDS, looks up how much space is
  left on the drive OBS records to (obs-websocket's GetRecordDirectory -
  clips from the replay buffer land there too). Below LOW_FREE_BYTES it
  notifies once; below CRITICAL_FREE_BYTES it notifies again, repeated
  every CRITICAL_REPEAT_SECONDS while OBS is writing
  (recording or replay buffer) - OBS stops a recording when the disk fills.
  Once free space is back above LOW_FREE_BYTES + REARM_MARGIN_BYTES the
  warnings start over.
- Recording size: the current recording's size (GetRecordStatus's
  outputBytes, which the poll loop already asks for) - each time it passes
  another RECORDING_WARNING_STEP_BYTES (100 GB, 200 GB, ...) it notifies.
  Starts over when the recording stops.

Notifications go through the tray's notify(), as the stuck/black-screen
ones do (stuck_screen.py). Both checks are off while the "disk space
warnings" setting is (obs_config.load_disk_space_warnings_enabled).
"""

import logging
import os
import shutil
import time

from obsws_client import ObsWebSocketError

log = logging.getLogger("tray")

GB = 1024 ** 3  # what Windows Explorer (and most file managers) call a GB

CHECK_INTERVAL_SECONDS = 60
LOW_FREE_BYTES = 20 * GB
CRITICAL_FREE_BYTES = 5 * GB
# Free space has to climb this far back above LOW_FREE_BYTES before the
# warnings re-arm - so hovering right at the line doesn't notify every check.
REARM_MARGIN_BYTES = 2 * GB
CRITICAL_REPEAT_SECONDS = 15 * 60

RECORDING_WARNING_STEP_BYTES = 100 * GB

LEVEL_OK, LEVEL_LOW, LEVEL_CRITICAL = 0, 1, 2


def format_size(num_bytes):
    if num_bytes >= GB:
        return f"{num_bytes / GB:.1f} GB"
    return f"{num_bytes / 1024 ** 2:.0f} MB"


def existing_dir(path):
    """path, or its nearest parent that exists (OBS creates the recording
    folder only when it first writes to it)."""
    path = os.path.abspath(path)
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return path


class DiskSpaceMonitor:
    """Call tick(client, enabled, writing, recording_active, recording_bytes)
    from the poll loop; free space is only checked every
    CHECK_INTERVAL_SECONDS. `enabled` is a callable (the setting, re-read
    each tick). `writing`: OBS is recording or running the replay buffer.
    notify(message) shows the tray notification. fallback_dir() is where to
    look if OBS can't say where it records (obs_config.load_clip_folder)."""

    def __init__(self, notify, fallback_dir=lambda: None, disk_usage=shutil.disk_usage,
                 clock=time.monotonic, interval_seconds=CHECK_INTERVAL_SECONDS):
        self._notify = notify
        self._fallback_dir = fallback_dir
        self._disk_usage = disk_usage
        self._clock = clock
        self._interval = interval_seconds
        self._enabled = None  # the setting as of the last tick - see tick
        self.forget()

    def forget(self):
        """Back to "nothing warned about yet" - while off, so turning the
        setting back on warns about whatever is still true."""
        self._next_check = None
        self._level = LEVEL_OK
        self._last_critical_notice = None
        self._recording_steps_shown = 0
        self.free_bytes = None  # last free space seen, or None

    def tick(self, client, enabled, writing, recording_active, recording_bytes):
        on = bool(enabled())
        if on != self._enabled:
            self._enabled = on
            log.info(f"Disk-space warnings: {'on' if on else 'off (Settings)'}.")
        if not on:
            self.forget()
            return
        self._check_recording_size(recording_active, recording_bytes)
        now = self._clock()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + self._interval
        try:
            self._check_free_space(client, writing, now)
        except ObsWebSocketError as exc:
            log.warning(f"Disk-space check failed: {exc}")
        except Exception:
            # A bug here must not look like a lost OBS connection to the
            # tray's poll loop (which would drop and reconnect, silently).
            log.exception("Disk-space check crashed")

    def _record_directory(self, client):
        try:
            directory = client.call("GetRecordDirectory").get("recordDirectory")
        except ObsWebSocketError as exc:
            log.info(f"Couldn't ask OBS for its recording folder ({exc}); using the clips folder.")
            directory = None
        return directory or self._fallback_dir()

    def _check_free_space(self, client, writing, now):
        directory = self._record_directory(client)
        target = existing_dir(directory) if directory else None
        if target is None:
            log.info(f"Disk-space check: no folder to check ({directory!r}).")
            return
        free = self._disk_usage(target).free
        self.free_bytes = free

        if free < CRITICAL_FREE_BYTES:
            level = LEVEL_CRITICAL
        elif free < LOW_FREE_BYTES:
            level = LEVEL_LOW
        elif free >= LOW_FREE_BYTES + REARM_MARGIN_BYTES:
            level = LEVEL_OK
        else:
            level = min(self._level, LEVEL_LOW)  # in the re-arm margin: no change unless it improved

        where = f"the drive with {directory}"
        if level == LEVEL_CRITICAL:
            repeat_due = (self._last_critical_notice is None
                          or (writing and now - self._last_critical_notice >= CRITICAL_REPEAT_SECONDS))
            if self._level != LEVEL_CRITICAL or repeat_due:
                self._last_critical_notice = now
                message = (f"Disk almost full: only {format_size(free)} free on {where}. "
                           "OBS will stop recording when the disk is full - free up space or stop recording.")
                log.warning(f"Disk-space check: {message}")
                self._notify(message)
        elif level == LEVEL_LOW and self._level == LEVEL_OK:
            message = f"Low disk space: {format_size(free)} free on {where}."
            log.warning(f"Disk-space check: {message}")
            self._notify(message)
        elif level == LEVEL_OK and self._level != LEVEL_OK:
            log.info(f"Disk-space check: {format_size(free)} free again on {where}.")

        if level != LEVEL_CRITICAL:
            self._last_critical_notice = None
        self._level = level

    def _check_recording_size(self, recording_active, recording_bytes):
        if not recording_active:
            self._recording_steps_shown = 0
            return
        steps = int((recording_bytes or 0) // RECORDING_WARNING_STEP_BYTES)
        if steps <= self._recording_steps_shown:
            return
        self._recording_steps_shown = steps
        size = format_size(recording_bytes)
        message = f"The current recording is over {steps * RECORDING_WARNING_STEP_BYTES // GB} GB ({size})."
        if self.free_bytes is not None:
            message += f" {format_size(self.free_bytes)} free on the disk."
        log.warning(f"Recording size: {message}")
        self._notify(message)
