"""Who finishes a saved clip - renames it and makes its copies - when the tray
widget records app tracks (tray-widget/tray_tracks.py) and the OBS script
(Scripts/clip_reexport.py) could both do it.

The tray finishes every clip it hears about, claiming it the moment OBS's
ReplayBufferSaved event arrives. The OBS script hears about every save too,
waits CLAIM_WAIT_SECONDS, and finishes whatever the tray didn't claim by then
(without app tracks - only the tray records those): the tray was
disconnected or restarting when the clip was saved, or isn't running at all.
Two writers on one clip would race, so a claim is a file created with
O_EXCL - only one of them can ever make it. No OBS imports here.
"""

import hashlib
import os
import time

SHARED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIMS_DIR = os.path.join(os.path.dirname(SHARED_DIR), "cache", "clip_claims")
# How long the OBS script gives the tray. The tray claims as the event comes
# in - milliseconds after the save - so this is only for a busy tray.
CLAIM_WAIT_SECONDS = 8
# Claims are only needed while both could still act on the save.
CLAIM_KEEP_SECONDS = 24 * 60 * 60


def _key(clip_path):
    normalized = os.path.normcase(os.path.abspath(clip_path))
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _prune(directory, now):
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(path) > CLAIM_KEEP_SECONDS:
                os.remove(path)
        except OSError:
            pass


def claim(clip_path, who, directory=None, now=None):
    """True if `who` ("tray" or "obs") now finishes clip_path - False if the
    other one already claimed it. A claim that can't be written at all (no
    cache folder, read-only disk) counts as made: finishing a clip twice is
    better than not at all."""
    directory = directory or CLAIMS_DIR
    now = time.time() if now is None else now
    try:
        os.makedirs(directory, exist_ok=True)
        _prune(directory, now)
        fd = os.open(os.path.join(directory, _key(clip_path)), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return True
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{who} {clip_path}\n")
    return True


def claimed_by(clip_path, directory=None):
    """Who claimed clip_path ("tray"/"obs"), or None."""
    try:
        with open(os.path.join(directory or CLAIMS_DIR, _key(clip_path)), encoding="utf-8") as f:
            return f.read().split(" ", 1)[0] or None
    except OSError:
        return None
