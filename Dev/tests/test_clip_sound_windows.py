"""The clip sound on real Windows audio: what comes out of the speakers,
read from Windows' own peak meter. Each sound plays in a child process with
nothing else in it - like the Settings dialog, whose thread hasn't set up
COM - while this one listens. The VM's speakers say nothing otherwise."""

import array
import ctypes
import subprocess
import sys
import textwrap
import time
import uuid
import wave

import pytest
from conftest import INSTALLER_DIR, SCRIPTS_DIR

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows' sound")


def _tone(path, seconds):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1), f.setsampwidth(2), f.setframerate(8000)
        f.writeframes(array.array("h", [((i // 20) % 2) * 20000 - 10000 for i in range(int(8000 * seconds))]).tobytes())
    return str(path)


class _Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]

    def __init__(self, text):
        super().__init__()
        ctypes.memmove(self.data, uuid.UUID(text).bytes_le, 16)


def _call(obj, index, *argtypes):
    """obj's COM method number index."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])


@pytest.fixture
def speakers():
    """A function giving the loudest the default speakers got over the next
    `seconds` (0 when silent)."""
    from ctypes import POINTER, byref, c_float, c_int, c_void_p, wintypes

    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitialize(None)
    enumerator, device, meter = c_void_p(), c_void_p(), c_void_p()
    try:
        ole32.CoCreateInstance(byref(_Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")), None, 1,  # MMDeviceEnumerator
                               byref(_Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")), byref(enumerator))
        _call(enumerator, 4, c_int, c_int, POINTER(c_void_p))(enumerator, 0, 1, byref(device))  # default speakers
        _call(device, 3, POINTER(_Guid), wintypes.DWORD, c_void_p, POINTER(c_void_p))(  # Activate
            device, byref(_Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064")), 23, None, byref(meter))
    except OSError as exc:
        pytest.skip(f"no speakers to listen to ({exc})")
    get_peak = _call(meter, 3, POINTER(c_float))

    def loudest(seconds):
        end, peak, value = time.monotonic() + seconds, 0.0, c_float()
        while time.monotonic() < end:
            get_peak(meter, byref(value))
            peak = max(peak, value.value)
            time.sleep(0.02)
        return peak

    if loudest(0.3):
        pytest.skip("something else is playing")
    return loudest


def _child(code):
    """Runs code in a new Python with ClipEm's modules, its output a line at a time."""
    setup = f"import sys, time\nsys.path[:0] = [{INSTALLER_DIR!r}, {SCRIPTS_DIR!r}]\n"
    return subprocess.Popen([sys.executable, "-c", setup + textwrap.dedent(code)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _expect(child, word):
    line = child.stdout.readline().split()
    if not line or line[0] != word:
        child.kill()
        pytest.fail(f"expected {word!r}, got {line!r}: {child.stderr.read()}")
    return line[1:]


def _audible(speakers, child):
    if not speakers(0.4):
        child.kill()
        pytest.skip("nothing came out of the speakers")


def test_stop_silences_a_preview_at_once(tmp_path, speakers):
    child = _child(f"""
        import obs_config
        obs_config.play_clip_sound(0.1, {_tone(tmp_path / "long.wav", 4)!r})
        print("playing", flush=True)
        time.sleep(0.6)
        start = time.monotonic()
        obs_config.stop_clip_sound()
        print("stopped", time.monotonic() - start, flush=True)
        time.sleep(3)  # still here, still able to play
    """)
    _expect(child, "playing")
    _audible(speakers, child)
    (took,) = _expect(child, "stopped")
    time.sleep(0.3)
    try:
        assert speakers(1) == 0, "still playing after Stop"
        assert float(took) < 0.5, f"Stop took {float(took):.1f} s, holding up the window"
    finally:
        child.kill()


def test_play_again_mid_sound_starts_it_over_and_stop_still_works(tmp_path, speakers):
    child = _child(f"""
        import obs_config
        tone = {_tone(tmp_path / "long.wav", 4)!r}
        obs_config.play_clip_sound(0.1, tone)
        print("playing", flush=True)
        time.sleep(0.6)
        start = time.monotonic()
        obs_config.play_clip_sound(0.1, tone)
        print("replayed", time.monotonic() - start, flush=True)
        time.sleep(0.6)
        obs_config.stop_clip_sound()
        print("stopped", flush=True)
        time.sleep(3)
    """)
    _expect(child, "playing")
    _audible(speakers, child)
    (took,) = _expect(child, "replayed")
    assert speakers(0.3), "the second Play didn't play"
    _expect(child, "stopped")
    time.sleep(0.3)
    try:
        assert speakers(1) == 0, "still playing after Stop"
        assert float(took) < 0.5, f"Play took {float(took):.1f} s with a sound already playing"
    finally:
        child.kill()


def test_obs_clip_sounds_play_out_one_after_another(tmp_path, speakers):
    """OBS's clip sound has no Stop: a second clip saved while the first
    one's sound plays doesn't cut it off."""
    child = _child(f"""
        import clip_post
        tone = {_tone(tmp_path / "one.wav", 1)!r}
        clip_post.play_wav_windows(tone, 0.1)
        clip_post.play_wav_windows(tone, 0.1)
        print("playing", flush=True)
        time.sleep(4)
    """)
    _expect(child, "playing")
    start = last = time.monotonic()
    try:
        while time.monotonic() - start < 4:
            if speakers(0.1):
                last = time.monotonic()
            elif time.monotonic() - last > 0.5:
                break
    finally:
        child.kill()
    if last == start:
        pytest.skip("nothing came out of the speakers")
    assert last - start > 1.6, f"sounded for {last - start:.1f} s, not both sounds' 2 s"
