import array
import json
import math
import os
import random
import shutil
import subprocess
import sys
import wave

import pytest
from conftest import SCRIPTS_DIR, TRAY_WIDGET_DIR

sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, TRAY_WIDGET_DIR)

import clip_edit  # noqa: E402
import clip_post  # noqa: E402
import clip_tracks  # noqa: E402
import clipem_common  # noqa: E402
import obs_config  # noqa: E402
import setup  # noqa: E402

RATE = clip_post.RATE


def test_settings_are_clamped_and_default_off(tmp_path):
    config = tmp_path / "run_config.json"
    assert clip_post.load_settings(str(config)) == clip_post.DEFAULTS
    config.write_text(json.dumps({"post_clip": {"enabled": True, "seconds": 999, "sound_after": 1}}))
    assert clip_post.load_settings(str(config)) == {"enabled": True, "seconds": clip_post.MAX_SECONDS,
                                                    "sound_after": True}
    assert clip_post.extra_seconds({"enabled": False, "seconds": 10}) == 0


def test_only_a_fresh_state_marks_a_post_clip_save(tmp_path):
    path = str(tmp_path / "state.json")
    assert clip_post.read_state(path) is None
    clip_post.write_state({"pressed_at": 100.0, "requested_at": 110.0, "clip_seconds": 30, "sound": None}, path)
    assert clip_post.read_state(path, now=111.0)["clip_seconds"] == 30
    assert clip_post.read_state(path, now=110.0 + clip_post.STATE_FRESH_SECONDS + 1) is None  # someone else's save
    (tmp_path / "state.json").write_text("{broken")
    assert clip_post.read_state(path, now=111.0) is None


def test_a_slow_save_is_still_a_post_clip_save(tmp_path):
    path = str(tmp_path / "state.json")
    clip_post.write_state({"pressed_at": 100.0, "requested_at": 110.0, "clip_seconds": 30, "sound": None}, path)
    assert clip_post.read_state(path, now=110.0 + 60) is not None


def test_turning_post_clip_off_forgets_the_last_press(tmp_path, monkeypatch):
    _post_clip_files(tmp_path, monkeypatch, {"enabled": True, "seconds": 10})
    state = tmp_path / "state.json"
    monkeypatch.setattr(clip_post, "STATE_PATH", str(state))
    state.write_text("{}")
    obs_config.save_post_clip(True, 10, False)
    assert state.exists()
    obs_config.save_post_clip(False, 10, False)
    assert not state.exists()
    clip_post.clear_state()  # nothing there: no error


def test_the_clip_ends_at_the_press_and_is_the_chosen_length():
    state = {"pressed_at": 100.0, "requested_at": 110.0, "clip_seconds": 30}
    assert clip_post.span(40.0, state) == (30.0, 0.0)
    assert clip_post.span(45.0, state) == (35.0, 5.0)
    assert clip_post.span(12.0, state) == (2.0, 0.0)  # OBS had only just started recording


def test_extra_command_copies_every_track_and_re_encodes_only_the_cancelled_ones():
    cmd = clip_post.build_extra_command("ffmpeg", "in.mkv", "out.mkv", 3, 30.5, ["Mix", "Mic", "Desktop"],
                                        app_tracks=[("Spotify", "app.mov")],
                                        cancels={0: ("c0.mov", 1000), 2: ("c2.mov", 1200)})
    joined = " ".join(cmd)
    assert cmd[cmd.index("-filter_complex") + 1].count("amix=inputs=2:normalize=0:duration=first") == 2
    assert "[1:a:0]adelay=delays=1000S:all=1" in joined and "[2:a:0]adelay=delays=1200S:all=1" in joined
    maps = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
    assert maps == ["0:v:0", "[c0]", "0:a:1", "[c2]", "3:a:0"]
    assert "-c:a:0" in cmd and "-c:a:2" in cmd and "-c:a:3" in cmd and "-c:a:1" not in cmd
    assert "title=Spotify" in cmd and f"{clip_post.MARK_TAG}=30.500" in cmd
    assert cmd[-3:] == ["-f", "matroska", "out.mkv"]


def test_cut_command_is_lossless_and_keeps_only_the_mix():
    cmd = clip_post.build_cut_command("ffmpeg", "in.mp4", "in.mp4.partial", 5.0, 35.0, "in.mp4")
    assert cmd[cmd.index("-ss") + 1] == "5.000" and cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-t") + 1] == "30.000"
    assert ["-c", "copy"] == cmd[cmd.index("-c"):cmd.index("-c") + 2]
    assert "0:a:0?" in cmd and "+faststart" in cmd and cmd[-2] == "mp4"
    assert "-ss" not in clip_post.build_cut_command("ffmpeg", "a.mkv", "b", 0.0, 10.0)


def _sound(seconds=0.3):
    random.seed(4)
    n = int(seconds * RATE)
    # A click: a decaying burst of noise.
    left = [random.gauss(0, 9000) * math.exp(-i / (RATE * 0.05)) for i in range(n)]
    return left, list(left)


def _pcm(left, right):
    return array.array("h", (max(-32768, min(32767, int(v))) for pair in zip(left, right, strict=True) for v in pair)).tobytes()


def test_the_sound_is_found_to_the_sample_under_game_audio():
    sound = _sound()
    n = 3 * RATE
    random.seed(9)
    game = [4000 * math.sin(2 * math.pi * 180 * i / RATE) + random.gauss(0, 600) for i in range(n)]
    start = int(1.537 * RATE)
    left, right = list(game), list(game)
    for k in range(len(sound[0])):
        left[start + k] += 0.4 * sound[0][k]
        right[start + k] += 0.2 * sound[1][k]
    found = clip_post.find_sound(_pcm(left, right), sound, around=1.45)
    assert found is not None
    at, gain_left, gain_right, _score = found
    assert at == start
    assert abs(gain_left - 0.4) < 0.02 and abs(gain_right - 0.2) < 0.02


def test_no_sound_means_nothing_is_cancelled():
    random.seed(2)
    noise = [random.gauss(0, 3000) for _ in range(2 * RATE)]
    assert clip_post.find_sound(_pcm(noise, noise), _sound(), around=0.5) is None


def _write_wav(path, sound):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(_pcm(*sound))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_post_clip_save_end_to_end_with_real_ffmpeg(tmp_path):
    """A 3-track save with the clip sound in Mix and Desktop 6 s from the
    end: the clip is cut at the press, the extra file keeps everything but
    the sound."""
    sound = _sound()
    wav = tmp_path / "clip.wav"
    _write_wav(wav, sound)
    seconds, press = 12.0, 6.0
    n = int(seconds * RATE)
    random.seed(5)
    game = [3000 * math.sin(2 * math.pi * 220 * i / RATE) + random.gauss(0, 500) for i in range(n)]
    desktop = list(game)
    at = int((press + 0.08) * RATE)
    for k in range(len(sound[0])):
        desktop[at + k] += 0.5 * sound[0][k]
    tracks = {"mix": desktop, "mic": [0.0] * n, "desktop": desktop}
    for name, samples in tracks.items():
        clip_tracks.write_pcm_mov(str(tmp_path / f"{name}.mov"), _pcm(samples, samples))
    save = tmp_path / "Replay 2026-10-27 10-00-00.mkv"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=s=64x64:r=30:d={seconds}",
                    *[arg for name in tracks for arg in ("-i", str(tmp_path / f"{name}.mov"))],
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a", "-c:v", "libx264", "-g", "30",
                    "-c:a", "aac", "-b:a", "192k", str(save)], check=True)
    state = {"pressed_at": 1000.0, "requested_at": 1000.0 + seconds - press, "clip_seconds": 4, "sound": str(wav)}
    logs = []
    clip, extra = clip_post.finish_post_save("ffmpeg", str(save), state, {"naming": None}, logs.append)
    assert extra == os.path.splitext(clip)[0] + "-extra.mkv", logs
    assert any("taking the clip sound out of track 3" in line for line in logs), logs

    info = clip_edit.probe("ffmpeg", extra)
    assert [t["title"] for t in info["audio"]] == ["Mix", "Mic", "Desktop"]
    assert abs(info["mark"] - press) < 0.05 and abs(info["duration"] - seconds) < 0.1
    assert clip_edit.default_project(info)["keep"] == [[0.0, round(info["mark"], 3)]]
    clip_info = clip_edit.probe("ffmpeg", clip)
    assert len(clip_info["audio"]) == 1
    # From a keyframe at or before the start (ffmpeg's seek can land one keyframe, 1 s here, early).
    assert 4.0 <= clip_info["duration"] <= 4.0 + 2.3

    out = subprocess.run(clip_post.track_pcm_command("ffmpeg", extra, 2), stdout=subprocess.PIPE, check=True).stdout
    left, _right = clip_post._channels(out)
    gain, _score = clip_post._fit(sound[0], left, at)
    assert abs(gain) < 0.05, f"the clip sound is still there at {gain:.2f} of its level"


def _post_clip_files(tmp_path, monkeypatch, post_clip):
    ini = tmp_path / "basic.ini"
    ini.write_text("[AdvOut]\nRecRBTime=40\n\n[Hotkeys]\nReplayBuffer={\"ReplayBuffer.Save\":"
                   " [{\"key\": \"OBS_KEY_F9\", \"control\": true}]}\n")
    scene = tmp_path / "scene.json"
    scene.write_text(json.dumps({"modules": {"scripts-tool": []}}))
    config = tmp_path / "run_config.json"
    config.write_text(json.dumps({"post_clip": post_clip}))
    for module in (obs_config, setup):
        monkeypatch.setattr(module, "BASIC_INI", str(ini))
        monkeypatch.setattr(module, "SCENE_COLLECTION_JSON", str(scene))
    monkeypatch.setattr(clipem_common, "RUN_CONFIG_PATH", str(config))
    monkeypatch.setattr(obs_config, "fit_replay_buffer_size", lambda: None)
    return ini, scene


def test_turning_post_clip_on_and_off_moves_the_hotkey_and_keeps_the_clip_length(tmp_path, monkeypatch):
    ini, scene = _post_clip_files(tmp_path, monkeypatch, {"enabled": False})
    assert setup.load_replay_buffer_seconds() == 40

    obs_config.save_post_clip(True, 10, False)
    assert obs_config.read_ini_section("AdvOut")["RecRBTime"] == "50"
    assert setup.load_replay_buffer_seconds() == 40
    assert obs_config.load_clip_hotkey() == (True, False, False, "F9")
    saved = json.loads(obs_config.read_ini_section("Hotkeys")["ReplayBuffer"])
    assert saved == {"ReplayBuffer.Save": []}  # one press, one save: OBS's own is left without keys
    entry = obs_config.find_script_entry(json.loads(scene.read_text()), "post_clip.py")
    assert entry["settings"]["post_clip_hotkey"] == [{"key": "OBS_KEY_F9", "control": True}]

    obs_config.save_clip_hotkey(False, True, False, "~")
    assert obs_config.load_clip_hotkey() == (False, True, False, "~")

    obs_config.save_post_clip(False, 10, False)
    assert obs_config.read_ini_section("AdvOut")["RecRBTime"] == "40"
    assert obs_config.load_clip_hotkey() == (False, True, False, "~")
    entry = obs_config.find_script_entry(json.loads(scene.read_text()), "post_clip.py")
    assert entry["settings"]["post_clip_hotkey"] == []


def test_queued_post_clip_settings_apply_after_the_hotkey(tmp_path, monkeypatch):
    _post_clip_files(tmp_path, monkeypatch, {"enabled": False})
    obs_config.apply_obs_settings({"clip_hotkey": [False, False, True, "F10"], "replay_buffer_seconds": 30,
                                   "post_clip": {"enabled": True, "seconds": 5, "sound_after": True}})
    assert obs_config.load_clip_hotkey() == (False, False, True, "F10")
    assert obs_config.read_ini_section("AdvOut")["RecRBTime"] == "35"
    assert obs_config.load_post_clip() == {"enabled": True, "seconds": 5, "sound_after": True}


def test_setup_keeps_recording_after_the_hotkey_like_settings(tmp_path, monkeypatch):
    """The text installer's prompts end up in the same place as Settings."""
    _post_clip_files(tmp_path, monkeypatch, {"enabled": False})
    answers = iter(["y", "25", "n"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))
    setup.configure_post_clip()
    assert obs_config.load_post_clip() == {"enabled": True, "seconds": 25, "sound_after": False}

