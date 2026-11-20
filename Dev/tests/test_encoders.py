"""Installer/encoders.py: which hardware encoders are found, and which
ffmpeg the clip editor exports with for each."""

import subprocess

import encoders
import pytest


def _completed(stdout):
    return subprocess.CompletedProcess([], 0, stdout, "")


@pytest.mark.parametrize(("names", "expected"), [
    (["NVIDIA GeForce RTX 4070"], [encoders.NVENC]),
    (["AMD Radeon RX 7800 XT"], [encoders.AMF]),
    (["Intel(R) UHD Graphics 770", "NVIDIA GeForce GTX 1660"], [encoders.NVENC, encoders.QSV]),
    (["Radeon(TM) Graphics", "AMD Radeon RX 6600"], [encoders.AMF]),  # one entry per encoder
    (["Microsoft Basic Display Adapter"], []),
    ([], []),
])
def test_windows_offers_each_vendors_encoder_once_then_x264(monkeypatch, names, expected):
    monkeypatch.setattr(encoders, "windows_gpu_names", lambda: names)
    assert encoders.available("Windows") == [(e, None) for e in expected] + [(encoders.X264, None)]


def test_a_word_inside_another_isnt_a_vendor(monkeypatch):
    monkeypatch.setattr(encoders, "windows_gpu_names", lambda: ["Matrox G200 (Parhelia/Kati edition)"])
    assert encoders.available("Windows") == [(encoders.X264, None)]


def test_linux_offers_the_first_vaapi_node(monkeypatch):
    monkeypatch.setattr(encoders, "vaapi_devices", lambda: ["/dev/dri/renderD128", "/dev/dri/renderD129"])
    assert encoders.available("Linux") == [(encoders.VAAPI, "/dev/dri/renderD128"), (encoders.X264, None)]


def test_macos_always_has_videotoolbox():
    assert encoders.available("Darwin") == [(encoders.VIDEOTOOLBOX, None), (encoders.X264, None)]


def test_an_unknown_system_only_gets_x264():
    assert encoders.available("Plan9") == [(encoders.X264, None)]


def test_vaapi_devices_falls_back_to_vainfo_without_libva(monkeypatch):
    monkeypatch.setattr(encoders.glob, "glob", lambda _pattern: ["/dev/dri/renderD129", "/dev/dri/renderD128"])
    monkeypatch.setattr(encoders, "_vaapi_nodes_libva", lambda nodes: None)
    asked = []

    def vainfo(nodes):
        asked.append(nodes)
        return nodes[:1]

    monkeypatch.setattr(encoders, "_vaapi_nodes_vainfo", vainfo)
    assert encoders.vaapi_devices() == ["/dev/dri/renderD128"]
    assert asked == [["/dev/dri/renderD128", "/dev/dri/renderD129"]]  # in order


def test_no_render_nodes_means_no_vaapi(monkeypatch):
    monkeypatch.setattr(encoders.glob, "glob", lambda _pattern: [])
    monkeypatch.setattr(encoders, "_vaapi_nodes_libva", lambda nodes: pytest.fail("probed with no nodes"))
    assert encoders.vaapi_devices() == []


def test_vainfo_output_is_read_per_node(monkeypatch):
    monkeypatch.setattr(encoders.shutil, "which", lambda _name: "/usr/bin/vainfo")
    outputs = {
        "/dev/dri/renderD128": "VAProfileH264Main : VAEntrypointVLD\nVAProfileH264High   :\tVAEntrypointEncSlice\n",
        "/dev/dri/renderD129": "VAProfileH264Main : VAEntrypointVLD\nVAProfileHEVCMain : VAEntrypointEncSlice\n",
        "/dev/dri/renderD130": None,  # vainfo hangs
    }

    def run(cmd, **_kwargs):
        out = outputs[cmd[-1]]
        if out is None:
            raise subprocess.TimeoutExpired(cmd, 10)
        return _completed(out)

    monkeypatch.setattr(encoders.subprocess, "run", run)
    assert encoders._vaapi_nodes_vainfo(list(outputs)) == ["/dev/dri/renderD128"]


def test_without_vainfo_nothing_is_found(monkeypatch):
    monkeypatch.setattr(encoders.shutil, "which", lambda _name: None)
    assert encoders._vaapi_nodes_vainfo(["/dev/dri/renderD128"]) == []


def test_windows_gpu_names_survive_powershell_failing(monkeypatch):
    import autostart

    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("powershell", 30)

    monkeypatch.setattr(autostart, "run_powershell", fail)
    assert encoders.windows_gpu_names() == []
    monkeypatch.setattr(autostart, "run_powershell", lambda *_a, **_k: _completed("  NVIDIA RTX \r\n\r\nIntel Arc\n"))
    assert encoders.windows_gpu_names() == ["NVIDIA RTX", "Intel Arc"]


def test_encoder_settings():
    assert encoders.encoder_settings(encoders.X264, "6000") == {"bitrate": 6000, "rate_control": "CBR"}
    assert encoders.encoder_settings(encoders.VIDEOTOOLBOX, 6000)["rate_control"] == "ABR"
    assert encoders.encoder_settings(encoders.VAAPI, 6000, "/dev/dri/renderD128")["vaapi_device"] == "/dev/dri/renderD128"
    assert "vaapi_device" not in encoders.encoder_settings(encoders.NVENC, 6000, "/dev/dri/renderD128")


def test_encoder_for_obs_id_round_trips():
    for encoder, obs_id in encoders.OBS_IDS.items():
        assert encoders.encoder_for_obs_id(obs_id) == encoder
    assert encoders.encoder_for_obs_id("something_else") is None


def test_every_encoder_has_a_label_and_an_ffmpeg_name():
    assert set(encoders.LABELS) == set(encoders.OBS_IDS) == set(encoders.FFMPEG_NAMES)


def test_bundled_ffmpeg(tmp_path):
    assert encoders.bundled_ffmpeg(str(tmp_path), "Linux") is None
    exe = tmp_path / "Linux" / "ffmpeg" / "bin" / "ffmpeg"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert encoders.bundled_ffmpeg(str(tmp_path), "Linux") == str(exe)
    assert encoders.bundled_ffmpeg(str(tmp_path), "Windows") is None
    assert encoders.bundled_ffmpeg(str(tmp_path), "Darwin") is None  # macOS uses the system's


def test_ffmpeg_encoders_parses_the_list():
    out = (" V....D libx264              libx264 H.264 / AVC\n"
           " V....D h264_vaapi           H.264/AVC (VAAPI)\n"
           "Encoders:\n ------\n\n")
    names = encoders.ffmpeg_encoders("ffmpeg", run=lambda *_a, **_k: _completed(out))
    assert {"libx264", "h264_vaapi"} <= names


def test_ffmpeg_encoders_of_an_ffmpeg_that_wont_run():
    def run(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    assert encoders.ffmpeg_encoders("ffmpeg", run=run) == set()


def test_export_options_use_whichever_ffmpeg_has_the_encoder():
    lists = {"bundled": " V....D libx264  x\n", "system": " V....D libx264  x\n V....D h264_vaapi  x\n"}

    def run(cmd, **_kwargs):
        return _completed(lists[cmd[0]])

    found = [(encoders.VAAPI, "/dev/dri/renderD128"), (encoders.X264, None)]
    options = encoders.export_options("bundled", "system", found, run=run)
    assert [(o[0], o[1], o[3]) for o in options] == [
        (encoders.VAAPI, "/dev/dri/renderD128", "system"), (encoders.X264, None, "bundled")]


def test_export_options_leave_out_an_encoder_no_ffmpeg_has():
    def run(cmd, **_kwargs):
        return _completed(" V....D libx264  x\n")

    found = [(encoders.NVENC, None), (encoders.X264, None)]
    assert [o[0] for o in encoders.export_options("bundled", "bundled", found, run=run)] == [encoders.X264]
