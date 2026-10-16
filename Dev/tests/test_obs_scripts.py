"""Runs every OBS script in App/Shared/Scripts through its whole lifecycle -
load, defaults, properties, update, frontend events, timers, unload - the
way OBS drives it, against a stand-in `obspython` that only has the names
the real one has (tests/obspython_api.json, generated from our OBS build by
Dev/generate_obspython_api.py). Any other name is recorded and fails the
test, even where the script catches the resulting AttributeError itself.

What this can't catch: wrong argument types/counts, or OBS behaving
differently from these fakes - only a real OBS run shows those.
"""

import ctypes
import glob
import importlib.util
import json
import math
import os
import sys
import types

import pytest
from conftest import SCRIPTS_DIR

API = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "obspython_api.json")))
SCRIPTS = sorted(glob.glob(os.path.join(SCRIPTS_DIR, "*.py")))


class Data:
    """obs_data_t: a dict plus defaults."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.defaults = {}

    def get(self, key, fallback):
        return self.values.get(key, self.defaults.get(key, fallback))


class Handle:
    """Any other opaque OBS pointer (sources, scenes, properties...)."""

    def __init__(self, kind):
        self.kind = kind


class Struct:
    """A SWIG struct (e.g. obs_transform_info) that only has its real fields."""

    def __init__(self, fake, cls):
        object.__setattr__(self, "_fake", fake)
        object.__setattr__(self, "_cls", cls)
        object.__setattr__(self, "_fields", {})
        if cls == "obs_transform_info":
            for name in ("pos", "scale", "bounds"):
                self._fields[name] = Struct(fake, "vec2")

    def _check(self, name):
        if name not in API["classes"][self._cls]:
            self._fake.missing.append(f"{self._cls}.{name}")
            raise AttributeError(f"{self._cls} has no attribute {name!r}")

    def __getattr__(self, name):
        self._check(name)
        return self._fields.get(name)

    def __setattr__(self, name, value):
        self._check(name)
        self._fields[name] = value


class FakeLibobs:
    """The libobs volmeter functions stuck_output_check.py calls via ctypes."""

    def __init__(self):
        self.meters = {}  # meter id -> {"callback", "destroyed"}

    def obs_volmeter_create(self, fader_type):
        meter = len(self.meters) + 1
        self.meters[meter] = {"callback": None, "destroyed": False}
        return meter

    def obs_volmeter_add_callback(self, meter, callback, param):
        self.meters[meter]["callback"] = callback

    def obs_volmeter_attach_source(self, meter, source):
        return bool(source)

    def obs_volmeter_destroy(self, meter):
        self.meters[meter]["destroyed"] = True

    def obs_get_source_by_uuid(self, uuid):
        return 1 if uuid == b"mic-uuid" else None

    def obs_source_release(self, source):
        pass

    def obs_queue_task(self, task_type, callback, param, wait):
        """OBS runs it on the UI thread later; here, straight away."""
        self.ui_tasks = getattr(self, "ui_tasks", 0) + 1
        callback(param)

    def live_meters(self):
        return [m for m in self.meters.values() if not m["destroyed"]]

    def send_peak_db(self, peak_db):
        """What OBS's audio thread does: the callback with per-channel dB arrays."""
        levels = (ctypes.c_float * 8)(peak_db, peak_db - 6, *([-math.inf] * 6))
        for meter in self.live_meters():
            meter["callback"](None, levels, levels, levels)


class FakeObspython(types.ModuleType):
    """Only names from obspython_api.json exist. The ones ClipEm's scripts
    use get small working fakes; the rest return a Handle."""

    def __init__(self, tmp_path):
        super().__init__("obspython")
        self.missing = []
        self.logs = []
        self.timers = {}
        self.event_callbacks = []
        self.tmp_path = tmp_path
        self.last_replay = None
        self.audio_source = Handle("source")
        self.volumes = {self.audio_source: 1.0}
        # A PipeWire screen capture the tray widget has asked to restart.
        self.capture_source = Handle("source")
        self.capture_settings = Data({"clipem_reload_request": 1})
        self.capture_button = Handle("property")
        self.buttons_clicked = []
        self._constants = {}
        self._impl = self._fakes()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name not in API["names"]:
            self.missing.append(name)
            raise AttributeError(f"module 'obspython' has no attribute {name!r}")
        if name in self._impl:
            return self._impl[name]
        if name.isupper() or name[:1].isupper() and name.upper() == name:
            return self._constants.setdefault(name, 1000 + len(self._constants))
        if name in API["classes"]:
            return lambda: Struct(self, name)
        return lambda *args: Handle(name)

    def constant(self, name):
        return getattr(self, name)

    def _fakes(self):
        data_types = {"bool": False, "int": 0, "double": 0.0, "string": ""}
        fakes = {
            "script_log": lambda level, msg: self.logs.append(msg),
            "timer_add": lambda fn, ms: self.timers.__setitem__(fn, ms),
            "timer_remove": lambda fn: self.timers.pop(fn, None),
            "obs_frontend_add_event_callback": self.event_callbacks.append,
            "obs_frontend_remove_event_callback": lambda cb: self.event_callbacks.remove(cb)
            if cb in self.event_callbacks else None,
            "obs_frontend_get_last_replay": lambda: self.last_replay,
            "obs_enum_sources": lambda: [Handle("source"), self.audio_source, self.capture_source],
            "obs_source_get_id": lambda source: "pipewire-screen-capture-source"
            if source is self.capture_source else "other",
            "obs_source_get_settings": lambda source: self.capture_settings
            if source is self.capture_source else Data(),
            "obs_source_get_name": lambda source: "Screen Capture" if source is self.capture_source else "Mic",
            "obs_get_source_by_name": lambda name: self.capture_source if name == "Screen Capture" else None,
            "obs_data_has_user_value": lambda data, key: key in data.values,
            "obs_data_erase": lambda data, key: data.values.pop(key, None),
            "obs_properties_get": lambda props, name: self.capture_button if name == "Reload" else None,
            "obs_property_button_clicked": lambda prop, source: self.buttons_clicked.append((prop, source)),
            "source_list_release": lambda sources: None,
            "obs_source_release": lambda source: None,
            "obs_get_source_by_uuid": lambda uuid: self.audio_source if uuid == "mic-uuid" else None,
            "obs_source_get_uuid": lambda source: "mic-uuid" if source is self.audio_source else "other-uuid",
            "obs_source_get_type": lambda source: self.constant("OBS_SOURCE_TYPE_INPUT"),
            "obs_source_get_output_flags": lambda source: self.constant("OBS_SOURCE_AUDIO")
            if source is self.audio_source else 0,
            "obs_source_get_volume": lambda source: self.volumes[source],
            "obs_source_set_volume": lambda source, volume: self.volumes.__setitem__(source, volume),
        }
        for kind, default in data_types.items():
            fakes[f"obs_data_get_{kind}"] = lambda data, key, d=default: data.get(key, d)
            fakes[f"obs_data_set_{kind}"] = lambda data, key, value: data.values.__setitem__(key, value)
            fakes[f"obs_data_set_default_{kind}"] = lambda data, key, value: data.defaults.__setitem__(key, value)
        return fakes



# Per-script settings (on top of each script's own defaults) that switch on
# its interesting code paths.
SETTINGS = {
    "clip_reexport.py": {"split_tracks_enabled": True, "reexport_enabled": True, "target_mb": 10.0, "crop_seconds": 30},
    "replay_buffer_save_sound.py": {"sound_enabled": True},
    "stuck_output_check.py": {"debug_enabled": True, "peak_guard_enabled": True},
}


def _load_script(path, fake, monkeypatch):
    monkeypatch.setitem(sys.modules, "obspython", fake)
    name = "obs_script_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # What OBS adds to each script's own module after importing it.
    module.script_path = lambda: os.path.dirname(path) + os.sep
    return module


def test_every_script_has_settings_here():
    """A new script gets its interesting paths switched on in SETTINGS."""
    assert sorted(SETTINGS) == [os.path.basename(p) for p in SCRIPTS]


def test_api_snapshot_is_from_a_real_obs_build():
    assert "script_path" not in API["names"]  # OBS adds it to each script's module, not obspython
    for name in ("script_log", "timer_add", "obs_frontend_add_event_callback", "OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED"):
        assert name in API["names"]


@pytest.mark.parametrize("path", SCRIPTS, ids=os.path.basename)
def test_script_runs_its_whole_lifecycle_against_the_real_api(path, tmp_path, monkeypatch):
    fake = FakeObspython(tmp_path)
    script = _load_script(path, fake, monkeypatch)
    name = os.path.basename(path)

    # Keep side effects in tmp_path: no real sound or ffmpeg, no writes into App/logs.
    if name == "replay_buffer_save_sound.py":
        wav = tmp_path / "clip.wav"
        wav.write_bytes(b"RIFF")
        SETTINGS[name]["audio_file"] = str(wav)
        played = []
        monkeypatch.setattr(script.subprocess, "Popen", lambda cmd, **kw: played.append(cmd))
        monkeypatch.setitem(sys.modules, "winsound", types.SimpleNamespace(
            PlaySound=lambda *a: played.append(a), SND_FILENAME=1, SND_ASYNC=2))
    if name == "stuck_output_check.py":
        libobs = FakeLibobs()
        monkeypatch.setattr(script, "load_libobs", lambda: libobs)
    if name == "clip_reexport.py":
        clip = tmp_path / "Replay 2026-09-25.mkv"
        clip.write_bytes(b"clip")
        fake.last_replay = str(clip)
        started = []
        monkeypatch.setattr(script, "process_clip", lambda *args: started.append(args))
        monkeypatch.setattr(script, "find_ffmpeg", lambda scripts_dir: str(tmp_path / "ffmpeg"))

    settings = Data()
    assert isinstance(script.script_description(), str)
    if hasattr(script, "script_defaults"):
        script.script_defaults(settings)
    settings.values.update(SETTINGS.get(name, {}))
    if hasattr(script, "script_properties"):
        script.script_properties()
    if hasattr(script, "script_load"):
        script.script_load(settings)
    if hasattr(script, "script_update"):
        script.script_update(settings)

    # Every frontend event OBS can send, then several rounds of timers.
    events = [n for n in API["names"] if n.startswith("OBS_FRONTEND_EVENT_")]
    for callback in list(fake.event_callbacks):
        for event in events:
            callback(fake.constant(event))
    if name == "stuck_output_check.py":
        libobs.send_peak_db(3.0)  # the mic clips at +3 dB
    for _ in range(12):
        for timer in list(fake.timers):
            timer()

    if hasattr(script, "script_unload"):
        script.script_unload()

    assert fake.missing == [], f"{name} uses names obspython doesn't have: {fake.missing}"
    errors = [line for line in fake.logs if "rror" in line or "Traceback" in line]
    assert errors == [], f"{name} logged errors: {errors}"

    if name == "replay_buffer_save_sound.py":
        assert played, "the clip sound should have played on REPLAY_BUFFER_SAVED"
    if name == "clip_reexport.py":
        assert len(started) == 1 and started[0][1] == str(clip)
        assert started[0][2]["split_tracks"] and started[0][2]["reexport"]
        assert started[0][2]["app_labels"] == ["", "", ""]  # no app slot sources in this OBS
    if name == "stuck_output_check.py":
        assert not fake.timers, "script_unload should stop its timers"
        assert abs(20 * math.log10(fake.volumes[fake.audio_source]) - -4.5) < 1e-6, "a +3 dB peak should land at -1.5 dB"
        assert libobs.meters and not libobs.live_meters(), "script_unload should destroy its volume meters"
        assert fake.buttons_clicked == [(fake.capture_button, fake.capture_source)], \
            "the tray's restart request should press the capture's Select Monitor button"
        assert libobs.ui_tasks == 1, "...on OBS's UI thread, once"
        assert "clipem_reload_request" not in fake.capture_settings.values
