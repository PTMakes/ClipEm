"""
The clip editors the Clip Browser (tray-widget/clip_browser.py) opens clips
in: LosslessCut (GPL-2, https://github.com/mifi/lossless-cut), downloaded on
request into App/<platform>/LosslessCut, and the user's own editor - a path
set in the tray's Settings (run_config.json's "custom_editor_path").

LosslessCut is listed in clipem.app's signed install manifest like OBS, so
it's fetched with obs_install.fetch_verified (sha256-checked against the
signed manifest) and unpacked into a staging folder that's only moved into
place once complete.
"""

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time

from clipem_common import APP_DIR, LOG_DIR, load_json, load_run_config, write_json_atomic

PLATFORM_DIRS = {"Windows": "Windows", "Linux": "Linux"}
LOSSLESSCUT_EXE_NAMES = {"Windows": "LosslessCut.exe", "Linux": "losslesscut"}
LOSSLESSCUT_DOWNLOAD_MB = {"Windows": 139, "Linux": 181}
STAGING_DIR_NAME = ".losslesscut-staging"
# Electron dies straight away when its sandbox can't start (a chrome-sandbox
# helper without setuid root, and no unprivileged user namespaces).
SANDBOX_FAILURE_SECONDS = 3
EDITOR_LOG_PATH = os.path.join(LOG_DIR, "editor.log")


class EditorError(Exception):
    pass


def losslesscut_supported(system=None):
    return (system or platform.system()) in PLATFORM_DIRS


def losslesscut_dir(system=None):
    system = system or platform.system()
    return os.path.join(APP_DIR, PLATFORM_DIRS.get(system, system), "LosslessCut")


def _find_exe(root, exe_name, max_depth=2):
    """The LosslessCut executable at root or up to max_depth folders below
    it, shallowest first (the archives wrap it in a folder whose name has
    changed before)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = 0 if dirpath == root else os.path.relpath(dirpath, root).count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
        if exe_name in filenames:
            found.append((depth, os.path.join(dirpath, exe_name)))
    return min(found)[1] if found else None


def find_losslesscut(system=None):
    system = system or platform.system()
    root = losslesscut_dir(system)
    if not os.path.isdir(root):
        return None
    return _find_exe(root, LOSSLESSCUT_EXE_NAMES[system])


def _extract(archive, dest, system):
    if system == "Windows":
        # Windows 10 1803+ ships libarchive's tar, which reads 7z - no 7-Zip
        # or py7zr needed.
        tar = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
        result = subprocess.run(
            [tar, "-xf", archive, "-C", dest], capture_output=True, text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if result.returncode != 0:
            raise EditorError(f"Couldn't unpack LosslessCut: {result.stderr.strip()[-300:]}")
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")


def install_losslesscut(log=print, system=None):
    """Downloads and unpacks LosslessCut. Returns its executable's path.
    Raises on any failure, leaving no half-installed copy behind."""
    import obs_install  # imports updater (network) - only needed here

    system = system or platform.system()
    if not losslesscut_supported(system):
        raise EditorError(f"LosslessCut isn't available through ClipEm on {system}.")
    existing = find_losslesscut(system)
    if existing:
        return existing
    platform_dir = os.path.dirname(losslesscut_dir(system))
    os.makedirs(platform_dir, exist_ok=True)
    staging = os.path.join(platform_dir, STAGING_DIR_NAME)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    archive = os.path.join(platform_dir, "losslesscut-download" + (".7z" if system == "Windows" else ".tar.bz2"))
    try:
        log(f"Downloading LosslessCut (~{LOSSLESSCUT_DOWNLOAD_MB[system]} MB)...")
        entry = obs_install.fetch_verified(system.lower(), "losslesscut", archive, log=log)
        log("Unpacking LosslessCut...")
        _extract(archive, staging, system)
        exe_name = LOSSLESSCUT_EXE_NAMES[system]
        if not _find_exe(staging, exe_name):
            raise EditorError(f"The LosslessCut download has no {exe_name}.")
        shutil.rmtree(losslesscut_dir(system), ignore_errors=True)
        os.replace(staging, losslesscut_dir(system))
        exe = find_losslesscut(system)
        if system != "Windows":
            os.chmod(exe, 0o755)
        _record_version(entry)
        log("LosslessCut installed.")
        return exe
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.exists(archive):
            os.remove(archive)


def _record_version(entry):
    """Its version is in the release URL (.../download/v3.69.0/...), not the
    file name obs_install.record_installed_version reads."""
    parts = entry.get("url", "").split("/")
    version = next((p[1:] for p in parts if p.startswith("v") and p[1:2].isdigit()), None)
    if version:
        import obs_install

        versions = load_json(obs_install.INSTALLED_VERSIONS_PATH, {}) or {}
        versions["losslesscut"] = version
        write_json_atomic(obs_install.INSTALLED_VERSIONS_PATH, versions)


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


def open_in_editor(editor_path, clip_path, log=print, system=None, popen=subprocess.Popen, clock=time.monotonic):
    """Starts the editor on clip_path, detached. On Linux, retries LosslessCut
    (any Electron app) once with --no-sandbox if it dies at startup over
    its sandbox."""
    system = system or platform.system()
    cmd = editor_command(editor_path, clip_path, system)
    if system != "Linux":
        popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached_kwargs())
        return
    started = clock()
    # stderr to a file, not a pipe: the editor keeps logging long after
    # this returns, and a pipe nobody reads would fill up and stall it.
    os.makedirs(os.path.dirname(EDITOR_LOG_PATH), exist_ok=True)
    with open(EDITOR_LOG_PATH, "w+", encoding="utf-8", errors="replace") as err_log:
        proc = popen(cmd, stdout=subprocess.DEVNULL, stderr=err_log, **_detached_kwargs())
        try:
            proc.wait(timeout=SANDBOX_FAILURE_SECONDS)
        except subprocess.TimeoutExpired:
            return  # still running - it started fine
        err_log.seek(0)
        err = err_log.read()
    if proc.returncode and clock() - started < SANDBOX_FAILURE_SECONDS + 1 and "sandbox" in err.lower():
        log(f"{editor_label(editor_path)} couldn't start its sandbox - starting it with --no-sandbox.")
        popen([cmd[0], "--no-sandbox", *cmd[1:]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              **_detached_kwargs())
    elif proc.returncode:
        raise EditorError(f"{editor_label(editor_path)} exited straight away: {err.strip()[-300:]}")
