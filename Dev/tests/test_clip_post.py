import array
import datetime
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



def test_a_broken_config_is_the_defaults(tmp_path):
    config = tmp_path / "run_config.json"
    for content in ("{broken", "[1]", '{"post_clip": "on"}', "{}"):
        config.write_text(content)
        assert clip_post.load_settings(str(config)) == clip_post.DEFAULTS


def test_defaults_are_off_for_ten_seconds():
    assert clip_post.DEFAULTS == {"enabled": False, "seconds": 10, "sound_after": False}
    assert (clip_post.MIN_SECONDS, clip_post.MAX_SECONDS) == (1, 120)


@pytest.mark.parametrize("saved, seconds", [
    ({}, 10), ({"seconds": 0}, 1), ({"seconds": -5}, 1), ({"seconds": 1}, 1), ({"seconds": 120}, 120),
    ({"seconds": 121}, 120), ({"seconds": "12"}, 12), ({"seconds": "many"}, 10), ({"seconds": None}, 10),
])
def test_seconds_are_kept_in_range(saved, seconds):
    settings = clip_post.normalize(saved)
    assert settings["seconds"] == seconds and settings["enabled"] is False and settings["sound_after"] is False


def test_a_state_needs_every_field_and_the_right_kinds(tmp_path):
    path = tmp_path / "state.json"
    good = {"pressed_at": 100.0, "requested_at": 110.0, "clip_seconds": 30}
    assert clip_post.read_state(str(path), now=110.0) is None
    for bad in ({**good, "pressed_at": "soon"}, {**good, "clip_seconds": None}, {"requested_at": 110.0}, [1], 5):
        path.write_text(json.dumps(bad))
        assert clip_post.read_state(str(path), now=110.0) is None, bad


@pytest.mark.parametrize("now, fresh", [
    (108.9, False), (109.0, True), (110.0, True), (230.0, True), (230.5, False),
])
def test_a_state_is_fresh_from_a_second_before_to_two_minutes_after(tmp_path, now, fresh):
    path = str(tmp_path / "state.json")
    clip_post.write_state({"pressed_at": 100.0, "requested_at": 110.0, "clip_seconds": 30}, path)
    assert (clip_post.read_state(path, now=now) is not None) is fresh
    assert clip_post.STATE_FRESH_SECONDS == 120


def test_writing_a_state_leaves_only_the_state(tmp_path):
    path = tmp_path / "state.json"
    clip_post.write_state({"a": 1}, str(path))
    assert os.listdir(tmp_path) == ["state.json"]


def test_a_press_after_the_request_is_at_the_end():
    # The clock went backwards, or a very quick second press.
    assert clip_post.span(40.0, {"pressed_at": 110.0, "requested_at": 100.0, "clip_seconds": 30}) == (40.0, 10.0)


def test_a_press_before_the_save_began_is_at_its_start():
    assert clip_post.span(5.0, {"pressed_at": 100.0, "requested_at": 110.0, "clip_seconds": 30}) == (0.0, 0.0)


def _ffmpeg_says(text):
    return lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stderr=text)


def test_probing_reads_the_length_and_counts_audio_tracks():
    stderr = ("  Duration: 01:02:03.50, start: 0.000000, bitrate: 900 kb/s\n"
              "  Stream #0:0: Video: h264\n"
              "  Stream #0:1(eng): Audio: aac, 48000 Hz\n"
              "    Metadata:\n      title: not a Stream #0:9: Audio: track\n"
              "  Stream #0:2: Audio: aac\n")
    assert clip_post.probe("ffmpeg", "c.mkv", run=_ffmpeg_says(stderr)) == {"duration": 3723.5, "audio_tracks": 2}


def test_probing_something_else_says_so():
    with pytest.raises(ValueError, match="couldn't read c.mkv"):
        clip_post.probe("ffmpeg", "/x/c.mkv", run=_ffmpeg_says("Invalid data found"))


def test_probing_when_ffmpeg_says_nothing():
    with pytest.raises(ValueError):
        clip_post.probe("ffmpeg", "c.mkv", run=_ffmpeg_says(None))


@pytest.mark.parametrize("name, container", [
    ("a.mkv", "matroska"), ("a.MOV", "mov"), ("a.flv", "flv"), ("a.mp4", "mp4"), ("a.other", "mp4"),
])
def test_the_container_follows_the_extension(name, container):
    assert clip_post._container(name) == container


def test_only_mp4_and_mov_get_faststart():
    assert "+faststart" not in clip_post.build_cut_command("ffmpeg", "a.mkv", "b", 0.0, 1.0, "a.mkv")
    assert "+faststart" in clip_post.build_cut_command("ffmpeg", "a.mov", "b", 0.0, 1.0, "a.mov")


def test_an_extra_file_with_nothing_cancelled_copies_everything():
    cmd = clip_post.build_extra_command("ffmpeg", "in.mkv", "out.mkv", 2, 1.0)
    assert "-filter_complex" not in cmd and not [a for a in cmd if a.startswith("-c:a:")]
    assert not [a for a in cmd if a.startswith("-metadata:s:a:")] and "-disposition:a" not in cmd
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"] == ["in.mkv"]


def test_app_tracks_are_encoded_and_only_the_mix_is_the_default():
    cmd = clip_post.build_extra_command("ffmpeg", "in.mkv", "out.mkv", 1, 1.0, ["Mix"], app_tracks=[("Game", "g.mov")])
    assert cmd[cmd.index("-c:a:1") + 1] == "aac" and cmd[cmd.index("-b:a:1") + 1] == "192k"
    i = cmd.index("-disposition:a")
    assert cmd[i:i + 4] == ["-disposition:a", "0", "-disposition:a:0", "default"]
    assert cmd[cmd.index("-metadata:s:a:1") + 1] == "title=Game"


def test_tracks_keep_their_bitrate():
    assert clip_post.TRACK_AUDIO_KBPS == obs_config.DEFAULT_AUDIO_TRACK_KBPS


def _wav(path, frames, channels=2, width=2, rate=None):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(width)
        f.setframerate(rate or RATE)
        f.writeframes(frames)
    return str(path)


def test_stereo_wavs_read_each_side(tmp_path):
    frames = array.array("h", [100, -100, 200, -200]).tobytes()
    assert clip_post.read_wav(_wav(tmp_path / "s.wav", frames)) == ([100, 200], [-100, -200])


def test_mono_wavs_read_the_same_on_both_sides(tmp_path):
    frames = array.array("h", [100, 200, 300]).tobytes()
    assert clip_post.read_wav(_wav(tmp_path / "m.wav", frames, channels=1)) == ([100, 200, 300], [100, 200, 300])


def test_8_bit_wavs_are_scaled_to_16(tmp_path):
    left, _right = clip_post.read_wav(_wav(tmp_path / "8.wav", bytes([128, 129, 127]), channels=1, width=1))
    assert left == [0, 256, -256]


def test_32_bit_wavs_are_scaled_to_16(tmp_path):
    frames = array.array("i", [65536, -131072]).tobytes()
    left, _right = clip_post.read_wav(_wav(tmp_path / "32.wav", frames, channels=1, width=4))
    assert left == [1, -2]


def test_24_bit_wavs_are_refused(tmp_path):
    with pytest.raises(wave.Error, match="unsupported sample width 3"):
        clip_post.read_wav(_wav(tmp_path / "24.wav", bytes(6), channels=1, width=3))


def test_other_rates_are_resampled(tmp_path):
    frames = array.array("h", [0, 1000, 2000, 3000]).tobytes()
    left, right = clip_post.read_wav(_wav(tmp_path / "r.wav", frames, channels=1, rate=RATE // 2))
    assert left == right == [0, 500, 1000, 1500, 2000, 2500, 3000, 3000]


def test_pcm_splits_into_channels_ignoring_a_torn_frame():
    left, right = clip_post._channels(array.array("h", [1, 2, 3, 4]).tobytes() + b"\x05")
    assert list(left) == [1, 3] and list(right) == [2, 4]


def test_the_envelope_sums_each_blocks_changes():
    assert clip_post._envelope([0, 1, 3, 3, 0, 4, 5], 2) == [3, 3, 5]


def test_the_envelope_match_is_found_exactly():
    ref = [0, 9, 0, 5]
    assert clip_post._best_envelope_lag(ref, [1, 1, 1, 0, 9, 0, 5, 1, 2]) == 3
    assert clip_post._best_envelope_lag(ref, ref) == 0  # no room to slide
    assert clip_post._best_envelope_lag(ref, ref[:3]) is None
    assert clip_post._best_envelope_lag([], [1, 2]) is None
    assert clip_post._best_envelope_lag([3, 3], [1, 2, 3]) is None  # a flat ref fits nothing
    assert clip_post._best_envelope_lag([1, 2], [4, 4, 4]) is None  # nor flat audio


def test_the_fit_finds_the_gain_and_how_much_it_explains():
    gain, score = clip_post._fit([1, 2, 3], [0, 2, 4, 6, 0], 1)
    assert gain == pytest.approx(2.0) and score == pytest.approx(1.0)
    gain, score = clip_post._fit([1, 2, 3], [0, -1, -2, -3], 1)
    assert gain == pytest.approx(-1.0) and score == pytest.approx(-1.0)
    gain, score = clip_post._fit([1, 0], [1, 1], 0)
    assert gain == pytest.approx(1.0) and score == pytest.approx(math.sqrt(0.5))


@pytest.mark.parametrize("ref, sig, start", [
    ([1, 2], [1], 0),  # runs off the end
    ([0, 0], [1, 2], 0),
    ([1, 2], [0, 0], 0),
])
def test_the_fit_of_nothing_is_nothing(ref, sig, start):
    assert clip_post._fit(ref, sig, start) == (0.0, 0.0)


def _track_with_sound(sound, at, seconds=3.0, gain=0.5):
    random.seed(11)
    n = int(seconds * RATE)
    left = [random.gauss(0, 200) for _ in range(n)]
    for k, value in enumerate(sound[0]):
        if at + k < n:
            left[at + k] += gain * value
    return _pcm(left, left)


@pytest.mark.parametrize("offset, found", [
    (-0.19, True),   # just inside SOUND_SEARCH_BEFORE
    (0.99, True),    # just inside SOUND_SEARCH_AFTER
    (-0.4, False),   # well before the press: not this sound
])
def test_the_sound_is_only_looked_for_around_the_press(offset, found):
    sound = _sound()
    at = int((1.0 + offset) * RATE)
    result = clip_post.find_sound(_track_with_sound(sound, at), sound, around=1.0)
    assert (result is not None and result[0] == at) is found


def test_a_press_near_the_start_doesnt_look_before_it():
    sound = _sound()
    assert clip_post.find_sound(_track_with_sound(sound, 0), sound, around=0.0)[0] == 0


def test_a_track_too_short_for_the_sound_has_nothing_to_find():
    sound = _sound()
    assert clip_post.find_sound(bytes(100), sound, around=0.0) is None
    assert clip_post.find_sound(_pcm(*sound)[:-4], sound, around=0.0) is None


def test_a_track_that_is_only_the_sound_has_it():
    sound = _sound()
    assert clip_post.find_sound(_pcm(*sound), sound, around=0.0)[0] == 0


def test_a_silent_track_has_no_sound_in_it():
    sound = _sound()
    assert clip_post.find_sound(bytes(4 * 2 * RATE), sound, around=1.0) is None


def test_a_track_ending_just_after_the_sound_can_still_have_it():
    sound = _sound()
    n = len(sound[0])
    pcm = _track_with_sound(sound, RATE // 2, seconds=(RATE // 2 + n) / RATE)
    assert clip_post.find_sound(pcm, sound, around=0.5)[0] == RATE // 2


def test_an_inverted_sound_isnt_cancelled_louder():
    """A gain outside MIN_GAIN..MAX_GAIN isn't this sound."""
    sound = _sound()
    assert clip_post.find_sound(_track_with_sound(sound, RATE, gain=-0.5), sound, around=1.0) is None


def test_a_loud_match_is_refused(monkeypatch):
    sound = _sound()
    pcm = _track_with_sound(sound, RATE, gain=0.5)
    monkeypatch.setattr(clip_post, "MAX_GAIN", 0.2)
    assert clip_post.find_sound(pcm, sound, around=1.0) is None
    monkeypatch.undo()
    monkeypatch.setattr(clip_post, "MIN_GAIN", 0.8)
    assert clip_post.find_sound(pcm, sound, around=1.0) is None


def test_each_side_gets_its_own_gain():
    sound = _sound()
    random.seed(3)
    n = 2 * RATE
    left = [random.gauss(0, 100) for _ in range(n)]
    right = [random.gauss(0, 100) for _ in range(n)]
    for k, value in enumerate(sound[0]):
        left[RATE + k] += 0.2 * value
        right[RATE + k] += 0.6 * value
    _at, gain_left, gain_right, _score = clip_post.find_sound(_pcm(left, right), sound, around=1.0)
    assert gain_left == pytest.approx(0.2, abs=0.02) and gain_right == pytest.approx(0.6, abs=0.02)


def test_the_cancelling_copy_is_inverted_scaled_and_clipped(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(clip_tracks, "write_pcm_mov", lambda path, pcm: written.append((path, pcm)))
    clip_post.write_cancel("c.mov", ([100, 30000, -30000], [10, 20, 30]), 2.0, 0.5)
    assert written[0][0] == "c.mov"
    assert list(array.array("h", written[0][1])) == [-200, -5, -32768, -10, 32767, -15]


def _cancels(tmp_path, run, tracks=(0, 2)):
    sound = _sound()
    wav = tmp_path / "s.wav"
    _write_wav(wav, sound)
    logs = []
    return clip_post.cancels_for("ffmpeg", "c.mkv", list(tracks), str(wav), 1.0, str(tmp_path), logs.append, run=run), logs


def test_a_clip_sound_that_cant_be_read_is_left_in(tmp_path):
    (tmp_path / "bad.wav").write_bytes(b"not a wav")
    logs = []
    assert clip_post.cancels_for("ffmpeg", "c", [0], str(tmp_path / "bad.wav"), 1.0, str(tmp_path), logs.append) == {}
    assert "couldn't read the clip sound" in logs[0]
    assert clip_post.cancels_for("ffmpeg", "c", [0], str(tmp_path / "gone.wav"), 1.0, str(tmp_path), logs.append) == {}


def test_a_track_that_cant_be_read_is_skipped_and_the_rest_still_done(tmp_path):
    sound = _sound()
    good = _track_with_sound(sound, RATE)

    def run(cmd, **_kwargs):
        if cmd[cmd.index("-map") + 1] == "0:a:0":
            raise OSError("ffmpeg vanished")
        return subprocess.CompletedProcess(cmd, 0, stdout=good)

    cancels, logs = _cancels(tmp_path, run)
    assert list(cancels) == [2] and cancels[2] == (str(tmp_path / "cancel-2.mov"), RATE)
    assert os.path.isfile(cancels[2][0])
    assert any("couldn't read track 1" in line for line in logs)


def test_a_failed_decode_cancels_nothing(tmp_path):
    good = _track_with_sound(_sound(), RATE)
    cancels, logs = _cancels(tmp_path, lambda cmd, **_k: subprocess.CompletedProcess(cmd, 1, stdout=good), tracks=(0,))
    assert cancels == {} and "not clearly in track 1" in logs[0]


class Finish:
    """finish_post_save with ffmpeg faked: `steps` records what ran, in
    order; a step in `fails` fails as ffmpeg would."""

    def __init__(self, tmp_path, monkeypatch, audio=3, fails=()):
        self.path = tmp_path / "Replay 2026-10-27 10-00-00.mkv"
        self.path.write_bytes(b"save")
        self.logs = []
        self.steps = []
        self.reexports = []
        self.audio = audio

        def run_to(cmd_for, out_path, what, log):
            step = "extra" if "extra seconds" in what else "cut"
            self.steps.append((step, cmd_for(out_path + ".partial")))
            return step not in fails

        monkeypatch.setattr(clip_tracks, "run_to", run_to)
        monkeypatch.setattr(clip_tracks, "reexport", lambda *args: self.reexports.append(args))

    def run(self, cmd, **_kwargs):
        streams = "".join(f"  Stream #0:{i + 1}: Audio: aac\n" for i in range(self.audio))
        return subprocess.CompletedProcess(cmd, 0, stderr=f"Duration: 00:00:40.00\n{streams}")

    def __call__(self, options=None, state=None):
        state = {"pressed_at": 1_000_000.0, "requested_at": 1_000_010.0, "clip_seconds": 20, "sound": None, **(state or {})}
        return clip_post.finish_post_save("ffmpeg", str(self.path), state, options or {"naming": None},
                                           self.logs.append, run=self.run)


def _maps(cmd):
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]


def test_the_save_becomes_the_extra_file_then_the_clip(tmp_path, monkeypatch):
    finish = Finish(tmp_path, monkeypatch)
    clip, extra = finish()
    assert os.path.basename(clip).startswith("Replay-") and clip.endswith(".mkv")
    assert extra == os.path.splitext(clip)[0] + "-extra.mkv"
    assert [step for step, _cmd in finish.steps] == ["extra", "cut"]
    cut = finish.steps[1][1]
    assert cut[cut.index("-ss") + 1] == "10.000" and cut[cut.index("-t") + 1] == "20.000"


def test_no_extra_file_means_the_whole_save_is_kept(tmp_path, monkeypatch):
    """Nothing is lost: the clip isn't cut, nor is a smaller copy made of it."""
    finish = Finish(tmp_path, monkeypatch, fails={"extra"})
    clip, extra = finish({"naming": None, "reexport": True, "target_mb": 20, "crop_seconds": 30})
    assert extra is None and os.path.isfile(clip)
    assert [step for step, _cmd in finish.steps] == ["extra"] and finish.reexports == []
    assert any("keeps the extra seconds" in line for line in finish.logs)


def test_a_failed_cut_still_has_the_extra_file(tmp_path, monkeypatch):
    finish = Finish(tmp_path, monkeypatch, fails={"cut"})
    clip, extra = finish()
    assert extra is not None and os.path.isfile(clip)
    assert any("still has the extra seconds" in line for line in finish.logs)


def test_the_smaller_copy_is_made_from_the_clip_with_its_suffix(tmp_path, monkeypatch):
    finish = Finish(tmp_path, monkeypatch)
    naming = {**clip_post.clip_naming.DEFAULTS, "small_suffix": "discord"}
    clip, _extra = finish({"naming": naming, "reexport": True, "target_mb": 8, "crop_seconds": 15})
    assert len(finish.reexports) == 1
    args = finish.reexports[0]
    assert args[:4] == ("ffmpeg", clip, 8, 15) and args[-1] == "-discord"


def test_no_smaller_copy_unless_its_on(tmp_path, monkeypatch):
    finish = Finish(tmp_path, monkeypatch)
    finish()
    assert finish.reexports == []


def test_the_press_names_the_clip(tmp_path, monkeypatch):
    when = datetime.datetime(2026, 10, 27, 12, 30, 15)
    finish = Finish(tmp_path, monkeypatch)
    clip, _extra = finish(state={"pressed_at": when.timestamp(), "requested_at": when.timestamp() + 10})
    assert os.path.basename(clip) == "Replay-2026-10-27-12-30-15.mkv"


@pytest.mark.parametrize("audio, titles", [
    (0, []),
    (1, []),  # just the mix: nothing to name
    (2, []),
    (3, ["Mix", "Mic", "Desktop"]),
    (5, ["Mix", "Mic", "Desktop", "Track-4", "Track-5"]),
])
def test_tracks_are_named_when_the_save_was_split(tmp_path, monkeypatch, audio, titles):
    finish = Finish(tmp_path, monkeypatch, audio=audio)
    finish()
    extra = finish.steps[0][1]
    assert [arg.removeprefix("title=") for arg in extra if arg.startswith("title=")] == titles
    assert _maps(extra) == ["0:v:0", *[f"0:a:{n}" for n in range(audio)]]


@pytest.mark.parametrize("audio, tracks", [(0, []), (1, [0]), (2, [0]), (3, [0, 2]), (6, [0, 2])])
def test_the_sound_is_taken_from_the_mix_and_desktop_not_the_mic(tmp_path, monkeypatch, audio, tracks):
    finish = Finish(tmp_path, monkeypatch, audio=audio)
    wav = tmp_path / "s.wav"
    wav.write_bytes(b"x")
    asked = []
    monkeypatch.setattr(clip_post, "cancels_for", lambda ffmpeg, src, t, *_a, **_k: asked.append(t) or {})
    finish(state={"sound": str(wav)})
    assert asked == [tracks]


@pytest.mark.parametrize("sound", [None, "", "gone.wav"])
def test_no_sound_file_means_nothing_to_take_out(tmp_path, monkeypatch, sound):
    finish = Finish(tmp_path, monkeypatch)
    monkeypatch.setattr(clip_post, "cancels_for", lambda *_a, **_k: pytest.fail("no sound to find"))
    finish(state={"sound": sound and str(tmp_path / sound)})


def test_a_clip_ffmpeg_cant_read_raises_before_anything_changes(tmp_path, monkeypatch):
    finish = Finish(tmp_path, monkeypatch)
    finish.run = _ffmpeg_says("garbage")
    with pytest.raises(ValueError):
        finish()
    assert finish.path.exists()
