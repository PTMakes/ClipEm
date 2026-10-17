"""
Post-processes every saved replay-buffer clip, in a background thread:

1. Split audio tracks (split_tracks_enabled): the replay buffer records 6
   audio tracks (see setup.save_split_tracks) - this copies all of them,
   losslessly and always in the same order, into "<clip>-tracks.mkv" with
   each track named (Mix, Mic, Desktop, then the app on each app slot), and
   strips the clip itself back to track 1, the full mix, so it plays the
   same everywhere.
2. Smaller copy (reexport_enabled): the last `crop_seconds` of the clip,
   re-encoded to H.264/AAC MP4 at the bitrate that lands it near
   `target_mb` - sized for upload limits (e.g. Discord's 20 MB). The
   full-quality clip is never re-encoded.

Settings (set by ClipEm's setup wizard, editable in OBS's Tools > Scripts):
  split_tracks_enabled, reexport_enabled, target_mb, crop_seconds

Uses the ffmpeg ClipEm installs (App/Windows/ffmpeg or App/Linux/ffmpeg,
built from official source by Dev/build/build-ffmpeg.sh), falling back to one on
PATH (macOS). Encoding runs in a background thread at low CPU priority so it
never stalls OBS or the game being recorded.
"""

import os
import platform
import re
import shutil
import subprocess
import threading

import obspython as obs

# Must match setup.py's AUDIO_BITRATE_KBPS / compute_video_bitrate_for_filesize.
AUDIO_BITRATE_KBPS = 160
# Decimal MB, as upload limits like Discord's count them.
KBIT_PER_MB = 8000
MIN_VIDEO_BITRATE_KBPS = 500
# Container/muxing overhead and encoder overshoot - aim a little under target.
SIZE_HEADROOM = 0.95
OUTPUT_SUFFIX = "-small"
TRACKS_SUFFIX = "-tracks"

# Must match obs_config's APP_SLOT_SOURCE_NAMES / APP_SLOT_LABEL_KEY (this
# runs inside OBS, which can't import ClipEm's Installer modules).
FIXED_TRACK_TITLES = ("Mix", "Mic", "Desktop")
APP_SLOT_SOURCE_NAMES = ("ClipEm App A", "ClipEm App B", "ClipEm App C")
APP_SLOT_LABEL_KEY = "clipem_label"
FIRST_APP_TRACK = len(FIXED_TRACK_TITLES) + 1
# An app track whose loudest moment is below this had no sound in the clip:
# its slot's owner wasn't playing (a slot keeps its owner for days).
SILENCE_MAX_DB = -70.0

# Discord's free upload limit - must match obs_config.DISCORD_TARGET_MB.
DEFAULT_TARGET_MB = 20.0
DEFAULT_CROP_SECONDS = 30

_SYSTEM = platform.system()

# This script's own folder (App/Shared/Scripts). Not OBS's script_path(),
# which OBS injects into the script's module rather than obspython.
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

split_tracks_enabled = False
reexport_enabled = False
target_mb = DEFAULT_TARGET_MB
crop_seconds = DEFAULT_CROP_SECONDS


def video_bitrate_kbps(target_mb, duration_seconds):
    duration_seconds = max(1, duration_seconds)
    total_kbps = target_mb * KBIT_PER_MB * SIZE_HEADROOM / duration_seconds
    return max(MIN_VIDEO_BITRATE_KBPS, int(total_kbps - AUDIO_BITRATE_KBPS))


def output_path_for(clip_path):
    stem, _ext = os.path.splitext(clip_path)
    return f"{stem}{OUTPUT_SUFFIX}.mp4"


def build_command(ffmpeg, clip_path, out_path, target_mb, crop_seconds):
    kbps = video_bitrate_kbps(target_mb, crop_seconds)
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        # Seek relative to the end: keep only the last crop_seconds (the whole
        # clip if it's shorter).
        "-sseof", f"-{int(crop_seconds)}", "-i", clip_path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
        "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k",
        "-movflags", "+faststart",
        "-f", "mp4", out_path,
    ]


def tracks_path_for(clip_path):
    stem, _ext = os.path.splitext(clip_path)
    return f"{stem}{TRACKS_SUFFIX}.mkv"


def track_titles(app_labels, silent=()):
    """The 6 track names: Mix, Mic, Desktop, then each app slot's owner -
    or "Track-N" when the slot has none or was silent all clip (the indexes
    in `silent`, 0 for the first app track) - so track N always means the
    same thing."""
    titles = list(FIXED_TRACK_TITLES)
    for index, label in enumerate(app_labels):
        titles.append(label if label and index not in silent else f"Track-{FIRST_APP_TRACK + index}")
    return titles


def max_volume_command(ffmpeg, clip_path, track_index):
    return [ffmpeg, "-hide_banner", "-nostdin", "-i", clip_path, "-map", f"0:a:{track_index}",
            "-af", "volumedetect", "-f", "null", "-"]


def parse_max_volume(stderr):
    """volumedetect's max_volume in dB (-inf for digital silence), or None."""
    match = re.search(r"max_volume:\s*(-?inf|-?[\d.]+) dB", stderr or "")
    if not match:
        return None
    return float("-inf") if "inf" in match.group(1) else float(match.group(1))


def silent_app_tracks(ffmpeg, clip_path, app_labels, log):
    """The indexes of the labelled app tracks with no sound in the clip. A
    track that can't be measured counts as having sound (keeps its label)."""
    silent = set()
    for index, label in enumerate(app_labels):
        if not label:
            continue
        try:
            result = subprocess.run(max_volume_command(ffmpeg, clip_path, FIRST_APP_TRACK - 1 + index),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    errors="replace", **_low_priority_kwargs())
        except Exception as exc:
            log(f"couldn't measure {label}'s track: {exc}")
            continue
        level = parse_max_volume(result.stderr) if result.returncode == 0 else None
        if level is not None and level < SILENCE_MAX_DB:
            silent.add(index)
    return silent


def build_split_command(ffmpeg, clip_path, out_path, titles):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", clip_path, "-map", "0:v:0"]
    for index in range(len(titles)):
        cmd += ["-map", f"0:a:{index}"]
    cmd += ["-c", "copy"]
    for index, title in enumerate(titles):
        cmd += [f"-metadata:s:a:{index}", f"title={title}"]
    cmd += ["-disposition:a:0", "default", "-f", "matroska", out_path]
    return cmd


def build_strip_command(ffmpeg, clip_path, out_path):
    """The clip with only its first audio track (the full mix), losslessly."""
    _stem, ext = os.path.splitext(clip_path)
    container = {".mkv": "matroska", ".mov": "mov", ".flv": "flv"}.get(ext.lower(), "mp4")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", clip_path,
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
    ]
    if container in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]
    return cmd + ["-f", container, out_path]


def find_ffmpeg(scripts_dir):
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(scripts_dir)))
    exe = "ffmpeg.exe" if _SYSTEM == "Windows" else "ffmpeg"
    platform_dir = {"Windows": "Windows", "Linux": "Linux", "Darwin": "macOS"}.get(_SYSTEM, "")
    bundled = os.path.join(app_dir, platform_dir, "ffmpeg", "bin", exe)
    if os.path.isfile(bundled):
        return bundled
    return shutil.which("ffmpeg")


def _low_priority_kwargs():
    if _SYSTEM == "Windows":
        below_normal_priority = 0x00004000
        create_no_window = 0x08000000
        return {"creationflags": below_normal_priority | create_no_window}
    return {"preexec_fn": lambda: os.nice(10)}


def _run_to(cmd_for, out_path, what, log):
    """Runs cmd_for(partial path) and moves the result to out_path. True on
    success; never leaves the .partial file behind."""
    partial = out_path + ".partial"
    try:
        result = subprocess.run(
            cmd_for(partial), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            **_low_priority_kwargs()
        )
        if result.returncode != 0:
            log(f"{what} failed: {result.stderr.strip()[-500:]}")
            return False
        os.replace(partial, out_path)
        return True
    except Exception as exc:
        log(f"{what} failed: {exc}")
        return False
    finally:
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                pass


def reexport(ffmpeg, clip_path, target_mb, crop_seconds, log):
    out_path = output_path_for(clip_path)
    if not _run_to(lambda partial: build_command(ffmpeg, clip_path, partial, target_mb, crop_seconds),
                   out_path, f"re-export of {clip_path}", log):
        return None
    log(f"re-exported {out_path} ({os.path.getsize(out_path) / 1048576:.1f} MB)")
    return out_path


def split_tracks(ffmpeg, clip_path, app_labels, log):
    """Writes the "-tracks.mkv" copy, then strips the clip to the mix. The
    clip keeps every track if the copy couldn't be made."""
    out_path = tracks_path_for(clip_path)
    titles = track_titles(app_labels, silent_app_tracks(ffmpeg, clip_path, app_labels, log))
    if not _run_to(lambda partial: build_split_command(ffmpeg, clip_path, partial, titles),
                   out_path, f"splitting the audio tracks of {clip_path}", log):
        return None
    log(f"saved {out_path} (tracks: {', '.join(titles)})")
    # The .partial sits next to the clip, so os.replace over it is atomic
    # (build_strip_command names the container, which ".partial" hides).
    if not _run_to(lambda partial: build_strip_command(ffmpeg, clip_path, partial),
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
    obs.obs_data_set_default_bool(settings, "reexport_enabled", False)
    obs.obs_data_set_default_double(settings, "target_mb", DEFAULT_TARGET_MB)
    obs.obs_data_set_default_int(settings, "crop_seconds", DEFAULT_CROP_SECONDS)


def script_update(settings):
    global split_tracks_enabled, reexport_enabled, target_mb, crop_seconds
    split_tracks_enabled = obs.obs_data_get_bool(settings, "split_tracks_enabled")
    reexport_enabled = obs.obs_data_get_bool(settings, "reexport_enabled")
    target_mb = obs.obs_data_get_double(settings, "target_mb")
    crop_seconds = obs.obs_data_get_int(settings, "crop_seconds")


def script_load(settings):
    obs.obs_frontend_add_event_callback(on_event)


def script_unload():
    obs.obs_frontend_remove_event_callback(on_event)


def _log(message):
    obs.script_log(obs.LOG_INFO, f"clip_reexport: {message}")


def app_slot_labels():
    """Who each app slot source is pointed at right now (set by the tray
    widget) - read on OBS's thread, before the worker starts."""
    labels = []
    for name in APP_SLOT_SOURCE_NAMES:
        label = ""
        source = obs.obs_get_source_by_name(name)
        if source:
            settings = obs.obs_source_get_settings(source)
            label = obs.obs_data_get_string(settings, APP_SLOT_LABEL_KEY)
            obs.obs_data_release(settings)
            obs.obs_source_release(source)
        labels.append(label)
    return labels


def on_event(event):
    if event != obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED or not (split_tracks_enabled or reexport_enabled):
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
        "app_labels": app_slot_labels() if split_tracks_enabled else [],
        "reexport": reexport_enabled,
        "target_mb": target_mb,
        "crop_seconds": crop_seconds,
    }
    threading.Thread(target=process_clip, args=(ffmpeg, clip_path, options, _log), daemon=True).start()
