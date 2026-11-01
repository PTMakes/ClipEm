"""What saved clips are called - Settings > Clip Output > Clip names, kept in
run_config.json "clip_naming". Shared by Scripts/clip_reexport.py (inside
OBS), the tray widget (tray-widget/tray_tracks.py) and the clip editor
(tray-widget/clip_browser.py), so no OBS or ClipEm Installer imports here.

A clip's name is its parts - prefix, number or GUID, date, game or app - each
one optional, in the order chosen, joined by the separator, which also goes
between the date's own parts: e.g. "Replay-2026-10-25-21-50-07-Deadlock" (the
default), "Clip-0042" or "Clip-3f2a...".
Its copies add the separator and a suffix: "<clip>-tracks.mkv",
"<clip>-small.mp4" and, with post-clip on (clip_post.py), "<clip>-extra.mkv"
by default. The separator is never part of what's typed in Settings, and file
names never have spaces: any in a part become the separator.

OBS can only name clips by date, so every clip is renamed right after OBS saves
it, by whichever of the OBS script and the tray finishes it (the tray when it
records app tracks) - before any copy is made from it. The number counts up
from the highest one already used in the clip folder by clips with the same
prefix (or the starting number, if that's higher). The game or app comes from
the tray (app_audio.py), which keeps active_app() up to date; a clip saved
while nothing is playing sound just goes without.
"""

import datetime
import json
import os
import re
import uuid

SHARED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_CONFIG_PATH = os.path.join(SHARED_DIR, "run_config.json")
ACTIVE_APP_PATH = os.path.join(os.path.dirname(SHARED_DIR), "cache", "active_app.json")

ID_NONE, ID_NUMBER, ID_GUID = "none", "number", "guid"
PARTS = ("prefix", "id", "date", "app")
DEFAULTS = {
    "order": list(PARTS),
    "prefix": "Replay",
    "prefix_on": True,
    "separator": "-",
    "date": True,
    "date_format": "yyyyMMddhhmmss",
    "id": ID_NONE,
    "number_start": 1,
    "number_digits": 4,  # zero-padded to this many digits (0: none)
    "app": True,
    "small_suffix": "small",
    "tracks_suffix": "tracks",
    "extra_suffix": "extra",
}
# The suffixes before they could be changed - still recognised, so copies
# saved under them stay listed with their clip.
DEFAULT_SUFFIXES = ("-small", "-tracks", "-extra")
OLD_DATE_FORMAT = "%Y-%m-%d %H-%M-%S"  # the default before date codes like yyyy
# Date codes, longest first; hh is the 24-hour clock either way, so names sort.
_DATE_CODES = {"yyyy": "%Y", "yy": "%y", "MM": "%m", "dd": "%d", "HH": "%H", "hh": "%H", "mm": "%M", "ss": "%S"}
_DATE_CODE = re.compile("|".join(_DATE_CODES))
# What Settings lets clips' dates be made of, in any order.
DATE_PARTS = (("yyyy", "Year"), ("MM", "Month"), ("dd", "Day"), ("hh", "Hour"), ("mm", "Minute"), ("ss", "Second"))
_SAME_DATE_PART = {"yy": "yyyy", "HH": "hh", "%Y": "yyyy", "%y": "yyyy", "%m": "MM", "%d": "dd", "%H": "hh",
                   "%I": "hh", "%M": "mm", "%S": "ss"}
_ANY_DATE_CODE = re.compile("|".join(("%[YymdHIMS]", *_DATE_CODES)))
MAX_APP_LENGTH = 48
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".flv")
# Not allowed in file names on Windows (and "/" anywhere).
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(text):
    return _UNSAFE.sub("-", str(text))


def _no_spaces(text, separator):
    return re.sub(r"\s+", separator, clean(text).strip())


def format_date(when, date_format):
    """when in date codes (yyyyMMddhhmmss) - or strftime's, in a format saved
    before those."""
    if "%" in date_format:
        return when.strftime(date_format)
    return _DATE_CODE.sub(lambda code: when.strftime(_DATE_CODES[code.group()]), date_format)


def _only_codes(date_format):
    return "%" not in date_format and not _DATE_CODE.sub("", date_format)


def date_text(settings, when):
    """The date in a name: the separator between its parts, unless it's a
    saved format with its own punctuation."""
    date_format = settings["date_format"]
    if _only_codes(date_format):
        return settings["separator"].join(format_date(when, code) for code in _DATE_CODE.findall(date_format))
    return format_date(when, date_format)


def date_parts(date_format):
    """The six DATE_PARTS codes in a date format's order, any it doesn't have
    after them. Anything else in it (a saved "dd.MM.yyyy"'s dots) goes."""
    found = (_SAME_DATE_PART.get(code, code) for code in _ANY_DATE_CODE.findall(date_format))
    return list(dict.fromkeys([*found, *(code for code, _label in DATE_PARTS)]))


def upgrade(saved):
    """Settings as saved, whichever version - before part order and on/off
    switches, the separator was part of each suffix and could be a space."""
    settings = dict(DEFAULTS)
    if not isinstance(saved, dict):
        return settings
    settings.update({key: saved[key] for key in DEFAULTS if key in saved})
    if "order" in saved:
        return settings
    if isinstance(settings["separator"], str) and not settings["separator"].strip():
        settings["separator"] = DEFAULTS["separator"]
    if settings["date_format"] == OLD_DATE_FORMAT:
        settings["date_format"] = DEFAULTS["date_format"]
    settings["prefix_on"] = bool(settings["prefix"])
    for key in ("small_suffix", "tracks_suffix", "extra_suffix"):
        if isinstance(settings[key], str):
            settings[key] = settings[key].lstrip(" -_.") or DEFAULTS[key]
    return settings


def load(path=None):
    """The naming settings, defaults filled in for anything missing or wrong
    (a bad setting must never stop a clip being saved)."""
    try:
        with open(path or RUN_CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f).get("clip_naming")
    except (OSError, ValueError, AttributeError):
        saved = None
    settings = upgrade(saved)
    if validate(settings):
        return dict(DEFAULTS)
    return settings


def validate(settings):
    """Why these settings can't be used, or None."""
    try:
        if not all(isinstance(settings[key], bool) for key in ("date", "prefix_on", "app")) \
                or settings["id"] not in (ID_NONE, ID_NUMBER, ID_GUID) or sorted(settings["order"]) != sorted(PARTS):
            return "Unknown option."
        separator = settings["separator"]
        if not separator or re.search(r"\s", separator) or clean(separator) != separator:
            return 'Between Parts needs a character, not a space or any of < > : " / \\ | ? *'
        start, digits = int(settings["number_start"]), int(settings["number_digits"])
        if start < 0 or not 0 <= digits <= 12:
            return "The starting number can't be negative, and numbers are padded to at most 12 digits."
        if settings["date"]:
            sample = format_date(datetime.datetime(2026, 1, 2, 3, 4, 5), settings["date_format"])
            if not sample.strip() or sample == settings["date_format"]:
                return "The date format doesn't contain any date or time codes (like yyyy or hh)."
        suffixes = (settings["small_suffix"], settings["tracks_suffix"], settings["extra_suffix"])
        if not all(suffixes) or len(set(suffixes)) != len(suffixes):
            return "The addon suffixes must all be set, and be different."
        if any(clean(suffix) != suffix for suffix in suffixes):
            return 'Suffixes can\'t contain < > : " / \\ | ? *'
        if not ((settings["prefix_on"] and settings["prefix"]) or settings["date"] or settings["id"] != ID_NONE):
            return "Clip names need at least a prefix, a date or a number."
    except (KeyError, TypeError, ValueError) as exc:
        return f"Invalid setting ({exc})."
    return None


def active_app(path=None):
    """The game or app the tray last saw playing sound, or None."""
    try:
        with open(path or ACTIVE_APP_PATH, encoding="utf-8") as f:
            app = json.load(f).get("app")
    except (OSError, ValueError, AttributeError):
        return None
    return app if isinstance(app, str) and app.strip() else None


def publish_active_app(app, path=None):
    """Tells every process naming clips what active_app() is now."""
    path = path or ACTIVE_APP_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    partial = f"{path}.partial"
    with open(partial, "w", encoding="utf-8") as f:
        json.dump({"app": app}, f)
    os.replace(partial, path)


def stem(settings, when, number=None, guid=None, app=None):
    """The clip's name without its extension."""
    separator = settings["separator"]
    values = {"prefix": settings["prefix"] if settings["prefix_on"] else "", "id": "", "date": "", "app": ""}
    if settings["id"] == ID_NUMBER:
        values["id"] = str(number).zfill(int(settings["number_digits"]))
    elif settings["id"] == ID_GUID:
        values["id"] = guid or str(uuid.uuid4())
    if settings["date"]:
        values["date"] = date_text(settings, when)
    if settings["app"] and app:
        values["app"] = re.sub(r"\.exe$", "", app.strip(), flags=re.I)[:MAX_APP_LENGTH].strip(" .")
    parts = (_no_spaces(values[key], separator) for key in settings["order"])
    return separator.join(part for part in parts if part)


def _date_pattern(settings):
    date_format = settings["date_format"]
    if "%" in date_format:
        return ".+?"
    if _only_codes(date_format):
        return re.escape(settings["separator"]).join(rf"\d{{{len(code)}}}" for code in _DATE_CODE.findall(date_format))
    pattern, position = "", 0
    for code in _DATE_CODE.finditer(date_format):
        literal = clean(date_format[position:code.start()])
        pattern += re.escape(re.sub(r"\s+", settings["separator"], literal)) + rf"\d{{{len(code.group())}}}"
        position = code.end()
    return pattern + re.escape(clean(date_format[position:]))


def _number_pattern(settings):
    """Matches a clip's name with these parts, the number in group 1 - at most
    12 digits (the padding's limit), so a whole date never counts as one. A
    game or app may be missing; a space is the separator clips used to have."""
    between = rf"(?:{re.escape(settings['separator'])}| )"
    pattern, started = "", False
    for key in settings["order"]:
        if key == "app":
            if settings["app"]:
                pattern += rf"(?:{between}.+?)?" if started else rf"(?:.+?{between})?"
            continue
        part = {"prefix": re.escape(_no_spaces(settings["prefix"], settings["separator"])),
                "id": r"(\d{1,12})", "date": _date_pattern(settings)}[key]
        if key == "prefix" and not (settings["prefix_on"] and settings["prefix"]) \
                or key == "date" and not settings["date"] or key == "id" and settings["id"] != ID_NUMBER:
            continue
        pattern += (between if started else "") + part
        started = True
    return re.compile(rf"^{pattern}(?:{between}.*)?$")


def next_number(folder, settings):
    """One more than the highest number a clip with this prefix has in folder,
    or the starting number if that's higher."""
    if settings["id"] != ID_NUMBER:  # no number in these names to count from
        return int(settings["number_start"])
    pattern = _number_pattern(settings)
    # With the date off, a dated clip's year would look like a number.
    dated = None if settings["date"] else re.compile(rf"(?:\d{{2,4}}{re.escape(settings['separator'])}){{5}}\d{{2,4}}")
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
        if match and not (dated and dated.search(base)):
            highest = max(highest, int(match.group(1)))
    return highest + 1


def rename_clip(clip_path, settings=None, now=None, log=print):
    """Gives a clip OBS just saved its name from the settings. Returns its
    path afterwards - the same one when it already has that name, or it
    couldn't be renamed."""
    settings = settings or load()
    folder, name = os.path.split(clip_path)
    ext = os.path.splitext(name)[1]
    if now is None:
        try:
            now = datetime.datetime.fromtimestamp(os.path.getmtime(clip_path))
        except OSError:
            now = datetime.datetime.now()
    number = next_number(folder, settings) if settings["id"] == ID_NUMBER else None
    target_stem = stem(settings, now, number, app=active_app() if settings["app"] else None)
    if target_stem + ext == name:
        return clip_path
    target = os.path.join(folder, target_stem + ext)
    copy = 2
    while os.path.exists(target):  # two clips in the same second, or a name already used
        target = os.path.join(folder, f"{target_stem}{settings['separator']}{copy}{ext}")
        copy += 1
    try:
        os.rename(clip_path, target)
    except OSError as exc:
        log(f"couldn't rename {clip_path} to {os.path.basename(target)} ({exc}) - keeping OBS's name")
        return clip_path
    log(f"renamed {name} to {os.path.basename(target)}")
    return target


def suffix(settings, key):
    """What a copy adds to its clip's name; key: "small", "tracks" or "extra"."""
    separator = settings["separator"]
    return separator + _no_spaces(settings[f"{key}_suffix"], separator).removeprefix(separator)


def copy_path(clip_path, copy_suffix, ext):
    return os.path.splitext(clip_path)[0] + copy_suffix + ext


def small_path(clip_path, settings=None):
    return copy_path(clip_path, suffix(settings or load(), "small"), ".mp4")


def tracks_path(clip_path, settings=None):
    return copy_path(clip_path, suffix(settings or load(), "tracks"), ".mkv")


def extra_path(clip_path, settings=None):
    return copy_path(clip_path, suffix(settings or load(), "extra"), ".mkv")


def copy_suffixes(settings=None):
    """{suffix: "small_file"/"tracks_file"/"extra_file"} for listing copies
    with their clip: the ones in use and the defaults."""
    settings = settings or load()
    return {DEFAULT_SUFFIXES[0]: "small_file", DEFAULT_SUFFIXES[1]: "tracks_file", DEFAULT_SUFFIXES[2]: "extra_file",
            suffix(settings, "small"): "small_file", suffix(settings, "tracks"): "tracks_file",
            suffix(settings, "extra"): "extra_file"}
