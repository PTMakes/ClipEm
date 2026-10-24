"""The ffmpeg work on a saved clip, shared by Scripts/clip_reexport.py (which
runs inside OBS) and the tray widget (tray-widget/tray_tracks.py, which does
the same for clips with app tracks it recorded itself). No OBS imports here:
the tray can't import obspython.

- Smaller copy: the last N seconds re-encoded to H.264/AAC MP4 at the bitrate
  that lands it near a target size (Discord's upload limit).
- "-tracks.mkv": every audio track of the clip, named, in a fixed order -
  Mix, Mic, Desktop, then the apps - and the clip stripped back to the mix.
"""

import os
import platform
import re
import struct
import subprocess

# Must match setup.py's AUDIO_BITRATE_KBPS / compute_video_bitrate_for_filesize.
AUDIO_BITRATE_KBPS = 160
# Decimal MB, as upload limits like Discord's count them.
KBIT_PER_MB = 8000
MIN_VIDEO_BITRATE_KBPS = 500
# Container/muxing overhead and encoder overshoot - aim a little under target.
SIZE_HEADROOM = 0.95
OUTPUT_SUFFIX = "-small"
TRACKS_SUFFIX = "-tracks"

# Must match obs_config's FIXED_TRACK_TITLES / FIRST_APP_TRACK (the OBS script
# can't import ClipEm's Installer modules).
FIXED_TRACK_TITLES = ("Mix", "Mic", "Desktop")
FIRST_APP_TRACK = len(FIXED_TRACK_TITLES) + 1
# An app track whose loudest moment is below this had no sound in the clip:
# its owner wasn't playing (an auto track keeps its owner for days).
SILENCE_MAX_DB = -70.0

# Discord's free upload limit - must match obs_config.DISCORD_TARGET_MB.
DEFAULT_TARGET_MB = 20.0
DEFAULT_CROP_SECONDS = 30

# What app tracks recorded by the tray are: raw 16-bit stereo at 48 kHz.
PCM_RATE = 48000
PCM_CHANNELS = 2
PCM_BYTES_PER_FRAME = 4

_SYSTEM = platform.system()


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
    """The track names: Mix, Mic, Desktop, then each app track's label -
    or "Track-N" when it has none or was silent all clip (the indexes in
    `silent`, 0 for the first app track) - so track N always means the
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
                                    errors="replace", **low_priority_kwargs())
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


def build_tray_split_command(ffmpeg, clip_path, out_path, titles, pcm_paths):
    """The "-tracks.mkv" when the tray recorded the app tracks: the clip's
    video and its OBS audio tracks (Mix, Mic, Desktop) copied, then one
    write_pcm_mov() file per app track encoded as AAC, in order. Every one is
    as long as the clip. titles: one per track, OBS tracks first."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", clip_path]
    for path in pcm_paths:
        cmd += ["-i", path]
    cmd += ["-map", "0:v:0"]
    for index in range(len(FIXED_TRACK_TITLES)):
        cmd += ["-map", f"0:a:{index}"]
    for number in range(len(pcm_paths)):
        cmd += ["-map", f"{number + 1}:a:0"]
    cmd += ["-c", "copy"]
    for index in range(len(FIXED_TRACK_TITLES), len(titles)):
        cmd += [f"-c:a:{index}", "aac", f"-b:a:{index}", f"{AUDIO_BITRATE_KBPS}k"]
    for index, title in enumerate(titles):
        cmd += [f"-metadata:s:a:{index}", f"title={title}"]
    # The PCM inputs bring their own "default" flag: clear it so only Mix is default.
    cmd += ["-disposition:a", "0", "-disposition:a:0", "default", "-f", "matroska", out_path]
    return cmd


def _box(kind, *payload):
    body = b"".join(payload)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def write_pcm_mov(path, pcm):
    """Wraps raw 16-bit stereo 48 kHz PCM in a minimal QuickTime file (one
    'sowt' audio track) - ClipEm's ffmpeg is built without a raw PCM demuxer
    but can read this. Chunks of 100 ms keep the packets it makes small."""
    frames = len(pcm) // PCM_BYTES_PER_FRAME
    pcm = pcm[:frames * PCM_BYTES_PER_FRAME]
    chunk = PCM_RATE // 10
    full_chunks, rest = divmod(frames, chunk)
    ftyp = _box(b"ftyp", b"qt  ", struct.pack(">I", 0), b"qt  ")
    mdat_start = len(ftyp) + 8
    offsets = [mdat_start + i * chunk * PCM_BYTES_PER_FRAME for i in range(full_chunks + (1 if rest else 0))]
    stsc = [(1, chunk, 1)] if full_chunks else []
    if rest:
        stsc.append((full_chunks + 1, rest, 1))
    millis = frames * 1000 // PCM_RATE
    identity = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    version0 = struct.pack(">I", 0)
    entry = (b"\0" * 6 + struct.pack(">HHHIHHHHI", 1, 0, 0, 0, PCM_CHANNELS, 16, 0, 0, PCM_RATE << 16))
    stbl = _box(
        b"stbl",
        _box(b"stsd", version0, struct.pack(">I", 1), _box(b"sowt", entry)),
        _box(b"stts", version0, struct.pack(">III", 1, frames, 1)),
        _box(b"stsc", version0, struct.pack(">I", len(stsc)), *(struct.pack(">III", *e) for e in stsc)),
        _box(b"stsz", version0, struct.pack(">II", PCM_BYTES_PER_FRAME, frames)),
        _box(b"stco", version0, struct.pack(">I", len(offsets)), *(struct.pack(">I", o) for o in offsets)),
    )
    minf = _box(
        b"minf",
        _box(b"smhd", version0, struct.pack(">HH", 0, 0)),
        _box(b"dinf", _box(b"dref", version0, struct.pack(">I", 1), _box(b"url ", struct.pack(">I", 1)))),
        stbl,
    )
    mdia = _box(
        b"mdia",
        _box(b"mdhd", version0, struct.pack(">IIIIHH", 0, 0, PCM_RATE, frames, 0x55C4, 0)),
        _box(b"hdlr", version0, b"mhlr", b"soun", struct.pack(">III", 0, 0, 0), b"\0"),
        minf,
    )
    trak = _box(
        b"trak",
        _box(b"tkhd", struct.pack(">I", 7), struct.pack(">IIIII", 0, 0, 1, 0, millis), b"\0" * 8,
             struct.pack(">HHHH", 0, 0, 0x100, 0), identity, struct.pack(">II", 0, 0)),
        mdia,
    )
    moov = _box(
        b"moov",
        _box(b"mvhd", version0, struct.pack(">IIII", 0, 0, 1000, millis), struct.pack(">IH", 0x10000, 0x100),
             b"\0" * 10, identity, b"\0" * 24, struct.pack(">I", 2)),
        trak,
    )
    with open(path, "wb") as f:
        f.write(ftyp)
        f.write(struct.pack(">I4s", 8 + len(pcm), b"mdat"))
        f.write(pcm)
        f.write(moov)


def low_priority_kwargs():
    if _SYSTEM == "Windows":
        below_normal_priority = 0x00004000
        create_no_window = 0x08000000
        return {"creationflags": below_normal_priority | create_no_window}
    return {"preexec_fn": lambda: os.nice(10)}


def run_to(cmd_for, out_path, what, log):
    """Runs cmd_for(partial path) and moves the result to out_path. True on
    success; never leaves the .partial file behind."""
    partial = out_path + ".partial"
    try:
        result = subprocess.run(
            cmd_for(partial), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            errors="replace", **low_priority_kwargs()
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
    if not run_to(lambda partial: build_command(ffmpeg, clip_path, partial, target_mb, crop_seconds),
                   out_path, f"re-export of {clip_path}", log):
        return None
    log(f"re-exported {out_path} ({os.path.getsize(out_path) / 1048576:.1f} MB)")
    return out_path
