"""The clip editor's media work, all done with ClipEm's ffmpeg (there's no
ffprobe in its build): reading a clip's streams, per-track preview audio
and waveforms, and turning an edit project into an export.

An edit project (JSON, saved per clip by clip_browser.py) is:

    {"version": 1,
     "keep": [[start, end], ...],     # seconds of the clip to keep, in order
     "tracks": [{"index": n,          # the n-th audio stream of the source
                 "volume_db": 0,      # whole-track gain
                 "offset": 0.0,       # seconds to shift the track (+ later)
                 "muted": false,
                 "regions": [{"start": s, "end": e, "db": g}]}]}

Trims and cuts both just shorten `keep`. Gain regions are in the clip's own
timeline (before cuts), so a region keeps its place when a cut is added.

With "Split audio tracks" on, the source is the clip's "-tracks.mkv": track
1 is the full mix, which OBS records as exactly Mic (2) + Desktop (3), and
4-6 are single apps that are also part of Desktop. Exports rebuild the mix
from the edited Mic and Desktop, with track 1's own edits applied on top as
a master; the app tracks' edits only change their own track in the MKV.
"""

import array
import json
import math
import os
import re
import subprocess
import sys
import threading

from obs_config import DISCORD_TARGET_MB

PROJECT_VERSION = 1
SPLIT_TITLES = ("Mix", "Mic", "Desktop")

REQUIRED_FILTERS = (
    "trim", "atrim", "setpts", "asetpts", "concat", "split", "asplit", "scale", "aformat",
    "volume", "amix", "adelay", "apad", "afade",
)
REQUIRED_MUXERS = ("mp4", "matroska", "null", "s16le")

KBIT_PER_MB = 8000  # decimal MB, as Discord counts them
SIZE_HEADROOM = 0.95
DISCORD_AUDIO_KBPS = 128
DISCORD_MIN_VIDEO_KBPS = 300
DISCORD_HEIGHT = 1080
DISCORD_FPS = 30
CUSTOM_HEIGHTS = (2160, 1440, 1080, 720, 480)
CUSTOM_FPS = (120, 60, 30, 24)
CUSTOM_VIDEO_KBPS = (1000, 2500, 4000, 6000, 8000, 12000, 16000, 25000, 40000, 60000)
CUSTOM_AUDIO_KBPS = 192
FULL_CRF = 16
FULL_PRESET = "medium"
FULL_AUDIO_KBPS = 256
TRACK_AUDIO_KBPS = 192
PREVIEW_AUDIO_KBPS = 160

MIN_SEGMENT_SECONDS = 0.05
JOIN_FADE_SECONDS = 0.01
MAX_GAIN_DB = 12.0
MUTE_DB = -60.0  # at or below: silent
MAX_OFFSET_SECONDS = 30.0

PEAK_SAMPLE_RATE = 2000
PEAKS_PER_SECOND = 50

_SYSTEM_WINDOWS = os.name == "nt"


def _no_window_kwargs():
    return {"creationflags": 0x08000000} if _SYSTEM_WINDOWS else {}


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

_STREAM_RE = re.compile(r"^\s*Stream #0:(\d+)(?:\[0x[0-9a-fA-F]+\])?(?:\([^)]*\))?: (Video|Audio): (\w+)(.*)$")
_DURATION_RE = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_TITLE_RE = re.compile(r"^\s*title\s*:\s*(.*?)\s*$")


def parse_probe(stderr):
    """{"duration", "width", "height", "fps", "video_codec", "audio": [{"index",
    "codec", "title"}]} from what `ffmpeg -i <file>` prints. "index" counts
    audio streams only (ffmpeg's 0:a:N)."""
    info = {"duration": None, "width": None, "height": None, "fps": None, "video_codec": None, "audio": []}
    match = _DURATION_RE.search(stderr or "")
    if match:
        hours, minutes, seconds = match.groups()
        info["duration"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    current = None
    for line in (stderr or "").splitlines():
        stream = _STREAM_RE.match(line)
        if stream:
            _number, kind, codec, rest = stream.groups()
            current = None
            if kind == "Video" and info["video_codec"] is None and "attached pic" not in rest:
                info["video_codec"] = codec
                size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", rest)
                if size:
                    info["width"], info["height"] = int(size.group(1)), int(size.group(2))
                fps = re.search(r"([\d.]+) fps", rest) or re.search(r"([\d.]+) tbr", rest)
                if fps:
                    info["fps"] = float(fps.group(1))
            elif kind == "Audio":
                current = {"index": len(info["audio"]), "codec": codec, "title": None}
                info["audio"].append(current)
            continue
        title = _TITLE_RE.match(line)
        if title and current is not None and current["title"] is None:
            current["title"] = title.group(1)
        elif line.strip().startswith(("Input #", "Output #", "Chapters")):
            current = None
    return info


def probe(ffmpeg, path, run=subprocess.run):
    result = run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], capture_output=True, text=True,
                 errors="replace", **_no_window_kwargs())
    info = parse_probe(result.stderr)
    if info["duration"] is None or info["video_codec"] is None:
        raise ValueError(f"Couldn't read {os.path.basename(path)} as a video.")
    return info


def missing_features(ffmpeg, run=subprocess.run):
    """The REQUIRED_FILTERS/REQUIRED_MUXERS this ffmpeg lacks (an older
    ClipEm ffmpeg build) - empty when it can do everything."""
    found = set()
    for kind in ("-filters", "-muxers"):
        result = run([ffmpeg, "-hide_banner", kind], capture_output=True, text=True, errors="replace",
                     **_no_window_kwargs())
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                found.add(parts[1])
    return [name for name in REQUIRED_FILTERS + REQUIRED_MUXERS if name not in found]


def is_split_layout(info):
    titles = tuple(track["title"] for track in info["audio"][: len(SPLIT_TITLES)])
    return titles == SPLIT_TITLES


def track_title(info, index):
    return info["audio"][index]["title"] or f"Track {index + 1}"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def default_project(info):
    return {
        "version": PROJECT_VERSION,
        "keep": [[0.0, round(info["duration"], 3)]],
        "tracks": [
            {"index": track["index"], "volume_db": 0.0, "offset": 0.0, "muted": False, "regions": []}
            for track in info["audio"]
        ],
    }


def _number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value, low, high):
    return max(low, min(high, value))


def merge_ranges(ranges, duration, min_length=MIN_SEGMENT_SECONDS):
    """Sorted, clamped to [0, duration], overlaps merged, slivers dropped
    (a range dragged right to left counts the same as left to right)."""
    cleaned = []
    for item in ranges or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        values = [_number(value, default=None) for value in item]
        if None in values:
            continue
        start, end = sorted(_clamp(value, 0.0, duration) for value in values)
        if end - start >= min_length:
            cleaned.append([start, end])
    cleaned.sort()
    merged = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [[round(s, 3), round(e, 3)] for s, e in merged]


def normalize_project(project, info):
    """A clean copy of `project` for this clip: only real tracks, numbers
    clamped, ranges merged. Anything unreadable falls back to the defaults
    (the page saves whatever it has, so this is what keeps exports sane)."""
    duration = info["duration"]
    project = project if isinstance(project, dict) else {}
    base = default_project(info)
    keep = merge_ranges(project.get("keep", base["keep"]), duration)
    by_index = {}
    for track in project.get("tracks") or []:
        if isinstance(track, dict) and isinstance(track.get("index"), int):
            by_index[track["index"]] = track
    tracks = []
    for default in base["tracks"]:
        given = by_index.get(default["index"], {})
        regions = []
        for region in given.get("regions") or []:
            if not isinstance(region, dict):
                continue
            span = merge_ranges([[region.get("start"), region.get("end")]], duration, min_length=0.01)
            if span:
                db = _clamp(_number(region.get("db")), MUTE_DB, MAX_GAIN_DB)
                regions.append({"start": span[0][0], "end": span[0][1], "db": round(db, 2)})
        regions.sort(key=lambda r: r["start"])
        tracks.append({
            "index": default["index"],
            "volume_db": round(_clamp(_number(given.get("volume_db")), MUTE_DB, MAX_GAIN_DB), 2),
            "offset": round(_clamp(_number(given.get("offset")), -MAX_OFFSET_SECONDS, MAX_OFFSET_SECONDS), 3),
            "muted": bool(given.get("muted", False)),
            "regions": regions,
        })
    return {"version": PROJECT_VERSION, "keep": keep, "tracks": tracks}


def kept_duration(project):
    return sum(end - start for start, end in project["keep"])


def clean_name(name):
    """A user-typed export name as a safe file stem (no folders, no
    characters Windows refuses, no .mp4/.mkv on the end) - "" if nothing's left."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip()
    name = re.sub(r"\.(mp4|mkv)$", "", name, flags=re.IGNORECASE)
    return name.strip(" .")[:150]


def unique_path(folder, stem, ext, exists=os.path.exists):
    """folder/stem+ext, or stem-2, stem-3... - never an existing file."""
    path = os.path.join(folder, stem + ext)
    number = 2
    while exists(path):
        path = os.path.join(folder, f"{stem}-{number}{ext}")
        number += 1
    return path


# ---------------------------------------------------------------------------
# Filter graph
# ---------------------------------------------------------------------------

def _gain(db):
    return "0" if db <= MUTE_DB else f"{db:g}dB"


def _track_chain(track, duration):
    """Filters for one track in the clip's own timeline: shift, gain, mute."""
    filters = ["aformat=sample_rates=48000:channel_layouts=stereo"]
    offset = track["offset"]
    if offset > 0:
        filters.append(f"adelay=delays={round(offset * 1000)}:all=1")
    elif offset < 0:
        filters += [f"atrim=start={-offset:g}", "asetpts=PTS-STARTPTS"]
    filters.append(f"apad=whole_dur={duration:g}")
    filters.append(f"atrim=end={duration:g}")
    filters += _gain_filters(track)
    return filters


def _gain_filters(track):
    filters = []
    if track["muted"]:
        return ["volume=0"]
    if track["volume_db"]:
        filters.append(f"volume={_gain(track['volume_db'])}")
    for region in track["regions"]:
        if region["db"]:
            filters.append(
                f"volume=enable='between(t,{region['start']:g},{region['end']:g})':volume={_gain(region['db'])}"
            )
    return filters


class _Graph:
    def __init__(self):
        self.lines = []
        self._count = 0

    def label(self, prefix):
        self._count += 1
        return f"{prefix}{self._count}"

    def add(self, inputs, filters, outputs):
        self.lines.append("".join(f"[{i}]" for i in inputs) + ",".join(filters) + "".join(f"[{o}]" for o in outputs))

    def split(self, label, count, audio):
        if count == 1:
            return [label]
        outs = [self.label("s") for _ in range(count)]
        self.add([label], [f"{'asplit' if audio else 'split'}={count}"], outs)
        return outs

    def text(self):
        return ";".join(self.lines)


def _cut(graph, label, keep, audio):
    """`label` with only the kept ranges, joined. Audio joins get a 10 ms
    fade each side so a cut never clicks."""
    parts = graph.split(label, len(keep), audio)
    pieces = []
    for number, (part, (start, end)) in enumerate(zip(parts, keep, strict=True)):
        out = graph.label("p")
        if audio:
            filters = [f"atrim=start={start:g}:end={end:g}", "asetpts=PTS-STARTPTS"]
            length = end - start
            if number > 0:
                filters.append(f"afade=t=in:d={JOIN_FADE_SECONDS:g}")
            if number < len(keep) - 1:
                filters.append(f"afade=t=out:st={max(0.0, length - JOIN_FADE_SECONDS):g}:d={JOIN_FADE_SECONDS:g}")
        else:
            filters = [f"trim=start={start:g}:end={end:g}", "setpts=PTS-STARTPTS"]
        graph.add([part], filters, [out])
        pieces.append(out)
    if len(pieces) == 1:
        return pieces[0]
    out = graph.label("c")
    graph.add(pieces, [f"concat=n={len(pieces)}:v={0 if audio else 1}:a={1 if audio else 0}"], [out])
    return out


def build_graph(project, info, video_height=None, fps=None, audio="tracks"):
    """(filter_complex, video label, [audio labels]) for an export.

    audio: "tracks" - the rebuilt mix, then every other track (MKV);
           "mix" - the rebuilt mix only (MP4); None - no audio (pass 1)."""
    graph = _Graph()
    duration = info["duration"]
    keep = project["keep"]

    video = _cut(graph, "0:v:0", keep, audio=False)
    post = []
    if video_height:
        post.append(f"scale=-2:{int(video_height)}:flags=lanczos")
    if fps:
        post.append(f"fps={fps:g}")
    post.append("format=yuv420p")
    vout = graph.label("vout")
    graph.add([video], post, [vout])

    if not audio or not info["audio"]:
        return graph.text(), vout, []

    tracks = project["tracks"]
    split = is_split_layout(info)
    want_tracks = audio == "tracks"
    # How often each edited track is used: in the mix, and as its own track.
    uses = {}
    for track in tracks:
        index = track["index"]
        in_mix = index in (1, 2) if split else index == 0
        uses[index] = int(in_mix) + int(want_tracks and index != 0)
    edited = {}
    for track in tracks:
        if uses[track["index"]]:  # never for a split clip's Mix - it's the master
            label = graph.label("t")
            graph.add([f"0:a:{track['index']}"], _track_chain(track, duration), [label])
            edited[track["index"]] = graph.split(label, uses[track["index"]], audio=True)

    outputs = []
    if split:
        mixed = graph.label("m")
        graph.add([edited[1].pop(), edited[2].pop()],
                  ["amix=inputs=2:normalize=0:duration=longest:dropout_transition=0"], [mixed])
        master = _gain_filters(tracks[0])
        if master:
            out = graph.label("m")
            graph.add([mixed], master, [out])
            mixed = out
        outputs.append(_cut(graph, mixed, keep, audio=True))
    for track in tracks:
        if edited.get(track["index"]):
            outputs.append(_cut(graph, edited[track["index"]].pop(), keep, audio=True))
    return graph.text(), vout, outputs


# ---------------------------------------------------------------------------
# Export commands
# ---------------------------------------------------------------------------

def output_track_titles(info):
    """MKV track names, in build_graph(audio="tracks")'s order."""
    titles = [track_title(info, track["index"]) for track in info["audio"]]
    if titles and is_split_layout(info):
        titles[0] = "Mix"
    return titles


def _base(ffmpeg):
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]


def full_quality_commands(ffmpeg, source, project, info, out_mkv, out_mp4):
    """Commands for a full-quality export: near-lossless H.264 into an MKV
    with every track (when there's more than one), then the MP4 with just
    the mix, copied out of it. Returns [(command, label, weight)]."""
    multi = len(info["audio"]) > 1
    graph, vout, audio_outs = build_graph(project, info, audio="tracks" if multi else "mix")
    video_args = ["-c:v", "libx264", "-preset", FULL_PRESET, "-crf", str(FULL_CRF)]
    if not multi:
        cmd = _base(ffmpeg) + ["-i", source, "-filter_complex", graph, "-map", f"[{vout}]"]
        for label in audio_outs:
            cmd += ["-map", f"[{label}]"]
        cmd += video_args + ["-c:a", "aac", "-b:a", f"{FULL_AUDIO_KBPS}k", "-movflags", "+faststart",
                             "-f", "mp4", out_mp4]
        return [(cmd, "Encoding", 1.0)]
    cmd = _base(ffmpeg) + ["-i", source, "-filter_complex", graph, "-map", f"[{vout}]"]
    for label in audio_outs:
        cmd += ["-map", f"[{label}]"]
    cmd += video_args + ["-c:a", "aac", "-b:a", f"{TRACK_AUDIO_KBPS}k", "-b:a:0", f"{FULL_AUDIO_KBPS}k"]
    for number, title in enumerate(output_track_titles(info)):
        cmd += [f"-metadata:s:a:{number}", f"title={title}"]
    cmd += ["-disposition:a:0", "default", "-f", "matroska", out_mkv]
    remux = _base(ffmpeg) + ["-i", out_mkv, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                             "-movflags", "+faststart", "-f", "mp4", out_mp4]
    return [(cmd, "Encoding", 0.97), (remux, "Saving MP4", 0.03)]


def discord_settings(info, seconds, target_mb=DISCORD_TARGET_MB):
    """{"video_kbps", "height", "fps", "too_long"} so `seconds` of video
    lands just under target_mb, always at 1080p30 (or the source's own size
    and frame rate, when smaller - never scaled up)."""
    seconds = max(1.0, seconds)
    total_kbps = target_mb * KBIT_PER_MB * SIZE_HEADROOM / seconds
    audio_kbps = DISCORD_AUDIO_KBPS if info["audio"] else 0
    video_kbps = int(total_kbps - audio_kbps)
    return {
        "video_kbps": max(DISCORD_MIN_VIDEO_KBPS, video_kbps),
        "audio_kbps": audio_kbps,
        "height": min(DISCORD_HEIGHT, info["height"] or DISCORD_HEIGHT),
        "fps": min(info["fps"] or DISCORD_FPS, DISCORD_FPS),
        "too_long": video_kbps < DISCORD_MIN_VIDEO_KBPS,
        "target_mb": target_mb,
    }


def custom_options(info):
    """What the custom export's dropdowns offer for this clip: heights and
    frame rates up to the source's own (None: as recorded), and bitrates."""
    src_h, src_fps = info["height"] or 0, info["fps"] or 0
    return {
        "heights": [h for h in CUSTOM_HEIGHTS if h <= src_h],
        "fps": [f for f in CUSTOM_FPS if f <= src_fps + 0.5],
        "video_kbps": list(CUSTOM_VIDEO_KBPS),
        "audio_kbps": CUSTOM_AUDIO_KBPS if info["audio"] else 0,
    }


def custom_settings(info, height, fps, video_kbps):
    """Checked settings for a custom export (height/fps None: the source's)."""
    options = custom_options(info)
    if height is not None and height not in options["heights"]:
        raise ValueError(f"Can't export this clip at {height}p.")
    if fps is not None and fps not in options["fps"]:
        raise ValueError(f"Can't export this clip at {fps} fps.")
    if video_kbps not in options["video_kbps"]:
        raise ValueError(f"Unknown bitrate: {video_kbps}.")
    return {
        "video_kbps": video_kbps,
        "audio_kbps": options["audio_kbps"],
        "height": height or info["height"],
        "fps": fps or info["fps"],
    }


def two_pass_commands(ffmpeg, source, project, info, settings, out_mp4, passlog):
    """Two-pass H.264 at settings' bitrate, so the size is predictable."""
    height = settings["height"] if settings["height"] != info["height"] else None
    fps = settings["fps"] if info["fps"] and settings["fps"] < info["fps"] - 0.5 else None
    video_args = ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{settings['video_kbps']}k",
                  "-maxrate", f"{int(settings['video_kbps'] * 1.5)}k",
                  "-bufsize", f"{settings['video_kbps'] * 2}k", "-passlogfile", passlog]
    graph1, vout1, _ = build_graph(project, info, video_height=height, fps=fps, audio=None)
    pass1 = _base(ffmpeg) + ["-i", source, "-filter_complex", graph1, "-map", f"[{vout1}]"] + video_args + [
        "-pass", "1", "-an", "-f", "null", os.devnull]
    graph2, vout2, audio_outs = build_graph(project, info, video_height=height, fps=fps, audio="mix")
    pass2 = _base(ffmpeg) + ["-i", source, "-filter_complex", graph2, "-map", f"[{vout2}]"]
    for label in audio_outs:
        pass2 += ["-map", f"[{label}]"]
    pass2 += video_args + ["-pass", "2"]
    if audio_outs:
        pass2 += ["-c:a", "aac", "-b:a", f"{settings['audio_kbps']}k"]
    pass2 += ["-movflags", "+faststart", "-f", "mp4", out_mp4]
    return [(pass1, "Analysing", 0.4), (pass2, "Encoding", 0.6)]


def discord_commands(ffmpeg, source, project, info, out_mp4, passlog):
    """Two-pass H.264 so the size lands just under Discord's limit."""
    settings = discord_settings(info, kept_duration(project))
    return two_pass_commands(ffmpeg, source, project, info, settings, out_mp4, passlog)


# ---------------------------------------------------------------------------
# Preview audio and waveforms
# ---------------------------------------------------------------------------

def track_audio_command(ffmpeg, source, index, codec, out_path):
    """One audio track as an .m4a the page can play next to the (muted)
    video - copied when it's already AAC."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", source,
           "-map", f"0:a:{index}", "-vn"]
    cmd += ["-c:a", "copy"] if codec == "aac" else ["-c:a", "aac", "-b:a", f"{PREVIEW_AUDIO_KBPS}k"]
    return cmd + ["-f", "mp4", out_path]


def peaks_command(ffmpeg, source, index):
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", source, "-map", f"0:a:{index}",
            "-ac", "1", "-ar", str(PEAK_SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]


def peaks_from_pcm(pcm, sample_rate=PEAK_SAMPLE_RATE, per_second=PEAKS_PER_SECOND):
    """0-255 loudness peaks, per_second of them per second of audio."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    step = max(1, sample_rate // per_second)
    peaks = []
    for start in range(0, len(samples), step):
        chunk = samples[start:start + step]
        peak = max(max(chunk), -min(chunk))
        peaks.append(min(255, peak * 255 // 32767))
    return peaks


def _cached(path, make):
    """path once make(partial) has written it; made at most once at a time."""
    if os.path.isfile(path):
        return path
    with _cache_lock(path):
        if os.path.isfile(path):
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        partial = path + ".partial"
        try:
            make(partial)
            os.replace(partial, path)
        finally:
            if os.path.exists(partial):
                os.remove(partial)
    return path


_locks = {}
_locks_guard = threading.Lock()


def _cache_lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def track_audio(ffmpeg, source, info, index, cache_path, run=subprocess.run):
    def make(partial):
        result = run(track_audio_command(ffmpeg, source, index, info["audio"][index]["codec"], partial),
                     capture_output=True, text=True, errors="replace", **_no_window_kwargs())
        if result.returncode != 0:
            raise RuntimeError(f"Couldn't extract audio track {index + 1}: {result.stderr.strip()[-300:]}")
    return _cached(cache_path, make)


def track_peaks(ffmpeg, source, index, cache_path, run=subprocess.run):
    def make(partial):
        result = run(peaks_command(ffmpeg, source, index), capture_output=True, **_no_window_kwargs())
        if result.returncode != 0:
            raise RuntimeError(f"Couldn't read audio track {index + 1}: {result.stderr.decode(errors='replace')[-300:]}")
        with open(partial, "w", encoding="utf-8") as f:
            json.dump({"per_second": PEAKS_PER_SECOND, "peaks": peaks_from_pcm(result.stdout)}, f)
    with open(_cached(cache_path, make), "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Running an export
# ---------------------------------------------------------------------------

def parse_progress_seconds(line):
    """Seconds encoded so far from an ffmpeg -progress line, else None."""
    key, _sep, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms"):  # both are microseconds
        try:
            return max(0.0, int(value) / 1_000_000)
        except ValueError:
            return None
    return None


class ExportCancelled(Exception):
    pass


class ExportJob:
    """Runs one export's commands in a background thread, reporting
    {"state": idle|running|done|error|cancelled, "message", "progress" 0-1,
    "files": [names]}. Outputs are written to hidden ".partial" names and
    only renamed into place once every command has succeeded."""

    def __init__(self, popen=subprocess.Popen, log=None):
        self._popen = popen
        self._log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._proc = None
        self._cancel = False
        self.status = {"state": "idle", "message": "", "progress": 0.0, "files": []}

    def running(self):
        return self.status["state"] == "running"

    def start(self, steps, outputs, total_seconds, cleanup=()):
        """steps: [(command, label, weight)] writing to the partial paths in
        outputs ({partial: final}). Returns False if one is already running."""
        with self._lock:
            if self.running():
                return False
            self._cancel = False
            self.status = {"state": "running", "message": "Starting...", "progress": 0.0, "files": []}
        threading.Thread(target=self._run, args=(steps, outputs, total_seconds, cleanup), daemon=True).start()
        return True

    def cancel(self):
        with self._lock:
            self._cancel = True
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    def _run(self, steps, outputs, total_seconds, cleanup):
        done_weight = 0.0
        try:
            for cmd, label, weight in steps:
                self._step(cmd, label, done_weight, weight, total_seconds)
                done_weight += weight
            for partial, final in outputs.items():
                os.replace(partial, final)
            self.status = {"state": "done", "message": "Exported.", "progress": 1.0,
                           "files": [os.path.basename(final) for final in outputs.values()]}
        except ExportCancelled:
            self.status = {"state": "cancelled", "message": "Export cancelled.", "progress": 0.0, "files": []}
        except Exception as exc:
            self._log(f"Export failed: {exc}")
            self.status = {"state": "error", "message": str(exc), "progress": 0.0, "files": []}
        finally:
            for path in list(outputs) + list(cleanup):
                for candidate in (path, path + "-0.log", path + "-0.log.mbtree"):
                    if os.path.exists(candidate):
                        try:
                            os.remove(candidate)
                        except OSError:
                            pass

    def _step(self, cmd, label, done_weight, weight, total_seconds):
        with self._lock:
            if self._cancel:
                raise ExportCancelled()
            self._proc = self._popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     errors="replace", **_no_window_kwargs())
        proc = self._proc
        stderr = []
        reader = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
        reader.start()
        for line in proc.stdout:
            seconds = parse_progress_seconds(line)
            if seconds is not None and total_seconds > 0:
                fraction = min(1.0, seconds / total_seconds)
                self.status = {"state": "running", "message": f"{label}...",
                               "progress": round(done_weight + weight * fraction, 4), "files": []}
        proc.wait()
        reader.join(timeout=5)
        if self._cancel:
            raise ExportCancelled()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed ({label.lower()}): {''.join(stderr).strip()[-400:]}")
