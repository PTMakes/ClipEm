import platform
import subprocess

import obspython as obs

audio_file = ""
audio_volume = 0.5
sound_enabled = True

_SYSTEM = platform.system()  # "Windows", "Darwin", or "Linux"


def script_description():
    return "Plays a sound whenever the replay buffer is saved."


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "sound_enabled", "Play sound on clip save")
    obs.obs_properties_add_path(
        props, "audio_file", "Audio File", obs.OBS_PATH_FILE, "WAV files (*.wav)", None
    )
    obs.obs_properties_add_float_slider(props, "audio_volume", "Volume", 0.0, 1.0, 0.01)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "sound_enabled", True)
    obs.obs_data_set_default_double(settings, "audio_volume", 0.5)


def script_update(settings):
    global audio_file, audio_volume, sound_enabled
    sound_enabled = obs.obs_data_get_bool(settings, "sound_enabled")
    audio_file = obs.obs_data_get_string(settings, "audio_file")
    audio_volume = obs.obs_data_get_double(settings, "audio_volume")


def script_load(settings):
    obs.obs_frontend_add_event_callback(on_event)


def script_unload():
    obs.obs_frontend_remove_event_callback(on_event)


def on_event(event):
    if event == obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED and sound_enabled:
        play_sound(audio_file, audio_volume)


# Same as App/Shared/Installer/obs_config.py's - this runs inside OBS and
# can't import that.
def wav_at_volume(path, volume):
    """The WAV file at path, as bytes, with its samples scaled by volume
    (0-1) - winsound has no volume control of its own. Integer PCM only;
    raises wave.Error for anything else (e.g. float samples)."""
    import array
    import io
    import wave

    volume = max(0.0, min(1.0, volume))
    with wave.open(path, "rb") as src:
        params = src.getparams()
        frames = src.readframes(params.nframes)
    width = params.sampwidth
    if width == 1:  # unsigned, centred on 128
        frames = bytes(128 + int((b - 128) * volume) for b in frames)
    elif width in (2, 4):
        samples = array.array("h" if width == 2 else "i", frames)
        frames = array.array(samples.typecode, (int(s * volume) for s in samples)).tobytes()
    elif width == 3:
        scaled = bytearray(len(frames))
        for i in range(0, len(frames) - 2, 3):
            sample = int.from_bytes(frames[i:i + 3], "little", signed=True)
            scaled[i:i + 3] = int(sample * volume).to_bytes(3, "little", signed=True)
        frames = bytes(scaled)
    else:
        raise wave.Error(f"unsupported sample width {width}")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return buf.getvalue()


def play_wav_windows(path, volume):
    """Plays path at volume without blocking. winsound can't play from
    memory asynchronously, so the scaled copy plays on its own thread; a
    WAV it can't scale plays as-is (at the system volume)."""
    import threading
    import wave
    import winsound

    try:
        data = wav_at_volume(path, volume)
    except (wave.Error, EOFError):
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return
    threading.Thread(target=winsound.PlaySound, args=(data, winsound.SND_MEMORY), daemon=True).start()


def play_sound(filepath, volume):
    """Play a WAV file with whatever the host OS provides.

    Shelling out to a native player (instead of a Windows-only Winmm.dll
    call) is what lets this one script work unmodified across the
    Windows, macOS, and Linux copies of this portable profile.
    """
    if not filepath:
        return

    try:
        if _SYSTEM == "Windows":
            play_wav_windows(filepath, volume)
        elif _SYSTEM == "Darwin":
            subprocess.Popen(["afplay", "-v", str(volume), filepath])
        else:
            # Try paplay (PulseAudio/PipeWire) first, fall back to aplay (ALSA).
            pa_volume = str(int(max(0.0, min(1.0, volume)) * 65536))
            try:
                subprocess.Popen(
                    ["paplay", f"--volume={pa_volume}", filepath],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                subprocess.Popen(
                    ["aplay", filepath], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
    except Exception as exc:
        obs.script_log(obs.LOG_WARNING, f"replay_buffer_save_sound: failed to play sound: {exc}")
