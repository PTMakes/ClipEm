"""Installer/mic_filter.py's recording side: which device is opened, what
parecord is asked for, that it's always stopped, and the levels read back."""

import array
import io
import math
import subprocess
import sys
import types

import mic_filter
import pytest


class FakeParecord:
    """A parecord whose output is `samples` (int16), recording what it's asked."""

    started = []

    def __init__(self, samples, stuck=False):
        self.data = array.array("h", samples).tobytes()
        self.stuck = stuck

    def __call__(self, args, stdout):
        assert stdout is subprocess.PIPE
        FakeParecord.started.append(self)
        self.args = args
        self.stdout = io.BytesIO(self.data)
        self.terminated = self.killed = False
        self.waits = 0
        return self

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits += 1
        if self.stuck and not self.killed:
            raise subprocess.TimeoutExpired("parecord", timeout)
        return 0


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(mic_filter.time, "sleep", lambda _s: None)
    FakeParecord.started = []


def _record(monkeypatch, samples, device_id="alsa_input.usb-mic", stuck=False, **kwargs):
    fake = FakeParecord(samples, stuck)
    monkeypatch.setattr(mic_filter.subprocess, "Popen", fake)
    level = mic_filter._measure_audio_level_db_linux(device_id, 0.1, samplerate=1000, **kwargs)
    return level, fake


def test_parecord_records_the_chosen_source_and_is_stopped(monkeypatch):
    level, fake = _record(monkeypatch, [16384] * 100)
    assert fake.args[fake.args.index("--device") + 1] == "alsa_input.usb-mic"
    assert "--format=s16le" in fake.args and "--channels=1" in fake.args
    assert fake.stdout.closed and fake.terminated and not fake.killed
    assert level == pytest.approx(20 * math.log10(0.5), abs=0.01)


def test_the_default_source_isnt_named(monkeypatch):
    for device_id in ("default", "", None):
        _level, fake = _record(monkeypatch, [0] * 10, device_id=device_id)
        assert "--device" not in fake.args


def test_a_parecord_that_wont_stop_is_killed(monkeypatch):
    _level, fake = _record(monkeypatch, [100] * 100, stuck=True)
    assert fake.terminated and fake.killed and fake.waits == 2


def test_no_audio_at_all_is_silence(monkeypatch):
    assert _record(monkeypatch, [])[0] == -96.0
    assert _record(monkeypatch, [], noise_floor_db=-60.0)[0] == -96.0


def test_an_odd_trailing_byte_is_ignored(monkeypatch):
    fake = FakeParecord([16384] * 10)
    fake.data += b"\x01"
    monkeypatch.setattr(mic_filter.subprocess, "Popen", fake)
    assert mic_filter._measure_audio_level_db_linux("x", 0.1, samplerate=1000) == pytest.approx(-6.02, abs=0.01)


def test_speech_is_measured_only_where_someone_spoke(monkeypatch):
    # 30 ms frames at 1 kHz: 30 samples each. Half the frames loud, half silent.
    loud, quiet = [8192] * 30, [0] * 30
    level, _fake = _record(monkeypatch, (loud + quiet) * 5, noise_floor_db=-60.0)
    assert level == pytest.approx(20 * math.log10(0.25), abs=0.01)  # the silence doesn't pull it down
    whole, _fake = _record(monkeypatch, (loud + quiet) * 5)
    assert whole < level - 2


# -- sounddevice (Windows/macOS) -------------------------------------------

def _fake_sounddevice(monkeypatch, devices):
    module = types.SimpleNamespace(query_devices=lambda index=None: devices if index is None else devices[index])
    monkeypatch.setitem(sys.modules, "sounddevice", module)


DEVICES = [
    {"name": "Speakers (Realtek)", "max_input_channels": 0},
    {"name": "Microphone (USB Audio Device) ", "max_input_channels": 1},
    {"name": "Microphone Array (Realtek)", "max_input_channels": 2},
    {"name": "Line In", "max_input_channels": 2},
]


@pytest.mark.parametrize(("name", "index"), [
    ("Microphone (USB Audio Device)", 1),  # exact, despite the trailing space
    ("microphone array (realtek)", 2),  # case doesn't matter
    ("Microphone Array (Realtek High Definition Audio)", None),  # neither contains the other
    ("Line", 3),  # part of a name
    ("Speakers (Realtek)", None),  # an output only
    ("Default", None),
    ("", None),
    (None, None),
])
def test_a_mic_is_found_by_its_name(monkeypatch, name, index):
    _fake_sounddevice(monkeypatch, DEVICES)
    assert mic_filter._find_sounddevice_input_index(name) == index


def test_measuring_picks_the_platforms_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(mic_filter, "_measure_audio_level_db_linux", lambda *a, **k: calls.append(("linux", a)) or 1)
    monkeypatch.setattr(mic_filter, "_measure_audio_level_db_sounddevice",
                        lambda *a, **k: calls.append(("sd", a)) or 2)
    monkeypatch.setattr(mic_filter.platform, "system", lambda: "Linux")
    assert mic_filter.measure_audio_level_db("Mic", "pulse.mic", 5) == 1
    monkeypatch.setattr(mic_filter.platform, "system", lambda: "Windows")
    assert mic_filter.measure_audio_level_db("Mic", "pulse.mic", 5) == 2
    assert calls == [("linux", ("pulse.mic", 5)), ("sd", ("Mic", 5))]


def test_linux_needs_parecord(monkeypatch, capsys):
    monkeypatch.setattr(mic_filter.platform, "system", lambda: "Linux")
    monkeypatch.setattr(mic_filter.shutil, "which", lambda _name: None)
    assert mic_filter.ensure_mic_filter_deps() is False
    assert "parecord" in capsys.readouterr().err
    monkeypatch.setattr(mic_filter.shutil, "which", lambda _name: "/usr/bin/parecord")
    assert mic_filter.ensure_mic_filter_deps() is True
