"""Everything ClipEm asks ffmpeg for when it finishes clips, makes thumbnails
and measures tracks is in ClipEm's own ffmpeg builds (Dev/ffmpeg_build.py):
filters, muxers and encoders. test_clip_edit.py checks the editor's exports
the same way."""

import subprocess
import sys

import pytest
from conftest import SCRIPTS_DIR, TRAY_WIDGET_DIR

import ffmpeg_build

sys.path.insert(0, TRAY_WIDGET_DIR)
sys.path.insert(1, SCRIPTS_DIR)
import clip_browser  # noqa: E402
import clip_edit  # noqa: E402
import clip_post  # noqa: E402
import clip_tracks  # noqa: E402
import obs_config  # noqa: E402
import tray_tracks  # noqa: E402

TITLES = ["Mix", "Mic", "Desktop", "Game"]
# What OBS records clips as with ClipEm's profile (hybrid MP4), and every
# other format it can be set to in OBS: a clip is cut and stripped in its own.
CONTAINERS = (".mp4", ".mkv", ".mov", ".flv", ".ts")


def _commands(tmp_path):
    commands = []
    for ext in CONTAINERS:
        clip, save = "clip" + ext, "save" + ext
        commands += [
            clip_tracks.build_command("ffmpeg", clip, "small.mp4", 20, 30),
            clip_tracks.track_pcm_command("ffmpeg", clip, 3),
            clip_tracks.build_split_command("ffmpeg", clip, "t.mkv", TITLES),
            clip_tracks.build_strip_command("ffmpeg", clip, clip + ".partial"),
            clip_tracks.build_tray_split_command("ffmpeg", clip, "t.mkv", TITLES, ["g.mov"]),
            clip_post.build_cut_command("ffmpeg", save, clip + ".partial", 1.0, 31.0, clip),
            clip_post.build_extra_command("ffmpeg", save, "extra.mkv", 3, 30.0, TITLES[:3], [("Game", "g.mov")],
                                          {1: ("cancel.mov", 480)}),
            clip_post.track_pcm_command("ffmpeg", save, 1),
            tray_tracks.desktop_pcm_command("ffmpeg", clip),
            clip_edit.track_audio_command("ffmpeg", clip, 1, "opus", "t.m4a", level=True, gate_db=-48),
            clip_edit.peaks_command("ffmpeg", clip, 1, level=True, gate_db=-48),
        ]
    # Settings' and Setup's "Add sound..." (any sound file into the clip sound list).
    commands.append(obs_config.clip_sound_convert_command("ffmpeg", "horn.mp3", "horn.raw"))
    ran = []

    def run(cmd, **_kwargs):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "")

    clip_browser.Thumbnailer(ffmpeg="ffmpeg", cache_dir=str(tmp_path), run=run).thumbnail(str(tmp_path / "clip.mp4"))
    assert ran, "no thumbnail command"
    return commands + ran


def test_finishing_clips_only_asks_for_what_clipems_ffmpeg_has(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"x")
    missing, asked = ffmpeg_build.missing(_commands(tmp_path))
    # It saw what's there to see.
    assert {"adelay", "amix", "scale", "loudnorm", "agate"} <= asked["filter"]
    assert {"mp4", "matroska", "mov", "flv", "mpegts", "s16le", "image2"} <= asked["muxer"]
    assert {"libx264", "aac", "mjpeg"} <= asked["encoder"]
    assert not missing, f"not in ClipEm's ffmpeg builds: {missing}"


def test_what_a_command_asks_for():
    used = ffmpeg_build.used(["ffmpeg", "-f", "lavfi", "-i", "x", "-vf", "scale=-2:'min(720,trunc(ih/2)*2)',fps=30",
                              "-c:v", "libx264", "-c:a:1", "aac", "-c", "copy", "-f", "flv", "o.flv"])
    assert used == {"filter": {"scale", "fps"}, "muxer": {"flv"}, "encoder": {"libx264", "aac"}, "decoder": set()}
    assert ffmpeg_build.used(["ffmpeg", "-i", "c.mp4", "-frames:v", "1", "t.jpg"])["muxer"] == {"image2"}


@pytest.mark.parametrize("name", ["fps", "volumedetect"])
def test_what_once_went_wrong_isnt_built(name):
    # The checks above only mean something while these stay out of the build.
    assert name not in ffmpeg_build.built()["filter"]


def test_every_container_obs_records_in_is_built():
    built = ffmpeg_build.built()
    assert set(clip_tracks.CONTAINERS.values()) | {"mp4"} <= built["muxer"]
    assert "libdav1d" in built["decoder"]  # AV1 recordings (FFmpeg's own av1 decoder is hardware-only)
