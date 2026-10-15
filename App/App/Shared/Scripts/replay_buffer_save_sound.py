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
    if _SYSTEM == "Windows":
        obs.obs_properties_add_text(
            props,
            "volume_note",
            "Note: volume control isn't supported on Windows; the sound "
            "plays at your system volume.",
            obs.OBS_TEXT_INFO,
        )
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
            import winsound

            winsound.PlaySound(filepath, winsound.SND_FILENAME | winsound.SND_ASYNC)
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
