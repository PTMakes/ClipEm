"""Waits out a clips folder that isn't there yet, for the tray widget.

OBS won't start the replay buffer (or a recording) when the folder it
records to doesn't exist - it shows a "path is invalid" error instead. With
the folder on a cloud or network drive (pCloud, a NAS share, ...) that is
what happens at login: run.py starts OBS with --startreplaybuffer before
the drive is mounted, so the replay buffer stays off. (run.py now leaves
--startreplaybuffer off when the folder is missing, so OBS doesn't show
that error, and tells the tray widget it did - `deferred` below.)

ClipFolderWatch, driven from the tray's poll loop, notices that the folder
OBS records to (obs-websocket's GetRecordDirectory) is missing, says so,
and starts the replay buffer itself as soon as the folder shows up. It
never creates the folder on its own: on an unmounted cloud drive that would
put a local folder inside the empty mount point, which can stop the drive
from mounting at all. The tray's menu offers that ("Create Clips Folder")
for a folder that really is gone.
"""

import logging
import os

from obsws_client import ObsWebSocketError

log = logging.getLogger("tray")


class ClipFolderWatch:
    """Call tick(client, replay_active) from the poll loop. notify(message)
    shows the tray notification; fallback_dir() is where clips go if OBS
    can't say (obs_config.load_clip_folder). deferred: OBS was started
    without the replay buffer because the folder was missing, so start it
    once the folder is there - even if that's before the first check."""

    def __init__(self, notify, fallback_dir=lambda: None, isdir=os.path.isdir, deferred=False):
        self._notify = notify
        self._fallback_dir = fallback_dir
        self._isdir = isdir
        self._deferred = deferred  # survives forget(): it's about this OBS launch
        self.forget()

    def forget(self):
        """Back to "folder looks fine" - e.g. on a fresh OBS connection."""
        self.missing_dir = None  # the folder, while it's missing
        self._waiting = False  # start the replay buffer once it's back

    def tick(self, client, replay_active):
        try:
            self._check(client, replay_active)
        except ObsWebSocketError as exc:
            log.warning(f"Clips folder check failed: {exc}")
        except Exception:
            # A bug here must not look like a lost OBS connection to the
            # tray's poll loop (which would drop and reconnect, silently).
            log.exception("Clips folder check crashed")

    def _record_directory(self, client):
        try:
            directory = client.call("GetRecordDirectory").get("recordDirectory")
        except ObsWebSocketError:
            directory = None
        return directory or self._fallback_dir()

    def _check(self, client, replay_active):
        directory = self._record_directory(client)
        if not directory:
            return
        if replay_active:
            # Started (by the user or OBS) - nothing to do once it's back.
            self._waiting = self._deferred = False
        if not self._isdir(directory):
            if self.missing_dir != directory:
                self.missing_dir = directory
                self._waiting = not replay_active
                self._deferred = False
                log.warning(f"Clips folder {directory} isn't there"
                            f"{'' if replay_active else ' - waiting for it to start the replay buffer'}.")
                if replay_active:
                    self._notify(f"The clips folder {directory} isn't available - saving clips will fail until it is.")
                else:
                    self._notify(f"The clips folder {directory} isn't available, so the replay buffer is off. "
                                 "If it's on a cloud or network drive, ClipEm will start the replay buffer "
                                 "once the drive connects.")
            return
        if self.missing_dir is None:
            if self._deferred:
                self._deferred = False
                log.info(f"Clips folder {directory} is there now.")
                self._start_replay_buffer(client)
            return
        log.info(f"Clips folder {directory} is available again.")
        self.missing_dir = None
        if self._waiting and not replay_active:
            self._waiting = False
            self._start_replay_buffer(client)
        else:
            self._notify("The clips folder is available again.")

    def _start_replay_buffer(self, client):
        try:
            client.call("StartReplayBuffer")
        except ObsWebSocketError as exc:
            log.warning(f"Couldn't start the replay buffer: {exc}")
            self._notify(f"The clips folder is available again, but the replay buffer didn't start: {exc}")
            return
        log.info("Started the replay buffer now that the clips folder is there.")
        self._notify("The clips folder is available again - replay buffer started.")

    def create_missing_dir(self, client, replay_active):
        """The "Create Clips Folder" menu action: creates the missing folder,
        then starts the replay buffer on the next check."""
        directory = self.missing_dir
        if directory is None:
            return
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            log.warning(f"Couldn't create the clips folder {directory}: {exc}")
            self._notify(f"Couldn't create the clips folder {directory}: {exc}")
            return
        log.info(f"Created the clips folder {directory} (from the tray menu).")
        self._waiting = True
        self.tick(client, replay_active)
