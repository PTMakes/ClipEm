"""
The clip hotkey with post-clip on (Settings > Clip Output - see clip_post.py):
Settings moves the clip hotkey's keys here from OBS's own "Save Replay"
hotkey. A press plays the clip sound and, `seconds` later, saves the replay
buffer once - telling whoever finishes the save (clip_reexport.py or the tray)
where the press was. A second press while waiting saves straight away.

The tray's Save Clip triggers this hotkey by name (HOTKEY_NAME) over
obs-websocket. With post-clip off a press just saves, like OBS's hotkey.

Each save asked for is watched: when OBS says it saved the replay buffer,
the file it made is written into the state ("saved_path"), so a later save
by anyone else can't be taken for this one. If OBS hasn't saved it
clip_post.STATE_FRESH_SECONDS later, the save failed (OBS's own error is in
its log): the state is cleared and the failure logged as an error, which
ClipEm's OBS plugin shows as a notification.
"""

import datetime
import os
import sys

import obspython as obs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clip_post  # noqa: E402

HOTKEY_NAME = "clipem_post_clip"
HOTKEY_DESCRIPTION = "ClipEm: Save clip (with post-clip seconds)"
HOTKEY_SETTING = "post_clip_hotkey"  # what Settings writes the keys into

hotkey_id = None
pending = None  # {"pressed_at", "clip_seconds", "sound"} while waiting to save
awaiting = None  # the state written for a save OBS hasn't finished yet


def script_description():
    return ("ClipEm's clip hotkey when post-clip is on: keeps recording for a few seconds after the press, "
            "then saves the clip and a copy with those seconds (\"-extra.mkv\").")


def script_load(settings):
    global hotkey_id
    hotkey_id = obs.obs_hotkey_register_frontend(HOTKEY_NAME, HOTKEY_DESCRIPTION, on_hotkey)
    keys = obs.obs_data_get_array(settings, HOTKEY_SETTING)
    obs.obs_hotkey_load(hotkey_id, keys)
    obs.obs_data_array_release(keys)
    obs.obs_frontend_add_event_callback(on_event)


def script_save(settings):
    if hotkey_id is None:
        return
    keys = obs.obs_hotkey_save(hotkey_id)
    obs.obs_data_set_array(settings, HOTKEY_SETTING, keys)
    obs.obs_data_array_release(keys)


def script_unload():
    obs.timer_remove(save_now)
    obs.timer_remove(save_overdue)
    obs.obs_frontend_remove_event_callback(on_event)


def _log(message):
    obs.script_log(obs.LOG_INFO, f"post_clip: {message}")


def clip_sound():
    """replay_buffer_save_sound.py's module - OBS imports every script by its
    file name into one interpreter, so this is the one OBS set up."""
    try:
        import replay_buffer_save_sound
    except Exception as exc:
        _log(f"no clip sound ({exc})")
        return None
    return replay_buffer_save_sound


def buffer_seconds():
    """The replay buffer's length as OBS started it (RecRBTime)."""
    return obs.config_get_int(obs.obs_frontend_get_profile_config(), "AdvOut", "RecRBTime")


def on_hotkey(pressed):
    global pending
    if not pressed:
        return
    if pending is not None:
        save_now()
        return
    settings = clip_post.load_settings()
    if not settings["enabled"]:
        obs.obs_frontend_replay_buffer_save()
        return
    # Nothing will be saved, so no sound saying it was.
    if not obs.obs_frontend_replay_buffer_active():
        _log("the replay buffer isn't running - no clip")
        return
    sound = clip_sound()
    played = None
    if sound is not None and sound.sound_enabled and sound.audio_file:
        sound.play_sound(sound.audio_file, sound.audio_volume)
        played = sound.audio_file
    pending = {
        "pressed_at": datetime.datetime.now().timestamp(),
        "clip_seconds": max(1, buffer_seconds() - settings["seconds"]),
        "sound": played,
    }
    obs.timer_add(save_now, settings["seconds"] * 1000)


def save_now():
    global pending
    obs.timer_remove(save_now)
    if pending is None:
        return
    state = dict(pending, requested_at=datetime.datetime.now().timestamp())
    pending = None
    if not obs.obs_frontend_replay_buffer_active():
        _log("the replay buffer stopped before the post-clip seconds were up - no clip")
        return
    try:
        clip_post.write_state(state)
    except OSError as exc:
        _log(f"couldn't note the press ({exc}) - this save is finished as a plain clip")
        clip_post.clear_state()  # an older press mustn't be taken for this one
    else:
        await_save(state)
    obs.obs_frontend_replay_buffer_save()


def await_save(state):
    global awaiting
    awaiting = state
    obs.timer_remove(save_overdue)
    obs.timer_add(save_overdue, clip_post.STATE_FRESH_SECONDS * 1000)


def on_event(event):
    """OBS saved the replay buffer: the save asked for, if one is awaited."""
    global awaiting
    if event != obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED or awaiting is None:
        return
    state, awaiting = awaiting, None
    obs.timer_remove(save_overdue)
    saved_path = obs.obs_frontend_get_last_replay()
    if not saved_path:
        return
    try:
        clip_post.write_state(dict(state, saved_path=saved_path))
    except OSError as exc:
        _log(f"couldn't note which file the save made ({exc})")


def save_overdue():
    global awaiting
    obs.timer_remove(save_overdue)
    if awaiting is None:
        return
    pressed = datetime.datetime.fromtimestamp(float(awaiting["pressed_at"])).strftime("%H:%M:%S")
    awaiting = None
    clip_post.clear_state()
    obs.script_log(obs.LOG_ERROR, f"post_clip: OBS didn't save the clip from the press at {pressed} "
                                  f"within {clip_post.STATE_FRESH_SECONDS}s - no clip was saved (see OBS's log)")
