"""Moves or copies the clips of the old clip folder when Settings changes it -
files only, from the folder itself, keeping their dates. What the editor knows
of a clip goes with it: saved edits are found by path and date (clip_browser's
thumb_key), and exports.json by path.

Nothing's ever overwritten: a name already in the new folder stays where it is
and is reported. Every copy is written under a hidden name first, so a failed
one leaves nothing that looks like a clip.
"""

import hashlib
import json
import os
import shutil

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".flv")
CHUNK_BYTES = 1024 * 1024


def list_clips(folder):
    """The video files directly in folder (hidden ones are partial saves)."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    return sorted(entry.path for entry in entries
                  if entry.is_file() and not entry.name.startswith(".")
                  and os.path.splitext(entry.name)[1].lower() in VIDEO_EXTENSIONS)


def same_folder(old, new):
    return os.path.normcase(os.path.realpath(old)) == os.path.normcase(os.path.realpath(new))


def bytes_needed(paths):
    return sum(os.path.getsize(path) for path in paths)


def room_problem(paths, new, move):
    """Why these won't fit in the new folder, or None. A move on one drive
    needs no room."""
    try:
        if move and os.stat(paths[0]).st_dev == os.stat(new).st_dev:
            return None
        free, needed = shutil.disk_usage(new).free, bytes_needed(paths)
    except (OSError, IndexError):
        return None
    if needed <= free:
        return None
    return f"The clips need {needed / 1e9:.1f} GB, and that drive has {free / 1e9:.1f} GB free."


def _copy(path, target, copied):
    """Copies path to target, adding each chunk's size to copied(n)."""
    partial = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.partial")
    try:
        with open(path, "rb") as source, open(partial, "wb") as out:
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                out.write(chunk)
                copied(len(chunk))
        shutil.copystat(path, partial)
        os.replace(partial, target)
    finally:
        if os.path.exists(partial):
            os.remove(partial)


def edit_key(path):
    """clip_browser.thumb_key, for a file as it is now."""
    return hashlib.sha1(f"{os.path.abspath(path)}|{os.path.getmtime(path)}".encode()).hexdigest()


def _carry_edit(edits_dir, key, target, move):
    """A clip's saved edit, found again under its new path."""
    if not edits_dir:
        return
    old = os.path.join(edits_dir, f"{key}.json")
    try:
        with open(old, encoding="utf-8") as f:
            project = json.load(f)
        project["clip"] = os.path.realpath(target)
        with open(os.path.join(edits_dir, f"{edit_key(target)}.json"), "w", encoding="utf-8") as f:
            json.dump(project, f)
        if move:
            os.remove(old)
    except (OSError, ValueError):
        pass  # no edit, or one that can't be read: the clip itself is what matters


def _carry_exports(exports_path, moved):
    """exports.json's paths, for the clips and exports that moved."""
    if not exports_path or not moved:
        return
    try:
        with open(exports_path, encoding="utf-8") as f:
            exports = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(exports, dict):
        return
    updated = {}
    for clip, entry in exports.items():
        if isinstance(entry, dict) and entry.get("file") in moved:
            entry = {**entry, "file": moved[entry["file"]]}
        updated[moved.get(clip, clip)] = entry
    with open(exports_path, "w", encoding="utf-8") as f:
        json.dump(updated, f)


def transfer(old, new, move, edits_dir=None, exports_path=None, progress=lambda done, total: None):
    """Moves (or copies) every clip of old into new. progress(bytes done,
    bytes in all) is called along the way. Returns {"done": number of files,
    "skipped": [names already in new], "failed": [(name, why)]}."""
    os.makedirs(new, exist_ok=True)
    paths = list_clips(old)
    total = bytes_needed(paths)
    state = {"bytes": 0}

    def copied(count):
        state["bytes"] += count
        progress(state["bytes"], total)

    result = {"done": 0, "skipped": [], "failed": []}
    moved = {}
    for path in paths:
        name = os.path.basename(path)
        target = os.path.join(new, name)
        size = os.path.getsize(path)
        if os.path.exists(target):
            result["skipped"].append(name)
            copied(size)
            continue
        try:
            key = edit_key(path)
            if move:
                try:
                    os.rename(path, target)  # same drive: instant
                    copied(size)
                except OSError:
                    _copy(path, target, copied)
                    os.remove(path)
                moved[os.path.realpath(path)] = os.path.realpath(target)
            else:
                _copy(path, target, copied)
            _carry_edit(edits_dir, key, target, move)
            result["done"] += 1
        except OSError as exc:
            result["failed"].append((name, str(exc)))
    _carry_exports(exports_path, moved)
    return result
