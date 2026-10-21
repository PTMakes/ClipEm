"""
Post-processes every saved replay-buffer clip, in a background thread:

1. Split audio tracks (split_tracks_enabled): the replay buffer records 3
   audio tracks (see setup.save_split_tracks) - this copies all of them,
   losslessly and always in the same order, into "<clip>-tracks.mkv" with
   each track named (Mix, Mic, Desktop), and strips the clip itself back to
   track 1, the full mix, so it plays the same everywhere. Where the tray
   widget records the apps too (tray_tracks_enabled), it does all of this
   itself, adding their tracks, and this script leaves the clip alone.
2. Smaller copy (reexport_enabled): the last `crop_seconds` of the clip,
   re-encoded to H.264/AAC MP4 at the bitrate that lands it near
   `target_mb` - sized for upload limits (e.g. Discord's 20 MB). The
   full-quality clip is never re-encoded.

Settings (set by ClipEm's setup wizard, editable in OBS's Tools > Scripts):
  split_tracks_enabled, tray_tracks_enabled, reexport_enabled, target_mb,
  crop_seconds

Uses the ffmpeg ClipEm installs (App/Windows/ffmpeg or App/Linux/ffmpeg,
built from official source by Dev/build/build-ffmpeg.sh), falling back to one on
PATH (macOS). Encoding runs in a background thread at low CPU priority so it
never stalls OBS or the game being recorded.
"""

import os
import platform
import shutil
import subprocess  # noqa: F401 - tests patch clip_tracks' subprocess.run through this name
import sys
import threading

import obspython as obs

# clip_tracks.py sits next to this script; OBS runs scripts with their folder
# on the path, but not everything that loads this one does.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clip_tracks import (  # noqa: F401 - the rest are this script's public names
    AUDIO_BITRATE_KBPS,
    DEFAULT_CROP_SECONDS,
    DEFAULT_TARGET_MB,
    FIRST_APP_TRACK,
    FIXED_TRACK_TITLES,
    KBIT_PER_MB,
    MIN_VIDEO_BITRATE_KBPS,
    OUTPUT_SUFFIX,
    SILENCE_MAX_DB,
    SIZE_HEADROOM,
    TRACKS_SUFFIX,
    build_command,
    build_split_command,
    build_strip_command,
    low_priority_kwargs,
    max_volume_command,
    output_path_for,
    parse_max_volume,
    reexport,
    run_to,
    silent_app_tracks,
    track_titles,
    tracks_path_for,
    video_bitrate_kbps,
)

_SYSTEM = platform.system()

# This script's own folder (App/Shared/Scripts). Not OBS's script_path(),
# which OBS injects into the script's module rather than obspython.
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

split_tracks_enabled = False
tray_tracks_enabled = False
reexport_enabled = False
target_mb = DEFAULT_TARGET_MB
crop_seconds = DEFAULT_CROP_SECONDS


def find_ffmpeg(scripts_dir):
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(scripts_dir)))
    exe = "ffmpeg.exe" if _SYSTEM == "Windows" else "ffmpeg"
    platform_dir = {"Windows": "Windows", "Linux": "Linux", "Darwin": "macOS"}.get(_SYSTEM, "")
    bundled = os.path.join(app_dir, platform_dir, "ffmpeg", "bin", exe)
    if os.path.isfile(bundled):
        return bundled
    return shutil.which("ffmpeg")


def split_tracks(ffmpeg, clip_path, app_labels, log):
    """Writes the "-tracks.mkv" copy, then strips the clip to the mix. The
    clip keeps every track if the copy couldn't be made."""
    out_path = tracks_path_for(clip_path)
    titles = track_titles(app_labels, silent_app_tracks(ffmpeg, clip_path, app_labels, log))
    if not run_to(lambda partial: build_split_command(ffmpeg, clip_path, partial, titles),
                   out_path, f"splitting the audio tracks of {clip_path}", log):
        return None
    log(f"saved {out_path} (tracks: {', '.join(titles)})")
    # The .partial sits next to the clip, so os.replace over it is atomic
    # (build_strip_command names the container, which ".partial" hides).
    if not run_to(lambda partial: build_strip_command(ffmpeg, clip_path, partial),
                   clip_path, f"stripping {clip_path} to the mixed track", log):
        log(f"{clip_path} still has every audio track")
    return out_path


def process_clip(ffmpeg, clip_path, options, log):
    """Everything for one saved clip, one step after another - the steps
    read the clip the step before may have replaced."""
    if options.get("split_tracks"):
        split_tracks(ffmpeg, clip_path, options.get("app_labels", ()), log)
    if options.get("reexport"):
        reexport(ffmpeg, clip_path, options["target_mb"], options["crop_seconds"], log)


# ---------------------------------------------------------------------------
# OBS script API
# ---------------------------------------------------------------------------

def script_description():
    return (
        "Saves a copy of every replay-buffer clip with each audio track kept "
        f"separate (\"{TRACKS_SUFFIX}.mkv\"), and/or a smaller copy "
        f"(\"{OUTPUT_SUFFIX}.mp4\"), cropped to the last N seconds and sized for "
        "upload limits such as Discord's."
    )


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(
        props, "split_tracks_enabled", "Save a copy with every audio track (\"-tracks.mkv\")"
    )
    obs.obs_properties_add_bool(props, "reexport_enabled", "Save a smaller copy of each clip")
    obs.obs_properties_add_float(props, "target_mb", "Target file size (MB)", 1.0, 2000.0, 0.5)
    obs.obs_properties_add_int(props, "crop_seconds", "Keep the last N seconds", 1, 3600, 1)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "split_tracks_enabled", False)
    obs.obs_data_set_default_bool(settings, "tray_tracks_enabled", False)
    obs.obs_data_set_default_bool(settings, "reexport_enabled", False)
    obs.obs_data_set_default_double(settings, "target_mb", DEFAULT_TARGET_MB)
    obs.obs_data_set_default_int(settings, "crop_seconds", DEFAULT_CROP_SECONDS)


def script_update(settings):
    global split_tracks_enabled, tray_tracks_enabled, reexport_enabled, target_mb, crop_seconds
    split_tracks_enabled = obs.obs_data_get_bool(settings, "split_tracks_enabled")
    tray_tracks_enabled = obs.obs_data_get_bool(settings, "tray_tracks_enabled")
    reexport_enabled = obs.obs_data_get_bool(settings, "reexport_enabled")
    target_mb = obs.obs_data_get_double(settings, "target_mb")
    crop_seconds = obs.obs_data_get_int(settings, "crop_seconds")


def script_load(settings):
    obs.obs_frontend_add_event_callback(on_event)


def script_unload():
    obs.obs_frontend_remove_event_callback(on_event)


def _log(message):
    obs.script_log(obs.LOG_INFO, f"clip_reexport: {message}")


def on_event(event):
    if event != obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED or not (split_tracks_enabled or reexport_enabled):
        return
    if tray_tracks_enabled:
        # The tray widget records the app tracks and finishes the clip
        # (tray-widget/tray_tracks.py) - two writers would race on it.
        return
    clip_path = obs.obs_frontend_get_last_replay()
    if not clip_path or not os.path.isfile(clip_path):
        _log(f"saved clip not found ({clip_path!r}) - skipping")
        return
    ffmpeg = find_ffmpeg(SCRIPTS_DIR)
    if not ffmpeg:
        _log("no ffmpeg found (re-run ClipEm Setup to install it) - skipping")
        return
    options = {
        "split_tracks": split_tracks_enabled,
        "app_labels": [],
        "reexport": reexport_enabled,
        "target_mb": target_mb,
        "crop_seconds": crop_seconds,
    }
    threading.Thread(target=process_clip, args=(ffmpeg, clip_path, options, _log), daemon=True).start()
