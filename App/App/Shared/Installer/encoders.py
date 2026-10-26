"""The video encoders ClipEm offers for recording (and so the replay buffer):
x264 on the CPU everywhere, plus whichever hardware encoder this PC has -
which costs games next to nothing, where x264 at 1080p60 takes a real share
of the CPU.

OBS (Advanced output mode) records with AdvOut.RecEncoder and the settings
in recordEncoder.json. An encoder OBS can't open there doesn't fall back to
anything: recording and the replay buffer just fail to start. So a hardware
encoder is only offered when it's found here, and run.py checks again
before each start (a GPU can be swapped or its driver removed) - falling
back to x264 if it's gone (see ensure_available).

Found by:
- Linux: VA-API (AMD, Intel, and NVIDIA through nvidia-vaapi-driver) - each
  /dev/dri render node is asked through libva whether it can encode H.264.
  ClipEm's own Linux OBS is built without NVENC (Dev/build/
  build-obs-from-source-for-linux.sh), so there's nothing else.
- Windows: the GPUs Windows lists - NVIDIA (NVENC), AMD (AMF), Intel
  (QuickSync).
- macOS: VideoToolbox, which every Mac OBS supports has.
"""

import ctypes
import ctypes.util
import glob
import os
import platform
import re
import shutil
import subprocess

X264, NVENC, AMF, QSV, VAAPI, VIDEOTOOLBOX = "x264", "nvenc", "amf", "qsv", "vaapi", "videotoolbox"
DEFAULT_ENCODER = X264

# OBS 32's encoder ids (RecEncoder), and what Settings and Setup call them.
OBS_IDS = {
    X264: "obs_x264",
    NVENC: "obs_nvenc_h264_tex",
    AMF: "h264_texture_amf",
    QSV: "obs_qsv11_v2",
    VAAPI: "ffmpeg_vaapi_tex",
    VIDEOTOOLBOX: "com.apple.videotoolbox.videoencoder.ave.avc",
}
LABELS = {
    X264: "Software (x264) - uses the CPU",
    NVENC: "NVIDIA NVENC (graphics card)",
    AMF: "AMD AMF (graphics card)",
    QSV: "Intel QuickSync (graphics)",
    VAAPI: "VA-API (graphics card)",
    VIDEOTOOLBOX: "Apple VideoToolbox (hardware)",
}
# Windows GPU names (Win32_VideoController) -> the encoder each vendor's have.
WINDOWS_VENDORS = ((re.compile(r"nvidia|geforce|quadro|rtx", re.I), NVENC),
                   (re.compile(r"\bamd\b|radeon|\bati\b", re.I), AMF),
                   (re.compile(r"intel", re.I), QSV))

# libva (va/va.h): H.264 profiles, and the entrypoints that mean "can encode".
VA_PROFILES_H264 = (7, 6, 13)  # High, Main, ConstrainedBaseline
VA_ENTRYPOINTS_ENCODE = (6, 8)  # EncSlice, EncSliceLP


def encoder_settings(encoder, bitrate, device=None):
    """recordEncoder.json for `encoder` at `bitrate` kbps (constant bitrate,
    so clip sizes are predictable - VideoToolbox's closest is ABR)."""
    settings = {"bitrate": int(bitrate), "rate_control": "ABR" if encoder == VIDEOTOOLBOX else "CBR"}
    if encoder == VAAPI and device:
        settings["vaapi_device"] = device
    return settings


def _vaapi_can_encode_h264(node, libva, libva_drm):
    fd = os.open(node, os.O_RDWR)
    try:
        display = libva_drm.vaGetDisplayDRM(fd)
        if not display:
            return False
        major, minor = ctypes.c_int(), ctypes.c_int()
        if libva.vaInitialize(display, ctypes.byref(major), ctypes.byref(minor)) != 0:
            return False
        try:
            entrypoints = (ctypes.c_int * 32)()
            count = ctypes.c_int()
            for profile in VA_PROFILES_H264:
                if libva.vaQueryConfigEntrypoints(display, profile, entrypoints, ctypes.byref(count)) == 0:
                    if any(entrypoints[i] in VA_ENTRYPOINTS_ENCODE for i in range(count.value)):
                        return True
            return False
        finally:
            libva.vaTerminate(display)
    finally:
        os.close(fd)


def _vaapi_nodes_libva(nodes):
    try:
        libva = ctypes.CDLL(ctypes.util.find_library("va") or "libva.so.2")
        libva_drm = ctypes.CDLL(ctypes.util.find_library("va-drm") or "libva-drm.so.2")
    except OSError:
        return None
    libva_drm.vaGetDisplayDRM.restype = ctypes.c_void_p
    libva_drm.vaGetDisplayDRM.argtypes = [ctypes.c_int]
    libva.vaInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
    libva.vaTerminate.argtypes = [ctypes.c_void_p]
    libva.vaQueryConfigEntrypoints.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
                                               ctypes.POINTER(ctypes.c_int)]
    found = []
    for node in nodes:
        try:
            if _vaapi_can_encode_h264(node, libva, libva_drm):
                found.append(node)
        except OSError:
            pass
    return found


def _vaapi_nodes_vainfo(nodes):
    """The same, through vainfo's output, when libva can't be loaded here."""
    if not shutil.which("vainfo"):
        return []
    found = []
    for node in nodes:
        try:
            out = subprocess.run(["vainfo", "--display", "drm", "--device", node], capture_output=True,
                                 text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if re.search(r"VAProfileH264\w*\s*:\s*VAEntrypointEncSlice", out):
            found.append(node)
    return found


def vaapi_devices():
    """The render nodes that can encode H.264 through VA-API, in order."""
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    if not nodes:
        return []
    found = _vaapi_nodes_libva(nodes)
    return found if found is not None else _vaapi_nodes_vainfo(nodes)


def windows_gpu_names():
    script = "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"
    try:
        from autostart import run_powershell

        result = run_powershell(script, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def available(system=None):
    """[(encoder, device or None)] this PC can record with, x264 last - the
    first is the one to pick by default."""
    system = system or platform.system()
    found = []
    if system == "Linux":
        found += [(VAAPI, node) for node in vaapi_devices()[:1]]
    elif system == "Windows":
        names = windows_gpu_names()
        for pattern, encoder in WINDOWS_VENDORS:
            if any(pattern.search(name) for name in names) and (encoder, None) not in found:
                found.append((encoder, None))
    elif system == "Darwin":
        found.append((VIDEOTOOLBOX, None))
    return found + [(X264, None)]


def encoder_for_obs_id(obs_id):
    return next((encoder for encoder, known in OBS_IDS.items() if known == obs_id), None)
