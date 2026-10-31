"""Post-clip: the clip hotkey keeps recording for a few more seconds (Settings >
Recording, run_config.json "post_clip") so what's said right after it isn't
lost. Shared by Scripts/post_clip.py (the hotkey, inside OBS),
Scripts/clip_reexport.py and the tray widget (tray-widget/tray_tracks.py),
which finish the save - so no OBS imports here.

The replay buffer is kept N seconds longer than the clip length the user
chose (setup.save_replay_buffer_seconds). A hotkey press doesn't save: N
seconds later post_clip.py saves once, writing STATE_PATH just before, and
that save - the clip plus the N seconds after the press - becomes:

- "<clip>-extra.mkv": all of it, every track, tagged with where the press
  was (MARK_TAG) - what the clip editor opens, its trim end on the mark.
- "<clip>.<ext>": the usual clip - the chosen length, ending at the press,
  the mix only - with the usual smaller copy. No "-tracks.mkv": the extra
  file is the multitrack one.

The clip sound plays at the press, so OBS's Desktop capture records it in
the extra seconds. It's taken back out of the extra file's Desktop and Mix
tracks: the .wav is found in the track to the sample (track_align for
where, then a fine search), and an inverted copy at the gain that fits best
is mixed in. What the mic hears from the speakers stays.
"""

import array
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import wave
from itertools import accumulate
from math import sqrt
from operator import mul

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clip_naming  # noqa: E402
import clip_tracks  # noqa: E402

SHARED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_CONFIG_PATH = os.path.join(SHARED_DIR, "run_config.json")
STATE_PATH = os.path.join(SHARED_DIR, "post_clip_state.json")
# A save this long after post_clip.py asked for it is someone else's. Generous:
# the state is read when OBS finishes writing, and a long buffer on a slow disk
# takes a while. Every save goes through post_clip.py while post-clip is on,
# and turning it off clears the state, so nothing else can pick this up.
STATE_FRESH_SECONDS = 120

DEFAULTS = {"enabled": False, "seconds": 10, "sound_after": False}
MIN_SECONDS, MAX_SECONDS = 1, 120

MARK_TAG = "CLIPEM_MARK"
TRACK_AUDIO_KBPS = 192  # what OBS records each track at (obs_config.DEFAULT_AUDIO_TRACK_KBPS)

RATE = clip_tracks.PCM_RATE
# Where the sound can start, from the press: a player takes a moment to start.
SOUND_SEARCH_BEFORE = 0.2
SOUND_SEARCH_AFTER = 1.0
ENV_RATE = 1000
FINE_SAMPLES = 72  # 1.5 ms either side of the envelope's match
# How much of the track the sound explains where it was found (1: all of it).
# Loud game audio keeps this low even when it's right - and a wrong match
# fits a gain near 0, adding next to nothing - so this only rules out noise.
MIN_CANCEL_SCORE = 0.1
MIN_GAIN, MAX_GAIN = 0.01, 8.0


# ---------------------------------------------------------------------------
# Settings and the hand-over from post_clip.py
# ---------------------------------------------------------------------------

def load_settings(path=None):
    """run_config.json's "post_clip", defaults filled in."""
    try:
        with open(path or RUN_CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f).get("post_clip")
    except (OSError, ValueError, AttributeError):
        saved = None
    return normalize(saved)


def normalize(saved):
    settings = dict(DEFAULTS)
    if isinstance(saved, dict):
        settings["enabled"] = bool(saved.get("enabled", False))
        settings["sound_after"] = bool(saved.get("sound_after", False))
        try:
            settings["seconds"] = max(MIN_SECONDS, min(MAX_SECONDS, int(saved.get("seconds", DEFAULTS["seconds"]))))
        except (TypeError, ValueError):
            pass
    return settings


def extra_seconds(settings):
    """How much longer the replay buffer is kept than the clip length."""
    return settings["seconds"] if settings["enabled"] else 0


def write_state(state, path=None):
    """state: {"pressed_at", "requested_at" (epoch seconds), "clip_seconds",
    "sound": the .wav played at the press, or None}."""
    path = path or STATE_PATH
    partial = path + ".partial"
    with open(partial, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(partial, path)


def clear_state(path=None):
    try:
        os.remove(path or STATE_PATH)
    except OSError:
        pass


def read_state(path=None, now=None):
    """The state post_clip.py wrote for the save that just happened, or None
    if this save isn't a post-clip one."""
    try:
        with open(path or STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        requested = float(state["requested_at"])
        float(state["pressed_at"])
        float(state["clip_seconds"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    now = datetime.datetime.now().timestamp() if now is None else now
    return state if requested - 1 <= now <= requested + STATE_FRESH_SECONDS else None


def span(duration, state):
    """(mark, start): where the press is in the save, and where the clip of
    the chosen length before it starts (the save's start if it's shorter -
    OBS hadn't been recording that long)."""
    after = max(0.0, float(state["requested_at"]) - float(state["pressed_at"]))
    mark = max(0.0, min(duration, duration - after))
    return mark, max(0.0, mark - float(state["clip_seconds"]))


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_RE = re.compile(r"^\s*Stream #0:\d+.*: Audio: ", re.M)


def probe(ffmpeg, path, run=subprocess.run):
    """{"duration", "audio_tracks"} of a saved clip (there's no ffprobe)."""
    result = run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], stdout=subprocess.DEVNULL,
                 stderr=subprocess.PIPE, text=True, errors="replace", **clip_tracks.low_priority_kwargs())
    match = _DURATION_RE.search(result.stderr or "")
    if not match:
        raise ValueError(f"couldn't read {os.path.basename(path)}")
    hours, minutes, seconds = match.groups()
    return {"duration": int(hours) * 3600 + int(minutes) * 60 + float(seconds),
            "audio_tracks": len(_AUDIO_RE.findall(result.stderr))}


def _container(path):
    return {".mkv": "matroska", ".mov": "mov", ".flv": "flv"}.get(os.path.splitext(path)[1].lower(), "mp4")


def build_cut_command(ffmpeg, src, out_path, start, end, container_of=None):
    """The clip: start to end of the save, video and the mix, losslessly - so
    it starts on the keyframe at or before `start`."""
    container = _container(container_of or out_path)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src, "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
    if container in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]
    return cmd + ["-f", container, out_path]


def build_extra_command(ffmpeg, src, out_path, audio_tracks, mark, titles=None, app_tracks=(), cancels=None):
    """The "-extra.mkv": the whole save, every track copied - except those in
    `cancels` ({track index: (cancel .mov, delay in samples)}), which have
    the inverted clip sound mixed in and are re-encoded - then app_tracks
    ([(title, .mov)], see clip_tracks.write_pcm_mov) as AAC. titles: one per
    OBS track, or None to leave them untitled."""
    cancels = cancels or {}
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", src]
    order = sorted(cancels)
    for index in order:
        cmd += ["-i", cancels[index][0]]
    for _title, path in app_tracks:
        cmd += ["-i", path]
    graph = []
    for number, index in enumerate(order):
        delay = cancels[index][1]
        graph.append(f"[{number + 1}:a:0]adelay=delays={delay}S:all=1[d{index}];"
                     f"[0:a:{index}][d{index}]amix=inputs=2:normalize=0:duration=first[c{index}]")
    if graph:
        cmd += ["-filter_complex", ";".join(graph)]
    cmd += ["-map", "0:v:0"]
    for index in range(audio_tracks):
        cmd += ["-map", f"[c{index}]" if index in cancels else f"0:a:{index}"]
    for number in range(len(app_tracks)):
        cmd += ["-map", f"{len(order) + number + 1}:a:0"]
    cmd += ["-c", "copy"]
    for index in range(audio_tracks + len(app_tracks)):
        if index in cancels or index >= audio_tracks:
            cmd += [f"-c:a:{index}", "aac", f"-b:a:{index}", f"{TRACK_AUDIO_KBPS}k"]
    all_titles = list(titles or ()) + [title for title, _path in app_tracks]
    if titles:
        for index, title in enumerate(all_titles):
            cmd += [f"-metadata:s:a:{index}", f"title={title}"]
    if app_tracks:
        cmd += ["-disposition:a", "0", "-disposition:a:0", "default"]
    return cmd + ["-metadata", f"{MARK_TAG}={mark:.3f}", "-f", "matroska", out_path]


def track_pcm_command(ffmpeg, src, index):
    """A track as 16-bit stereo 48 kHz PCM on stdout, from its first sample."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", src, "-map", f"0:a:{index}",
            "-ac", "2", "-ar", str(RATE), "-f", "s16le", "pipe:1"]


# ---------------------------------------------------------------------------
# Taking the clip sound back out
# ---------------------------------------------------------------------------

def read_wav(path):
    """The .wav as (left, right) lists of samples at RATE, full scale 32768."""
    with wave.open(path, "rb") as src:
        channels, width, rate = src.getnchannels(), src.getsampwidth(), src.getframerate()
        frames = src.readframes(src.getnframes())
    if width == 2:
        samples = array.array("h", frames)
    elif width == 4:
        samples = [s / 65536 for s in array.array("i", frames)]
    elif width == 1:
        samples = [(b - 128) * 256 for b in frames]
    else:
        raise wave.Error(f"unsupported sample width {width}")
    left = list(samples[0::channels])
    right = list(samples[1::channels]) if channels > 1 else left
    if rate != RATE:
        left, right = _resample(left, rate), _resample(right, rate)
    return left, right


def _resample(samples, rate):
    """Linear interpolation - near enough what a player's resampler does for
    a short sound."""
    count = int(len(samples) * RATE / rate)
    out = []
    for n in range(count):
        position = n * rate / RATE
        i = int(position)
        nxt = samples[i + 1] if i + 1 < len(samples) else samples[i]
        out.append(samples[i] + (nxt - samples[i]) * (position - i))
    return out


def _channels(pcm):
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 4 * 4])
    return samples[0::2], samples[1::2]


def _envelope(samples, block):
    """How much each `block` samples change - the loudness of their highs,
    which a clip sound's click stands out in, where most game audio's
    weight is lower down."""
    steps = [b - a for a, b in zip(samples, samples[1:], strict=False)]
    return [sum(map(abs, steps[i:i + block])) for i in range(0, len(steps) - block + 1, block)]


def _best_envelope_lag(ref, sig):
    """Where (in envelope values) ref matches sig best, by normalized
    correlation of the mean-removed loudness."""
    if len(sig) < len(ref) or not ref:
        return None
    ref_mean = sum(ref) / len(ref)
    ref = [v - ref_mean for v in ref]
    ref_energy = sum(v * v for v in ref)
    sig_sums = [0.0, *accumulate(sig)]
    sig_squares = [0.0, *accumulate(v * v for v in sig)]
    best, best_score = None, -2.0
    n = len(ref)
    for lag in range(len(sig) - n + 1):
        window_sum = sig_sums[lag + n] - sig_sums[lag]
        energy = sig_squares[lag + n] - sig_squares[lag] - window_sum * window_sum / n
        if energy <= 0 or ref_energy <= 0:
            continue
        score = sum(map(mul, ref, sig[lag:lag + n])) / sqrt(ref_energy * energy)
        if score > best_score:
            best, best_score = lag, score
    return best


def _fit(ref, sig, start):
    """(gain, score) of ref against sig from `start`: the least-squares
    gain, and how much of that stretch of sig ref explains (1: all of it)."""
    piece = sig[start:start + len(ref)]
    if len(piece) < len(ref):
        return 0.0, 0.0
    dot = sum(map(mul, ref, piece))
    ref_energy = sum(map(mul, ref, ref))
    sig_energy = sum(map(mul, piece, piece))
    if ref_energy <= 0 or sig_energy <= 0:
        return 0.0, 0.0
    return dot / ref_energy, dot / sqrt(ref_energy * sig_energy)


def find_sound(track_pcm, sound, around):
    """Where the sound starts in a track and how loud it is there:
    (start sample, left gain, right gain, score), or None if it isn't
    clearly there. track_pcm: the track's 16-bit stereo PCM; sound: (left,
    right) from read_wav; around: the press, in seconds into the track."""
    left, right = _channels(track_pcm)
    lo = max(0, int((around - SOUND_SEARCH_BEFORE) * RATE))
    hi = min(len(left), int((around + SOUND_SEARCH_AFTER) * RATE) + len(sound[0]))
    if hi - lo < len(sound[0]):
        return None
    mono = [a + b for a, b in zip(left[lo:hi], right[lo:hi], strict=True)]
    ref = [a + b for a, b in zip(sound[0], sound[1], strict=True)]
    block = RATE // ENV_RATE
    coarse = _best_envelope_lag(_envelope(ref, block), _envelope(mono, block))
    if coarse is None:
        return None
    ref_energy = sum(map(mul, ref, ref))
    squares = [0, *accumulate(v * v for v in mono)]
    best, best_score = None, -2.0
    for start in range(max(0, coarse * block - FINE_SAMPLES), coarse * block + FINE_SAMPLES + 1):
        if start + len(ref) > len(mono):
            break
        energy = squares[start + len(ref)] - squares[start]
        if energy <= 0 or ref_energy <= 0:
            continue
        score = sum(map(mul, ref, mono[start:start + len(ref)])) / sqrt(ref_energy * energy)
        if score > best_score:
            best, best_score = start, score
    if best is None or best_score < MIN_CANCEL_SCORE:
        return None
    start = lo + best
    gain_left, _ = _fit(list(sound[0]), left, start)
    gain_right, _ = _fit(list(sound[1]), right, start)
    if not all(MIN_GAIN <= g <= MAX_GAIN for g in (gain_left, gain_right)):
        return None
    return start, gain_left, gain_right, best_score


def write_cancel(path, sound, gain_left, gain_right):
    """The sound, inverted and at the gains found, as a PCM .mov to mix in."""
    out = array.array("h")
    for a, b in zip(sound[0], sound[1], strict=True):
        out.append(max(-32768, min(32767, round(-a * gain_left))))
        out.append(max(-32768, min(32767, round(-b * gain_right))))
    clip_tracks.write_pcm_mov(path, out.tobytes())


def cancels_for(ffmpeg, src, tracks, sound_path, around, temp, log, run=subprocess.run):
    """{track index: (cancel .mov, delay)} for each of `tracks` the clip
    sound was found in."""
    try:
        sound = read_wav(sound_path)
    except (OSError, EOFError, wave.Error) as exc:
        log(f"couldn't read the clip sound ({exc}) - leaving it in the extra seconds")
        return {}
    cancels = {}
    for index in tracks:
        try:
            result = run(track_pcm_command(ffmpeg, src, index), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         **clip_tracks.low_priority_kwargs())
        except Exception as exc:
            log(f"couldn't read track {index + 1} ({exc}) - the clip sound stays in it")
            continue
        found = find_sound(result.stdout, sound, around) if result.returncode == 0 else None
        if not found:
            log(f"clip sound not clearly in track {index + 1} - left as it is")
            continue
        start, gain_left, gain_right, score = found
        path = os.path.join(temp, f"cancel-{index}.mov")
        write_cancel(path, sound, gain_left, gain_right)
        cancels[index] = (path, start)
        log(f"taking the clip sound out of track {index + 1} at {start / RATE:.3f}s (match {score:.2f})")
    return cancels


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

def finish_post_save(ffmpeg, save_path, state, options, log, app_tracks=(), run=subprocess.run):
    """Turns a post-clip save into the clip and its "-extra.mkv" (see the
    module notes), then the clip's smaller copy if that's on. options:
    {"naming", "reexport", "target_mb", "crop_seconds"}. app_tracks: [(title,
    .mov)] the tray cut for the whole save. Returns (clip, extra) paths -
    extra None if it couldn't be made, and then the clip is the whole save
    (nothing is lost)."""
    naming = options.get("naming") or clip_naming.DEFAULTS
    info = probe(ffmpeg, save_path, run=run)
    mark, start = span(info["duration"], state)
    pressed = datetime.datetime.fromtimestamp(float(state["pressed_at"]))
    clip_path = clip_naming.rename_clip(save_path, naming, now=pressed, log=log)
    extra_path = clip_naming.extra_path(clip_path, naming)
    audio = info["audio_tracks"]
    split = audio >= len(clip_tracks.FIXED_TRACK_TITLES)
    titles = list(clip_tracks.FIXED_TRACK_TITLES) + [f"Track-{n + 1}" for n in range(3, audio)] if split else None
    # Mix and Desktop hear the clip sound; Mic only from the speakers, which can't be undone.
    sound_tracks = [0, len(clip_tracks.FIXED_TRACK_TITLES) - 1] if split else [0] if audio else []

    with tempfile.TemporaryDirectory(prefix="clipem-post-") as temp:
        cancels = {}
        if state.get("sound") and os.path.isfile(state["sound"]):
            cancels = cancels_for(ffmpeg, clip_path, sound_tracks, state["sound"], mark, temp, log, run=run)
        made = clip_tracks.run_to(
            lambda partial: build_extra_command(ffmpeg, clip_path, partial, audio, mark, titles, app_tracks, cancels),
            extra_path, f"saving the extra seconds of {clip_path}", log)
    if not made:
        log(f"{clip_path} keeps the extra seconds")
        return clip_path, None
    log(f"saved {extra_path} (the press at {mark:.2f}s of {info['duration']:.2f}s)")
    if not clip_tracks.run_to(lambda partial: build_cut_command(ffmpeg, clip_path, partial, start, mark, clip_path),
                              clip_path, f"cutting {clip_path} to the clip", log):
        log(f"{clip_path} still has the extra seconds")
    if options.get("reexport"):
        clip_tracks.reexport(ffmpeg, clip_path, options["target_mb"], options["crop_seconds"], log,
                             naming["small_suffix"])
    return clip_path, extra_path
