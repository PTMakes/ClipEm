"""Missing-mic detection for the tray widget: catches the mic dropping out of
clips, and fixes what it can.

OBS's global mic inputs (Settings > Audio's Mic/Auxiliary Audio - the ones
Setup configures; GetSpecialInputs' mic1-mic4) are checked every
CHECK_INTERVAL_SECONDS while OBS is recording or running the replay buffer:

- Not on track 1 (the mix every clip keeps): put back on it.
- Volume below LOW_VOLUME_DB (as good as silent in the mix - e.g. turned
  down by hand or lowered over and over by the peak guard before it had a
  floor): set back to RESTORE_VOLUME_DB.
- No sound at all for DEAD_SECONDS: a live mic always has some noise, so the
  device has stopped delivering audio (unplugged, a Bluetooth headset
  switched profile, the default input changed under it). Its settings are
  re-applied once, which makes OBS reopen the device; if it's still silent
  after another DEAD_SECONDS, the user is told to check it.
- Muted in OBS: only notified - muting may be on purpose.

"No sound" comes from obs-websocket's InputVolumeMeters events (the Audio
Mixer's meters: level numbers only, several times a second - never audio),
so the tray's connection subscribes to them (EVENT_SUBSCRIPTIONS).
Notifications go through the tray's notify(), at most once per problem
until it's been fixed (and NOTIFY_COOLDOWN_SECONDS apart for a dead mic).
Everything is off while the "missing-mic detection" setting is
(obs_config.load_mic_check_enabled).
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
DEAD_SECONDS = 120
LOW_VOLUME_DB = -30.0
RESTORE_VOLUME_DB = 0.0
NOTIFY_COOLDOWN_SECONDS = 30 * 60
MIC_KEYS = ("mic1", "mic2", "mic3", "mic4")
MIX_TRACK = "1"


class MicCheck:
    """on_volume_meters(event) is the InputVolumeMeters handler (any thread);
    tick(client, enabled, writing) runs from the poll loop. `enabled` is a
    callable (the setting, re-read each tick); `writing`: OBS is recording or
    running the replay buffer. forget() on every (re)connect."""

    def __init__(self, notify, clock=time.monotonic, interval_seconds=CHECK_INTERVAL_SECONDS,
                 dead_seconds=DEAD_SECONDS):
        self._notify = notify
        self._clock = clock
        self._interval = interval_seconds
        self._dead_seconds = dead_seconds
        self._lock = threading.Lock()
        self._enabled = None
        self.forget()

    def forget(self):
        with self._lock:
            self._last_sound = {}  # input name -> clock time it last had any level above 0
        self._watch_since = {}  # input name -> clock time silence started being watched
        self._restarted = {}  # input name -> clock time its settings were re-applied
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
            log.info(f"Missing-mic detection: {'on' if on else 'off (Settings)'}.")
        if not on or not writing:
            # Nothing is being clipped - and silence while idle says nothing.
            self._watch_since.clear()
            return
        now = self._clock()
        if self._next_check is not None and now < self._next_check:
            return
        self._next_check = now + self._interval
        try:
            for name in self._mics(client):
                self._check(client, name, now)
        except ObsWebSocketError as exc:
            log.warning(f"Missing-mic check failed: {exc}")
        except Exception:
            # A bug here must not look like a lost OBS connection to the poll loop.
            log.exception("Missing-mic check crashed")

    @staticmethod
    def _mics(client):
        special = client.call("GetSpecialInputs")
        return [special[key] for key in MIC_KEYS if special.get(key)]

    def _check(self, client, name, now):
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
            self._tell(name, "muted", now, f"Your mic ({name}) is muted in OBS, so it isn't in your clips. "
                                           "Unmute it in OBS's Audio Mixer if that isn't on purpose.")
            self._watch_since.pop(name, None)  # a muted mic is silent - nothing more to find out
            return
        self._told.pop((name, "muted"), None)
        self._check_sound(client, name, now)

    def _check_sound(self, client, name, now):
        watch_since = self._watch_since.setdefault(name, now)
        with self._lock:
            last_sound = self._last_sound.get(name)
        silent_since = max(watch_since, last_sound) if last_sound is not None else watch_since
        if now - silent_since < self._dead_seconds:
            if last_sound is not None and last_sound >= self._restarted.get(name, now + 1):
                log.info(f"Missing mic: '{name}' has sound again.")
                self._restarted.pop(name, None)
                self._told.pop((name, "dead"), None)
            return
        restarted = self._restarted.get(name)
        if restarted is None:
            settings = client.call("GetInputSettings", {"inputName": name}).get("inputSettings", {})
            client.call("SetInputSettings", {"inputName": name, "inputSettings": settings, "overlay": True})
            self._restarted[name] = now
            self._watch_since[name] = now  # give it a fresh DEAD_SECONDS
            log.warning(f"Missing mic: no sound from '{name}' for {self._dead_seconds}s - reopened the device.")
            return
        self._tell(name, "dead", now,
                   f"No sound is coming from your mic ({name}), so it's missing from clips. Check it's "
                   "plugged in and chosen in Settings > Audio Devices.", cooldown=NOTIFY_COOLDOWN_SECONDS)

    def _tell(self, name, problem, now, message, cooldown=None):
        told = self._told.get((name, problem))
        if told is not None and (cooldown is None or now - told < cooldown):
            return
        self._told[(name, problem)] = now
        log.warning(f"Missing mic: {message}")
        self._notify(message)
