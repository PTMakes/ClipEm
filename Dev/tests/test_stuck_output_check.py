"""stuck_output_check.py's audio peak guard, run outside OBS with a stubbed
obspython module. (Its stuck-screen check moved to the tray widget - see
test_stuck_screen.py.)"""

import ctypes
import math
import sys
import types

import pytest
from conftest import SCRIPTS_DIR

obs = sys.modules.setdefault("obspython", types.ModuleType("obspython"))  # only exists inside OBS
sys.path.insert(0, SCRIPTS_DIR)
import stuck_output_check as check  # noqa: E402


def test_scripts_find_their_own_folder_without_obs():
    """obspython has no script_path() - OBS injects it into each script's own
    module - so the scripts use __file__ instead."""
    import clip_reexport

    for module in (check, clip_reexport):
        assert module.SCRIPTS_DIR == SCRIPTS_DIR
        with open(module.__file__, encoding="utf-8") as f:
            assert "obs.script_path" not in f.read()


def _db(volume):
    return 20 * math.log10(volume)


@pytest.mark.parametrize("peak_db, volume_db, expected_db", [
    (0.0, 0.0, -1.0),    # at clip level: down to the ceiling
    (6.0, -2.0, -9.0),   # clipping hard: down by as much as it's over
    (-3.0, 0.0, 0.0),    # below the ceiling: never turned up
    (6.0, -18.0, -20.0),  # never lowered past -20 dB, where it'd go missing from clips
    (0.0, -25.0, -25.0),  # already below that floor: not turned up either
])
def test_reduced_volume_puts_the_peak_on_the_ceiling(peak_db, volume_db, expected_db):
    volume = 10 ** (volume_db / 20)
    assert _db(check.reduced_volume(volume, peak_db, -1.0)) == pytest.approx(expected_db)


class Volumes(dict):
    pass


@pytest.fixture
def peak_guard(monkeypatch, tmp_path):
    """One audio source at 100% volume; returns its volume dict (with
    .clock, the guard's monotonic time, to move on)."""
    volumes = Volumes({"mic": 1.0})
    monkeypatch.setattr(obs, "LOG_INFO", 0, raising=False)
    monkeypatch.setattr(obs, "script_log", lambda level, msg: None, raising=False)
    monkeypatch.setattr(obs, "obs_get_source_by_uuid", lambda uuid: uuid if uuid in volumes else None, raising=False)
    monkeypatch.setattr(obs, "obs_source_get_volume", volumes.get, raising=False)
    monkeypatch.setattr(obs, "obs_source_set_volume", volumes.__setitem__, raising=False)
    monkeypatch.setattr(obs, "obs_source_get_name", lambda s: s, raising=False)
    monkeypatch.setattr(obs, "obs_source_release", lambda s: None, raising=False)
    monkeypatch.setattr(check, "peak_ceiling_db", -1.0)
    monkeypatch.setattr(check, "peak_guard_ticks", 0)
    monkeypatch.setattr(check, "sync_peak_meters", lambda: None)
    monkeypatch.setattr(check, "loudest_peaks", {})
    monkeypatch.setattr(check, "peak_guard_logged", set())
    monkeypatch.setattr(check, "lowered", {})
    monkeypatch.setattr(check, "PEAK_GUARD_STATE_PATH", str(tmp_path / "cache" / "peak_guard.json"))
    clock = [1000.0]
    monkeypatch.setattr(check.time, "monotonic", lambda: clock[0])
    volumes.clock = clock
    return volumes


def test_peak_guard_only_acts_on_clipping(peak_guard):
    check.loudest_peaks["mic"] = -1.2  # loud but not clipping
    check.peak_guard_tick()
    assert peak_guard["mic"] == 1.0

    check.loudest_peaks["mic"] = 2.0
    check.peak_guard_tick()
    assert _db(peak_guard["mic"]) == pytest.approx(-3.0)
    assert check.loudest_peaks == {}  # each tick looks at fresh peaks only


def test_peak_guard_ignores_silence_and_removed_sources(peak_guard):
    check.loudest_peaks.update({"mic": float("nan"), "gone": 5.0})
    check.peak_guard_tick()
    assert peak_guard == {"mic": 1.0}


def test_audio_thread_keeps_nothing_below_clip_level(monkeypatch):
    """Normal audio leaves no level data behind - only clip-level peaks, one
    number per source, are held (see Privacy in the script)."""
    monkeypatch.setattr(check, "loudest_peaks", {})
    callback = check.make_peak_callback("mic")

    def send(*channel_dbs):
        levels = (ctypes.c_float * 8)(*channel_dbs, *([-math.inf] * (8 - len(channel_dbs))))
        callback(None, levels, levels, levels)

    for db in (-40.0, -12.0, -1.1):  # speech, loud speech, just under clipping
        send(db, db - 3)
    assert check.loudest_peaks == {}

    send(1.5, 0.2)
    send(0.5)
    assert check.loudest_peaks == {"mic": 1.5}


def test_peaks_just_under_full_scale_count_as_clipping(peak_guard):
    """OBS's meter shows these as clipping (see PEAK_CLIP_DB), so the guard
    must act on them too - or YouTube audio "clips" forever."""
    check.loudest_peaks["mic"] = -0.1
    check.peak_guard_tick()
    assert _db(peak_guard["mic"]) == pytest.approx(-0.9)  # the peak now sits at the -1 dB ceiling


def test_peak_guard_logs_once_per_source_without_levels(peak_guard, monkeypatch):
    logs = []
    monkeypatch.setattr(obs, "script_log", lambda level, msg: logs.append(msg), raising=False)
    for peak in (2.0, 4.5, 1.0):
        check.loudest_peaks["mic"] = peak
        check.peak_guard_tick()
    assert len(logs) == 1
    assert "dB" not in logs[0] and not any(ch.isdigit() for ch in logs[0])


def test_libobs_is_found_only_if_it_has_the_volume_meter(monkeypatch):
    class Library:
        def __init__(self, has_meter):
            self.obs_queue_task = types.SimpleNamespace()
            for name in ("obs_get_source_by_uuid", "obs_source_release", "obs_volmeter_add_callback",
                         "obs_volmeter_attach_source", "obs_volmeter_destroy"):
                setattr(self, name, types.SimpleNamespace())
            if has_meter:
                self.obs_volmeter_create = types.SimpleNamespace()

    monkeypatch.setattr(check, "_SYSTEM", "Linux")
    libraries = {None: Library(False), "libobs.so.0": Library(True)}

    def open_library(name):
        if name not in libraries:
            raise OSError(f"no {name}")
        return libraries[name]

    monkeypatch.setattr(check.ctypes, "CDLL", open_library)
    assert check.load_libobs() is libraries["libobs.so.0"]
    del libraries["libobs.so.0"]
    assert check.load_libobs() is None


def _clip(peak_guard, peak_db=2.0):
    check.loudest_peaks["mic"] = peak_db
    check.peak_guard_tick()


def _wait(peak_guard, seconds):
    peak_guard.clock[0] += seconds
    check.peak_guard_tick()


def test_a_lowered_source_goes_back_up_once_it_stops_clipping(peak_guard):
    _clip(peak_guard)  # down 3 dB
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS - 1)
    assert _db(peak_guard["mic"]) == pytest.approx(-3.0)  # not yet
    for expected_db in (-2.0, -1.0, 0.0):
        _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS)
        assert _db(peak_guard["mic"]) == pytest.approx(expected_db, abs=1e-6)
    assert check.lowered == {}
    _wait(peak_guard, 10 * check.PEAK_GUARD_RESTORE_QUIET_SECONDS)
    assert peak_guard["mic"] == pytest.approx(1.0)  # never past where it was


def test_clipping_again_right_after_a_step_up_slows_the_next(peak_guard):
    _clip(peak_guard)
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS)  # up to -2 dB
    peak_guard.clock[0] += 1
    _clip(peak_guard, 0.0)  # clips again at once
    assert check.lowered["mic"]["wait"] == 2 * check.PEAK_GUARD_RESTORE_QUIET_SECONDS
    assert check.lowered["mic"]["base"] == 1.0  # still heading for where the user had it
    lowered = peak_guard["mic"]
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS)
    assert peak_guard["mic"] == lowered
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS)
    assert peak_guard["mic"] > lowered


def test_a_volume_moved_by_hand_while_lowered_is_left_there(peak_guard):
    _clip(peak_guard)
    peak_guard["mic"] = 0.3  # the user's choice
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS * 5)
    assert peak_guard["mic"] == 0.3 and check.lowered == {}


def test_restoring_carries_on_after_obs_restarts(peak_guard):
    import json

    _clip(peak_guard)
    with open(check.PEAK_GUARD_STATE_PATH, encoding="utf-8") as f:
        assert json.load(f) == {"mic": {"base": 1.0, "reclips": 0}}
    check.lowered.clear()
    check.lowered.update(check.load_lowered(peak_guard.clock[0]))  # what start_peak_guard does
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS)
    assert _db(peak_guard["mic"]) == pytest.approx(-2.0)


def test_a_missing_or_broken_state_file_is_nothing_lowered(peak_guard, tmp_path):
    assert check.load_lowered(0) == {}
    path = tmp_path / "cache" / "peak_guard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("not json", "[1, 2]", '{"mic": "loud", "desk": -1, "cam": {"base": 1.0, "reclips": "x"}}'):
        path.write_text(text)
        assert check.load_lowered(0) == {}


def test_an_older_state_file_of_just_volumes_still_loads(peak_guard, tmp_path):
    path = tmp_path / "cache" / "peak_guard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"mic": 0.8}')
    assert check.load_lowered(0)["mic"]["base"] == 0.8
    assert check.load_lowered(0)["mic"]["reclips"] == 0


def test_a_source_that_keeps_clipping_when_turned_up_stays_down(peak_guard):
    _clip(peak_guard)  # down 3 dB
    for _ in range(check.PEAK_GUARD_SETTLE_AFTER):
        _wait(peak_guard, check.lowered["mic"]["wait"])  # a step up...
        peak_guard.clock[0] += 1
        _clip(peak_guard, 0.0)  # ...and it clips again at once
    assert check.lowered == {}
    settled = peak_guard["mic"]
    assert settled < 1.0
    _wait(peak_guard, 100 * check.PEAK_GUARD_RESTORE_MAX_WAIT_SECONDS)
    assert peak_guard["mic"] == settled  # stays at the level that stopped the clipping
    _clip(peak_guard)  # still guarded - and it goes back up to the settled level, no higher
    assert peak_guard["mic"] < settled
    for _ in range(10):
        _wait(peak_guard, check.PEAK_GUARD_RESTORE_MAX_WAIT_SECONDS)
    assert peak_guard["mic"] == pytest.approx(settled)


def test_settling_survives_an_obs_restart(peak_guard):
    _clip(peak_guard)
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS)
    peak_guard.clock[0] += 1
    _clip(peak_guard, 0.0)  # one re-clip, then OBS restarts
    check.lowered.clear()
    check.lowered.update(check.load_lowered(peak_guard.clock[0]))
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS)  # first look since the restart
    _wait(peak_guard, check.PEAK_GUARD_RESTORE_QUIET_SECONDS)  # a step up
    peak_guard.clock[0] += 1
    _clip(peak_guard, 0.0)
    assert check.lowered == {}
