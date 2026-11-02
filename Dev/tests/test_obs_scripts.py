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
# Modules the scripts import, not scripts OBS loads.
HELPER_MODULES = {"clip_tracks.py", "clip_naming.py", "clip_post.py"}
SCRIPTS = sorted(p for p in glob.glob(os.path.join(SCRIPTS_DIR, "*.py")) if os.path.basename(p) not in HELPER_MODULES)


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
        self.hotkeys = {}  # name -> callback
        self.replay_saves = 0
        self.replay_active = True
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
            "obs_hotkey_register_frontend": lambda name, description, callback: self.hotkeys.__setitem__(
                name, callback) or name,
            "obs_frontend_replay_buffer_save": lambda: setattr(self, "replay_saves", self.replay_saves + 1),
            "obs_frontend_replay_buffer_active": lambda: self.replay_active,
            "config_get_int": lambda config, section, key: 70 if (section, key) == ("AdvOut", "RecRBTime") else 0,
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
    "post_clip.py": {},
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
    if name == "post_clip.py":
        import clip_post

        state_path = tmp_path / "post_clip_state.json"
        monkeypatch.setattr(clip_post, "STATE_PATH", str(state_path))
        monkeypatch.setattr(clip_post, "load_settings",
                            lambda path=None: dict(clip_post.DEFAULTS, enabled=True, seconds=10))
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
    if name == "post_clip.py":
        fake.hotkeys[script.HOTKEY_NAME](True)  # the clip hotkey
        fake.hotkeys[script.HOTKEY_NAME](False)
        assert fake.replay_saves == 0, "a press waits for the post-clip seconds"
        assert list(fake.timers.values()) == [10_000]
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
    if name == "post_clip.py":
        assert fake.replay_saves == 1, "...then saves once"
        state = json.loads(state_path.read_text())
        assert state["clip_seconds"] == 60  # the buffer (70 s) less the post-clip seconds
        assert 0 <= state["requested_at"] - state["pressed_at"] < 5
    if name == "clip_reexport.py":
        assert len(started) == 1 and os.path.basename(started[0][1]).startswith("Replay-") and not clip.exists()
        assert started[0][2]["split_tracks"] and started[0][2]["reexport"]
        assert started[0][2]["app_labels"] == []  # OBS records Mix, Mic and Desktop only
    if name == "stuck_output_check.py":
        assert not fake.timers, "script_unload should stop its timers"
        assert abs(20 * math.log10(fake.volumes[fake.audio_source]) - -4.5) < 1e-6, "a +3 dB peak should land at -1.5 dB"
        assert libobs.meters and not libobs.live_meters(), "script_unload should destroy its volume meters"
        assert fake.buttons_clicked == [(fake.capture_button, fake.capture_source)], \
            "the tray's restart request should press the capture's Select Monitor button"
        assert libobs.ui_tasks == 1, "...on OBS's UI thread, once"
        assert "clipem_reload_request" not in fake.capture_settings.values


def _post_clip_script(tmp_path, monkeypatch):
    import clip_post

    fake = FakeObspython(tmp_path)
    script = _load_script(os.path.join(SCRIPTS_DIR, "post_clip.py"), fake, monkeypatch)
    state_path = tmp_path / "post_clip_state.json"
    monkeypatch.setattr(clip_post, "STATE_PATH", str(state_path))
    monkeypatch.setattr(clip_post, "load_settings", lambda path=None: dict(clip_post.DEFAULTS, enabled=True, seconds=10))
    played = []
    monkeypatch.setattr(script, "clip_sound", lambda: types.SimpleNamespace(
        sound_enabled=True, audio_file="clip.wav", audio_volume=0.5, play_sound=lambda *a: played.append(a)))
    return fake, script, state_path, played


def test_post_clip_press_without_a_replay_buffer_does_nothing(tmp_path, monkeypatch):
    """No sound (it would say a clip was saved) and no state for a later save to pick up."""
    fake, script, state_path, played = _post_clip_script(tmp_path, monkeypatch)
    fake.replay_active = False

    script.on_hotkey(True)

    assert played == [] and script.pending is None and not fake.timers
    assert fake.replay_saves == 0 and not state_path.exists()
    assert any("isn't running" in line for line in fake.logs)


def test_post_clip_gives_up_if_the_buffer_stops_during_the_wait(tmp_path, monkeypatch):
    fake, script, state_path, played = _post_clip_script(tmp_path, monkeypatch)

    script.on_hotkey(True)
    assert played and script.pending is not None
    fake.replay_active = False
    script.save_now()

    assert script.pending is None and not fake.timers
    assert fake.replay_saves == 0 and not state_path.exists()
    assert any("stopped before" in line for line in fake.logs)


def test_clip_sound_volume_scaling_matches_obs_config(tmp_path, monkeypatch):
    """replay_buffer_save_sound.py can't import obs_config inside OBS, so it
    carries its own copy of the Windows volume scaling."""
    import inspect

    import obs_config

    script = _load_script(os.path.join(SCRIPTS_DIR, "replay_buffer_save_sound.py"), FakeObspython(tmp_path), monkeypatch)
    for name in ("wav_at_volume", "play_wav_windows"):
        assert inspect.getsource(getattr(script, name)) == inspect.getsource(getattr(obs_config, name))


def test_a_second_press_saves_straight_away(tmp_path, monkeypatch):
    fake, script, state_path, played = _post_clip_script(tmp_path, monkeypatch)
    script.on_hotkey(True)
    script.on_hotkey(True)
    assert fake.replay_saves == 1 and script.pending is None and not fake.timers
    assert len(played) == 1 and state_path.exists()


def test_releasing_the_hotkey_does_nothing(tmp_path, monkeypatch):
    fake, script, _state_path, played = _post_clip_script(tmp_path, monkeypatch)
    script.on_hotkey(False)
    assert script.pending is None and not played and fake.replay_saves == 0


def test_with_post_clip_off_a_press_just_saves(tmp_path, monkeypatch):
    import clip_post

    fake, script, state_path, played = _post_clip_script(tmp_path, monkeypatch)
    monkeypatch.setattr(clip_post, "load_settings", lambda path=None: dict(clip_post.DEFAULTS, enabled=False))
    script.on_hotkey(True)
    assert fake.replay_saves == 1 and not fake.timers and not played and not state_path.exists()


def test_a_timer_with_nothing_pending_saves_nothing(tmp_path, monkeypatch):
    fake, script, state_path, _played = _post_clip_script(tmp_path, monkeypatch)
    script.save_now()
    assert fake.replay_saves == 0 and not state_path.exists()


def test_the_clip_is_never_shorter_than_a_second(tmp_path, monkeypatch):
    import clip_post

    _fake, script, _state_path, _played = _post_clip_script(tmp_path, monkeypatch)
    monkeypatch.setattr(clip_post, "load_settings", lambda path=None: dict(clip_post.DEFAULTS, enabled=True, seconds=120))
    script.on_hotkey(True)
    assert script.pending["clip_seconds"] == 1


@pytest.mark.parametrize("sound", [
    None,
    types.SimpleNamespace(sound_enabled=False, audio_file="clip.wav"),
    types.SimpleNamespace(sound_enabled=True, audio_file=""),
])
def test_no_clip_sound_means_none_to_take_out_later(tmp_path, monkeypatch, sound):
    _fake, script, _state_path, _played = _post_clip_script(tmp_path, monkeypatch)
    monkeypatch.setattr(script, "clip_sound", lambda: sound)
    script.on_hotkey(True)
    assert script.pending["sound"] is None


def test_the_sound_played_is_noted_for_the_save(tmp_path, monkeypatch):
    _fake, script, state_path, played = _post_clip_script(tmp_path, monkeypatch)
    script.on_hotkey(True)
    script.save_now()
    assert played == [("clip.wav", 0.5)]
    assert json.loads(state_path.read_text())["sound"] == "clip.wav"


def test_a_press_that_cant_be_noted_is_still_saved_as_a_plain_clip(tmp_path, monkeypatch):
    import clip_post

    fake, script, state_path, _played = _post_clip_script(tmp_path, monkeypatch)
    state_path.write_text("an older press")

    def refuse(_state):
        raise OSError("disk full")

    monkeypatch.setattr(clip_post, "write_state", refuse)
    script.on_hotkey(True)
    script.save_now()
    assert fake.replay_saves == 1 and not state_path.exists()
    assert any("couldn't note the press" in line for line in fake.logs)


def test_unloading_cancels_a_pending_save(tmp_path, monkeypatch):
    fake, script, _state_path, _played = _post_clip_script(tmp_path, monkeypatch)
    script.on_hotkey(True)
    script.script_unload()
    assert not fake.timers


def test_the_hotkeys_keys_are_loaded_and_saved(tmp_path, monkeypatch):
    fake, script, _state_path, _played = _post_clip_script(tmp_path, monkeypatch)
    loaded, saved = [], []
    fake._impl["obs_hotkey_load"] = lambda hotkey, keys: loaded.append(hotkey)
    fake._impl["obs_hotkey_save"] = lambda hotkey: "the keys"
    fake._impl["obs_data_set_array"] = lambda data, key, value: saved.append((key, value))
    script.script_save(Data())  # before script_load: nothing to save
    assert saved == []
    script.script_load(Data())
    assert loaded == [script.HOTKEY_NAME]
    script.script_save(Data())
    assert saved == [(script.HOTKEY_SETTING, "the keys")]


def test_a_missing_clip_sound_script_is_no_sound(tmp_path, monkeypatch):
    fake = FakeObspython(tmp_path)
    script = _load_script(os.path.join(SCRIPTS_DIR, "post_clip.py"), fake, monkeypatch)
    monkeypatch.setitem(sys.modules, "replay_buffer_save_sound", None)  # importing it fails
    assert script.clip_sound() is None
    assert any("no clip sound" in line for line in fake.logs)
    module = types.ModuleType("replay_buffer_save_sound")
    monkeypatch.setitem(sys.modules, "replay_buffer_save_sound", module)
    assert script.clip_sound() is module


class ReexportScript:
    """clip_reexport.py with a clip just saved; `runs`: what it started, in
    its background thread (run here, at once)."""

    def __init__(self, tmp_path, monkeypatch, **settings):
        import clip_naming
        import clip_post

        self.fake = FakeObspython(tmp_path)
        self.script = _load_script(os.path.join(SCRIPTS_DIR, "clip_reexport.py"), self.fake, monkeypatch)
        self.clip = tmp_path / "Replay 2026-09-25 10-00-00.mkv"
        self.clip.write_bytes(b"clip")
        self.fake.last_replay = str(self.clip)
        self.script.script_update(Data({"target_mb": 10.0, "crop_seconds": 30, **settings}))
        self.state = None
        self.runs = []
        monkeypatch.setattr(clip_naming, "load", lambda path=None: dict(clip_naming.DEFAULTS))
        monkeypatch.setattr(clip_post, "read_state", lambda path=None, now=None: self.state)
        monkeypatch.setattr(self.script, "find_ffmpeg", lambda scripts_dir: "ffmpeg")
        monkeypatch.setattr(self.script, "process_clip", lambda *args: self.runs.append(("process", args)))
        monkeypatch.setattr(self.script, "finish_post_save", lambda *args: self.runs.append(("post", args)))
        monkeypatch.setattr(self.script.threading, "Thread", lambda target, args, daemon: types.SimpleNamespace(
            start=lambda: target(*args)))

    def saved(self):
        self.script.on_event(self.fake.constant("OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED"))


def test_other_frontend_events_leave_the_clip_alone(tmp_path, monkeypatch):
    obs = ReexportScript(tmp_path, monkeypatch, split_tracks_enabled=True)
    obs.script.on_event(obs.fake.constant("OBS_FRONTEND_EVENT_RECORDING_STARTED"))
    assert obs.runs == [] and obs.clip.exists()


def test_a_clip_the_tray_finishes_is_left_to_the_tray(tmp_path, monkeypatch):
    """Two writers would race on it - naming it included."""
    obs = ReexportScript(tmp_path, monkeypatch, tray_tracks_enabled=True, split_tracks_enabled=True)
    obs.saved()
    assert obs.runs == [] and obs.clip.exists()


@pytest.mark.parametrize("replay", [None, "", "gone.mkv"])
def test_a_saved_clip_that_isnt_there_is_skipped(tmp_path, monkeypatch, replay):
    obs = ReexportScript(tmp_path, monkeypatch, split_tracks_enabled=True)
    obs.fake.last_replay = replay and str(tmp_path / replay)
    obs.saved()
    assert obs.runs == [] and any("not found" in line for line in obs.fake.logs)


def test_with_nothing_to_do_a_clip_is_still_named(tmp_path, monkeypatch):
    obs = ReexportScript(tmp_path, monkeypatch)
    obs.script.find_ffmpeg = lambda scripts_dir: pytest.fail("no ffmpeg needed")
    obs.saved()
    assert obs.runs == [] and not obs.clip.exists()
    assert [name for name in os.listdir(tmp_path) if name.endswith(".mkv")][0].startswith("Replay-")


@pytest.mark.parametrize("settings, split, reexport", [
    ({"split_tracks_enabled": True}, True, False),
    ({"reexport_enabled": True}, False, True),
])
def test_each_setting_alone_starts_its_step(tmp_path, monkeypatch, settings, split, reexport):
    obs = ReexportScript(tmp_path, monkeypatch, **settings)
    obs.saved()
    (kind, (ffmpeg, path, options, log)), = obs.runs
    assert kind == "process" and ffmpeg == "ffmpeg" and os.path.isfile(path) and path != str(obs.clip)
    assert options["split_tracks"] is split and options["reexport"] is reexport
    assert options["target_mb"] == 10.0 and options["crop_seconds"] == 30


def test_no_ffmpeg_still_leaves_the_clip_named(tmp_path, monkeypatch):
    obs = ReexportScript(tmp_path, monkeypatch, reexport_enabled=True)
    monkeypatch.setattr(obs.script, "find_ffmpeg", lambda scripts_dir: None)
    obs.saved()
    assert obs.runs == [] and not obs.clip.exists()
    assert any("no ffmpeg found" in line for line in obs.fake.logs)


def test_a_post_clip_save_goes_its_own_way_unrenamed(tmp_path, monkeypatch):
    """finish_post_save names it after the press, not the save."""
    obs = ReexportScript(tmp_path, monkeypatch, split_tracks_enabled=True, reexport_enabled=True)
    obs.state = {"pressed_at": 1.0, "requested_at": 2.0, "clip_seconds": 3}
    obs.saved()
    (kind, (ffmpeg, path, state, options)), = obs.runs
    assert kind == "post" and path == str(obs.clip) and obs.clip.exists() and state is obs.state
    assert options["reexport"] is True and options["target_mb"] == 10.0 and options["crop_seconds"] == 30
    assert "split_tracks" not in options  # the extra file is the multitrack one


def test_a_post_clip_save_without_ffmpeg_keeps_its_seconds(tmp_path, monkeypatch):
    obs = ReexportScript(tmp_path, monkeypatch)
    obs.state = {"pressed_at": 1.0, "requested_at": 2.0, "clip_seconds": 3}
    monkeypatch.setattr(obs.script, "find_ffmpeg", lambda scripts_dir: None)
    obs.saved()
    assert obs.runs == [] and obs.clip.exists()
    assert any("keeps its post-clip seconds" in line for line in obs.fake.logs)


def test_a_failed_post_clip_save_is_logged_not_raised(tmp_path, monkeypatch):
    import clip_post

    fake = FakeObspython(tmp_path)
    script = _load_script(os.path.join(SCRIPTS_DIR, "clip_reexport.py"), fake, monkeypatch)

    def fail(*_args):
        raise ValueError("couldn't read it")

    monkeypatch.setattr(clip_post, "finish_post_save", fail)
    script.finish_post_save("ffmpeg", "c.mkv", {}, {})
    assert any("failed: couldn't read it" in line for line in fake.logs)
    monkeypatch.setattr(clip_post, "finish_post_save", lambda *args: fake.logs.append(args[3]))
    script.finish_post_save("ffmpeg", "c.mkv", {}, {"naming": None})
    assert fake.logs[-1] == {"naming": None}


def test_the_scripts_defaults_are_all_off_at_20_mb(tmp_path, monkeypatch):
    fake = FakeObspython(tmp_path)
    script = _load_script(os.path.join(SCRIPTS_DIR, "clip_reexport.py"), fake, monkeypatch)
    settings = Data()
    script.script_defaults(settings)
    assert settings.defaults == {"split_tracks_enabled": False, "tray_tracks_enabled": False,
                                 "reexport_enabled": False, "target_mb": 20.0, "crop_seconds": 30}


def test_unloading_stops_listening(tmp_path, monkeypatch):
    fake = FakeObspython(tmp_path)
    script = _load_script(os.path.join(SCRIPTS_DIR, "clip_reexport.py"), fake, monkeypatch)
    script.script_load(Data())
    assert fake.event_callbacks == [script.on_event]
    script.script_unload()
    assert fake.event_callbacks == []


def test_the_scripts_properties_are_what_obs_shows(tmp_path, monkeypatch):
    fake = FakeObspython(tmp_path)
    script = _load_script(os.path.join(SCRIPTS_DIR, "clip_reexport.py"), fake, monkeypatch)
    added = []
    for kind in ("bool", "float", "int"):
        fake._impl[f"obs_properties_add_{kind}"] = lambda props, name, *rest: added.append((name, *rest[1:]))
    props = script.script_properties()
    assert isinstance(props, Handle) and [item[0] for item in added] == [
        "split_tracks_enabled", "reexport_enabled", "target_mb", "crop_seconds"]
    assert added[3] == ("crop_seconds", 1, 3600, 1)
