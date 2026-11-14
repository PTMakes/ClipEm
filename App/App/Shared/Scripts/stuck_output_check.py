import ctypes
import json
import math
import os
import platform
import time

import obspython as obs

_SYSTEM = platform.system()  # "Windows", "Darwin", or "Linux"

# Stuck-screen detection used to live here too (hence the file name, kept
# so existing scene collections still load this script); it's now the tray
# widget's - see tray-widget/stuck_screen.py.

# This script's own folder (App/Shared/Scripts). Not OBS's script_path(),
# which OBS injects into the script's module rather than obspython.
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def dbg(debug_message):
    global debug_enabled

    if not debug_enabled:
        return
    
    obs.script_log(obs.LOG_INFO, f"[DEBUG-INFO] {debug_message}")

    return


# -------------------------------------------------------------------------------------------------------------------------------------------------
# Audio Peak Guard
# -------------------------------------------------------------------------------------------------------------------------------------------------
# obspython has no way to read audio levels (volmeter callbacks aren't
# wrapped), so this loads libobs itself with ctypes and attaches one of
# OBS's own volume meters - what the Audio Mixer's meters are - to each audio
# input. The meter calls back on OBS's audio thread with each channel's
# post-fader peak in dBFS; the callback only records the loudest one, and
# peak_guard_tick() (an OBS timer) turns the volume down if it hit clip level.
#
# Turning down isn't for good: once a lowered source has gone
# PEAK_GUARD_RESTORE_QUIET_SECONDS without clipping, its volume goes back up
# PEAK_GUARD_RESTORE_STEP_DB at a time, until it's back where it was. One that
# clips again soon after a step up waits twice as long before the next (up
# to PEAK_GUARD_RESTORE_MAX_WAIT_SECONDS). One that does that
# PEAK_GUARD_SETTLE_AFTER times really is too loud where it was: it stays at
# the level that stopped the clipping, which becomes its volume - no more
# steps up. A volume changed by hand while
# lowered is the user's call: it's left there. Where each lowered source was
# is kept in PEAK_GUARD_STATE_PATH, so restoring carries on after OBS restarts.
#
# Privacy: nothing here can be turned back into speech or other audio. The
# meter hands over only level numbers (one dB value per channel per audio
# packet) - never samples - and only the post-fader peak is read. Anything
# below clip level is dropped on arrival, so normal talking leaves no trace;
# at most one number per source (its loudest clip-level peak) is held, in
# memory, until the next tick. Nothing is written to disk except one log
# line per source per session (no levels or times of later clips), the
# lowered volume itself, which OBS saves like any slider change, and the
# volume it had before (PEAK_GUARD_STATE_PATH) - slider positions only.
#
# ctypes.CDLL (not PyDLL) releases the GIL during each libobs call - needed
# so obs_volmeter_destroy() can wait out an in-flight callback (which needs
# the GIL) without deadlocking.

MAX_AUDIO_CHANNELS = 8  # libobs media-io/audio-io.h
OBS_FADER_CUBIC = 0  # enum obs_fader_type; only affects the fader, not peaks
PEAK_GUARD_TICK_MS = 100
PEAK_GUARD_RESCAN_TICKS = 50  # re-look for new/removed audio sources every 5s

_FloatArray = ctypes.POINTER(ctypes.c_float)
VOLMETER_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, _FloatArray, _FloatArray, _FloatArray)

# Where each lowered source's volume was: App/cache, next to the tray's files.
PEAK_GUARD_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(SCRIPTS_DIR)), "cache", "peak_guard.json")
PEAK_GUARD_RESTORE_QUIET_SECONDS = 30
PEAK_GUARD_RESTORE_STEP_DB = 1.0
PEAK_GUARD_RESTORE_MAX_WAIT_SECONDS = 10 * 60
# Clips right after a step up before a source stays where it was turned down to.
PEAK_GUARD_SETTLE_AFTER = 2
VOLUME_TOLERANCE = 1e-4  # OBS keeps volumes as 32-bit floats

libobs = None
# source uuid -> {"base": its volume before it was lowered, "set": what the
# guard last set it to (None: not known yet - after a restart), "changed_at",
# "wait": seconds without clipping before the next step up, "raised_at",
# "reclips": times it clipped again right after a step up}
lowered = {}
peak_meters = {}  # source uuid -> {"meter": c_void_p, "callback": VOLMETER_CALLBACK}
loudest_peaks = {}  # source uuid -> loudest post-fader peak (dBFS) since the last tick
peak_guard_ticks = 0
peak_guard_logged = set()  # source uuids already logged this session


def load_libobs():
    """Returns libobs (already loaded into OBS's process) with the volmeter
    functions typed, or None if it can't be found."""
    candidates = ("obs",) if _SYSTEM == "Windows" else (None, "libobs.so.0", "libobs.0.dylib")
    for name in candidates:
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        # With name=None this is the whole process's symbol table - skip
        # anything that isn't libobs.
        if not hasattr(lib, "obs_volmeter_create"):
            continue
        lib.obs_volmeter_create.restype = ctypes.c_void_p
        lib.obs_volmeter_create.argtypes = [ctypes.c_int]
        lib.obs_volmeter_destroy.restype = None
        lib.obs_volmeter_destroy.argtypes = [ctypes.c_void_p]
        lib.obs_volmeter_attach_source.restype = ctypes.c_bool
        lib.obs_volmeter_attach_source.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.obs_volmeter_add_callback.restype = None
        lib.obs_volmeter_add_callback.argtypes = [ctypes.c_void_p, VOLMETER_CALLBACK, ctypes.c_void_p]
        lib.obs_get_source_by_uuid.restype = ctypes.c_void_p
        lib.obs_get_source_by_uuid.argtypes = [ctypes.c_char_p]
        lib.obs_source_release.restype = None
        lib.obs_source_release.argtypes = [ctypes.c_void_p]
        lib.obs_queue_task.restype = None
        lib.obs_queue_task.argtypes = [ctypes.c_int, TASK_CALLBACK, ctypes.c_void_p, ctypes.c_bool]
        return lib
    return None


def make_peak_callback(uuid):
    """Runs on OBS's audio thread - keep it tiny. Only clip-level peaks are
    kept (see Privacy above); magnitude and input_peak are never read."""
    def on_levels(param, magnitude, peak, input_peak):
        loudest = max(peak[i] for i in range(MAX_AUDIO_CHANNELS))
        if loudest >= PEAK_CLIP_DB and loudest > loudest_peaks.get(uuid, -math.inf):
            loudest_peaks[uuid] = loudest
    return VOLMETER_CALLBACK(on_levels)


def audio_input_uuids():
    """UUIDs of every input source that outputs audio (mics, desktop audio,
    media/browser sources...)."""
    uuids = []
    sources = obs.obs_enum_sources()
    if sources is not None:
        for source in sources:
            if (obs.obs_source_get_type(source) == obs.OBS_SOURCE_TYPE_INPUT
                    and obs.obs_source_get_output_flags(source) & obs.OBS_SOURCE_AUDIO):
                uuids.append(obs.obs_source_get_uuid(source))
        obs.source_list_release(sources)
    return uuids


def attach_peak_meter(uuid):
    source = libobs.obs_get_source_by_uuid(uuid.encode())
    if not source:
        return
    meter = libobs.obs_volmeter_create(OBS_FADER_CUBIC)
    callback = make_peak_callback(uuid)
    libobs.obs_volmeter_add_callback(meter, callback, None)
    # The meter holds no reference; it detaches itself if the source is destroyed.
    attached = libobs.obs_volmeter_attach_source(meter, source)
    libobs.obs_source_release(source)
    if not attached:
        libobs.obs_volmeter_destroy(meter)
        return
    # The callback object must outlive the meter, or OBS calls freed memory.
    peak_meters[uuid] = {"meter": meter, "callback": callback}


def detach_peak_meter(uuid):
    entry = peak_meters.pop(uuid)
    libobs.obs_volmeter_destroy(entry["meter"])  # no callbacks after this returns
    loudest_peaks.pop(uuid, None)


def sync_peak_meters():
    current = set(audio_input_uuids())
    for uuid in set(peak_meters) - current:
        detach_peak_meter(uuid)
    for uuid in current - set(peak_meters):
        attach_peak_meter(uuid)


def load_lowered(now):
    """The lowered sources saved last session - what they're at now is
    looked at on the next tick."""
    try:
        with open(PEAK_GUARD_STATE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    loaded = {}
    for uuid, value in saved.items():
        # {"base", "reclips"}; older saves had just the base volume.
        base, reclips = (value.get("base"), value.get("reclips", 0)) if isinstance(value, dict) else (value, 0)
        if isinstance(base, (int, float)) and base > 0 and isinstance(reclips, int):
            loaded[uuid] = {"base": float(base), "set": None, "changed_at": now,
                            "wait": PEAK_GUARD_RESTORE_QUIET_SECONDS, "raised_at": None, "reclips": reclips}
    return loaded


def save_lowered():
    try:
        os.makedirs(os.path.dirname(PEAK_GUARD_STATE_PATH), exist_ok=True)
        partial = PEAK_GUARD_STATE_PATH + ".partial"
        with open(partial, "w", encoding="utf-8") as f:
            json.dump({uuid: {"base": entry["base"], "reclips": entry["reclips"]} for uuid, entry in lowered.items()},
                      f)
        os.replace(partial, PEAK_GUARD_STATE_PATH)
    except OSError as e:
        dbg(f"Couldn't save the peak guard's volumes: {e}")


def note_lowered(uuid, volume, new_volume, now):
    """The guard turned uuid down from volume to new_volume. True if it's
    now settled there: it kept clipping whenever it was turned back up."""
    entry = lowered.get(uuid)
    if entry is None or entry["set"] is None or abs(volume - entry["set"]) > VOLUME_TOLERANCE:
        # Not lowered before - or moved by hand since: that's the level to go back to.
        entry = {"base": volume, "wait": PEAK_GUARD_RESTORE_QUIET_SECONDS, "raised_at": None, "reclips": 0}
    elif entry["raised_at"] is not None and now - entry["raised_at"] < entry["wait"]:
        # Clipped again soon after being turned up: slower next time - or, if
        # it keeps doing that, no next time.
        entry["reclips"] += 1
        if entry["reclips"] >= PEAK_GUARD_SETTLE_AFTER:
            lowered.pop(uuid, None)
            save_lowered()
            return True
        entry["wait"] = min(entry["wait"] * 2, PEAK_GUARD_RESTORE_MAX_WAIT_SECONDS)
    entry["set"] = new_volume
    entry["changed_at"] = now
    lowered[uuid] = entry
    save_lowered()
    return False


def restore_lowered(now):
    """Steps each lowered source that's been quiet long enough back up."""
    changed = False
    for uuid, entry in list(lowered.items()):
        source = obs.obs_get_source_by_uuid(uuid)
        if source is None:
            continue  # gone for now - it may come back (a scene collection switch)
        try:
            volume = obs.obs_source_get_volume(source)
            if entry["set"] is None:
                entry["set"] = volume  # first look since a restart
            if abs(volume - entry["set"]) > VOLUME_TOLERANCE or volume >= entry["base"] - VOLUME_TOLERANCE:
                # Moved by hand (or already back): the user's level stays.
                del lowered[uuid]
                changed = True
                continue
            if now - entry["changed_at"] < entry["wait"]:
                continue
            new_volume = min(entry["base"], volume * 10 ** (PEAK_GUARD_RESTORE_STEP_DB / 20))
            obs.obs_source_set_volume(source, new_volume)
            if new_volume >= entry["base"] - VOLUME_TOLERANCE:
                del lowered[uuid]
                obs.script_log(obs.LOG_INFO, f"[ClipEm] '{obs.obs_source_get_name(source)}' stopped clipping; "
                                             "its volume is back where it was")
            else:
                entry.update(set=new_volume, changed_at=now, raised_at=now)
            changed = True
        finally:
            obs.obs_source_release(source)
    if changed:
        save_lowered()


def stop_peak_guard():
    obs.timer_remove(peak_guard_tick)
    if libobs is not None:
        for uuid in list(peak_meters):
            detach_peak_meter(uuid)
    loudest_peaks.clear()


def start_peak_guard():
    global libobs, peak_guard_ticks
    stop_peak_guard()
    if not peak_guard_enabled:
        dbg("Audio peak guard disabled; not starting timer")
        return
    if libobs is None:
        libobs = load_libobs()
        if libobs is None:
            obs.script_log(obs.LOG_WARNING, "[ClipEm] Audio peak guard unavailable: couldn't load libobs")
            return
    peak_guard_ticks = 0
    lowered.clear()
    lowered.update(load_lowered(time.monotonic()))
    sync_peak_meters()
    obs.timer_add(peak_guard_tick, PEAK_GUARD_TICK_MS)


# Never lowered past this (-20 dB): a source that keeps peaking - a mic
# behind a strong gain or upward compressor filter - would otherwise be
# turned down a little more every time, until it's missing from clips.
PEAK_GUARD_MIN_VOLUME = 0.1


def reduced_volume(volume, peak_db, ceiling_db):
    """The volume multiplier that brings a post-fader peak of peak_db down
    to ceiling_db, but not below PEAK_GUARD_MIN_VOLUME. Never turns the
    volume up."""
    return min(volume, max(volume * 10 ** ((ceiling_db - peak_db) / 20), PEAK_GUARD_MIN_VOLUME))


def peak_guard_tick():
    global peak_guard_ticks
    try:
        now = time.monotonic()
        peak_guard_ticks += 1
        if peak_guard_ticks >= PEAK_GUARD_RESCAN_TICKS:
            peak_guard_ticks = 0
            sync_peak_meters()

        for uuid in list(loudest_peaks):
            peak_db = loudest_peaks.pop(uuid, -math.inf)
            if not peak_db >= PEAK_CLIP_DB:  # also skips NaN
                continue
            source = obs.obs_get_source_by_uuid(uuid)
            if source is None:
                continue
            try:
                volume = obs.obs_source_get_volume(source)
                new_volume = reduced_volume(volume, peak_db, peak_ceiling_db)
                if new_volume < volume:
                    obs.obs_source_set_volume(source, new_volume)
                    if note_lowered(uuid, volume, new_volume, now):
                        obs.script_log(
                            obs.LOG_INFO,
                            f"[ClipEm] '{obs.obs_source_get_name(source)}' kept clipping whenever it was turned "
                            "back up; leaving its volume where it stopped clipping",
                        )
                    elif uuid not in peak_guard_logged:
                        peak_guard_logged.add(uuid)
                        obs.script_log(
                            obs.LOG_INFO,
                            f"[ClipEm] '{obs.obs_source_get_name(source)}' was clipping; lowering its volume "
                            "to stop it (logged once per session)",
                        )
            finally:
                obs.obs_source_release(source)
        restore_lowered(now)
    except Exception as e:
        dbg(f"Error in audio peak guard: {e}")


# -------------------------------------------------------------------------------------------------------------------------------------------------
# PipeWire "Select Monitor", on request from the tray widget
# -------------------------------------------------------------------------------------------------------------------------------------------------
# When a PipeWire screen capture's stream dies (e.g. its display was
# disconnected), only a new portal session - the source's own "Select
# Monitor" button - brings it back. The tray widget spots that
# (tray-widget/stuck_screen.py), but pressing the button over obs-websocket
# runs it on obs-websocket's thread, where the portal session never gets
# going. So the tray just sets RELOAD_REQUEST_KEY in the source's settings,
# and this presses the button on OBS's UI thread - exactly like a click in
# the Properties dialog - via obs_queue_task (through ctypes: obspython
# can't pass a Python function as its callback).

RELOAD_REQUEST_KEY = "clipem_reload_request"  # must match stuck_screen.PIPEWIRE_RELOAD_REQUEST_KEY
PIPEWIRE_SOURCE_IDS = ("pipewire-screen-capture-source",)
PIPEWIRE_RELOAD_BUTTON = "Reload"
RELOAD_POLL_MS = 1000
OBS_TASK_UI = 0  # enum obs_task_type, libobs/obs.h

TASK_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
task_libobs = None
pending_tasks = []  # queued callbacks - must stay alive until OBS runs them


def press_reload_button(source_name):
    """On the UI thread: the source's "Select Monitor" button."""
    source = obs.obs_get_source_by_name(source_name)
    if source is None:
        return
    props = obs.obs_source_properties(source)
    try:
        button = obs.obs_properties_get(props, PIPEWIRE_RELOAD_BUTTON)
        if button is None:
            obs.script_log(obs.LOG_WARNING, f"[ClipEm] '{source_name}' has no Select Monitor button")
            return
        obs.script_log(obs.LOG_INFO, f"[ClipEm] Restarting the capture session of '{source_name}' (tray widget request)")
        obs.obs_property_button_clicked(button, source)
    finally:
        obs.obs_properties_destroy(props)
        obs.obs_source_release(source)


def queue_on_ui_thread(work):
    def run(param):
        try:
            work()
        except Exception as e:
            obs.script_log(obs.LOG_WARNING, f"[ClipEm] UI task failed: {e}")
        finally:
            pending_tasks.remove(callback)

    callback = TASK_CALLBACK(run)
    pending_tasks.append(callback)
    task_libobs.obs_queue_task(OBS_TASK_UI, callback, None, False)


def reload_request_tick():
    try:
        requested = []
        sources = obs.obs_enum_sources()
        if sources is not None:
            for source in sources:
                if obs.obs_source_get_id(source) not in PIPEWIRE_SOURCE_IDS:
                    continue
                settings = obs.obs_source_get_settings(source)
                if obs.obs_data_has_user_value(settings, RELOAD_REQUEST_KEY):
                    obs.obs_data_erase(settings, RELOAD_REQUEST_KEY)
                    requested.append(obs.obs_source_get_name(source))
                obs.obs_data_release(settings)
            obs.source_list_release(sources)
        for name in requested:
            queue_on_ui_thread(lambda name=name: press_reload_button(name))
    except Exception as e:
        dbg(f"Error handling a capture restart request: {e}")


def start_reload_requests():
    global task_libobs
    obs.timer_remove(reload_request_tick)
    if task_libobs is None:
        task_libobs = load_libobs()
        if task_libobs is None:
            obs.script_log(obs.LOG_WARNING, "[ClipEm] Capture restart requests unavailable: couldn't load libobs")
            return
    obs.timer_add(reload_request_tick, RELOAD_POLL_MS)


# -------------------------------------------------------------------------------------------------------------------------------------------------
# OBS Script Hooks
# -------------------------------------------------------------------------------------------------------------------------------------------------
def script_description():
    return ("Audio peak guard: when an audio source's level gets within 1 dB of full scale (where OBS's meters show clipping), turns its volume down so the peak sits at the ceiling below - and back up a step at a time once it stops."
            "\n\nAlso restarts a PipeWire screen capture's session when the ClipEm tray icon asks (its stuck-screen detection - see its Settings).")

# Default Config Variables
debug_enabled = False
peak_guard_enabled = True
# A post-fader peak at or above PEAK_CLIP_DB counts as peaking; the source's
# volume is then lowered so that peak would have landed on peak_ceiling_db.
# Not 0 dB: OBS's meters flash "clipping" for any peak within one pixel of
# the top (VolumeMeter truncates to whole pixels - about -0.2 to -0.6 dB
# depending on the meter's size), and most music/video (e.g. YouTube) peaks
# just under 0 dB without ever reaching it.
PEAK_CLIP_DB = -1.0
peak_ceiling_db = -1.5

def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "debug_enabled", "Enable Debug Messages")
    obs.obs_properties_add_bool(props, "peak_guard_enabled", "Lower Volume of Audio Sources That Peak")
    obs.obs_properties_add_float(props, "peak_ceiling_db", "Peak Ceiling After Lowering (dB)", -20.0, PEAK_CLIP_DB - 0.1, 0.1)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "debug_enabled", debug_enabled)
    obs.obs_data_set_default_bool(settings, "peak_guard_enabled", peak_guard_enabled)
    obs.obs_data_set_default_double(settings, "peak_ceiling_db", peak_ceiling_db)
    return


def script_update(settings):
    global debug_enabled, peak_guard_enabled, peak_ceiling_db
    debug_enabled = obs.obs_data_get_bool(settings, "debug_enabled")
    peak_guard_enabled = obs.obs_data_get_bool(settings, "peak_guard_enabled")
    peak_ceiling_db = obs.obs_data_get_double(settings, "peak_ceiling_db")

    start_peak_guard()


def script_load(settings):
    dbg("Loading script")
    start_reload_requests()


def script_unload():
    dbg("Unloading script")
    obs.timer_remove(reload_request_tick)
    stop_peak_guard()
