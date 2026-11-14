"""Missing-audio detection for the tray widget: catches the mic or the desktop
audio dropping out of clips, fixes what it can, and keeps telling the user
about what it can't.

OBS's global audio inputs (Settings > Audio - the ones Setup configures;
GetSpecialInputs' mic1-mic4 and desktop1-desktop2) are checked every
CHECK_INTERVAL_SECONDS while OBS is recording or running the replay buffer.

Mic:
- Not on track 1 (the mix every clip keeps): put back on it.
- Volume below LOW_VOLUME_DB (as good as silent in the mix - e.g. turned
  down by hand or lowered over and over by the peak guard before it had a
  floor): set back to RESTORE_VOLUME_DB.
- No sound at all for DEAD_SECONDS: a live mic always has some noise, so the
  device has stopped delivering audio (unplugged, a Bluetooth headset
  switched profile, the default input changed under it). A noise gate makes
  a working mic just as silent between words, so with one on, the device
  itself is measured first (audio_levels.device_peak): sound there means
  the gate is only doing its job.
- Muted in OBS: only notified - muting may be on purpose.

Desktop audio: silent for DESKTOP_SILENT_SECONDS while an app is making
sound on the current output (audio_levels.heard_apps - measured, not just a
stream being open) means OBS has lost it - its device went away, or the
default output changed under it. Silence with nothing playing is just a
quiet PC. Muted in OBS: only notified.

Fixing (both): up to FIX_ATTEMPTS tries, RETRY_SECONDS apart, each
followed by a look at whether sound came back - reopen the device
(re-applying its settings), then switch it to the current default device
("default", if it was set to a particular one), then reopen once more. Only
then is the user told - and told again every ALERT_REPEAT_SECONDS, with
another reopen each time, for as long as it lasts. When sound comes back
after the user was told, or after a switch of device, they're told that too.

"No sound" comes from obs-websocket's InputVolumeMeters events (the Audio
Mixer's meters: level numbers only, several times a second - never audio),
so the tray's connection subscribes to them (EVENT_SUBSCRIPTIONS).
Notifications go through the tray's notify(). Everything is off while the
"missing-mic detection" setting is (obs_config.load_mic_check_enabled).
"""

import logging
import threading
import time

from obsws_client import ObsWebSocketError

log = logging.getLogger("tray")

# obs-websocket's EventSubscription: All (the low-volume events, bits 0-10)
# plus InputVolumeMeters (1 << 16), which All leaves out.
EVENT_SUBSCRIPTIONS = 0x7FF | (1 << 16)

CHECK_INTERVAL_SECONDS = 10
DEAD_SECONDS = 60
DESKTOP_SILENT_SECONDS = 20
RETRY_SECONDS = 20
FIX_ATTEMPTS = 3
ALERT_REPEAT_SECONDS = 5 * 60
LOW_VOLUME_DB = -30.0
RESTORE_VOLUME_DB = 0.0
MIC_KEYS = ("mic1", "mic2", "mic3", "mic4")
DESKTOP_KEYS = ("desktop1", "desktop2")
MIX_TRACK = "1"
DEFAULT_DEVICE = "default"
NOISE_GATE_KIND = "noise_gate_filter"
DESKTOP = "desktop"  # the desktop inputs' trouble is tracked together


def _default_device_peak(device_id, flow):
    import audio_levels

    return audio_levels.device_peak(device_id, flow)


def _default_heard_apps(output_device):
    import audio_levels

    return audio_levels.heard_apps(output_device)


class _Trouble:
    """An input that's gone silent, and what's been done about it."""

    def __init__(self, now):
        self.since = now  # sound after this means it's fixed
        self.attempts = 0
        self.next_fix = now
        self.told_at = None
        self.switched_from = None
        self.device_level = None  # what the device itself delivered, when measured
        self.apps = []  # desktop: the apps heard while it was silent


class MicCheck:
    """on_volume_meters(event) is the InputVolumeMeters handler (any thread);
    tick(client, enabled, writing) runs from the poll loop. `enabled` is a
    callable (the setting, re-read each tick); `writing`: OBS is recording or
    running the replay buffer. forget() on every (re)connect.

    device_peak(device_id, flow) and heard_apps(output_device) are
    audio_levels' - measured only once an input has gone silent."""

    def __init__(self, notify, clock=time.monotonic, interval_seconds=CHECK_INTERVAL_SECONDS,
                 dead_seconds=DEAD_SECONDS, device_peak=_default_device_peak, heard_apps=_default_heard_apps):
        self._notify = notify
        self._clock = clock
        self._interval = interval_seconds
        self._dead_seconds = dead_seconds
        self._device_peak = device_peak
        self._heard_apps = heard_apps
        self._lock = threading.Lock()
        self._enabled = None
        self.forget()

    def forget(self):
        with self._lock:
            self._last_sound = {}  # input name -> clock time it last had any level above 0
        self._watch_since = {}  # input name -> clock time silence started being watched
        self._trouble = {}  # mic name or DESKTOP -> _Trouble
        self._told = {}  # (input name, problem) -> clock time the user was told
        self._next_check = None

    def on_volume_meters(self, event):
        now = self._clock()
        with self._lock:
            for meter in event.get("inputs") or []:
                levels = meter.get("inputLevelsMul") or []
                if any(value > 0 for channel in levels for value in channel):
                    self._last_sound[meter.get("inputName")] = now

    def tick(self, client, enabled, writing):
        on = bool(enabled())
        if on != self._enabled:
            self._enabled = on
            log.info(f"Missing-audio detection: {'on' if on else 'off (Settings)'}.")
        if not on or not writing:
            # Nothing is being clipped - and silence while idle says nothing.
            self._watch_since.clear()
            return
        now = self._clock()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + self._interval
        try:
            special = client.call("GetSpecialInputs")
            for name in [special[key] for key in MIC_KEYS if special.get(key)]:
                self._check_mic(client, name, now)
            desktops = [special[key] for key in DESKTOP_KEYS if special.get(key)]
            if desktops:
                self._check_desktop(client, desktops, now)
        except ObsWebSocketError as exc:
            log.warning(f"Missing-audio check failed: {exc}")
        except Exception:
            # A bug here must not look like a lost OBS connection to the poll loop.
            log.exception("Missing-audio check crashed")

    # -- what the meters say -------------------------------------------------

    def _silent_for(self, name, now):
        watch_since = self._watch_since.setdefault(name, now)
        with self._lock:
            last_sound = self._last_sound.get(name)
        return now - (max(watch_since, last_sound) if last_sound is not None else watch_since)

    def _sound_since(self, name, since):
        with self._lock:
            last_sound = self._last_sound.get(name)
        return last_sound is not None and last_sound > since

    # -- the mic ---------------------------------------------------------------

    def _check_mic(self, client, name, now):
        tracks = client.call("GetInputAudioTracks", {"inputName": name}).get("inputAudioTracks", {})
        if tracks and not tracks.get(MIX_TRACK, True):
            client.call("SetInputAudioTracks", {"inputName": name, "inputAudioTracks": {MIX_TRACK: True}})
            log.warning(f"Missing mic: '{name}' wasn't on track 1 (the mix clips keep) - put it back.")
            self._notify(f"Your mic ({name}) wasn't being recorded into clips - ClipEm has fixed it.")

        volume_db = client.call("GetInputVolume", {"inputName": name}).get("inputVolumeDb")
        if volume_db is not None and volume_db < LOW_VOLUME_DB:
            client.call("SetInputVolume", {"inputName": name, "inputVolumeDb": RESTORE_VOLUME_DB})
            log.warning(f"Missing mic: '{name}' was at {volume_db:.1f} dB - set back to {RESTORE_VOLUME_DB:g} dB.")
            self._notify(f"Your mic ({name}) was turned almost all the way down, so it was missing from "
                         "clips - ClipEm has turned it back up.")

        if client.call("GetInputMute", {"inputName": name}).get("inputMuted"):
            self._tell_once(name, "muted", f"Your mic ({name}) is muted in OBS, so it isn't in your clips. "
                                           "Unmute it in OBS's Audio Mixer if that isn't on purpose.")
            self._watch_since.pop(name, None)  # a muted mic is silent - nothing more to find out
            return
        self._told.pop((name, "muted"), None)

        trouble = self._trouble.get(name)
        if trouble is not None and self._sound_since(name, trouble.since):
            self._recovered(name, trouble, f"Your mic ({name})", "input")
            return
        if trouble is None:
            if self._silent_for(name, now) < self._dead_seconds:
                return
            device = self._device_id(client, name)
            level = self._device_peak(device, "input")
            if level and self._has_noise_gate(client, name):
                # The device hears the room; the gate keeps it out until someone talks.
                self._watch_since[name] = now
                return
            trouble = self._trouble[name] = _Trouble(now)
            trouble.device_level = level
            log.warning(f"Missing mic: no sound from '{name}' for {self._dead_seconds}s"
                        + (" (its device delivers none either)" if level == 0 else
                           " (its device has sound - OBS isn't getting it)" if level else ""))
        self._fix_or_tell(client, name, trouble, now, "input", self._mic_alert)

    def _mic_alert(self, name, trouble, repeat):
        tried = "ClipEm reopened it" + (" and switched it to your default input" if trouble.switched_from else "")
        start = f"Still no sound from your mic ({name})" if repeat else f"No sound is coming from your mic ({name})"
        cause = (" - not even background noise -" if trouble.device_level == 0 else "")
        return (f"{start}{cause}, so it's missing from clips. {tried}, but it's still silent. Check it's plugged "
                "in, not muted on the device or in your system's sound settings, and chosen in "
                "Settings > Audio Devices.")

    @staticmethod
    def _has_noise_gate(client, name):
        try:
            filters = client.call("GetSourceFilterList", {"sourceName": name}).get("filters") or []
        except ObsWebSocketError:
            return False
        return any(f.get("filterKind") == NOISE_GATE_KIND and f.get("filterEnabled", True) for f in filters)

    # -- desktop audio -----------------------------------------------------------

    def _check_desktop(self, client, names, now):
        live = [name for name in names if not client.call("GetInputMute", {"inputName": name}).get("inputMuted")]
        if not live:
            self._tell_once(names[0], "muted", f"Your desktop audio ({names[0]}) is muted in OBS, so game and "
                                               "app sound isn't in your clips. Unmute it in OBS's Audio Mixer "
                                               "if that isn't on purpose.")
            for name in names:
                self._watch_since.pop(name, None)
            return
        self._told.pop((names[0], "muted"), None)

        trouble = self._trouble.get(DESKTOP)
        if trouble is not None and any(self._sound_since(name, trouble.since) for name in live):
            self._recovered(DESKTOP, trouble, f"Your desktop audio ({live[0]})", "output")
            return
        if min(self._silent_for(name, now) for name in live) < DESKTOP_SILENT_SECONDS:
            return
        target = live[0]
        heard = self._heard_apps(self._device_id(client, target))
        if not heard:
            return  # nothing playing: silence is right
        if trouble is None:
            trouble = self._trouble[DESKTOP] = _Trouble(now)
            log.warning(f"Missing desktop audio: '{target}' hears nothing while {', '.join(heard)} "
                        f"{'is' if len(heard) == 1 else 'are'} playing sound.")
        trouble.apps = heard
        self._fix_or_tell(client, target, trouble, now, "output", self._desktop_alert)

    def _desktop_alert(self, name, trouble, repeat):
        apps = ", ".join(trouble.apps) or "Apps"
        start = "Still no desktop audio" if repeat else "No desktop audio"
        tried = "ClipEm reopened it" + (" and switched it to your current output" if trouble.switched_from else "")
        return (f"{start} in clips: {apps} {'is' if len(trouble.apps) == 1 else 'are'} playing sound, but OBS's "
                f"Desktop Audio ({name}) hears nothing. {tried}, but it's still silent. Check the output "
                "chosen in Settings > Audio Devices.")

    # -- fixing ----------------------------------------------------------------

    @staticmethod
    def _device_id(client, name):
        settings = client.call("GetInputSettings", {"inputName": name}).get("inputSettings", {})
        return settings.get("device_id") or DEFAULT_DEVICE

    def _fix_or_tell(self, client, name, trouble, now, flow, alert):
        if now < trouble.next_fix:
            return
        if trouble.attempts < FIX_ATTEMPTS:
            what = self._fix(client, name, trouble, flow)
            trouble.attempts += 1
            log.warning(f"Missing audio: '{name}' still silent - {what} (try {trouble.attempts} of {FIX_ATTEMPTS}).")
            trouble.since = now
            trouble.next_fix = now + RETRY_SECONDS
            return
        message = alert(name, trouble, trouble.told_at is not None)
        trouble.told_at = now
        log.warning(f"Missing audio: {message}")
        self._notify(message)
        self._reopen(client, name)  # keep trying between reminders
        trouble.since = now
        trouble.next_fix = now + ALERT_REPEAT_SECONDS

    def _fix(self, client, name, trouble, flow):
        """One more try at getting sound from it; says what was done."""
        if trouble.attempts == 1:
            device = self._device_id(client, name)
            if device != DEFAULT_DEVICE:
                client.call("SetInputSettings", {"inputName": name, "inputSettings": {"device_id": DEFAULT_DEVICE},
                                                 "overlay": True})
                trouble.switched_from = device
                return f"switched it from device '{device}' to the current default {flow}"
        self._reopen(client, name)
        return "reopened the device"

    @staticmethod
    def _reopen(client, name):
        """Re-applies its settings, which makes OBS reopen the device."""
        settings = client.call("GetInputSettings", {"inputName": name}).get("inputSettings", {})
        client.call("SetInputSettings", {"inputName": name, "inputSettings": settings, "overlay": True})

    def _recovered(self, key, trouble, what, flow):
        del self._trouble[key]
        log.info(f"Missing audio: {what} has sound again"
                 + (f" after {trouble.attempts} fix(es)." if trouble.attempts else "."))
        if trouble.switched_from:
            self._notify(f"{what} stopped working on its device, so ClipEm switched it to your current default "
                         f"{flow} - it's in your clips again. Pick another in Settings > Audio Devices if you like.")
        elif trouble.told_at is not None:
            self._notify(f"{what} is working again - it's back in your clips.")

    def _tell_once(self, name, problem, message):
        if (name, problem) in self._told:
            return
        self._told[(name, problem)] = self._clock()
        log.warning(f"Missing audio: {message}")
        self._notify(message)
