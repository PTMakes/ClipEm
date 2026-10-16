"""
The external editor ClipEm's clip editor (tray-widget/clip_browser.py) opens clips
in: the user's own editor - a path set in the tray's Settings
(run_config.json's "custom_editor_path").
"""

import os
import platform
import subprocess
import sys

from clipem_common import LOG_DIR, load_run_config

# An editor that exits within this long of starting failed to start.
STARTUP_FAILURE_SECONDS = 3
EDITOR_LOG_PATH = os.path.join(LOG_DIR, "editor.log")


class EditorError(Exception):
    pass


def custom_editor_path():
    path = (load_run_config().get("custom_editor_path") or "").strip()
    return path or None


def editor_label(path):
    name = path.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".exe", ".app", ".AppImage", ".appimage"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _detached_kwargs():
    if sys.platform == "win32":
        return {"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return {"start_new_session": True}


def editor_command(editor_path, clip_path, system=None):
    system = system or platform.system()
    if system == "Darwin" and editor_path.endswith(".app"):
        return ["open", "-a", editor_path, clip_path]
    return [editor_path, clip_path]


def open_in_editor(editor_path, clip_path, log=print, system=None, popen=subprocess.Popen):
    """Starts the editor on clip_path, detached. On Linux, raises EditorError
    with its stderr if it exits straight away."""
    system = system or platform.system()
    cmd = editor_command(editor_path, clip_path, system)
    if system != "Linux":
        popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached_kwargs())
        return
    # stderr to a file, not a pipe: the editor keeps logging long after
    # this returns, and a pipe nobody reads would fill up and stall it.
    os.makedirs(os.path.dirname(EDITOR_LOG_PATH), exist_ok=True)
    with open(EDITOR_LOG_PATH, "w+", encoding="utf-8", errors="replace") as err_log:
        proc = popen(cmd, stdout=subprocess.DEVNULL, stderr=err_log, **_detached_kwargs())
        try:
            proc.wait(timeout=STARTUP_FAILURE_SECONDS)
        except subprocess.TimeoutExpired:
            return  # still running - it started fine
        err_log.seek(0)
        err = err_log.read()
    if proc.returncode:
        raise EditorError(f"{editor_label(editor_path)} exited straight away: {err.strip()[-300:]}")
