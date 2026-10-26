"""What saved clips are called - Settings > Clip Output > Clip names, kept in
run_config.json "clip_naming". Shared by Scripts/clip_reexport.py (inside
OBS), the tray widget (tray-widget/tray_tracks.py) and the clip editor
(tray-widget/clip_browser.py), so no OBS or ClipEm Installer imports here.

A clip is named [prefix] [number or GUID] [date], each part optional, joined
by the separator - e.g. "Replay 2026-10-25 21-50-07" (the default, what
OBS itself writes), "Clip 0042", or "Clip 3f2a...". Its copies add a suffix:
"<clip>-tracks.mkv" and "<clip>-small.mp4" by default.

OBS can only name clips by date, so a clip is renamed right after OBS saves
it, by whichever of the OBS script and the tray finishes it (the tray when it
records app tracks) - before any copy is made from it. The number counts up
from the highest one already used in the clip folder by clips with the same
prefix (or the starting number, if that's higher).
"""

import datetime
import json
import os
import re
import uuid

RUN_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_config.json")

ID_NONE, ID_NUMBER, ID_GUID = "none", "number", "guid"
DEFAULTS = {
    "prefix": "Replay",
    "separator": " ",
    "date": True,
    "date_format": "%Y-%m-%d %H-%M-%S",
    "id": ID_NONE,
    "number_start": 1,
    "number_digits": 4,  # zero-padded to this many digits (0: none)
    "small_suffix": "-small",
    "tracks_suffix": "-tracks",
}
# The suffixes before they could be changed - still recognised, so copies
# saved under them stay listed with their clip.
DEFAULT_SUFFIXES = (DEFAULTS["small_suffix"], DEFAULTS["tracks_suffix"])
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".flv")
# Not allowed in file names on Windows (and "/" anywhere).
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(text):
    return _UNSAFE.sub("-", str(text))


def load(path=None):
    """The naming settings, defaults filled in for anything missing or wrong
    (a bad setting must never stop a clip being saved)."""
    try:
        with open(path or RUN_CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f).get("clip_naming")
    except (OSError, ValueError, AttributeError):
        saved = None
    settings = dict(DEFAULTS)
    if isinstance(saved, dict):
        settings.update({key: saved[key] for key in DEFAULTS if key in saved})
    if validate(settings):
        return dict(DEFAULTS)
    return settings


def validate(settings):
    """Why these settings can't be used, or None."""
    try:
        if not isinstance(settings["date"], bool) or settings["id"] not in (ID_NONE, ID_NUMBER, ID_GUID):
            return "Unknown option."
        start, digits = int(settings["number_start"]), int(settings["number_digits"])
        if start < 0 or not 0 <= digits <= 12:
            return "The starting number can't be negative, and numbers are padded to at most 12 digits."
        if settings["date"]:
            sample = datetime.datetime(2026, 1, 2, 3, 4, 5).strftime(settings["date_format"])
            if not sample.strip() or sample == settings["date_format"]:
                return "The date format doesn't contain any date or time codes (like %Y or %H)."
        small, tracks = settings["small_suffix"], settings["tracks_suffix"]
        if not small or not tracks or small == tracks:
            return "The two copy suffixes must both be set, and be different."
        if clean(small) != small or clean(tracks) != tracks:
            return 'Suffixes can\'t contain < > : " / \\ | ? *'
        if not (settings["prefix"] or settings["date"] or settings["id"] != ID_NONE):
            return "Clip names need at least a prefix, a date or a number."
    except (KeyError, TypeError, ValueError) as exc:
        return f"Invalid setting ({exc})."
    return None


def stem(settings, when, number=None, guid=None):
    """The clip's name without its extension."""
    parts = [settings["prefix"]]
    if settings["id"] == ID_NUMBER:
        parts.append(str(number).zfill(int(settings["number_digits"])))
    elif settings["id"] == ID_GUID:
        parts.append(guid or str(uuid.uuid4()))
    if settings["date"]:
        parts.append(when.strftime(settings["date_format"]))
    return clean(settings["separator"].join(part for part in parts if part)).strip()


def _number_pattern(settings):
    prefix = settings["prefix"]
    lead = re.escape(prefix + settings["separator"]) if prefix else ""
    return re.compile(rf"^{lead}(\d+)(?:{re.escape(settings['separator'])}|$)")


def next_number(folder, settings):
    """One more than the highest number a clip with this prefix has in folder,
    or the starting number if that's higher."""
    pattern = _number_pattern(settings)
    highest = int(settings["number_start"]) - 1
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        base, ext = os.path.splitext(name)
        if ext.lower() not in VIDEO_EXTENSIONS:
            continue
        match = pattern.match(base)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def rename_clip(clip_path, settings=None, now=None, log=print):
    """Gives a clip OBS just saved its name from the settings. Returns its
    path afterwards - the same one when it already has that name, or it
    couldn't be renamed."""
    settings = settings or load()
    if settings == DEFAULTS:
        return clip_path  # what OBS names clips itself - nothing to do
    folder, name = os.path.split(clip_path)
    ext = os.path.splitext(name)[1]
    if now is None:
        try:
            now = datetime.datetime.fromtimestamp(os.path.getmtime(clip_path))
        except OSError:
            now = datetime.datetime.now()
    number = next_number(folder, settings) if settings["id"] == ID_NUMBER else None
    target_stem = stem(settings, now, number)
    if target_stem + ext == name:
        return clip_path
    target = os.path.join(folder, target_stem + ext)
    copy = 2
    while os.path.exists(target):  # two clips in the same second, or a name already used
        target = os.path.join(folder, f"{target_stem} ({copy}){ext}")
        copy += 1
    try:
        os.rename(clip_path, target)
    except OSError as exc:
        log(f"couldn't rename {clip_path} to {os.path.basename(target)} ({exc}) - keeping OBS's name")
        return clip_path
    log(f"renamed {name} to {os.path.basename(target)}")
    return target


def copy_path(clip_path, suffix, ext):
    return os.path.splitext(clip_path)[0] + suffix + ext


def small_path(clip_path, settings=None):
    return copy_path(clip_path, (settings or load())["small_suffix"], ".mp4")


def tracks_path(clip_path, settings=None):
    return copy_path(clip_path, (settings or load())["tracks_suffix"], ".mkv")


def copy_suffixes(settings=None):
    """{suffix: "small_file"/"tracks_file"} for listing copies with their
    clip: the ones in use and the defaults."""
    settings = settings or load()
    return {DEFAULT_SUFFIXES[0]: "small_file", DEFAULT_SUFFIXES[1]: "tracks_file",
            settings["small_suffix"]: "small_file", settings["tracks_suffix"]: "tracks_file"}
