"""
Mic Filter Configuration: measures a mic's noise floor and speaking level,
then computes/writes the OBS Gain + Noise Gate filter settings that match
(see gui.py's "Start Mic Filter Configuration" button and
mic_filter_gui.MicFilterWizard, the popout that drives the functions
here). Kept separate from setup.py purely so this narrowly-scoped audio
calibration logic doesn't bloat the general install-step module - setup.py
imports this module and wires its functions into the wizard's controller
dict, same as everything else it exposes to gui.py.

Linux records straight from PulseAudio via `parecord`, using the exact
same device_id (a Pulse source name) OBS itself points at - see
setup.py's _list_audio_devices_linux. Windows/macOS go through
sounddevice+numpy instead (see ensure_mic_filter_deps), matched by
friendly device name, because there's no parecord equivalent there.
Earlier this used sounddevice on Linux too, matched by friendly name
against sounddevice.query_devices() - but PortAudio's ALSA hostapi (the
only one this build has) only lists raw hw: devices plus one generic
"pulse"/"default" entry, never individual Pulse source names, so that
match always missed and silently fell back to the system default input
device - recording from the same fixed source regardless of which mic was
actually selected, which is why redoing the speech measurement looked
"stuck" at the same level.
"""

import array
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid

from clipem_common import install_requirements, write_json_atomic

INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.dirname(INSTALLER_DIR)
APP_DIR = os.path.dirname(SHARED_DIR)
MIC_FILTER_REQUIREMENTS = os.path.join(INSTALLER_DIR, "mic-filter", "requirements.txt")

NOISE_FLOOR_SAMPLE_SECONDS = 5
SPEECH_SAMPLE_SECONDS = 5
TARGET_SPEECH_DBFS = -18.0


def _mic_filter_deps_dir():
    """Where sounddevice/numpy get installed on macOS - kept inside the
    project folder via pip's --target, same trick install_tray_widget
    uses, rather than touching the system/user site-packages. Windows has
    no separate dir: it installs straight into the embedded interpreter's
    own site-packages, already on sys.path once ensure_windows_pip ran.
    Linux doesn't need this at all - see measure_audio_level_db."""
    return None if platform.system() == "Windows" else os.path.join(APP_DIR, "macOS", "mic-filter-deps")


def ensure_mic_filter_deps():
    """Installs sounddevice+numpy the first time Mic Filter Configuration is
    used, rather than burdening every install with them. Returns True once
    the platform is ready to record (on Linux, that just means `parecord`
    exists - already a dependency of list_audio_devices' `pactl` calls, so
    it's normally already present)."""
    if platform.system() == "Linux":
        if shutil.which("parecord"):
            return True
        print(
            "Missing `parecord` - install the pulseaudio-utils package "
            "(e.g. `sudo pacman -S libpulse`, `sudo apt install "
            "pulseaudio-utils`) to use Mic Filter Configuration.",
            file=sys.stderr,
        )
        return False

    deps_dir = _mic_filter_deps_dir()
    if deps_dir and deps_dir not in sys.path:
        sys.path.insert(0, deps_dir)

    def _importable():
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
            return True
        except ImportError:
            return False

    if _importable():
        return True

    print("Installing microphone calibration dependencies (sounddevice, numpy)...")
    pip_args = [sys.executable, "-m", "pip", "install"]
    if deps_dir:
        os.makedirs(deps_dir, exist_ok=True)
        pip_args += ["--target", deps_dir]
    try:
        install_requirements(pip_args, MIC_FILTER_REQUIREMENTS)
    except Exception as exc:
        print(f"Could not install microphone calibration dependencies: {exc}", file=sys.stderr)
        return False

    # Without this, the _importable() pre-check above left Python's path
    # finder holding a stale (empty) directory listing for deps_dir, so the
    # packages pip just wrote there wouldn't be seen as importable until a
    # fresh process re-scanned it.
    import importlib
    importlib.invalidate_caches()

    if not _importable():
        print("Microphone calibration dependencies failed to import after install.", file=sys.stderr)
        return False
    return True


def _find_sounddevice_input_index(device_name):
    """Matches the friendly name shown in the wizard's dropdown against
    sounddevice's own device list (Windows' WASAPI hostapi, and macOS'
    CoreAudio one, both expose one entry per real endpoint, unlike Linux's
    ALSA-only view - see the module docstring above). Falls back to
    PortAudio's default input device (None) if nothing matches, which
    covers both the "Default" entry and an unmatched name."""
    if not device_name or device_name == "Default":
        return None
    import sounddevice as sd
    devices = sd.query_devices()
    inputs = [(i, d["name"].strip()) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    lname = device_name.strip().lower()
    for i, name in inputs:
        if name.lower() == lname:
            return i
    for i, name in inputs:
        if lname in name.lower() or name.lower() in lname:
            return i
    return None


# Speech-level measurement splits the recording into short frames and
# averages only the ones louder than the noise floor (see
# _voice_gated_level_db) - a plain whole-window RMS average would include
# whatever fraction of the 5s window was silence (between words, before
# starting, after finishing), which shifts with how fast someone reads the
# sentence and made the reported level inconsistent run to run. The noise
# floor measurement itself doesn't use this - it's supposed to be silence
# for the whole window, so a plain average is exactly right there.
VOICE_FRAME_SECONDS = 0.03
VOICE_ACTIVITY_MARGIN_DB = 6.0


def _amplitude_ratio_to_db(ratio):
    return -96.0 if ratio <= 1e-9 else max(-96.0, 20.0 * math.log10(ratio))


def _rms_db_int16(samples):
    if not samples:
        return -96.0
    mean_square = sum(s * s for s in samples) / len(samples)
    return _amplitude_ratio_to_db(math.sqrt(mean_square) / 32768.0)


def _voice_gated_level_db(frame_levels_db, noise_floor_db, margin_db=VOICE_ACTIVITY_MARGIN_DB):
    """Averages (in power, i.e. proper RMS-of-RMS) only the frames louder
    than noise_floor_db + margin_db, so silent/quiet stretches within the
    recording don't drag the result down. Falls back to every frame (the
    old whole-window behavior) if nothing crossed the threshold - e.g. the
    mic was too quiet or nobody spoke - rather than reporting silence."""
    if not frame_levels_db:
        return -96.0
    active = [lvl for lvl in frame_levels_db if lvl > noise_floor_db + margin_db]
    if not active:
        active = frame_levels_db
    mean_power_ratio = sum(10.0 ** (lvl / 10.0) for lvl in active) / len(active)
    return -96.0 if mean_power_ratio <= 1e-9 else max(-96.0, 10.0 * math.log10(mean_power_ratio))


def _measure_audio_level_db_linux(device_id, duration_seconds, samplerate=48000, noise_floor_db=None):
    """Records via `parecord` directly from the Pulse source named by
    device_id (OBS's own device_id string on Linux - see
    setup.py's _list_audio_devices_linux), sidestepping PortAudio/
    sounddevice entirely so the actually-selected mic is what gets
    measured.

    Calibration opens and closes the mic several times in quick
    succession (noise floor, speech, every Redo), which is exactly the
    pattern that trips up flaky USB audio hardware if a `parecord` from a
    previous measurement is ever left attached: once we've read our
    target_bytes, parecord keeps writing and the pipe fills up, so it
    blocks in write() - if it doesn't react to SIGTERM immediately from
    inside that blocked syscall (dependent on its own PulseAudio mainloop/
    signal handling), a plain terminate()+wait() with no timeout can hang
    forever, and if the wizard gets killed while stuck there, the orphaned
    parecord keeps holding the device until something forces the OS to
    reset it - which is exactly what unplugging/replugging the mic does.
    Closing our end of the pipe first guarantees parecord gets SIGPIPE on
    its very next write (no dependency on signal-handling timing), and the
    kill() fallback guarantees this function never blocks waiting on a
    child process that won't die."""
    args = ["parecord", "--raw", "--format=s16le", "--rate", str(samplerate), "--channels=1"]
    if device_id and device_id != "default":
        args += ["--device", device_id]
    frame_bytes = 2
    target_bytes = int(duration_seconds * samplerate) * frame_bytes
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)
    try:
        raw = proc.stdout.read(target_bytes)
    finally:
        proc.stdout.close()
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    # A brief settle gap before the caller potentially reopens the same
    # device (the next measurement, or a Redo) - some USB audio hardware
    # needs a moment to fully release its isochronous endpoint after a
    # capture stream closes, and reopening immediately is the other half
    # of the pattern that's led to mics needing a physical replug.
    time.sleep(0.2)
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return -96.0
    if noise_floor_db is None:
        return _rms_db_int16(samples)

    frame_size = max(1, int(samplerate * VOICE_FRAME_SECONDS))
    frame_levels = [
        _rms_db_int16(samples[i:i + frame_size])
        for i in range(0, len(samples) - frame_size + 1, frame_size)
    ]
    return _voice_gated_level_db(frame_levels, noise_floor_db)


def _measure_audio_level_db_sounddevice(device_name, duration_seconds, noise_floor_db=None):
    """Windows/macOS path: records via sounddevice, matched by friendly
    device name - see _find_sounddevice_input_index. See
    _measure_audio_level_db_linux's docstring for why the calibration
    flow's rapid open/close/reopen pattern gets defensive cleanup here
    too: sd.stop() in a finally guarantees the stream never lingers open
    if recording raises partway through, and the settle gap before
    returning gives WASAPI/CoreAudio a moment to fully release the
    device before the caller (the next measurement, or a Redo) reopens
    it."""
    import numpy as np
    import sounddevice as sd
    device_index = _find_sounddevice_input_index(device_name)
    samplerate = 48000
    if device_index is not None:
        samplerate = int(sd.query_devices(device_index)["default_samplerate"])
    try:
        recording = sd.rec(
            int(duration_seconds * samplerate), samplerate=samplerate, channels=1,
            dtype="float32", device=device_index,
        )
        sd.wait()
    finally:
        sd.stop()
        time.sleep(0.2)
    flat = recording.reshape(-1)
    if flat.size == 0:
        return -96.0
    if noise_floor_db is None:
        rms = float(np.sqrt(np.mean(np.square(flat))))
        return _amplitude_ratio_to_db(rms)

    frame_size = max(1, int(samplerate * VOICE_FRAME_SECONDS))
    num_frames = flat.size // frame_size
    if num_frames == 0:
        rms = float(np.sqrt(np.mean(np.square(flat))))
        return _amplitude_ratio_to_db(rms)
    frames = flat[: num_frames * frame_size].reshape(num_frames, frame_size)
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1))
    frame_levels = [_amplitude_ratio_to_db(float(r)) for r in frame_rms]
    return _voice_gated_level_db(frame_levels, noise_floor_db)


def measure_audio_level_db(device_name, device_id, duration_seconds, noise_floor_db=None):
    """Records `duration_seconds` of mono audio from the given input
    device and returns its level in dBFS (0 dBFS = full scale, -96 is
    used as a silence floor to keep log10 finite). device_name/device_id
    are the same pair setup.py's list_audio_devices()["input"] entries
    carry.

    Pass noise_floor_db (the earlier noise-floor measurement) when this
    call is measuring speech, not silence - it switches to averaging only
    the voiced frames (see _voice_gated_level_db) instead of the whole
    window, so the result doesn't depend on how much silence ended up in
    the recording. Leave it None for the noise-floor measurement itself,
    which is supposed to be silence for the whole window."""
    if platform.system() == "Linux":
        return _measure_audio_level_db_linux(device_id, duration_seconds, noise_floor_db=noise_floor_db)
    return _measure_audio_level_db_sounddevice(device_name, duration_seconds, noise_floor_db=noise_floor_db)


def compute_mic_filter_values(noise_floor_db, speech_level_db):
    """Returns (gain_db, open_threshold_db, close_threshold_db) from a
    measured noise floor and speaking level: the Gain filter's db pulls
    the speaking level up (or down) to TARGET_SPEECH_DBFS, and the Noise
    Gate's thresholds sit just above the noise floor so it stays closed
    on room noise but opens as soon as the mic'd voice comes back.

    The gate sits after the Gain filter in the chain (HEAD_FILTER_IDS), so
    it sees both levels shifted by gain_db - its thresholds are placed
    relative to the noise floor as it is after that gain."""
    gain_db = max(-30.0, min(30.0, TARGET_SPEECH_DBFS - speech_level_db))
    close_threshold_db = max(-96.0, min(0.0, noise_floor_db + gain_db + 3.0))
    open_threshold_db = max(-96.0, min(0.0, close_threshold_db + 5.0))
    return gain_db, open_threshold_db, close_threshold_db


def _new_filter_entry(name, filter_id, settings):
    return {
        "prev_ver": 520093698,
        "name": name,
        "uuid": str(uuid.uuid4()),
        "id": filter_id,
        "versioned_id": filter_id,
        "settings": settings,
        "mixers": 255,
        "sync": 0,
        "flags": 0,
        "volume": 1.0,
        "balance": 0.5,
        "enabled": True,
        "muted": False,
        "push-to-mute": False,
        "push-to-mute-delay": 0,
        "push-to-talk": False,
        "push-to-talk-delay": 0,
        "hotkeys": {},
        "deinterlace_mode": 0,
        "deinterlace_field_order": 0,
        "monitoring_type": 0,
        "private_settings": {},
    }


# The full voice chain, in the signal-flow order OBS actually applies
# filters (top to bottom of a source's filter list): boost the level up to
# a working volume, gate out room noise now that it's audible, clean up
# what's left, even out dynamics, then catch whatever peaks the earlier
# boost/compression produced before they clip. Noise Suppression and
# Compressor aren't measurement-driven - see save_mic_filter_levels - so
# they're only created with OBS's own built-in defaults (settings: {}) if
# genuinely missing; an existing hand-tuned filter is left untouched.
HEAD_FILTER_IDS = ["gain_filter", "noise_gate_filter", "noise_suppress_filter"]
TAIL_FILTER_IDS = ["compressor_filter", "limiter_filter"]
FILTER_DISPLAY_NAMES = {
    "gain_filter": "Gain",
    "noise_gate_filter": "Noise Gate",
    "noise_suppress_filter": "Noise Suppression",
    "compressor_filter": "Compressor",
    "limiter_filter": "Limiter",
}


def has_mic_filter_been_configured(scene_collection_json_path, mic_scene_key):
    """Whether save_mic_filter_levels has ever run against this scene
    collection: it's the one thing that creates gain_filter/noise_gate_filter
    on the mic source, and the shipped default profile has neither -
    letting the GUI show "Redo" instead of "Start" across app restarts,
    not just within the same wizard session."""
    if not os.path.exists(scene_collection_json_path):
        return False
    try:
        with open(scene_collection_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    entry = data.get(mic_scene_key)
    if entry is None:
        return False
    filter_ids = {f.get("id") for f in entry.get("filters", [])}
    return "gain_filter" in filter_ids or "noise_gate_filter" in filter_ids


def save_mic_filter_levels(scene_collection_json_path, mic_scene_key, noise_floor_db, speech_level_db):
    """Writes the measured levels into the mic source's Gain and Noise Gate
    filters, and makes sure the rest of the voice chain (Noise Suppression,
    Compressor, Limiter) exists too - creating any of the five that are
    missing - then reorders the filter list to HEAD_FILTER_IDS + (anything
    else already there, e.g. a hand-added Upward Compressor) + TAIL_FILTER_IDS
    so the chain always applies in the right order regardless of what state
    it started in. scene_collection_json_path/mic_scene_key are setup.py's
    SCENE_COLLECTION_JSON and AUDIO_DEVICE_SCENE_KEYS["input"] - passed in
    rather than imported so this module doesn't need to depend on setup.py
    at all."""
    print(f"Scene collection: {scene_collection_json_path}")
    if not os.path.exists(scene_collection_json_path):
        print("  Scene collection not found or unreadable - can't set mic filters.", file=sys.stderr)
        return
    with open(scene_collection_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    entry = data.get(mic_scene_key)
    if entry is None:
        print(
            f"  '{mic_scene_key}' not found in scene collection - "
            "no mic source to filter.", file=sys.stderr,
        )
        return

    gain_db, open_threshold_db, close_threshold_db = compute_mic_filter_values(
        noise_floor_db, speech_level_db
    )
    measured_settings = {
        "gain_filter": {"db": gain_db},
        "noise_gate_filter": {"close_threshold": close_threshold_db, "open_threshold": open_threshold_db},
    }

    filters = entry.setdefault("filters", [])
    by_id = {f.get("id"): f for f in filters}
    created, updated = [], []

    for filter_id in HEAD_FILTER_IDS + TAIL_FILTER_IDS:
        settings = measured_settings.get(filter_id, {})
        existing = by_id.get(filter_id)
        if existing:
            if settings:
                existing.setdefault("settings", {}).update(settings)
                updated.append(filter_id)
        else:
            new_filter = _new_filter_entry(FILTER_DISPLAY_NAMES[filter_id], filter_id, settings)
            filters.append(new_filter)
            by_id[filter_id] = new_filter
            created.append(filter_id)

    chain_ids = set(HEAD_FILTER_IDS) | set(TAIL_FILTER_IDS)
    head = [by_id[filter_id] for filter_id in HEAD_FILTER_IDS]
    tail = [by_id[filter_id] for filter_id in TAIL_FILTER_IDS]
    middle = [f for f in filters if f.get("id") not in chain_ids]
    entry["filters"] = head + middle + tail

    write_json_atomic(scene_collection_json_path, data)

    if created:
        print(f"  Added missing filter(s): {', '.join(FILTER_DISPLAY_NAMES[i] for i in created)}.")
    print(
        f"  Mic filters -> Gain {gain_db:+.1f} dB, Noise Gate "
        f"open {open_threshold_db:.1f} dB / close {close_threshold_db:.1f} dB "
        f"({', '.join(FILTER_DISPLAY_NAMES[i] for i in HEAD_FILTER_IDS + TAIL_FILTER_IDS)} "
        "all present and in order)."
    )
