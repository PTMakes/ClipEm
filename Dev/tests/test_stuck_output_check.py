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
])
def test_reduced_volume_puts_the_peak_on_the_ceiling(peak_db, volume_db, expected_db):
    volume = 10 ** (volume_db / 20)
    assert _db(check.reduced_volume(volume, peak_db, -1.0)) == pytest.approx(expected_db)


@pytest.fixture
def peak_guard(monkeypatch):
    """One audio source at 100% volume; returns its volume dict."""
    volumes = {"mic": 1.0}
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
