"""Is there sound? One-off level checks for the audio check (mic_check.py),
made only when OBS's Mic or Desktop audio has gone silent - to tell a broken
capture from a quiet room or a quiet PC.

- device_peak(device_id, flow): the loudest sample a recording device (a mic,
  or an output's monitor) delivers over PROBE_SECONDS, straight from the OS -
  not through OBS. 0.0 is digital silence: a live mic always has some noise,
  so 0 means it isn't delivering audio at all.
- heard_apps(output_device): the apps playing sound right now that OBS's
  Desktop audio should hear: on the current output (the default one) or the
  one Desktop audio records. Each app's own stream is measured
  (app_audio.py lists them): an app with a stream open isn't necessarily
  making any sound.

Linux: a short `parec` of the device or of the app's stream (as
app_recorder.py does, for PROBE_SECONDS). Windows: the endpoint's or the
app's audio session's peak meter (pycaw's IAudioMeterInformation) - what the
Volume Mixer's bars show. Elsewhere: unknown (None / no apps).

Privacy: only the loudest level of each probe is kept, in memory. The audio
itself is read into memory, measured and dropped.
"""

import array
import logging
import platform
import subprocess
import threading

import app_audio

log = logging.getLogger("tray")

PROBE_SECONDS = 0.5
RATE = 48000
# An app's sound this loud (about -50 dBFS) or louder counts as playing.
HEARD_LEVEL = 10 ** (-50 / 20)
MAX_PROBED_APPS = 3  # each probe takes PROBE_SECONDS on the tray's poll loop
DEFAULT_DEVICE = "default"

_SYSTEM = platform.system()


def pcm_peak(pcm):
    """The loudest sample of 16-bit PCM, 0-1."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    return max(max(samples), -min(samples)) / 32768


# ---------------------------------------------------------------------------
# Linux: parec
# ---------------------------------------------------------------------------

def parec_command(args):
    return ["parec", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=2", "--latency-msec=50",
            "--client-name=ClipEm", "--stream-name=ClipEm level check", *args]


def parec_peak(args, seconds=PROBE_SECONDS, popen=subprocess.Popen):
    """The peak of what parec records with these arguments, or None if it
    recorded nothing at all (no such device, a stream that's not playing)."""
    try:
        proc = popen(parec_command(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    # parec delivers in real time; a stream with nothing coming never would.
    timer = threading.Timer(seconds + 1.0, proc.kill)
    timer.start()
    try:
        data = proc.stdout.read(int(seconds * RATE) * 4)
    finally:
        timer.cancel()
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    return pcm_peak(data) if data else None


def linux_default_monitor(run=subprocess.run):
    """The monitor source of the default output (what "default" Desktop
    audio records), or None."""
    try:
        result = run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5)
        sink = result.stdout.strip() if result.returncode == 0 else ""
        if not sink:
            result = run(["pactl", "info"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    sink = line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{sink}.monitor" if sink else None


def _linux_device_peak(device_id, flow, probe=parec_peak):
    if not device_id or device_id == DEFAULT_DEVICE:
        device_id = "@DEFAULT_SOURCE@" if flow == "input" else "@DEFAULT_MONITOR@"
    return probe(["-d", device_id])


def _linux_heard_apps(output_device, list_streams, probe=parec_peak, default_monitor=linux_default_monitor):
    current = {default_monitor()}
    if output_device and output_device != DEFAULT_DEVICE:
        current.add(output_device)
    heard, probed = [], 0
    for stream in list_streams():
        if stream.corked or stream.label in heard or probed >= MAX_PROBED_APPS:
            continue
        _kind, index, monitor = stream.handle
        if monitor is not None and monitor not in current:
            continue  # playing somewhere Desktop audio isn't meant to hear
        probed += 1
        args = [f"--monitor-stream={index}"] + (["-d", monitor] if monitor else [])
        peak = probe(args)
        if peak is not None and peak >= HEARD_LEVEL:
            heard.append(stream.label)
    return heard


# ---------------------------------------------------------------------------
# Windows: WASAPI peak meters
# ---------------------------------------------------------------------------

_com = threading.local()


def _com_init():
    import comtypes

    if not getattr(_com, "done", False):
        comtypes.CoInitialize()
        _com.done = True


def _windows_device_peak(device_id, flow):
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

    _com_init()
    if not device_id or device_id == DEFAULT_DEVICE:
        device = AudioUtilities.GetMicrophone() if flow == "input" else AudioUtilities.GetSpeakers()
    else:
        device = AudioUtilities.GetDeviceEnumerator().GetDevice(device_id)
    if device is None:
        return None
    device = getattr(device, "_dev", device)  # newer pycaw wraps the IMMDevice
    meter = cast(device.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None), POINTER(IAudioMeterInformation))
    return float(meter.GetPeakValue())


def _windows_heard_apps(output_device):
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

    _com_init()
    heard = []
    for session in AudioUtilities.GetAllSessions():  # the default output's sessions
        try:
            process = session.Process
            if process is None:
                continue
            exe = process.name()
            label = session.DisplayName
            if not label or label.startswith("@"):
                label = exe.rsplit(".", 1)[0]
            if app_audio.is_ignored(exe, label) or label in heard:
                continue
            peak = session._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue()
        except Exception:
            continue  # e.g. the process just exited
        if peak >= HEARD_LEVEL:
            heard.append(label)
    return heard


# ---------------------------------------------------------------------------

def device_peak(device_id, flow, system=None):
    """The loudest level (0-1) the device delivers right now, or None if
    that can't be told. flow: "input" (a mic) or "output" (what an output
    plays, from its monitor)."""
    system = system or _SYSTEM
    try:
        if system == "Linux":
            return _linux_device_peak(device_id, flow)
        if system == "Windows":
            return _windows_device_peak(device_id, flow)
    except Exception as exc:
        log.info(f"Couldn't measure the {flow} device '{device_id}': {exc}")
    return None


def heard_apps(output_device, system=None):
    """Labels of the apps making sound right now on the current output (or
    on output_device, what Desktop audio records). Empty if none - or if it
    can't be told here."""
    system = system or _SYSTEM
    try:
        if system == "Linux":
            return _linux_heard_apps(output_device, app_audio.list_audio_streams)
        if system == "Windows":
            return _windows_heard_apps(output_device)
    except Exception as exc:
        log.info(f"Couldn't tell which apps are playing sound: {exc}")
    return []
