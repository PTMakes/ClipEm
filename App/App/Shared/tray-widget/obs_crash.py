"""Restarting OBS after a crash, from the tray widget.

OBS tells obs-websocket clients it's closing (ExitStarted) on every normal
exit, however it was asked to: its own window, SIGTERM, WM_CLOSE. A crash
just ends the connection. So the connection going without ExitStarted, and
OBS's process being gone a moment later, is a crash - and only that restarts
it. A process still there is OBS hanging or restarting, which the tray's own
reconnecting handles.

Crashing again and again is a problem restarting won't solve: after
MAX_RESTARTS in RESTART_WINDOW_SECONDS, it stops and says so.
"""

import logging
import time

log = logging.getLogger("tray")

MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 5 * 60
# How long OBS's process gets to be gone - and whoever's closing OBS and the
# tray together (Kill Existing, an update) to have closed the tray too.
PROCESS_GONE_SECONDS = 2


class ObsCrashWatch:
    """restart() starts OBS again; notify(message) shows a notification;
    is_running() says whether OBS's process is there."""

    def __init__(self, notify, restart, is_running, clock=time.monotonic, sleep=time.sleep):
        self._notify = notify
        self._restart = restart
        self._is_running = is_running
        self._clock = clock
        self._sleep = sleep
        self._connected = False
        self.exited_cleanly = False  # ExitStarted came on the last connection
        self._restarts = []  # clock() of each, in the last RESTART_WINDOW_SECONDS

    def connected(self):
        self._connected = True
        self.exited_cleanly = False

    def exit_started(self, _event=None):
        self.exited_cleanly = True

    def check(self):
        """Call whenever the connection is down: acts once per lost connection."""
        if not self._connected:
            return
        self._connected = False
        if self.exited_cleanly:
            return
        self._sleep(PROCESS_GONE_SECONDS)
        if self._is_running():
            log.warning("Lost the connection to OBS, but it's still running - not treating that as a crash.")
            return
        now = self._clock()
        self._restarts = [moment for moment in self._restarts if now - moment < RESTART_WINDOW_SECONDS]
        if len(self._restarts) >= MAX_RESTARTS:
            log.error(f"OBS crashed {MAX_RESTARTS + 1} times in {RESTART_WINDOW_SECONDS}s - not restarting it again.")
            self._notify("OBS keeps stopping unexpectedly, so ClipEm has stopped restarting it. "
                         "Start ClipEm again to retry; Settings > View Logs shows what happened.")
            return
        self._restarts.append(now)
        log.error(f"OBS stopped unexpectedly - restarting it ({len(self._restarts)} of {MAX_RESTARTS}).")
        self._notify("OBS stopped unexpectedly, so ClipEm is restarting it. "
                     "Anything in the replay buffer that wasn't saved as a clip is lost.")
        try:
            self._restart()
        except Exception as exc:
            log.warning(f"Couldn't restart OBS: {exc}")
            self._notify(f"Couldn't restart OBS ({exc}) - start ClipEm from its shortcut.")
