"""Recording app tracks in memory (tray-widget/app_recorder.py): the rings,
placing each helper's sound in time, and starting/stopping helpers."""

import io
import random
import sys
import types
from array import array

import pytest
from conftest import TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))
sys.path.insert(0, TRAY_WIDGET_DIR)

import app_recorder  # noqa: E402
from app_recorder import BYTES_PER_FRAME, RATE, StreamPlacer, TrackRing  # noqa: E402


def pcm(values):
    """Stereo PCM of the same value on both sides, for each in `values`."""
    samples = array("h")
    for value in values:
        samples.extend((value, value))
    return samples.tobytes()


def values(data):
    samples = array("h")
    samples.frombytes(data)
    return list(samples[0::2])


def test_ring_reads_back_what_was_added_and_silence_elsewhere():
    ring = TrackRing(1)
    ring.add(100, pcm([1, 2, 3]))
    assert values(ring.read(98, 105)) == [0, 0, 1, 2, 3, 0, 0]


def test_ring_sums_streams_added_at_the_same_time_and_clips():
    ring = TrackRing(1)
    ring.add(10, pcm([100, 32000, -32000]))
    ring.add(10, pcm([50, 32000, -32000]))
    assert values(ring.read(10, 13)) == [150, 32767, -32768]


def test_ring_forgets_what_is_older_than_its_length_and_wraps():
    ring = TrackRing(1)  # RATE frames
    ring.add(5, pcm([7]))
    ring.add(RATE + 20, pcm([9, 9]))  # the ring has moved on: frame 5 is gone
    assert values(ring.read(5, 6)) == [0]
    assert values(ring.read(RATE + 20, RATE + 22)) == [9, 9]
    ring.add(RATE + 21, pcm([1]))  # wraps over the slot frame 21 had
    assert values(ring.read(RATE + 20, RATE + 22)) == [9, 10]


def test_ring_drops_sound_older_than_it_holds_and_advances_over_gaps():
    ring = TrackRing(1)
    ring.add(3 * RATE, pcm([1]))
    ring.add(10, pcm([5, 5, 5]))  # far too old
    assert values(ring.read(10, 13)) == [0, 0, 0]
    ring.advance(5 * RATE)  # time passes with no sound
    assert values(ring.read(3 * RATE, 3 * RATE + 1)) == [0]


def test_mix_pcm_matches_without_audioop(monkeypatch):
    a, b = pcm([1, 32000, -32000, 5]), pcm([2, 32000, -32000, -9])
    expected = values(app_recorder.mix_pcm(a, b))
    monkeypatch.setattr(app_recorder, "audioop", None)
    assert values(app_recorder.mix_pcm(a, b)) == expected == [3, 32767, -32768, -4]


def test_placer_puts_steady_chunks_back_to_back_despite_jitter():
    placer = StreamPlacer()
    positions = []
    for i, jitter in enumerate([0, 300, 50, 0, 500, 20]):  # frames each chunk was read after it ended
        positions.append(placer.place(10_000 + (i + 1) * 960 + jitter, 960))
    assert positions[0] == 10_000
    assert all(abs(p - (10_000 + i * 960)) < 20 for i, p in enumerate(positions))


def test_placer_follows_a_pause_in_the_sound():
    placer = StreamPlacer()
    for i in range(10):
        placer.place((i + 1) * 960, 960)
    # The app paused for 2 s; the sound that follows belongs after the gap.
    resumed = [placer.place((11 + i) * 960 + 2 * RATE, 960) for i in range(8)]
    assert all(abs(p - (10 * 960 + 2 * RATE + i * 960)) <= 960 for i, p in enumerate(resumed))
    assert abs(resumed[-1] - (10 * 960 + 2 * RATE + 7 * 960)) < 20


def test_capture_command_for_linux_and_windows(monkeypatch):
    cmd = app_recorder.capture_command(("pa", 42, "sink.monitor"))
    assert cmd[:2] == ["parec", "--monitor-stream=42"] and cmd[-2:] == ["-d", "sink.monitor"]
    assert "--format=s16le" in cmd and "--rate=48000" in cmd and "--channels=2" in cmd
    monkeypatch.setattr(app_recorder.obs_config, "APP_CAPTURE_HELPER_PATH", "C:\\ClipEm\\capture.exe")
    assert app_recorder.capture_command(("win", 1234)) == ["C:\\ClipEm\\capture.exe", "--pid", "1234"]
    assert app_recorder.capture_command(("?", 1)) is None


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeStream:
    """Stands in for CaptureStream: remembers it was started, can 'die'."""

    started = []

    def __init__(self, handle, command, ring, clock, popen=None):
        self.handle, self.command, self.ring = handle, command, ring
        self.alive = True
        self.stopped = False
        FakeStream.started.append(self)

    def stop(self):
        self.stopped = True
        self.alive = False


def make_recorder(clock):
    FakeStream.started = []
    return app_recorder.AppRecorder(seconds=lambda: 1, monotonic=clock, stream_class=FakeStream,
                                    command_for=lambda handle: ["capture", str(handle)])


def test_recorder_starts_and_stops_helpers_as_targets_change():
    clock = FakeClock()
    recorder = make_recorder(clock)
    recorder.set_targets({"t1": (("a",), [("pa", 1, "m")]), "t2": ((), [])})
    assert [s.handle for s in FakeStream.started] == [("pa", 1, "m")]
    recorder.set_targets({"t1": (("a",), [("pa", 1, "m")])})  # same again: nothing restarts
    assert len(FakeStream.started) == 1
    recorder.set_targets({"t1": (("a",), [("pa", 2, "m")])})
    assert FakeStream.started[0].stopped and FakeStream.started[1].handle == ("pa", 2, "m")
    recorder.set_targets({})
    assert FakeStream.started[1].stopped


def test_recorder_restarts_a_helper_that_died_but_not_in_a_tight_loop():
    clock = FakeClock()
    recorder = make_recorder(clock)
    target = {"t1": (("a",), [("pa", 1, "m")])}
    recorder.set_targets(target)
    FakeStream.started[0].alive = False
    recorder.set_targets(target)
    assert len(FakeStream.started) == 1  # wait a bit first
    clock.t += app_recorder.RESTART_SECONDS + 1
    recorder.set_targets(target)
    assert len(FakeStream.started) == 2


def test_recorder_clears_a_track_whose_apps_changed():
    clock = FakeClock()
    recorder = make_recorder(clock)
    recorder.set_targets({"t1": (("a",), [])})
    ring = recorder._rings["t1"]
    ring.add(recorder.clock.frame(), pcm([5] * 10))
    recorder.set_targets({"t1": (("a",), [])})
    assert any(values(ring.read(0, ring.head)))
    recorder.set_targets({"t1": (("b",), [])})
    assert not any(values(ring.read(0, ring.head)))


def test_snapshot_windows_cover_what_was_recorded():
    clock = FakeClock()
    recorder = make_recorder(clock)
    recorder.set_targets({"t1": (("a",), [])})
    clock.t += 0.5
    now = recorder.clock.frame()
    recorder._rings["t1"].add(now - 3, pcm([4, 5, 6]))
    clock.t += 0.25
    snapshot = recorder.snapshot()
    assert snapshot.end_frame == recorder.clock.frame()
    assert values(snapshot.window("t1", now - 4, now)) == [0, 4, 5, 6]
    # Before the recording began, and for a track it doesn't have: silence.
    assert values(snapshot.window("t1", -2, 2)) == [0, 0, 0, 0]
    assert values(snapshot.window("nope", 0, 3)) == [0, 0, 0]


def test_a_capture_stream_feeds_its_ring_from_the_helpers_output():
    clock = FakeClock()
    ring = TrackRing(2)
    data = pcm(list(range(1, 2401)))  # 2400 frames = 50 ms

    class Output(io.BytesIO):
        def read(self, size=-1):
            clock.t += 0.02  # a chunk of sound takes 20 ms to arrive
            return super().read(size)

    class Proc:
        stdout = Output(data)
        stdin = None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    sample_clock = app_recorder.SampleClock(clock)
    clock.t += 1.0
    stream = app_recorder.CaptureStream(("pa", 1, "m"), ["parec"], ring, sample_clock, popen=lambda *a, **k: Proc())
    stream._thread.join(5)
    assert not stream.alive
    got = [v for v in values(ring.read(0, ring.head)) if v]
    assert got == list(range(1, 2401))  # all of it, in order, nothing on top of anything else


@pytest.mark.skipif(sys.platform != "linux", reason="uses /bin/sh")
def test_a_real_helper_process_is_read_until_it_exits():
    ring = TrackRing(2)
    clock = app_recorder.SampleClock()
    # 5 times 40 ms of 0x0101 samples, as fast as sound would arrive.
    command = ["/bin/sh", "-c", "for i in 1 2 3 4 5; do head -c 7680 /dev/zero | tr '\\0' '\\1'; sleep 0.04; done"]
    stream = app_recorder.CaptureStream(("x", 1), command, ring, clock)
    stream._thread.join(10)
    assert not stream.alive
    assert sum(1 for v in values(ring.read(0, ring.head)) if v) >= 5000


def test_ring_is_quick_enough_for_a_dozen_streams():
    ring = TrackRing(60)
    rng = random.Random(1)
    chunk = pcm([rng.randint(-1000, 1000) for _ in range(960)])
    for i in range(500):  # 10 s of one stream, with a second one on top
        ring.add(i * 960, chunk)
        ring.add(i * 960, chunk)
    assert values(ring.read(0, 960)) == [max(-32768, min(32767, 2 * v)) for v in values(chunk)]
    assert BYTES_PER_FRAME == 4
