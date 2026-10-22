"""The clip editor's media work, all done with ClipEm's ffmpeg (there's no
ffprobe in its build): reading a clip's streams, per-track preview audio
and waveforms, and turning an edit project into an export.

An edit project (JSON, saved per clip by clip_browser.py) is:

    {"version": 2,
     "keep": [[start, end], ...],     # seconds of the clip to keep, in order
     "tracks": [{"index": n,          # the n-th audio stream of the source
                 "volume_db": 0,      # whole-track gain
                 "offset": 0.0,       # seconds to shift the track (+ later)
                 "muted": false,
                 "level": false,      # auto-level: loudnorm to LEVEL_TARGET_LUFS
                 "gate_db": null,     # noise gate threshold (dBFS), null: off
                 "mono": false,       # left and right combined, on both sides
                 "regions": [{"start": s, "end": e, "db": g,
                              "channel": null}]}],  # 0 left / 1 right only, null both
     "master_db": 0,                  # gain on the mix (the first audio track) only
     "crop": null}                    # or {"x", "y", "w", "h"}: the part of the frame
                                      # to keep, in source pixels (even numbers)

Trims and cuts both just shorten `keep`. Gain regions are in the clip's own
timeline (before cuts), so a region keeps its place when a cut is added.

With "Split audio tracks" on, the source is the clip's "-tracks.mkv": track
1 is the full mix, which OBS records as exactly Mic (2) + Desktop (3), and
4-6 are single apps that are also part of Desktop. The export's mix is
what the editor plays: every unmuted track whose containing tracks are all
muted (see mix_inputs()) - Mix; or with Mix muted, Mic + Desktop; or with
Desktop muted too, Mic + each app. Every track is also its own MKV track.
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

PROJECT_VERSION = 2
SUB_MIX = (1, 2)  # what a split clip's Mix is recorded from: Mic + Desktop
SPLIT_TITLES = ("Mix", "Mic", "Desktop")

REQUIRED_FILTERS = (
    "trim", "atrim", "setpts", "asetpts", "concat", "split", "asplit", "scale", "aformat",
    "volume", "amix", "adelay", "apad", "afade",
)
REQUIRED_MUXERS = ("mp4", "matroska", "null", "s16le")
# Only for "Level" (older ClipEm ffmpeg builds lack them - the button is
# then unavailable, everything else still works).
LEVEL_FILTERS = ("loudnorm", "alimiter")
# Only for cropping, likewise.
CROP_FILTERS = ("crop",)
MIN_CROP_PIXELS = 64
# Only for the noise gate, likewise.
GATE_FILTERS = ("agate",)
# Noise gate: below the threshold, sound is turned down by GATE_RANGE_DB -
# the hiss and hum between words. Before Level: leveling boosts quiet parts
# (by ~40 dB on a quiet mic), lifting the noise far above any fixed gate
# threshold. Mic levels vary too much for one threshold, so it's set from
# the track's own noise floor and speech level (gate_levels/gate_presets).
GATE_DEFAULT_DB = -60  # only when the track can't be measured
GATE_MIN_DB, GATE_MAX_DB = -80, -10
# Where between the noise floor (0) and the speech level (1) each strength
# puts the threshold - measured on real ClipEm mic tracks: about 0.55 cuts
# the noise ~28 dB with the speech untouched; 0.7+ starts eating quiet words.
GATE_STRENGTHS = {"light": 0.4, "normal": 0.55, "strong": 0.7}
GATE_WINDOW_SECONDS = 0.05
GATE_SAMPLE_RATE = 8000
GATE_RANGE_DB = -30
GATE_RATIO = 8
GATE_ATTACK_MS = 5
GATE_RELEASE_MS = 150
# Only for "Mono" and one-channel volume changes, likewise.
CHANNEL_FILTERS = ("pan", "channelsplit", "join")
MONO_PAN = "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"

# "Level": loudnorm's dynamic mode evens a track out - quiet parts up, loud
# parts down - to the loudness most web video is mastered to (YouTube and
# the streaming services normalize to about -14 LUFS; Discord plays files
# as they are, so that's a good level there too), with true peaks kept
# under -1.5 dBTP so the AAC encode doesn't clip.
LEVEL_TARGET_LUFS = -14
LEVEL_TRUE_PEAK_DB = -1.5
LEVEL_RANGE_LU = 7
# Leveled tracks are each at the target, so a mix of several is louder -
# a limiter on the mix keeps its peaks at the same ceiling.
MIX_LIMIT = 10 ** (LEVEL_TRUE_PEAK_DB / 20)

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
MIN_MASTER_DB = -30.0  # the master slider's range
MAX_MASTER_DB = 12.0

PEAK_SAMPLE_RATE = 2000
PEAKS_PER_SECOND = 50
PEAK_FULL_SCALE = 32767

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
    "codec", "title", "channels"}]} from what `ffmpeg -i <file>` prints.
    "index" counts audio streams only (ffmpeg's 0:a:N)."""
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
                current = {"index": len(info["audio"]), "codec": codec, "title": None,
                           "channels": _channel_count(rest)}
                info["audio"].append(current)
            continue
        title = _TITLE_RE.match(line)
        if title and current is not None and current["title"] is None:
            current["title"] = title.group(1)
        elif line.strip().startswith(("Input #", "Output #", "Chapters")):
            current = None
    return info


def _channel_count(stream_details):
    """1 for "mono", 2 for "stereo" (or unknown), 6 for "5.1"..."""
    layout = re.search(r"\bHz, ([^,]+)", stream_details)
    name = layout.group(1).strip() if layout else ""
    count = re.match(r"(\d+) channels", name)
    if count:
        return int(count.group(1))
    return {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "5.0": 5, "5.1": 6, "6.1": 7, "7.1": 8}.get(
        name.split("(")[0], 2)


def channel_count(info, index):
    return info["audio"][index].get("channels", 2)


def probe(ffmpeg, path, run=subprocess.run):
    result = run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], capture_output=True, text=True,
                 errors="replace", **_no_window_kwargs())
    info = parse_probe(result.stderr)
    if info["duration"] is None or info["video_codec"] is None:
        raise ValueError(f"Couldn't read {os.path.basename(path)} as a video.")
    return info


def ffmpeg_features(ffmpeg, run=subprocess.run):
    """The names of every filter and muxer this ffmpeg has."""
    found = set()
    for kind in ("-filters", "-muxers"):
        result = run([ffmpeg, "-hide_banner", kind], capture_output=True, text=True, errors="replace",
                     **_no_window_kwargs())
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                found.add(parts[1])
    return found


def missing_features(ffmpeg, run=subprocess.run, found=None):
    """The REQUIRED_FILTERS/REQUIRED_MUXERS this ffmpeg lacks (an older
    ClipEm ffmpeg build) - empty when it can do everything."""
    found = ffmpeg_features(ffmpeg, run) if found is None else found
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
            {"index": track["index"], "volume_db": 0.0, "offset": 0.0, "muted": False, "level": False,
             "gate_db": None, "mono": False, "regions": []}
            for track in info["audio"]
        ],
        "master_db": 0.0,
        "crop": None,
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
        stereo = channel_count(info, default["index"]) >= 2
        regions = []
        for region in given.get("regions") or []:
            if not isinstance(region, dict):
                continue
            span = merge_ranges([[region.get("start"), region.get("end")]], duration, min_length=0.01)
            if span:
                db = _clamp(_number(region.get("db")), MUTE_DB, MAX_GAIN_DB)
                channel = region.get("channel")
                channel = channel if stereo and channel in (0, 1) and not isinstance(channel, bool) else None
                regions.append({"start": span[0][0], "end": span[0][1], "db": round(db, 2), "channel": channel})
        regions.sort(key=lambda r: r["start"])
        tracks.append({
            "index": default["index"],
            "volume_db": round(_clamp(_number(given.get("volume_db")), MUTE_DB, MAX_GAIN_DB), 2),
            "offset": round(_clamp(_number(given.get("offset")), -MAX_OFFSET_SECONDS, MAX_OFFSET_SECONDS), 3),
            "muted": bool(given.get("muted", False)),
            "level": bool(given.get("level", False)),
            "gate_db": _gate_db(given.get("gate_db")),
            "mono": stereo and bool(given.get("mono", False)),
            "regions": regions,
        })
    if _number(project.get("version"), PROJECT_VERSION) < 2 and is_split_layout(info):
        # Version 1 rebuilt the mix from Mic + Desktop ("mix_source":
        # "tracks", or before that choice whenever they were edited) - now
        # that's what muting the Mix does.
        source = project.get("mix_source")
        if source == "tracks" or (source is None and any(_is_edited(t) for t in tracks if t["index"] in SUB_MIX)):
            tracks[0]["muted"] = True
    master_db = round(_clamp(_number(project.get("master_db")), MIN_MASTER_DB, MAX_MASTER_DB), 2)
    return {"version": PROJECT_VERSION, "keep": keep, "tracks": tracks, "master_db": master_db,
            "crop": normalize_crop(project.get("crop"), info)}


def _even(value):
    return int(value) // 2 * 2


def normalize_crop(crop, info):
    """A crop that fits the frame, in even numbers (H.264's 4:2:0 needs
    them), at least MIN_CROP_PIXELS a side - or None for the whole frame
    (also when it can't be read)."""
    width, height = info.get("width"), info.get("height")
    if not isinstance(crop, dict) or not width or not height:
        return None
    try:
        x, y, w, h = (float(crop[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        return None
    w = _even(_clamp(w, MIN_CROP_PIXELS, width))
    h = _even(_clamp(h, MIN_CROP_PIXELS, height))
    x = _even(_clamp(x, 0, width - w))
    y = _even(_clamp(y, 0, height - h))
    if (x, y, w, h) == (0, 0, _even(width), _even(height)):
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def cropped_info(info, project):
    """info with the video's size after the project's crop - what the
    export settings (Discord, Custom) work from."""
    crop = project.get("crop") if project else None
    if not crop:
        return info
    return dict(info, width=crop["w"], height=crop["h"])


def _gate_db(value):
    """A noise gate threshold, whole dB within range - None: no gate."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(round(_clamp(value, GATE_MIN_DB, GATE_MAX_DB)))


def _containers(index):
    """The tracks a split clip's track is recorded inside, nearest first."""
    if index == 0:
        return []
    return [0] if index in SUB_MIX else [2, 0]  # an app is in Desktop, which is in the Mix


def mix_inputs(project, info):
    """The track indexes the export's mix is made of - what the editor
    plays: the soloed tracks (project["solo"], sent with an export), else
    every unmuted track whose containers are all muted, so nothing's heard
    twice. A plain clip's mix is its first track."""
    by_index = {track["index"]: track for track in project["tracks"]}
    solo = sorted(index for index in set(project.get("solo") or []) if index in by_index)
    if solo:
        return solo
    if not is_split_layout(info):
        return [0]
    heard = [index for index in sorted(by_index) if not by_index[index]["muted"]
             and all(by_index[c]["muted"] for c in _containers(index))]
    return heard or [0]  # all muted: the (muted, so silent) Mix


def _is_edited(track):
    return bool(track["volume_db"] or track["offset"] or track["muted"] or track.get("level") or track.get("mono")
                or track.get("gate_db") is not None or track["regions"])


def uses_channels(project):
    """Whether the project needs CHANNEL_FILTERS (Mono, one-channel changes)."""
    return any(track.get("mono") or any(r.get("channel") is not None for r in track["regions"])
               for track in project["tracks"])


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


def level_filters():
    """The "Level" chain - loudnorm works at 192 kHz, so back to 48 kHz after."""
    return [f"loudnorm=I={LEVEL_TARGET_LUFS}:TP={LEVEL_TRUE_PEAK_DB}:LRA={LEVEL_RANGE_LU}",
            "aresample=48000", "aformat=sample_rates=48000:channel_layouts=stereo"]


def gate_filters(threshold_db):
    return [f"agate=threshold={10 ** (threshold_db / 20):.6f}:range={10 ** (GATE_RANGE_DB / 20):.6f}"
            f":ratio={GATE_RATIO}:attack={GATE_ATTACK_MS}:release={GATE_RELEASE_MS}"]


def effect_filters(level=False, gate_db=None, volume_db=0.0):
    """The noise gate, the track's volume (only given here with Level on -
    see pre_level_db), then Level - as the editor previews them
    (track_audio()/track_peaks() with the same arguments)."""
    filters = gate_filters(gate_db) if gate_db is not None else []
    if level:
        if volume_db:
            filters.append(f"volume={_gain(volume_db)}")
        filters += level_filters()
    return filters


def pre_level_db(track):
    """The whole-track volume when it goes before Level Out Audio (which
    then evens out the result), else 0. The slider's "muted" end stays
    after it, so it still silences the track."""
    db = track.get("volume_db") or 0.0
    return db if track.get("level") and db > MUTE_DB else 0.0


def gate_levels(ffmpeg, source, index, run=subprocess.run):
    """(noise floor dB, speech level dB) of a recorded track: the quietest
    and loudest tenth of its 50 ms stretches. None if it can't be read."""
    result = run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", source, "-map", f"0:a:{index}",
                  "-ac", "1", "-ar", str(GATE_SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
                 capture_output=True, **_no_window_kwargs())
    if result.returncode != 0:
        return None
    samples = array.array("h")
    samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    window = int(GATE_SAMPLE_RATE * GATE_WINDOW_SECONDS)
    levels = sorted(
        20 * math.log10(max(math.sqrt(sum(x * x for x in samples[i:i + window]) / window) / 32768, 1e-6))
        for i in range(0, len(samples) - window + 1, window)
    )
    if len(levels) < 10:
        return None
    return levels[len(levels) // 10], levels[len(levels) * 9 // 10]


def gate_presets(noise_db, speech_db):
    """{strength: threshold dB} for a track with this noise floor and speech
    level - always above the noise and at least 6 dB under the speech."""
    presets = {}
    for name, fraction in GATE_STRENGTHS.items():
        threshold = noise_db + fraction * (speech_db - noise_db)
        threshold = min(threshold, speech_db - 6)
        presets[name] = int(round(_clamp(threshold, GATE_MIN_DB, GATE_MAX_DB)))
    return presets


def _track_chain(track, duration):
    """Filters for one track in the clip's own timeline: level, noise gate,
    mono, shift, gain, mute (one-channel changes are added by _add_track).
    The effects come first, on the whole recorded track - what the editor
    previews - then it's made mono the way the editor does."""
    filters = ["aformat=sample_rates=48000:channel_layouts=stereo"]
    if not track["muted"]:
        filters += effect_filters(track.get("level"), track.get("gate_db"), pre_level_db(track))
    if track.get("mono") and not track["muted"]:
        filters.append(MONO_PAN)
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
    if track["volume_db"] and not pre_level_db(track):  # else applied before Level Out Audio
        filters.append(f"volume={_gain(track['volume_db'])}")
    return filters + _region_filters(track, None)


def _region_filters(track, channel):
    """The volume changes on both channels (None), or on just one."""
    return [
        f"volume=enable='between(t,{region['start']:g},{region['end']:g})':volume={_gain(region['db'])}"
        for region in track["regions"] if region["db"] and region.get("channel") == channel
    ]


def _add_track(graph, track, duration):
    """Adds one track's chain to graph; returns its output label. One-channel
    volume changes split it into left and right, change each, and join them."""
    label = graph.label("t")
    chain = _track_chain(track, duration)
    sides = [] if track["muted"] else [_region_filters(track, channel) for channel in (0, 1)]
    if not any(sides):
        graph.add([f"0:a:{track['index']}"], chain, [label])
        return label
    left, right = graph.label("l"), graph.label("r")
    graph.add([f"0:a:{track['index']}"], chain + ["channelsplit=channel_layout=stereo"], [left, right])
    joined = []
    for side, filters in zip((left, right), sides, strict=True):
        out = graph.label("c")
        graph.add([side], filters or ["anull"], [out])
        joined.append(out)
    graph.add(joined, ["join=inputs=2:channel_layout=stereo"], [label])
    return label


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


def build_graph(project, info, video_height=None, fps=None, audio="tracks", with_video=True):
    """(filter_complex, video label, [audio labels]) for an export.

    audio: "tracks" - the rebuilt mix, then every other track (MKV);
           "mix" - the rebuilt mix only (MP4); None - no audio (pass 1).
    with_video=False leaves the video out (vout is None): for measuring audio."""
    graph = _Graph()
    duration = info["duration"]
    keep = project["keep"]

    vout = None
    if with_video:
        video = _cut(graph, "0:v:0", keep, audio=False)
        post = []
        crop = project.get("crop")
        if crop:
            post.append(f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}")
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
    inputs = mix_inputs(project, info)
    rebuild = inputs != [0]
    want_tracks = audio == "tracks"
    # How often each edited track is used: in the mix, and as its own track.
    uses = {}
    for track in tracks:
        index = track["index"]
        uses[index] = int(index in inputs) + int(want_tracks and index != 0)
    edited = {}
    for track in tracks:
        if uses[track["index"]]:  # never for a rebuilt clip's Mix - it isn't used
            label = _add_track(graph, track, duration)
            edited[track["index"]] = graph.split(label, uses[track["index"]], audio=True)

    outputs = []
    if rebuild:
        sources = [edited[index].pop() for index in inputs]
        mixed = sources[0]
        if len(sources) > 1:
            mixed = graph.label("m")
            mix = [f"amix=inputs={len(sources)}:normalize=0:duration=longest:dropout_transition=0"]
            if any(track.get("level") for track in tracks if track["index"] in inputs):
                mix.append(f"alimiter=limit={MIX_LIMIT:.4f}:level=false:latency=1")  # no lookahead delay
            graph.add(sources, mix, [mixed])
        outputs.append(_cut(graph, mixed, keep, audio=True))
    for track in tracks:
        if edited.get(track["index"]):
            outputs.append(_cut(graph, edited[track["index"]].pop(), keep, audio=True))
    master_db = project.get("master_db") or 0.0
    if outputs and master_db:
        # The master slider is the gain on the mix, which is the first output.
        louder = graph.label("g")
        filters = [f"volume={_gain(master_db)}"]
        if master_db > 0:
            filters.append(f"alimiter=limit={MIX_LIMIT:.4f}:level=false:latency=1")
        graph.add([outputs[0]], filters, [louder])
        outputs[0] = louder
    return graph.text(), vout, outputs


# ---------------------------------------------------------------------------
# Export commands
# ---------------------------------------------------------------------------

def mix_loudness_command(ffmpeg, source, project, info):
    """Command that prints the mix's integrated loudness as it'd be exported -
    without the master gain - in loudnorm's measuring pass (its JSON, on
    stderr): ClipEm's ffmpeg has no ebur128 filter."""
    unmastered = dict(project, master_db=0.0)
    graph, _, outputs = build_graph(unmastered, info, audio="mix", with_video=False)
    graph += f";[{outputs[0]}]loudnorm=print_format=json[measured]"
    return [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-i", source,
            "-filter_complex", graph, "-map", "[measured]", "-f", "null", "-"]


def parse_integrated_lufs(stderr):
    """loudnorm's "input_i" as a float, or None (silence is -inf)."""
    found = re.findall(r'"input_i"\s*:\s*"(-?\d+(?:\.\d+)?|-inf)"', stderr)
    if not found or found[-1] == "-inf":
        return None
    return float(found[-1])


def auto_master_db(lufs):
    """The master gain that takes a mix measured at `lufs` to LEVEL_TARGET_LUFS."""
    return round(_clamp(LEVEL_TARGET_LUFS - lufs, MIN_MASTER_DB, MAX_MASTER_DB), 1)


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
    height = settings["height"] if settings["height"] != cropped_info(info, project)["height"] else None
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
    settings = discord_settings(cropped_info(info, project), kept_duration(project))
    return two_pass_commands(ffmpeg, source, project, info, settings, out_mp4, passlog)


# ---------------------------------------------------------------------------
# Preview audio and waveforms
# ---------------------------------------------------------------------------

def track_audio_command(ffmpeg, source, index, codec, out_path, level=False, gate_db=None, volume_db=0.0):
    """One audio track as an .m4a the page can play next to the (muted)
    video - copied when it's already AAC. level/gate_db: with those effects."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", source,
           "-map", f"0:a:{index}", "-vn"]
    effects = effect_filters(level, gate_db, volume_db)
    if effects:
        cmd += ["-af", ",".join(effects)]
    cmd += ["-c:a", "copy"] if codec == "aac" and not effects else ["-c:a", "aac", "-b:a", f"{PREVIEW_AUDIO_KBPS}k"]
    return cmd + ["-f", "mp4", out_path]


def peaks_command(ffmpeg, source, index, level=False, channels=1, gate_db=None, volume_db=0.0):
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", source, "-map", f"0:a:{index}"]
    effects = effect_filters(level, gate_db, volume_db)
    if effects:
        cmd += ["-af", ",".join(effects)]
    return cmd + ["-ac", str(channels), "-ar", str(PEAK_SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]


def peaks_from_pcm(pcm, sample_rate=PEAK_SAMPLE_RATE, per_second=PEAKS_PER_SECOND, channels=1):
    """0-255 loudness peaks, per_second of them per second of audio - of
    interleaved PCM's first channel (peaks_by_channel() for the others)."""
    return peaks_by_channel(pcm, sample_rate, per_second, channels)[0]


def peaks_by_channel(pcm, sample_rate=PEAK_SAMPLE_RATE, per_second=PEAKS_PER_SECOND, channels=1, full_scale=255):
    """peaks_from_pcm() for each channel of interleaved PCM."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // (2 * channels) * 2 * channels])
    if sys.byteorder == "big":
        samples.byteswap()
    return [_peaks(samples[channel::channels], sample_rate, per_second, full_scale) for channel in range(channels)]


def _peaks(samples, sample_rate, per_second, full_scale=255):
    step = max(1, sample_rate // per_second)
    peaks = []
    for start in range(0, len(samples), step):
        chunk = samples[start:start + step]
        peak = max(max(chunk), -min(chunk))
        peaks.append(min(full_scale, peak * full_scale // 32767))
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


def track_audio(ffmpeg, source, info, index, cache_path, run=subprocess.run, level=False, gate_db=None,
                volume_db=0.0):
    def make(partial):
        result = run(track_audio_command(ffmpeg, source, index, info["audio"][index]["codec"], partial, level,
                                         gate_db, volume_db),
                     capture_output=True, text=True, errors="replace", **_no_window_kwargs())
        if result.returncode != 0:
            raise RuntimeError(f"Couldn't extract audio track {index + 1}: {result.stderr.strip()[-300:]}")
    return _cached(cache_path, make)


def track_peaks(ffmpeg, source, index, cache_path, run=subprocess.run, level=False, channels=1, gate_db=None,
                volume_db=0.0):
    """{"per_second", "peaks", "channels": [left peaks, right peaks]} - the
    loudest of both in "peaks", and "channels" only for a stereo track."""
    channels = 2 if channels >= 2 else 1

    def make(partial):
        result = run(peaks_command(ffmpeg, source, index, level, channels, gate_db, volume_db), capture_output=True,
                     **_no_window_kwargs())
        if result.returncode != 0:
            raise RuntimeError(f"Couldn't read audio track {index + 1}: {result.stderr.decode(errors='replace')[-300:]}")
        # 16-bit peaks: the editor draws them in dB, down to -60 dB and below.
        sides = peaks_by_channel(result.stdout, channels=channels, full_scale=PEAK_FULL_SCALE)
        data = {"per_second": PEAKS_PER_SECOND, "scale": PEAK_FULL_SCALE,
                "peaks": [max(values) for values in zip(*sides, strict=True)]}
        if channels == 2:
            data["channels"] = sides
        with open(partial, "w", encoding="utf-8") as f:
            json.dump(data, f)
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

    def start(self, steps, outputs, total_seconds, cleanup=(), on_done=None):
        """steps: [(command, label, weight)] writing to the partial paths in
        outputs ({partial: final}). on_done([final paths]) runs once they're
        all in place. Returns False if one is already running."""
        with self._lock:
            if self.running():
                return False
            self._cancel = False
            self.status = {"state": "running", "message": "Starting...", "progress": 0.0, "files": []}
        threading.Thread(target=self._run, args=(steps, outputs, total_seconds, cleanup, on_done),
                         daemon=True).start()
        return True

    def cancel(self):
        with self._lock:
            self._cancel = True
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    def _run(self, steps, outputs, total_seconds, cleanup, on_done=None):
        done_weight = 0.0
        try:
            for cmd, label, weight in steps:
                self._step(cmd, label, done_weight, weight, total_seconds)
                done_weight += weight
            for partial, final in outputs.items():
                os.replace(partial, final)
            if on_done:
                try:
                    on_done(list(outputs.values()))
                except Exception as exc:  # the export itself worked
                    self._log(f"After the export: {exc}")
            self.status = {"state": "done", "message": "Exported.", "progress": 1.0,
                           "files": [os.path.basename(final) for final in outputs.values()],
                           "paths": list(outputs.values()),
                           "sizes": [os.path.getsize(final) for final in outputs.values()]}
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
