"""Replacing or renaming a clip that something may still have open.

On Windows a file that's open can't be replaced or renamed - not even when
whatever has it open only reads it: os.replace() and os.rename() fail with
"Access is denied" (PermissionError). Right after a clip is saved, the clip
editor may be reading it (its thumbnail, waveforms, length: a few seconds of
ffmpeg each), and the clip is finished then - cut, stripped to its mix track,
renamed. So these wait for the file to be let go, up to WAIT_SECONDS, before
giving up with the error. Elsewhere they're os.replace() and os.rename()."""

import os
import sys
import time

WAIT_SECONDS = 15.0
POLL_SECONDS = 0.25


def _when_free(move, src, dst, wait_seconds, sleep):
    deadline = time.monotonic() + (WAIT_SECONDS if wait_seconds is None else wait_seconds)
    while True:
        try:
            return move(src, dst)
        except PermissionError:
            if sys.platform != "win32" or time.monotonic() >= deadline:
                raise
            sleep(POLL_SECONDS)


def replace(src, dst, wait_seconds=None, sleep=time.sleep):
    """os.replace(src, dst), once nothing has dst (or src) open."""
    return _when_free(os.replace, src, dst, wait_seconds, sleep)


def rename(src, dst, wait_seconds=None, sleep=time.sleep):
    """os.rename(src, dst), once nothing has src open."""
    return _when_free(os.rename, src, dst, wait_seconds, sleep)
