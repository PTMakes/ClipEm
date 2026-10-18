"""clip_edit.py: probing, edit projects, export filter graphs and commands -
plus real exports with ffmpeg (CLIPEM_FFMPEG picks a specific build, e.g.
ClipEm's bundled one from Dev/dist/ffmpeg)."""

import json
import os
import shutil
import subprocess
import sys
import threading

import pytest
from conftest import TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
import clip_edit  # noqa: E402

PROBE_SPLIT = """Input #0, matroska,webm, from 'clip-tracks.mkv':
  Metadata:
    ENCODER         : Lavf62.3.100
  Duration: 00:00:10.02, start: 0.000000, bitrate: 2000 kb/s
  Stream #0:0: Video: h264 (High), yuv420p(progressive), 1920x1080 [SAR 1:1 DAR 16:9], 60 fps, 60 tbr, 1k tbn
      Metadata:
        DURATION        : 00:00:10.000000000
  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp (default)
      Metadata:
        title           : Mix
  Stream #0:2: Audio: aac (LC), 48000 Hz, stereo, fltp
      Metadata:
        title           : Mic
  Stream #0:3: Audio: aac (LC), 48000 Hz, stereo, fltp
      Metadata:
        title           : Desktop
  Stream #0:4: Audio: aac (LC), 48000 Hz, stereo, fltp
      Metadata:
        title           : Firefox
"""

PROBE_PLAIN = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip.mp4':
  Duration: 00:01:02.50, start: 0.000000, bitrate: 18000 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 2560x1440, 17800 kb/s, 59.94 fps, 59.94 tbr, 90k tbn (default)
      Metadata:
        handler_name    : VideoHandler
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 160 kb/s (default)
      Metadata:
        handler_name    : SoundHandler
"""


def _info(text=PROBE_SPLIT):
    return clip_edit.parse_probe(text)


# ---------------------------------------------------------------------------
# Probing and projects
# ---------------------------------------------------------------------------

def test_probe_reads_video_and_named_audio_tracks():
    info = _info()
    assert info["duration"] == pytest.approx(10.02)
    assert (info["width"], info["height"], info["fps"], info["video_codec"]) == (1920, 1080, 60.0, "h264")
    assert [(t["index"], t["title"], t["codec"]) for t in info["audio"]] == [
        (0, "Mix", "aac"), (1, "Mic", "aac"), (2, "Desktop", "aac"), (3, "Firefox", "aac"),
    ]
    assert clip_edit.is_split_layout(info)


def test_probe_of_a_plain_clip():
    info = _info(PROBE_PLAIN)
    assert info["duration"] == pytest.approx(62.5)
    assert (info["width"], info["height"], info["fps"]) == (2560, 1440, 59.94)
    assert [(t["index"], t["title"]) for t in info["audio"]] == [(0, None)]
    assert not clip_edit.is_split_layout(info)
    assert clip_edit.output_track_titles(info) == ["Track 1"]


def test_probe_rejects_a_file_without_video():
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "garbage.txt: Invalid data found")
    with pytest.raises(ValueError):
        clip_edit.probe("ffmpeg", "garbage.txt", run=run)


def test_default_project_keeps_everything():
    project = clip_edit.default_project(_info())
    assert project["keep"] == [[0.0, 10.02]]
    assert [t["index"] for t in project["tracks"]] == [0, 1, 2, 3]
    assert clip_edit.normalize_project(project, _info()) == project


def test_normalize_cleans_up_whatever_the_page_sends():
    project = {
        "keep": [[5, 7], [-3, 2], [6.5, 8], [9.99, 9.995], "junk", [8, None]],
        "tracks": [
            {"index": 1, "volume_db": 99, "offset": -100, "muted": 1,
             "regions": [{"start": 4, "end": 2, "db": -500}, {"start": 1, "end": 3, "db": "x"}, "junk"]},
            {"index": 42, "volume_db": 3},
        ],
    }
    clean = clip_edit.normalize_project(project, _info())
    assert clean["keep"] == [[0.0, 2.0], [5.0, 8.0]]
    mic = clean["tracks"][1]
    assert mic["volume_db"] == clip_edit.MAX_GAIN_DB and mic["offset"] == -clip_edit.MAX_OFFSET_SECONDS
    assert mic["muted"] is True
    assert mic["regions"] == [{"start": 1.0, "end": 3.0, "db": 0.0, "channel": None},
                              {"start": 2.0, "end": 4.0, "db": -60.0, "channel": None}]
    assert [t["index"] for t in clean["tracks"]] == [0, 1, 2, 3]  # no track 42
    assert clip_edit.kept_duration(clean) == pytest.approx(5.0)
    assert clean["version"] == clip_edit.PROJECT_VERSION and "mix_source" not in clean


def test_version_1_projects_that_rebuilt_the_mix_now_mute_it():
    info = _info()
    edited_mic = {"version": 1, "tracks": [{"index": 1, "volume_db": 3}]}
    assert clip_edit.normalize_project(edited_mic, info)["tracks"][0]["muted"]  # always rebuilt then
    assert clip_edit.normalize_project({**edited_mic, "mix_source": "tracks"}, info)["tracks"][0]["muted"]
    assert not clip_edit.normalize_project({**edited_mic, "mix_source": "mix"}, info)["tracks"][0]["muted"]
    assert not clip_edit.normalize_project({"version": 1}, info)["tracks"][0]["muted"]
    assert not clip_edit.normalize_project({**edited_mic, "version": 2}, info)["tracks"][0]["muted"]


def test_unique_path_never_overwrites(tmp_path):
    (tmp_path / "a-edit.mp4").write_text("")
    (tmp_path / "a-edit-2.mp4").write_text("")
    assert clip_edit.unique_path(str(tmp_path), "a-edit", ".mp4") == str(tmp_path / "a-edit-3.mp4")
    assert clip_edit.unique_path(str(tmp_path), "b-edit", ".mp4") == str(tmp_path / "b-edit.mp4")


# ---------------------------------------------------------------------------
# Filter graphs
# ---------------------------------------------------------------------------

def test_trim_only_graph_has_no_concat():
    info = _info(PROBE_PLAIN)
    project = clip_edit.default_project(info)
    project["keep"] = [[2.0, 30.0]]
    graph, vout, audio = clip_edit.build_graph(project, info, audio="mix")
    assert "concat" not in graph and "split" not in graph
    assert "trim=start=2:end=30" in graph and "atrim=start=2:end=30" in graph
    assert vout.startswith("vout") and len(audio) == 1


def test_cuts_gain_and_offset_in_a_split_clip():
    info = _info()
    project = clip_edit.default_project(info)
    project["keep"] = [[0.0, 3.0], [5.0, 10.0]]
    project["tracks"][0]["muted"] = True  # the mix is rebuilt from Mic + Desktop
    project["tracks"][0]["regions"] = [{"start": 1.0, "end": 2.0, "db": -6.0}]
    project["tracks"][1]["offset"] = 0.5
    project["tracks"][1]["regions"] = [{"start": 6.0, "end": 8.0, "db": 6.0}]
    project["tracks"][2]["offset"] = -0.25
    project["tracks"][3]["muted"] = True
    graph, _vout, audio = clip_edit.build_graph(project, info, audio="tracks")
    assert "[0:a:0]" not in graph  # the recorded mix is rebuilt, not used
    assert "adelay=delays=500:all=1" in graph and "atrim=start=0.25" in graph
    assert "volume=enable='between(t,6,8)':volume=6dB" in graph
    assert "amix=inputs=2:normalize=0" in graph
    assert "volume=enable='between(t,1,2)':volume=-6dB" not in graph
    assert "volume=0" in graph  # Firefox muted
    assert graph.count("concat=n=2:v=0:a=1") == 4  # mix, Mic, Desktop, Firefox
    assert graph.count("concat=n=2:v=1:a=0") == 1
    assert "afade=t=out:st=2.99:d=0.01" in graph and "afade=t=in:d=0.01" in graph
    assert len(audio) == 4
    assert clip_edit.output_track_titles(info) == ["Mix", "Mic", "Desktop", "Firefox"]
    _graph, _vout, mix_only = clip_edit.build_graph(project, info, audio="mix")
    assert len(mix_only) == 1


def test_split_clip_with_the_mix_on_uses_the_recorded_mix():
    info = _info()
    project = clip_edit.default_project(info)
    project["tracks"][1]["regions"] = [{"start": 6.0, "end": 8.0, "db": 6.0}]
    project["tracks"][0]["volume_db"] = -60.0  # silent, but the Mix is still what's used
    assert clip_edit.mix_inputs(project, info) == [0]
    graph, _vout, audio = clip_edit.build_graph(project, info, audio="mix")
    assert "[0:a:0]" in graph and "[0:a:1]" not in graph and "amix" not in graph
    assert "volume=0" in graph and len(audio) == 1
    graph, _vout, audio = clip_edit.build_graph(project, info, audio="tracks")
    assert "amix" not in graph and "volume=enable='between(t,6,8)':volume=6dB" in graph  # Mic's own track
    assert len(audio) == 4


def test_split_clip_with_desktop_muted_mixes_each_app():
    info = _info()
    project = clip_edit.default_project(info)
    project["tracks"][0]["muted"] = True
    project["tracks"][2]["muted"] = True
    assert clip_edit.mix_inputs(project, info) == [1, 3]  # Mic + Firefox
    graph, _vout, audio = clip_edit.build_graph(project, info, audio="mix")
    assert "[0:a:0]" not in graph and "[0:a:2]" not in graph
    assert "[0:a:1]" in graph and "[0:a:3]" in graph and "amix=inputs=2" in graph
    assert len(audio) == 1
    _graph, _vout, audio = clip_edit.build_graph(project, info, audio="tracks")
    assert len(audio) == 4  # the mix, then Mic, Desktop (muted) and Firefox
    project["tracks"][0]["muted"] = False
    assert clip_edit.mix_inputs(project, info) == [0]  # the Mix already has the apps in it


def test_what_is_heard_is_what_is_mixed():
    info = _info()
    project = clip_edit.default_project(info)
    tracks = project["tracks"]
    tracks[0]["muted"] = True
    assert clip_edit.mix_inputs(project, info) == [1, 2]
    tracks[1]["muted"] = True
    assert clip_edit.mix_inputs(project, info) == [2]
    graph, _vout, _audio = clip_edit.build_graph(project, info, audio="mix")
    assert "amix" not in graph and "[0:a:2]" in graph
    tracks[2]["muted"] = tracks[3]["muted"] = True
    assert clip_edit.mix_inputs(project, info) == [0]  # nothing on: the muted Mix, silence
    tracks[0]["muted"] = tracks[1]["muted"] = False
    assert clip_edit.mix_inputs({**project, "solo": [3, 1, 3, 99]}, info) == [1, 3]  # solo, as played
    plain = clip_edit.default_project(_info(PROBE_PLAIN))
    assert clip_edit.mix_inputs(plain, _info(PROBE_PLAIN)) == [0]


def test_level_runs_first_on_each_leveled_track_and_limits_the_mix():
    info = _info()
    project = clip_edit.default_project(info)
    project["tracks"][0]["muted"] = True  # Mic + Desktop
    project["tracks"][1]["level"] = True
    project["tracks"][1]["volume_db"] = 3
    project = clip_edit.normalize_project(project, info)
    assert project["tracks"][1]["level"] is True and project["tracks"][2]["level"] is False
    graph, _vout, _audio = clip_edit.build_graph(project, info, audio="mix")
    mic = next(line for line in graph.split(";") if line.startswith("[0:a:1]"))
    assert mic.index("loudnorm=I=-14:TP=-1.5") < mic.index("volume=3dB")  # leveled, then the user's gain
    assert graph.count("loudnorm") == 1 and "alimiter" in graph
    project["tracks"][1]["level"] = False
    assert "alimiter" not in clip_edit.build_graph(project, info, audio="mix")[0]
    project["tracks"][1].update(level=True, muted=True)  # a muted track isn't leveled
    assert "loudnorm" not in clip_edit.build_graph(project, info, audio="tracks")[0]


def test_probe_reads_channel_counts():
    assert clip_edit._channel_count(", 48000 Hz, stereo, fltp, 128 kb/s") == 2
    assert clip_edit._channel_count(", 48000 Hz, mono, fltp") == 1
    assert clip_edit._channel_count(", 48000 Hz, 5.1(side), fltp") == 6
    assert clip_edit._channel_count(", 44100 Hz, 3 channels, s16") == 3
    assert clip_edit._channel_count(" (default)") == 2  # unknown: treated as stereo


def test_mono_and_one_channel_changes():
    info = _info()
    info["audio"][3]["channels"] = 1
    project = clip_edit.default_project(info)
    project["tracks"][0]["muted"] = True
    mic = project["tracks"][1]
    mic["mono"] = True
    mic["regions"] = [{"start": 1, "end": 2, "db": -6, "channel": 0}, {"start": 3, "end": 4, "db": 3},
                      {"start": 5, "end": 6, "db": 2, "channel": True}]
    project["tracks"][3].update(mono=True, regions=[{"start": 1, "end": 2, "db": -6, "channel": 1}])
    project = clip_edit.normalize_project(project, info)
    mic = project["tracks"][1]
    assert mic["mono"] is True and [r["channel"] for r in mic["regions"]] == [0, None, None]
    assert project["tracks"][3]["mono"] is False and project["tracks"][3]["regions"][0]["channel"] is None
    assert clip_edit.uses_channels(project)
    graph, _vout, _audio = clip_edit.build_graph(project, info, audio="mix")
    lines = graph.split(";")
    split = next(line for line in lines if line.startswith("[0:a:1]"))
    assert clip_edit.MONO_PAN in split and split.index("pan=") < split.index("volume=enable")
    assert "between(t,3,4)" in split and "channelsplit=channel_layout=stereo" in split
    left = [line for line in lines if "between(t,1,2)" in line]
    assert len(left) == 1 and "channelsplit" not in left[0]  # only on the left side, after the split
    assert sum("join=inputs=2" in line for line in lines) == 1
    mic["mono"] = False
    mic["regions"] = [r for r in mic["regions"] if r["channel"] is None]
    assert not clip_edit.uses_channels({"tracks": [mic]})


def test_noise_gate_comes_after_level_and_before_mono():
    info = _info()
    project = clip_edit.default_project(info)
    project["tracks"][0]["muted"] = True
    project["tracks"][1].update(level=True, gate_db=-47.6, mono=True)
    project["tracks"][2]["gate_db"] = True  # not a number: off
    project = clip_edit.normalize_project(project, info)
    assert project["tracks"][1]["gate_db"] == -48 and project["tracks"][2]["gate_db"] is None
    assert clip_edit.normalize_project({"tracks": [{"index": 1, "gate_db": -500}]}, info)["tracks"][1]["gate_db"] == (
        clip_edit.GATE_MIN_DB)
    graph, _vout, _audio = clip_edit.build_graph(project, info, audio="mix")
    mic = next(line for line in graph.split(";") if line.startswith("[0:a:1]"))
    assert mic.index("loudnorm") < mic.index("agate=threshold=0.003981") < mic.index("pan=")
    preview = clip_edit.track_audio_command("ffmpeg", "x", 1, "aac", "o", gate_db=-48)
    assert "copy" not in preview and preview[preview.index("-c:a") + 1] == "aac"  # re-encoded with the gate
    assert "agate" in " ".join(clip_edit.peaks_command("ffmpeg", "x", 1, gate_db=-48))


def test_peaks_by_channel():
    import array
    frames = array.array("h", [32767, 0] * 40 + [0, -16384] * 40)  # left loud, then right half
    if sys.byteorder == "big":
        frames.byteswap()
    left, right = clip_edit.peaks_by_channel(frames.tobytes(), sample_rate=40, per_second=1, channels=2)
    assert left == [255, 0] and right == [0, 127]


def test_every_filter_output_is_used_exactly_once():
    import re
    for info, audio, muted in [(_info(text), audio, muted) for text in (PROBE_SPLIT, PROBE_PLAIN)
                               for audio in ("tracks", "mix", None) for muted in ((), (0,), (0, 2))]:
        project = clip_edit.default_project(info)
        for index in muted:
            if index < len(project["tracks"]):
                project["tracks"][index]["muted"] = True
        project["keep"] = [[0.0, 3.0], [4.0, 6.0], [7.0, 10.0]]
        project["tracks"][-1]["regions"] = [{"start": 1.0, "end": 2.0, "db": -6.0, "channel": 0}]
        graph, vout, outs = clip_edit.build_graph(project, info, video_height=720, fps=30, audio=audio)
        produced, consumed = [], []
        for line in graph.split(";"):
            match = re.fullmatch(r"((?:\[[^\]]+\])+)([^\[]+)((?:\[[^\]]+\])*)", line)
            assert match, line
            consumed += re.findall(r"\[([^\]]+)\]", match.group(1))
            produced += re.findall(r"\[([^\]]+)\]", match.group(3))
        internal = [label for label in consumed if not label.startswith("0:")]
        assert sorted(internal + [vout] + outs) == sorted(produced)
        assert len(set(consumed)) == len(consumed)


# ---------------------------------------------------------------------------
# Export settings
# ---------------------------------------------------------------------------

def test_discord_size_and_resolution():
    info = _info()
    short = clip_edit.discord_settings(info, 20)
    total_bytes = (short["video_kbps"] + short["audio_kbps"]) * 1000 / 8 * 20
    assert total_bytes <= 20_000_000 * clip_edit.SIZE_HEADROOM
    assert short["height"] == 1080 and short["fps"] <= 30 and not short["too_long"]
    medium = clip_edit.discord_settings(info, 90)
    assert medium["height"] == 1080 and medium["video_kbps"] < short["video_kbps"]
    long = clip_edit.discord_settings(info, 900)
    assert long["too_long"] and long["height"] == 1080
    assert long["video_kbps"] == clip_edit.DISCORD_MIN_VIDEO_KBPS
    low = clip_edit.discord_settings(_info(PROBE_PLAIN.replace("2560x1440", "854x480")), 20)
    assert low["height"] == 480  # never scaled up


def test_custom_settings_only_allow_offered_options():
    info = _info()
    options = clip_edit.custom_options(info)
    assert options["heights"] == [1080, 720, 480]  # never above the source
    assert all(f <= info["fps"] + 0.5 for f in options["fps"])
    settings = clip_edit.custom_settings(info, 720, None, 8000)
    assert settings["height"] == 720 and settings["fps"] == info["fps"] and settings["video_kbps"] == 8000
    source = clip_edit.custom_settings(info, None, None, 4000)
    assert source["height"] == info["height"]
    for bad in [(1440, None, 8000), (None, 7, 8000), (None, None, 1234)]:
        with pytest.raises(ValueError):
            clip_edit.custom_settings(info, *bad)
    steps = clip_edit.two_pass_commands("ffmpeg", "in.mp4", clip_edit.default_project(info), info, settings,
                                        "out.mp4", "pass")
    assert "8000k" in steps[1][0] and any("scale=-2:720" in arg for arg in steps[1][0])


def test_full_quality_writes_mkv_then_mp4_for_multitrack():
    info = _info()
    project = clip_edit.default_project(info)
    steps = clip_edit.full_quality_commands("ffmpeg", "in.mkv", project, info, "out.mkv", "out.mp4")
    assert [label for _cmd, label, _w in steps] == ["Encoding", "Saving MP4"]
    encode = steps[0][0]
    assert encode[-3:] == ["-f", "matroska", "out.mkv"] and "-crf" in encode
    assert "title=Firefox" in encode
    assert steps[1][0][-3:] == ["-f", "mp4", "out.mp4"] and "copy" in steps[1][0]
    single = clip_edit.full_quality_commands("ffmpeg", "in.mp4", clip_edit.default_project(_info(PROBE_PLAIN)),
                                             _info(PROBE_PLAIN), "out.mkv", "out.mp4")
    assert len(single) == 1 and single[0][0][-1] == "out.mp4"


def test_progress_lines():
    assert clip_edit.parse_progress_seconds("out_time_us=2500000\n") == 2.5
    assert clip_edit.parse_progress_seconds("out_time_ms=1000000") == 1.0
    assert clip_edit.parse_progress_seconds("out_time_us=N/A") is None
    assert clip_edit.parse_progress_seconds("frame=12") is None


def test_peaks_from_pcm():
    import array
    samples = array.array("h", [0, 100, -32767, 5] * 10 + [16384] * 40)
    if sys.byteorder == "big":
        samples.byteswap()
    peaks = clip_edit.peaks_from_pcm(samples.tobytes(), sample_rate=40, per_second=1)
    assert peaks == [255, 127]


# ---------------------------------------------------------------------------
# The export job
# ---------------------------------------------------------------------------

class FakeProc:
    def __init__(self, lines, returncode=0, stderr="", hold=None):
        self._lines, self.returncode_final, self._stderr, self._hold = lines, returncode, stderr, hold
        self.returncode = None
        self.stderr = self
        self.stdout = iter(self._gen())
        self.terminated = False

    def _gen(self):
        yield from self._lines
        if self._hold:
            self._hold.wait(5)

    def read(self):
        return self._stderr

    def poll(self):
        return self.returncode

    def wait(self):
        self.returncode = -15 if self.terminated else self.returncode_final
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self._hold:
            self._hold.set()


def _wait(job):
    for _ in range(250):
        if not job.running():
            return
        threading.Event().wait(0.02)


def test_export_job_renames_outputs_only_when_everything_worked(tmp_path):
    partial, final = tmp_path / ".x.partial", tmp_path / "x-edit.mp4"

    def popen(cmd, **kwargs):
        partial.write_text("video")
        return FakeProc(["out_time_us=5000000\n", "progress=end\n"])

    job = clip_edit.ExportJob(popen=popen)
    assert job.start([(["ffmpeg"], "Encoding", 1.0)], {str(partial): str(final)}, total_seconds=10)
    _wait(job)
    assert job.status["state"] == "done" and job.status["files"] == ["x-edit.mp4"]
    assert final.read_text() == "video" and not partial.exists()


def test_export_job_reports_ffmpeg_errors_and_cleans_up(tmp_path):
    partial, final = tmp_path / ".x.partial", tmp_path / "x-edit.mp4"

    def popen(cmd, **kwargs):
        partial.write_text("half")
        return FakeProc([], returncode=1, stderr="Invalid argument")

    job = clip_edit.ExportJob(popen=popen)
    job.start([(["ffmpeg"], "Encoding", 1.0)], {str(partial): str(final)}, total_seconds=10)
    _wait(job)
    assert job.status["state"] == "error" and "Invalid argument" in job.status["message"]
    assert not partial.exists() and not final.exists()


def test_export_job_cancel(tmp_path):
    hold = threading.Event()
    procs = []

    def popen(cmd, **kwargs):
        procs.append(FakeProc(["out_time_us=1000000\n"], hold=hold))
        return procs[-1]

    job = clip_edit.ExportJob(popen=popen)
    job.start([(["a"], "Analysing", 0.5), (["b"], "Encoding", 0.5)], {}, total_seconds=10)
    assert not job.start([(["c"], "x", 1.0)], {}, 1)  # one at a time
    for _ in range(100):
        if procs:
            break
        threading.Event().wait(0.02)
    job.cancel()
    _wait(job)
    assert job.status["state"] == "cancelled" and len(procs) == 1 and procs[0].terminated


# ---------------------------------------------------------------------------
# Real exports
# ---------------------------------------------------------------------------

FFMPEG = os.environ.get("CLIPEM_FFMPEG") or shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
TITLES = ("Mix", "Mic", "Desktop", "Firefox", "Track 5 (unused)", "Track 6 (unused)")


@pytest.fixture(scope="module")
def tracks_clip(tmp_path_factory):
    """A 10 s 720p60 clip with 6 named sine-tone tracks, like a -tracks.mkv.
    Made with the system ffmpeg (ClipEm's build has no lavfi or test sources)."""
    maker = shutil.which("ffmpeg")
    if not maker:
        pytest.skip("needs a system ffmpeg to make the test clip")
    path = tmp_path_factory.mktemp("clip") / "clip-tracks.mkv"
    cmd = [maker, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=60:d=10"]
    for number in range(6):
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={220 + 110 * number}:duration=10:sample_rate=48000"]
    cmd += ["-map", "0:v"] + [arg for n in range(1, 7) for arg in ("-map", f"{n}:a")]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "2"]
    for number, title in enumerate(TITLES):
        cmd += [f"-metadata:s:a:{number}", f"title={title}"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def _run(steps):
    for cmd, _label, _weight in steps:
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


@needs_ffmpeg
def test_real_full_quality_export(tracks_clip, tmp_path):
    missing = clip_edit.missing_features(FFMPEG)
    if missing:
        pytest.skip(f"this ffmpeg lacks {missing}")
    info = clip_edit.probe(FFMPEG, str(tracks_clip))
    assert [t["title"] for t in info["audio"]] == list(TITLES)
    project = clip_edit.default_project(info)
    project["keep"] = [[0.0, 3.0], [5.0, 10.0]]
    project["tracks"][1]["regions"] = [{"start": 6, "end": 8, "db": 6}]
    project["tracks"][2]["offset"] = 0.5
    project = clip_edit.normalize_project(project, info)
    mkv, mp4 = tmp_path / "out.mkv", tmp_path / "out.mp4"
    _run(clip_edit.full_quality_commands(FFMPEG, str(tracks_clip), project, info, str(mkv), str(mp4)))
    out = clip_edit.probe(FFMPEG, str(mkv))
    assert out["duration"] == pytest.approx(8.0, abs=0.1)
    assert [t["title"] for t in out["audio"]] == ["Mix", "Mic", "Desktop", "Firefox",
                                                  "Track 5 (unused)", "Track 6 (unused)"]
    mp4_info = clip_edit.probe(FFMPEG, str(mp4))
    assert len(mp4_info["audio"]) == 1 and mp4_info["duration"] == pytest.approx(8.0, abs=0.1)


@needs_ffmpeg
def test_real_discord_export_fits(tracks_clip, tmp_path):
    if clip_edit.missing_features(FFMPEG):
        pytest.skip("this ffmpeg lacks the editor's filters")
    info = clip_edit.probe(FFMPEG, str(tracks_clip))
    project = clip_edit.default_project(info)
    out = tmp_path / "out.mp4"
    _run(clip_edit.discord_commands(FFMPEG, str(tracks_clip), project, info, str(out), str(tmp_path / "pass")))
    assert 0 < out.stat().st_size <= 20_000_000
    assert len(clip_edit.probe(FFMPEG, str(out))["audio"]) == 1


@needs_ffmpeg
def test_real_preview_audio_and_peaks(tracks_clip, tmp_path):
    info = clip_edit.probe(FFMPEG, str(tracks_clip))
    m4a = clip_edit.track_audio(FFMPEG, str(tracks_clip), info, 3, str(tmp_path / "t3.m4a"))
    assert os.path.getsize(m4a) > 1000
    if "s16le" in clip_edit.missing_features(FFMPEG):
        pytest.skip("this ffmpeg can't write raw PCM")
    data = clip_edit.track_peaks(FFMPEG, str(tracks_clip), 1, str(tmp_path / "p1.json"))
    assert data["per_second"] == clip_edit.PEAKS_PER_SECOND
    assert abs(len(data["peaks"]) - 10 * clip_edit.PEAKS_PER_SECOND) <= 2
    assert max(data["peaks"]) > 20
    assert json.loads((tmp_path / "p1.json").read_text()) == data


def _integrated_lufs(path):
    result = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
                             "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
    return float(result.stderr.rsplit("I:", 1)[1].split("LUFS")[0])


@needs_ffmpeg
def test_real_level_reaches_the_target(tracks_clip, tmp_path):
    found = clip_edit.ffmpeg_features(FFMPEG)
    if not all(name in found for name in clip_edit.LEVEL_FILTERS) or clip_edit.missing_features(FFMPEG, found=found):
        pytest.skip("this ffmpeg can't level audio")
    info = clip_edit.probe(FFMPEG, str(tracks_clip))
    m4a = clip_edit.track_audio(FFMPEG, str(tracks_clip), info, 1, str(tmp_path / "t1.m4a"), level=True)
    assert _integrated_lufs(m4a) == pytest.approx(clip_edit.LEVEL_TARGET_LUFS, abs=1.0)
    project = clip_edit.default_project(info)
    project["tracks"][0]["muted"] = True
    project["tracks"][1]["level"] = project["tracks"][2]["level"] = True
    mkv, mp4 = tmp_path / "out.mkv", tmp_path / "out.mp4"
    _run(clip_edit.full_quality_commands(FFMPEG, str(tracks_clip), project, info, str(mkv), str(mp4)))
    assert clip_edit.probe(FFMPEG, str(mp4))["duration"] == pytest.approx(10.0, abs=0.1)


@needs_ffmpeg
def test_real_mono_and_left_only_export(tracks_clip, tmp_path):
    found = clip_edit.ffmpeg_features(FFMPEG)
    if not all(name in found for name in clip_edit.CHANNEL_FILTERS) or clip_edit.missing_features(FFMPEG, found=found):
        pytest.skip("this ffmpeg can't split channels")
    info = clip_edit.probe(FFMPEG, str(tracks_clip))
    assert all(track["channels"] == 2 for track in info["audio"])
    project = clip_edit.default_project(info)
    project["tracks"][0]["muted"] = True
    project["tracks"][1]["mono"] = True
    project["tracks"][2]["regions"] = [{"start": 2, "end": 4, "db": -60, "channel": 0}]
    project = clip_edit.normalize_project(project, info)
    mkv, mp4 = tmp_path / "out.mkv", tmp_path / "out.mp4"
    _run(clip_edit.full_quality_commands(FFMPEG, str(tracks_clip), project, info, str(mkv), str(mp4)))
    assert clip_edit.probe(FFMPEG, str(mp4))["audio"][0]["channels"] == 2
    data = clip_edit.track_peaks(FFMPEG, str(mkv), 2, str(tmp_path / "desktop.json"), channels=2)
    left, right = data["channels"]
    per = data["per_second"]
    assert max(left[int(2.5 * per):int(3.5 * per)]) < 5 < min(right[int(2.5 * per):int(3.5 * per)])


@needs_ffmpeg
def test_real_noise_gate_turns_down_the_quiet_part(tmp_path):
    if "agate" not in clip_edit.ffmpeg_features(FFMPEG):
        pytest.skip("this ffmpeg has no noise gate")
    maker = shutil.which("ffmpeg")
    clip = tmp_path / "speech.mkv"
    # 2 s of hiss (about -34 dB), then 2 s of a loud tone.
    subprocess.run([maker, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "anoisesrc=d=2:a=0.02:r=48000[n];aevalsrc=0.5*sin(2*PI*440*t):d=2:s=48000[t];[n][t]concat=n=2:v=0:a=1",
                    "-ac", "2", "-c:a", "pcm_s16le", str(clip)], check=True)
    plain = clip_edit.track_peaks(FFMPEG, str(clip), 0, str(tmp_path / "plain.json"))["peaks"]
    gated = clip_edit.track_peaks(FFMPEG, str(clip), 0, str(tmp_path / "gated.json"), gate_db=-25)["peaks"]
    per = clip_edit.PEAKS_PER_SECOND
    assert max(gated[per // 2:per * 3 // 2]) < max(plain[per // 2:per * 3 // 2]) / 4  # the hiss, turned down
    assert max(gated[per * 5 // 2:per * 7 // 2]) >= max(plain[per * 5 // 2:per * 7 // 2]) * 0.9  # the tone, kept
