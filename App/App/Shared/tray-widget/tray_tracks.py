"""Finishes every saved clip when the tray widget records the app tracks (see
obs_config's notes on app_tracks.json) - the work Scripts/clip_reexport.py
does inside OBS when it doesn't:

1. "<clip>-tracks.mkv": the clip's own tracks (Mix, Mic, Desktop) plus one
   track per app track, each cut from what the tray recorded and lined up
   with the clip (track_align.py), named after the app or the track's rule.
2. The clip itself is stripped back to the mix, so it plays the same everywhere.
3. The smaller copy, if that's on.

OBS tells the tray a clip was saved over obs-websocket (ReplayBufferSaved) -
the snapshot of the recorded audio is taken right then, before the rings move
on; the rest runs on a worker thread at low priority.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading

import clip_edit
import obs_config
import track_align
from app_recorder import RATE
from clipem_common import APP_DIR

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Scripts"))
import clip_tracks  # noqa: E402 - Scripts/clip_tracks.py, shared with the OBS script

log = logging.getLogger("tray")

# How much recorded sound is taken either side of where the clip should be,
# to find where it really is in.
WINDOW_MARGIN_SECONDS = 3.0
SEARCH_SECONDS = 2.5
# The clip ends this long before OBS says it was saved (the file is written,
# then announced).
EVENT_LATENCY_SECONDS = 0.4
# Matches this good or better are believed; below it the tracks go where the
# clock says (a track's own match is only used when it's clearly there).
MIN_SCORE = 0.15
MIN_TRACK_SCORE = 0.3
TRACK_SPAN_MS = 60
# ...and has to be clearly better than where the others are: OBS's own app
# tracks sit a few dozen ms off its Desktop track too, and a sharp match
# there is the truth, while a flat one is noise.
TRACK_BETTER_BY = 0.05
DESKTOP_TRACK = len(clip_tracks.FIXED_TRACK_TITLES) - 1  # index of the Desktop track in the clip
ANALYSIS_RATE = 8000


def find_ffmpeg():
    exe, platform_dir = {"win32": ("ffmpeg.exe", "Windows"), "linux": ("ffmpeg", "Linux")}.get(
        sys.platform, ("ffmpeg", "macOS"))
    bundled = os.path.join(APP_DIR, platform_dir, "ffmpeg", "bin", exe)
    return bundled if os.path.isfile(bundled) else shutil.which("ffmpeg")


def desktop_pcm_command(ffmpeg, clip_path, rate=ANALYSIS_RATE):
    """The clip's Desktop track as mono 16-bit PCM on stdout."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", clip_path,
            "-map", f"0:a:{DESKTOP_TRACK}", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]


def align_tracks(snapshot, track_ids, clip_env, duration, log=log.info):
    """Where each track's sound for the clip starts, from the snapshot of
    what the tray recorded: {track id: (start frame, silent)}. clip_env: the
    clip's Desktop envelope (track_align)."""
    count = round(duration * RATE)
    nominal_start = snapshot.end_frame - round(EVENT_LATENCY_SECONDS * RATE) - count
    margin = round(WINDOW_MARGIN_SECONDS * RATE)
    window_start = nominal_start - margin
    window_end = nominal_start + count + margin
    envelopes = {
        track_id: track_align.stereo_envelope(snapshot.window(track_id, window_start, window_end), RATE)
        for track_id in track_ids
    }
    nominal = round(WINDOW_MARGIN_SECONDS * track_align.ENV_RATE)
    lag, score = nominal, 0.0
    total = track_align.add_envelopes(envelopes.values())
    if total and clip_env and not track_align.is_silent(total, RATE // track_align.ENV_RATE):
        found, score = track_align.locate(clip_env, total, nominal, round(SEARCH_SECONDS * track_align.ENV_RATE))
        if score >= MIN_SCORE:
            lag = found
    if score >= MIN_SCORE:
        log(f"app tracks line up with the clip {lag - nominal:+d} ms from where the clock puts them "
            f"(match {score:.2f})")
    else:
        log("app tracks couldn't be matched to the clip's sound (quiet clip?) - using the clock")

    clip_length = round(duration * track_align.ENV_RATE)
    starts = {}
    for track_id, env in envelopes.items():
        quiet = track_align.is_silent(env[lag:lag + clip_length], RATE // track_align.ENV_RATE)
        track_lag = lag
        if not quiet and score >= MIN_SCORE:
            found, track_score, score_at_lag = track_align.refine(clip_env, env, lag, TRACK_SPAN_MS)
            if track_score >= MIN_TRACK_SCORE and track_score >= score_at_lag + TRACK_BETTER_BY:
                track_lag = found
        starts[track_id] = (window_start + track_lag * RATE // track_align.ENV_RATE, quiet)
    return starts


def finish_clip(clip_path, snapshot, tracks, ffmpeg, options, log=log.info, run=subprocess.run):
    """Everything for one saved clip. tracks: [(track id, label)] for the app
    tracks, in order; options: {"reexport", "target_mb", "crop_seconds"}."""
    kwargs = clip_tracks.low_priority_kwargs()
    info = clip_edit.probe(ffmpeg, clip_path)
    duration = info["duration"]
    if len(info["audio"]) < len(clip_tracks.FIXED_TRACK_TITLES):
        log(f"{clip_path} has {len(info['audio'])} audio track(s), not "
            f"{len(clip_tracks.FIXED_TRACK_TITLES)} - Split audio tracks wasn't on when it was recorded; "
            "leaving it alone")
    else:
        clip_env = None
        try:
            result = run(desktop_pcm_command(ffmpeg, clip_path), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         **kwargs)
            if result.returncode == 0:
                clip_env = track_align.mono_envelope(result.stdout, ANALYSIS_RATE)
        except Exception as exc:
            log(f"couldn't read the clip's Desktop track ({exc}) - lining the app tracks up by the clock")
        starts = align_tracks(snapshot, [t[0] for t in tracks], clip_env, duration, log)
        split_tracks(clip_path, snapshot, tracks, starts, duration, ffmpeg, log)
    if options.get("reexport"):
        clip_tracks.reexport(ffmpeg, clip_path, options["target_mb"], options["crop_seconds"], log)


def split_tracks(clip_path, snapshot, tracks, starts, duration, ffmpeg, log):
    """Writes the "-tracks.mkv", then strips the clip to the mix. The clip
    keeps every track if the copy couldn't be made."""
    out_path = clip_tracks.tracks_path_for(clip_path)
    count = round(duration * RATE)
    titles = list(clip_tracks.FIXED_TRACK_TITLES)
    with tempfile.TemporaryDirectory(prefix="clipem-tracks-") as temp:
        pcm_paths = []
        for number, (track_id, label) in enumerate(tracks, start=clip_tracks.FIRST_APP_TRACK):
            start, quiet = starts[track_id]
            path = os.path.join(temp, f"{track_id}.mov")
            clip_tracks.write_pcm_mov(path, snapshot.window(track_id, start, start + count))
            pcm_paths.append(path)
            titles.append(f"Track-{number}" if quiet or not label else label)
        if not clip_tracks.run_to(
                lambda partial: clip_tracks.build_tray_split_command(ffmpeg, clip_path, partial, titles, pcm_paths),
                out_path, f"splitting the audio tracks of {clip_path}", log):
            return None
    log(f"saved {out_path} (tracks: {', '.join(titles)})")
    # The .partial sits next to the clip, so os.replace over it is atomic
    # (build_strip_command names the container, which ".partial" hides).
    if not clip_tracks.run_to(lambda partial: clip_tracks.build_strip_command(ffmpeg, clip_path, partial),
                              clip_path, f"stripping {clip_path} to the mixed track", log):
        log(f"{clip_path} still has every audio track")
    return out_path


class ClipFinisher:
    """Hooks the tray up to OBS's ReplayBufferSaved event."""

    def __init__(self, recorder, monitor, enabled=lambda: obs_config.load_tray_tracks()
                 and obs_config.app_capture_supported()):
        self._recorder = recorder
        self._monitor = monitor
        self._enabled = enabled

    def on_replay_saved(self, event):
        """Runs on the websocket's thread: only takes the snapshot, which has
        to be now, and hands the rest to a worker."""
        try:
            if not self._enabled():
                return
            clip_path = (event or {}).get("savedReplayPath")
            if not clip_path:
                return
            snapshot = self._recorder.snapshot()
            state = self._monitor.state() or obs_config.load_app_tracks()
            tracks = [
                (track["id"], obs_config.track_label(track, number))
                for number, track in enumerate(state["tracks"], start=obs_config.FIRST_APP_TRACK)
            ]
            settings = obs_config.load_clip_script_settings()
        except Exception:
            log.exception("Couldn't start finishing the saved clip")
            return
        options = {
            "reexport": bool(settings.get("reexport_enabled")),
            "target_mb": float(settings.get("target_mb", clip_tracks.DEFAULT_TARGET_MB)),
            "crop_seconds": int(settings.get("crop_seconds", clip_tracks.DEFAULT_CROP_SECONDS)),
        }
        threading.Thread(target=self._finish, args=(clip_path, snapshot, tracks, options), daemon=True).start()

    @staticmethod
    def _finish(clip_path, snapshot, tracks, options):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            log.warning("No ffmpeg found (re-run ClipEm Setup to install it) - not finishing the saved clip.")
            return
        if not os.path.isfile(clip_path):
            log.warning(f"Saved clip not found ({clip_path!r}) - skipping.")
            return
        try:
            finish_clip(clip_path, snapshot, tracks, ffmpeg, options, log.info)
        except Exception:
            log.exception(f"Finishing {clip_path} failed")

