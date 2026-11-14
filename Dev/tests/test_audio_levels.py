"""tray-widget/audio_levels.py: the one-off level checks the missing-audio
detection makes once OBS's Mic or Desktop audio has gone silent."""

import array
import sys
import types

from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import app_audio  # noqa: E402
import audio_levels  # noqa: E402


def pcm(*samples):
    return array.array("h", samples).tobytes()


def test_pcm_peak():
    assert audio_levels.pcm_peak(b"") == 0.0
    assert audio_levels.pcm_peak(pcm(0, 0, 0)) == 0.0
    assert audio_levels.pcm_peak(pcm(10, -16384, 5)) == 0.5
    assert audio_levels.pcm_peak(pcm(-32768)) == 1.0
    assert audio_levels.pcm_peak(pcm(100) + b"\x01") == 100 / 32768  # a stray odd byte is ignored


class FakeProc:
    def __init__(self, data):
        self.stdout = types.SimpleNamespace(read=lambda n: data[:n])
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_parec_peak_measures_what_parec_records():
    started = []

    def popen(cmd, **kwargs):
        started.append(cmd)
        started.append(proc := FakeProc(pcm(0, 3277, -100)))
        return proc

    assert audio_levels.parec_peak(["-d", "mic"], seconds=0.01, popen=popen) == 3277 / 32768
    cmd, proc = started
    assert cmd[0] == "parec" and cmd[-2:] == ["-d", "mic"] and "--raw" in cmd
    assert proc.killed


def test_parec_recording_nothing_is_unknown_not_silence():
    assert audio_levels.parec_peak([], seconds=0.01, popen=lambda cmd, **kw: FakeProc(b"")) is None


def test_no_parec_is_unknown():
    def popen(cmd, **kwargs):
        raise FileNotFoundError("parec")

    assert audio_levels.parec_peak([], popen=popen) is None


def test_default_devices_are_what_the_default_device_names_mean():
    probes = []
    audio_levels._linux_device_peak("default", "input", probe=lambda args: probes.append(args) or 0.0)
    audio_levels._linux_device_peak("default", "output", probe=lambda args: probes.append(args) or 0.0)
    audio_levels._linux_device_peak("alsa_input.usb", "input", probe=lambda args: probes.append(args) or 0.0)
    assert probes == [["-d", "@DEFAULT_SOURCE@"], ["-d", "@DEFAULT_MONITOR@"], ["-d", "alsa_input.usb"]]


def stream(label, index, monitor, corked=False):
    return app_audio.Stream(label.lower(), label.lower(), label, ("pa", index, monitor), corked)


def test_only_apps_making_sound_where_desktop_audio_listens_are_heard():
    levels = {1: 0.2, 2: 0.0, 3: 0.3, 4: 0.5, 5: 0.4}
    streams = [
        stream("Deadlock", 1, "speakers.monitor"),
        stream("Discord", 2, "speakers.monitor"),  # a stream open, but silent
        stream("Spotify", 3, "speakers.monitor", corked=True),  # paused
        stream("Firefox", 4, "headset.monitor"),  # another output Desktop audio doesn't record
        stream("Game", 5, "hdmi.monitor"),  # the output Desktop audio is set to
    ]
    probed = []

    def probe(args):
        index = int(args[0].split("=")[1])
        probed.append(index)
        return levels[index]

    heard = audio_levels._linux_heard_apps("hdmi.monitor", lambda: streams, probe=probe,
                                           default_monitor=lambda: "speakers.monitor")
    assert heard == ["Deadlock", "Game"]
    assert probed == [1, 2, 5]


def test_only_a_few_apps_are_measured():
    streams = [stream(f"App{n}", n, "speakers.monitor") for n in range(10)]
    probed = []
    audio_levels._linux_heard_apps("default", lambda: streams, probe=lambda args: probed.append(args) or 0.0,
                                   default_monitor=lambda: "speakers.monitor")
    assert len(probed) == audio_levels.MAX_PROBED_APPS


def test_the_default_monitor_comes_from_pactl():
    def run(cmd, **kwargs):
        if cmd[1] == "get-default-sink":
            return types.SimpleNamespace(returncode=1, stdout="")
        return types.SimpleNamespace(returncode=0, stdout="Server Name: PipeWire\nDefault Sink: alsa_output.pci\n")

    assert audio_levels.linux_default_monitor(run=run) == "alsa_output.pci.monitor"
    assert audio_levels.linux_default_monitor(
        run=lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout="hdmi\n")) == "hdmi.monitor"


def test_where_it_cant_be_told_its_unknown():
    assert audio_levels.device_peak("default", "input", system="Darwin") is None
    assert audio_levels.heard_apps("default", system="Darwin") == []


def test_a_failing_probe_is_unknown_not_a_crash(monkeypatch):
    def fail(*args):
        raise RuntimeError("COM went away")

    monkeypatch.setattr(audio_levels, "_windows_device_peak", fail)
    monkeypatch.setattr(audio_levels, "_windows_heard_apps", fail)
    assert audio_levels.device_peak("default", "input", system="Windows") is None
    assert audio_levels.heard_apps("default", system="Windows") == []
