import io
import json
import os
import shutil
import subprocess
import sys
import types
import zipfile

import pytest
from conftest import SCRIPTS_DIR

sys.modules.setdefault("obspython", types.ModuleType("obspython"))  # only exists inside OBS
sys.path.insert(0, SCRIPTS_DIR)

import clip_reexport  # noqa: E402
import portable_launch_helper  # noqa: E402
import setup  # noqa: E402
import updater  # noqa: E402


def test_bitrate_leaves_headroom_under_target():
    kbps = clip_reexport.video_bitrate_kbps(20, 20)
    total_bytes = (kbps + clip_reexport.AUDIO_BITRATE_KBPS) * 1000 / 8 * 20
    assert 18_000_000 < total_bytes <= 20_000_000 * clip_reexport.SIZE_HEADROOM  # Discord counts decimal MB
    assert clip_reexport.DEFAULT_TARGET_MB == setup.DEFAULT_TARGET_FILESIZE_MB == 20
    assert clip_reexport.video_bitrate_kbps(1, 3600) == clip_reexport.MIN_VIDEO_BITRATE_KBPS


def test_output_is_a_sibling_mp4():
    assert clip_reexport.output_path_for("/clips/Replay 2026.mkv") == "/clips/Replay 2026-small.mp4"
    assert clip_reexport.output_path_for("C:\\c\\a.mp4").endswith("a-small.mp4")


def test_command_crops_from_end_and_never_overwrites_input():
    cmd = clip_reexport.build_command("ffmpeg", "in.mp4", "out.mp4", 10, 20)
    assert cmd[cmd.index("-sseof") + 1] == "-20"
    assert cmd.index("-sseof") < cmd.index("-i")
    assert cmd[-1] == "out.mp4" and "in.mp4" != cmd[-1]
    assert "0:a:0?" in cmd  # clips without audio still work


def test_find_ffmpeg_prefers_bundled(tmp_path, monkeypatch):
    monkeypatch.setattr(clip_reexport, "_SYSTEM", "Linux")
    scripts = tmp_path / "App" / "Shared" / "Scripts"
    scripts.mkdir(parents=True)
    bundled = tmp_path / "App" / "Linux" / "ffmpeg" / "bin" / "ffmpeg"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    assert clip_reexport.find_ffmpeg(str(scripts) + os.sep) == str(bundled)


def test_reexport_failure_leaves_no_partial_file(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_text("not a video")
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    fake_ffmpeg.chmod(0o755)
    logs = []
    assert clip_reexport.reexport(str(fake_ffmpeg), str(clip), 10, 20, logs.append) is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4", "ffmpeg"]
    assert "boom" in logs[0]


def _scene(tmp_path, scripts):
    path = tmp_path / "DefaultPTM.json"
    path.write_text(json.dumps({"modules": {"scripts-tool": scripts}}))
    return path


def test_setup_saves_reexport_without_touching_bitrate(tmp_path, monkeypatch):
    scene = _scene(tmp_path, [])
    encoder = tmp_path / "recordEncoder.json"
    encoder.write_text('{"bitrate": 18000}')
    monkeypatch.setattr(setup, "SCENE_COLLECTION_JSON", str(scene))
    monkeypatch.setattr(setup.obs_config, "SCENE_COLLECTION_JSON", str(scene))
    assert setup.load_clip_reexport()["reexport_enabled"] is False
    setup.save_clip_reexport(True, 8, 25)
    assert setup.load_clip_reexport() == {"reexport_enabled": True, "target_mb": 8.0, "crop_seconds": 25}
    assert json.loads(encoder.read_text()) == {"bitrate": 18000}


def test_launch_helper_registers_new_script_once(tmp_path):
    config = tmp_path / "OBS-Config"
    (config / "basic" / "scenes").mkdir(parents=True)
    scene = config / "basic" / "scenes" / "DefaultPTM.json"
    scene.write_text(json.dumps({"modules": {"scripts-tool": [{"path": "/old/stuck_output_check.py"}]}}))
    scripts = str(tmp_path / "Scripts")
    portable_launch_helper._fix_script_paths(str(config), scripts)
    portable_launch_helper._fix_script_paths(str(config), scripts)
    entries = json.loads(scene.read_text())["modules"]["scripts-tool"]
    assert [os.path.basename(e["path"]) for e in entries] == ["stuck_output_check.py", "clip_reexport.py"]
    assert all(e["path"].startswith(scripts) for e in entries)
    assert entries[1]["settings"]["reexport_enabled"] is False


def test_validate_zip_root():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ffmpeg/bin/ffmpeg.exe", "x")
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        updater.validate_zip(zf, root="ffmpeg")
        with pytest.raises(updater.UpdateError):
            updater.validate_zip(zf)


def test_split_command_keeps_all_six_tracks_in_order_with_titles():
    titles = clip_reexport.track_titles(["Spotify", "", "Discord"])
    assert titles == ["Mix", "Mic", "Desktop", "Spotify", "Track-5", "Discord"]
    # Discord owns its slot but made no sound in this clip.
    assert clip_reexport.track_titles(["Spotify", "", "Discord"], silent={2})[3:] == ["Spotify", "Track-5", "Track-6"]
    cmd = clip_reexport.build_split_command("ffmpeg", "in.mp4", "out.mkv", titles)
    maps = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
    assert maps == ["0:v:0"] + [f"0:a:{n}" for n in range(6)]
    assert cmd[cmd.index("-c") + 1] == "copy"  # lossless
    assert cmd[cmd.index("-metadata:s:a:3") + 1] == "title=Spotify"
    assert cmd[-1] == "out.mkv"


def test_parse_max_volume():
    assert clip_reexport.parse_max_volume("[Parsed_volumedetect_0 @ 0x1] max_volume: -12.3 dB\n") == -12.3
    assert clip_reexport.parse_max_volume("max_volume: -inf dB") == float("-inf")
    assert clip_reexport.parse_max_volume("no audio") is None


def test_an_unmeasurable_track_keeps_its_label(monkeypatch):
    monkeypatch.setattr(clip_reexport.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom"))
    assert clip_reexport.silent_app_tracks("ffmpeg", "clip.mp4", ["Spotify", "", ""], lambda m: None) == set()


def test_strip_command_keeps_only_the_mix_in_the_clips_own_container():
    cmd = clip_reexport.build_strip_command("ffmpeg", "clip.mp4", "clip.mp4.partial")
    maps = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
    assert maps == ["0:v:0", "0:a:0?"]
    assert cmd[cmd.index("-f") + 1] == "mp4" and cmd[-1] == "clip.mp4.partial"
    mkv = clip_reexport.build_strip_command("ffmpeg", "clip.mkv", "clip.mkv.partial")
    assert mkv[mkv.index("-f") + 1] == "matroska" and "-movflags" not in mkv


def test_process_clip_splits_before_the_smaller_copy(monkeypatch):
    calls = []
    monkeypatch.setattr(clip_reexport, "split_tracks", lambda *a: calls.append("split"))
    monkeypatch.setattr(clip_reexport, "reexport", lambda *a: calls.append("small"))
    options = {"split_tracks": True, "app_labels": [], "reexport": True, "target_mb": 10.0, "crop_seconds": 30}
    clip_reexport.process_clip("ffmpeg", "clip.mp4", options, print)
    assert calls == ["split", "small"]
    calls.clear()
    clip_reexport.process_clip("ffmpeg", "clip.mp4", {**options, "split_tracks": False}, print)
    assert calls == ["small"]


def test_failed_split_leaves_the_clip_untouched(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_text("six tracks")
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    fake_ffmpeg.chmod(0o755)
    logs = []
    assert clip_reexport.split_tracks(str(fake_ffmpeg), str(clip), ["", "", ""], logs.append) is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4", "ffmpeg"]
    assert clip.read_text() == "six tracks"
    assert "boom" in logs[0]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_split_tracks_end_to_end_with_real_ffmpeg(tmp_path):
    """A 6-track clip like OBS's replay buffer writes: the .mkv gets all six,
    named; the clip is left with just the mix."""
    clip = tmp_path / "Replay.mp4"
    tones = []
    for n in range(6):
        # Track 6's owner ("Discord" below) isn't playing: a silent track.
        source = "anullsrc=r=48000:cl=stereo:d=1" if n == 5 else f"sine=frequency={220 * (n + 1)}:duration=1"
        tones += ["-f", "lavfi", "-i", source]
    maps = ["-map", "0:v"] + [arg for n in range(6) for arg in ("-map", f"{n + 1}:a")]
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=64x64:d=1", *tones,
         *maps, "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)],
        check=True,
    )
    logs = []
    out = clip_reexport.split_tracks("ffmpeg", str(clip), ["Spotify", "", "Discord"], logs.append)
    assert out == str(tmp_path / "Replay-tracks.mkv"), logs

    def audio_titles(path):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=title",
             "-of", "json", path], check=True, capture_output=True, text=True,
        )
        return [s.get("tags", {}).get("title") for s in json.loads(probe.stdout)["streams"]]

    assert audio_titles(out) == ["Mix", "Mic", "Desktop", "Spotify", "Track-5", "Track-6"]
    assert len(audio_titles(str(clip))) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Replay-tracks.mkv", "Replay.mp4"]


def _shipped_config(tmp_path, monkeypatch):
    profile = os.path.join(os.path.dirname(SCRIPTS_DIR), "OBS-Config", "basic")
    scene = tmp_path / "DefaultPTM.json"
    ini = tmp_path / "basic.ini"
    scene.write_text(open(os.path.join(profile, "scenes", "DefaultPTM.json"), encoding="utf-8").read())
    ini.write_text(open(os.path.join(profile, "profiles", "DefaultPTM", "basic.ini"), encoding="utf-8").read())
    monkeypatch.setattr(setup, "SCENE_COLLECTION_JSON", str(scene))
    monkeypatch.setattr(setup.obs_config, "SCENE_COLLECTION_JSON", str(scene))
    monkeypatch.setattr(setup, "BASIC_INI", str(ini))
    monkeypatch.setattr(setup.obs_config, "BASIC_INI", str(ini))
    monkeypatch.setattr(setup.obs_config, "APP_TRACKS_PATH", str(tmp_path / "app_tracks.json"))
    return scene


def _rec_tracks():
    return setup.obs_config.read_ini_section("AdvOut")["RecTracks"]


def test_split_tracks_on_routes_each_source_to_its_own_track(tmp_path, monkeypatch):
    scene = _shipped_config(tmp_path, monkeypatch)
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup.obs_config.shutil, "which", lambda name: "/usr/bin/" + name)
    assert setup.load_split_tracks() is False
    setup.save_split_tracks(True)
    setup.save_split_tracks(True)  # idempotent
    data = json.loads(scene.read_text())
    assert setup.load_split_tracks() is True and _rec_tracks() == "7"  # OBS records Mix, Mic and Desktop
    assert data["DesktopAudioDevice1"]["mixers"] == 0b101  # mix + desktop
    assert data["AuxAudioDevice1"]["mixers"] == 0b011  # mix + mic
    # The tray records the apps, so OBS has no app sources and its script stands aside.
    assert not any(s["name"].startswith("ClipEm App") for s in data["sources"])
    assert setup.obs_config.load_tray_tracks() is True


def test_split_tracks_off_restores_the_shipped_routing(tmp_path, monkeypatch):
    scene = _shipped_config(tmp_path, monkeypatch)
    shipped = json.loads(scene.read_text())
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    setup.save_split_tracks(True)
    setup.save_split_tracks(False)
    data = json.loads(scene.read_text())
    assert setup.load_split_tracks() is False and _rec_tracks() == "1"
    for config in (data, shipped):  # scene item ids are never reused, so the counter stays moved on
        next(s for s in config["sources"] if s["id"] == "scene")["settings"].pop("id_counter")
    assert data["sources"] == shipped["sources"]
    assert data["DesktopAudioDevice1"] == shipped["DesktopAudioDevice1"]


def test_split_tracks_on_mac_has_no_app_tracks(tmp_path, monkeypatch):
    scene = _shipped_config(tmp_path, monkeypatch)
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    setup.save_split_tracks(True)
    data = json.loads(scene.read_text())
    assert _rec_tracks() == "7"
    assert not any(s["name"].startswith("ClipEm App") for s in data["sources"])
    assert setup.obs_config.load_tray_tracks() is False  # clip_reexport.py splits the 3 tracks itself


def test_turning_split_tracks_on_removes_the_app_sources_older_versions_added(tmp_path, monkeypatch):
    scene = _shipped_config(tmp_path, monkeypatch)
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    data = json.loads(scene.read_text())
    scene_source = next(s for s in data["sources"] if s["id"] == "scene")
    for number, name in enumerate(setup.OLD_APP_SLOT_SOURCE_NAMES, start=90):
        data["sources"].append({"name": name, "uuid": f"u{number}", "id": "pipewire_audio_application_capture"})
        scene_source["settings"]["items"].append({"name": name, "source_uuid": f"u{number}", "id": number})
    scene.write_text(json.dumps(data))
    setup.save_split_tracks(True)
    data = json.loads(scene.read_text())
    assert not any(s["name"].startswith("ClipEm App") for s in data["sources"])
    scene_source = next(s for s in data["sources"] if s["id"] == "scene")
    assert [i["name"] for i in scene_source["settings"]["items"]] == ["Display Capture"]


def test_app_track_rules_pin_and_reset(tmp_path, monkeypatch):
    _shipped_config(tmp_path, monkeypatch)
    state = setup.obs_config.empty_app_tracks()
    first, second, third = state["tracks"]
    second["owner"] = {"exe": "spotify", "label": "Spotify", "last_heard": 5.0, "pinned": False}
    setup.obs_config.save_app_tracks(state)
    setup.set_app_track_pinned(second["id"], True)
    assert setup.load_app_tracks()["tracks"][1]["owner"]["pinned"] is True

    # Settings: a rule track in front, a bare entry (an auto track), the others kept.
    setup.save_app_track_rules([
        {"name": "Voice", "apps": ["Discord.exe", "discord", "TeamSpeak"]},
        {"id": second["id"], "apps": []},
        {"id": third["id"], "name": "ignored", "apps": []},
    ])
    tracks = setup.load_app_tracks()["tracks"]
    assert [(t["name"], t["apps"]) for t in tracks] == [("Voice", ["discord", "teamspeak"]), ("", []), ("ignored", [])]
    assert tracks[1]["owner"]["exe"] == "spotify"  # an auto track keeps its app
    assert tracks[0]["id"] not in (first["id"], second["id"], third["id"])

    setup.reset_app_tracks()
    assert all(t["owner"] is None for t in setup.load_app_tracks()["tracks"])
    assert setup.load_app_tracks()["tracks"][0]["apps"] == ["discord", "teamspeak"]  # rules stay
