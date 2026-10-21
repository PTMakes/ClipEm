"""Finishing a saved clip when the tray recorded its app tracks
(tray-widget/track_align.py, tray_tracks.py, Scripts/clip_tracks.py)."""

import os
import random
import shutil
import subprocess
import sys
import types
from array import array

import pytest
from conftest import SCRIPTS_DIR, TRAY_WIDGET_DIR

sys.modules.setdefault("websocket", types.ModuleType("websocket"))
sys.path.insert(0, TRAY_WIDGET_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import clip_edit  # noqa: E402
import clip_tracks  # noqa: E402
import obs_config  # noqa: E402
import track_align  # noqa: E402
import tray_tracks  # noqa: E402
from app_recorder import RATE, Snapshot  # noqa: E402

SYSTEM_FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not SYSTEM_FFMPEG, reason="needs an ffmpeg with lavfi to make a test clip")


def bursts(seed, seconds):
    """Mono 16-bit noise in bursts of different lengths and loudness, with silence between."""
    rng = random.Random(seed)
    total = seconds * RATE
    out = array("h", bytes(2 * total))
    t = 0
    while t < total:
        on, off, amp = rng.randint(2400, 14400), rng.randint(2400, 19200), rng.randint(3000, 12000)
        for i in range(t, min(t + on, total)):
            out[i] = rng.randint(-amp, amp)
        t += on + off
    return out


def stereo(mono):
    out = array("h")
    for value in mono:
        out.extend((value, value))
    return out.tobytes()


def mono_8k(samples):
    """What ffmpeg gives for the clip's Desktop track: mono at 8 kHz."""
    return array("h", (sum(samples[i:i + 6]) // 6 for i in range(0, len(samples) - 5, 6))).tobytes()


def mixed(*tracks):
    out = array("h", bytes(2 * len(tracks[0])))
    for track in tracks:
        for i, value in enumerate(track):
            out[i] = max(-32768, min(32767, out[i] + value))
    return out


class Recording:
    """Two apps (A, B) the tray recorded, a sound only Desktop has (C), a
    clip of them, and a snapshot - where the clip really starts is `shift`
    ms later than the clock says."""

    seconds = 12

    def __init__(self, shift_ms=37, b_extra_ms=0, quiet_b=False):
        self.count = self.seconds * RATE
        a, b, c = bursts(1, self.seconds), bursts(2, self.seconds), bursts(3, self.seconds)
        if quiet_b:
            b = array("h", bytes(2 * len(b)))
        # The clip's own Desktop track: all three.
        self.clip_pcm = mono_8k(mixed(a, b, c))
        shift = shift_ms * RATE // 1000
        self.true_start = 5 * RATE
        nominal_start = self.true_start - shift
        end = nominal_start + self.count + round(tray_tracks.EVENT_LATENCY_SECONDS * RATE)
        timeline = {}
        for name, signal, extra in (("a", a, 0), ("b", b, b_extra_ms * RATE // 1000)):
            pcm = array("h", bytes(4 * (end + RATE)))
            context = bursts(10 + len(timeline), 5 + self.seconds + 2)  # other sound around the clip
            for i, value in enumerate(context):
                if i < len(pcm) // 2:
                    pcm[2 * i] = pcm[2 * i + 1] = value // 2
            start = self.true_start + extra
            for i, value in enumerate(signal):
                pcm[2 * (start + i)] = pcm[2 * (start + i) + 1] = value
            timeline[name] = pcm[:2 * end].tobytes()
        self.snapshot = Snapshot(end, end, timeline)
        self.clip_env = track_align.mono_envelope(self.clip_pcm, 8000)


def test_envelope_locate_finds_a_known_shift():
    rec = Recording(shift_ms=0)
    sig = track_align.stereo_envelope(rec.snapshot.window("a", 0, 17 * RATE), RATE)
    ref = track_align.mono_envelope(mono_8k(bursts(1, rec.seconds)), 8000)
    lag, score = track_align.locate(ref, sig, 4800, 2500)
    assert lag == 5000 and score > 0.9


@pytest.mark.parametrize("shift_ms", [0, 37, -120, 300, -1000])  # the clip ends before the save event, never after
def test_align_tracks_finds_where_the_clip_starts(shift_ms):
    rec = Recording(shift_ms=shift_ms)
    starts = tray_tracks.align_tracks(rec.snapshot, ["a", "b"], rec.clip_env, rec.seconds, lambda m: None)
    for name in ("a", "b"):
        start, quiet = starts[name]
        assert abs(start - rec.true_start) <= RATE // 1000 + 1, (name, start - rec.true_start)
        assert not quiet


def test_a_track_recorded_late_by_its_own_helper_is_corrected_on_its_own():
    rec = Recording(shift_ms=20, b_extra_ms=30)  # B's helper stamped its sound 30 ms later than A's
    starts = tray_tracks.align_tracks(rec.snapshot, ["a", "b"], rec.clip_env, rec.seconds, lambda m: None)
    assert abs(starts["a"][0] - rec.true_start) <= RATE // 1000 + 1
    assert abs(starts["b"][0] - (rec.true_start + 30 * RATE // 1000)) <= RATE // 1000 + 1


def test_a_silent_track_is_marked_and_does_not_throw_the_match_off():
    rec = Recording(shift_ms=60, quiet_b=True)
    starts = tray_tracks.align_tracks(rec.snapshot, ["a", "b"], rec.clip_env, rec.seconds, lambda m: None)
    assert starts["b"][1] is True and starts["a"][1] is False
    assert abs(starts["a"][0] - rec.true_start) <= RATE // 1000 + 1
    assert abs(starts["b"][0] - starts["a"][0]) <= 0  # no sound of its own to match: goes with the others


def test_without_a_match_the_clock_decides():
    rec = Recording(shift_ms=60)
    logs = []
    starts = tray_tracks.align_tracks(rec.snapshot, ["a"], None, rec.seconds, logs.append)  # clip unreadable
    nominal = rec.snapshot.end_frame - round(tray_tracks.EVENT_LATENCY_SECONDS * RATE) - rec.count
    assert starts["a"][0] == nominal and "clock" in logs[0]
    noise = array("h", [0] * 8000 * rec.seconds).tobytes()  # a silent clip
    starts = tray_tracks.align_tracks(rec.snapshot, ["a"], track_align.mono_envelope(noise, 8000), rec.seconds,
                                      logs.append)
    assert starts["a"][0] == nominal


def test_pcm_mov_is_a_playable_file_of_the_right_length(tmp_path):
    ffmpeg = SYSTEM_FFMPEG or shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("needs ffmpeg")
    frames = 2 * RATE + 777
    path = tmp_path / "t.mov"
    clip_tracks.write_pcm_mov(str(path), bytes(frames * 4))
    info = clip_edit.parse_probe(subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True,
                                                text=True).stderr)
    assert abs(info["duration"] - frames / RATE) < 0.01 and len(info["audio"]) == 1  # ffmpeg prints 10 ms steps


def test_tray_split_command_copies_the_obs_tracks_and_encodes_the_rest():
    cmd = clip_tracks.build_tray_split_command("ffmpeg", "clip.mp4", "out.mkv", ["Mix", "Mic", "Desktop", "Voice", "Track-5"],
                                               ["a.mov", "b.mov"])
    assert cmd[cmd.index("-i") + 1] == "clip.mp4" and cmd.count("-i") == 3
    maps = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-map"]
    assert maps == ["0:v:0", "0:a:0", "0:a:1", "0:a:2", "1:a:0", "2:a:0"]
    assert ["-c:a:3", "aac"] == cmd[cmd.index("-c:a:3"):cmd.index("-c:a:3") + 2] and "-c:a:2" not in cmd
    assert "title=Voice" in cmd and "title=Track-5" in cmd and cmd[-1] == "out.mkv"


@needs_ffmpeg
def test_finish_clip_writes_the_tracks_file_and_strips_the_clip(tmp_path):
    rec = Recording(shift_ms=40)
    seconds = rec.seconds
    # A real clip: video and three AAC tracks - Mix, Mic, Desktop (A+B+C).
    desktop = tmp_path / "desktop.wav"
    import wave
    with wave.open(str(desktop), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(rec.clip_pcm)
    clip = tmp_path / "Replay 2026.mp4"
    subprocess.run([SYSTEM_FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=black:s=64x64:r=30:d={seconds}",
                    "-i", str(desktop), "-i", str(desktop), "-i", str(desktop),
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a",
                    "-c:v", "libx264", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", str(clip)],
                   check=True)
    logs = []
    tray_tracks.finish_clip(str(clip), rec.snapshot, [("a", "Spotify"), ("b", "Discord"), ("c", "Voice")],
                            SYSTEM_FFMPEG, {"reexport": False}, logs.append)
    tracks_file = tmp_path / "Replay 2026-tracks.mkv"
    assert tracks_file.exists(), logs
    info = clip_edit.probe(SYSTEM_FFMPEG, str(tracks_file))
    # Voice recorded nothing during the clip, so it's just "Track-6" here.
    assert [t["title"] for t in info["audio"]] == ["Mix", "Mic", "Desktop", "Spotify", "Discord", "Track-6"]
    assert abs(info["duration"] - seconds) < 0.2
    assert len(clip_edit.probe(SYSTEM_FFMPEG, str(clip))["audio"]) == 1  # the clip itself is just the mix
    assert not [p for p in tmp_path.iterdir() if p.suffix in (".partial", ".mov", ".pcm")]


@needs_ffmpeg
def test_finish_clip_leaves_a_clip_alone_that_was_recorded_without_split_tracks(tmp_path):
    clip = tmp_path / "Replay.mp4"
    subprocess.run([SYSTEM_FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=30:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)], check=True)
    before = clip.read_bytes()
    logs = []
    tray_tracks.finish_clip(str(clip), Snapshot(0, 0, {}), [("a", "x")], SYSTEM_FFMPEG, {"reexport": False}, logs.append)
    assert clip.read_bytes() == before and not (tmp_path / "Replay-tracks.mkv").exists()
    assert "audio track" in logs[0]


class FakeRecorder:
    def snapshot(self):
        return "snapshot"


class FakeMonitor:
    def __init__(self, state=None):
        self._state = state

    def state(self):
        return self._state


def test_clip_finisher_takes_the_snapshot_at_once_and_finishes_on_a_worker(monkeypatch, tmp_path):
    state = obs_config.empty_app_tracks()
    state["tracks"][0]["owner"] = {"exe": "spotify", "label": "Spotify", "last_heard": 0, "pinned": False}
    state["tracks"].insert(0, obs_config.new_app_track("Voice", ["discord"]))
    monkeypatch.setattr(obs_config, "load_clip_script_settings",
                        lambda: {"reexport_enabled": True, "target_mb": 20.0, "crop_seconds": 30})
    done = []
    monkeypatch.setattr(tray_tracks, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(tray_tracks, "finish_clip", lambda *args: done.append(args))
    clip = tmp_path / "Replay.mp4"
    clip.write_bytes(b"x")
    finisher = tray_tracks.ClipFinisher(FakeRecorder(), FakeMonitor(state), enabled=lambda: True)
    finisher.on_replay_saved({"savedReplayPath": str(clip)})
    for thread in __import__("threading").enumerate():
        if thread is not __import__("threading").current_thread() and thread.daemon:
            thread.join(5)
    (path, snapshot, tracks, ffmpeg, options, _log), = done
    assert (path, snapshot, ffmpeg) == (str(clip), "snapshot", "ffmpeg")
    assert [label for _id, label in tracks] == ["Voice", "Spotify", "Track-6", "Track-7"]
    assert options == {"reexport": True, "target_mb": 20.0, "crop_seconds": 30}


def test_clip_finisher_does_nothing_when_tray_tracks_are_off():
    finisher = tray_tracks.ClipFinisher(FakeRecorder(), FakeMonitor(), enabled=lambda: False)
    finisher.on_replay_saved({"savedReplayPath": "/x.mp4"})  # no snapshot, no thread, no error


def test_the_obs_script_stands_aside_when_the_tray_finishes_clips(monkeypatch):
    # (the script itself is exercised in test_obs_scripts.py; this is the rule)
    source = open(os.path.join(SCRIPTS_DIR, "clip_reexport.py"), encoding="utf-8").read()
    assert "if tray_tracks_enabled:" in source
