"""The clip editor's window: a pinned Electron release - Chromium with
H.264/AAC playback built in - that ClipEm installs into
App/<platform>/editor-window, so the editor never depends on a browser (or
a system web view) the user may or may not have. It runs ClipEm's own tiny
Electron app, tray-widget/editor-window/, which just shows the editor page
in a window with ClipEm's name and icon.

Setup installs it (setup.ensure_editor_window), components.py keeps it in
step with the signed install manifest, and clip_browser.py runs it. The
download is Electron's own release zip, pinned in Dev/release/upstream-pins.json
by Dev/release.sh, which reads ELECTRON_VERSION below.

The executable is renamed (Linux/Windows) to EXE_NAMES: Linux taskbars
(KDE Plasma) match a window to a desktop entry by its process name before
its app id, so an "electron" process could pick up another app's icon.
"""

import os
import platform
import shutil
import stat
import subprocess
import zipfile

from clipem_common import APP_DIR

ELECTRON_VERSION = "44.5.1"
DIR_NAME = "editor-window"
EXE_NAMES = {"Linux": "clipem-editor", "Windows": "ClipEm Editor.exe"}
_ORIGINAL_EXE = {"Linux": "electron", "Windows": "electron.exe"}
MACOS_EXE = os.path.join("Electron.app", "Contents", "MacOS", "Electron")
# Electron's zips unpack to ~300 MB in a few hundred files.
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 20000
SHA256_COMPONENT = "editor_window"


class ExtractError(Exception):
    pass


def _system(system=None):
    return system or platform.system()


def runtime_dir(system=None):
    folder = {"Windows": "Windows", "Darwin": "macOS"}.get(_system(system), "Linux")
    return os.path.join(APP_DIR, folder, DIR_NAME)


def exe_path(system=None, root=None):
    system = _system(system)
    root = root or runtime_dir(system)
    return os.path.join(root, MACOS_EXE if system == "Darwin" else EXE_NAMES.get(system, EXE_NAMES["Linux"]))


def installed(system=None):
    return os.path.isfile(exe_path(system))


def manifest_key(system=None, machine=None):
    """(platform key, artifact) of this platform's Electron in the manifest."""
    system = _system(system)
    if system == "Windows":
        return "windows", "editor_window"
    if system == "Darwin":
        machine = (machine or platform.machine()).lower()
        return "macos", "editor_window_arm64" if machine in ("arm64", "aarch64") else "editor_window_x86_64"
    return "linux", "editor_window"


def check_zip(zf):
    """No absolute paths or ".." components, and sane size limits."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ExtractError("The editor window package has too many entries.")
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        parts = name.split("/")
        if name.startswith("/") or ":" in parts[0] or ".." in parts:
            raise ExtractError(f"The editor window package contains an unsafe path: {info.filename}")
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise ExtractError("The editor window package is larger than expected.")


def extract(archive, dest, system=None, run=subprocess.run):
    """Unpacks Electron's zip into dest (which must not exist yet) and
    renames its executable. Linux keeps each file's permission bits (Python's
    zipfile drops them); macOS uses ditto, which keeps Electron.app's
    symlinks and signature intact."""
    system = _system(system)
    with zipfile.ZipFile(archive) as zf:
        check_zip(zf)
        if system == "Darwin":
            os.makedirs(dest)
            run(["ditto", "-x", "-k", archive, dest], check=True, capture_output=True)
        else:
            zf.extractall(dest)
            if system != "Windows":
                for info in zf.infolist():
                    mode = (info.external_attr >> 16) & 0o777
                    path = os.path.join(dest, *info.filename.replace("\\", "/").split("/"))
                    if mode and os.path.isfile(path) and not os.path.islink(path):
                        os.chmod(path, mode)
    if system in _ORIGINAL_EXE:
        original = os.path.join(dest, _ORIGINAL_EXE[system])
        if not os.path.isfile(original):
            raise ExtractError(f"The editor window package has no {_ORIGINAL_EXE[system]}.")
        os.replace(original, exe_path(system, dest))
        if system == "Linux":
            os.chmod(exe_path(system, dest), 0o755)
    if not os.path.isfile(exe_path(system, dest)):
        raise ExtractError("The editor window package is missing its executable.")


def install(fetch, system=None, log=print):
    """Downloads (fetch(platform_key, artifact, path) -> manifest entry,
    sha256-checked) and installs this platform's Electron in place of any
    older one: unpacked beside it first, then swapped in by rename, so a
    failed download leaves the working one alone. Returns the entry."""
    system = _system(system)
    dest = runtime_dir(system)
    parent = os.path.dirname(dest)
    staging = os.path.join(parent, f".{DIR_NAME}.update-staging")
    backup = os.path.join(parent, f".{DIR_NAME}.update-backup")
    archive = os.path.join(parent, f"{DIR_NAME}-download.zip")
    os.makedirs(parent, exist_ok=True)
    for leftover in (staging, backup):
        shutil.rmtree(leftover, ignore_errors=True)
    try:
        platform_key, artifact = manifest_key(system)
        entry = fetch(platform_key, artifact, archive)
        log("Extracting the editor window...")
        extract(archive, staging, system)
        if os.path.lexists(dest):
            os.replace(dest, backup)
        try:
            os.replace(staging, dest)
        except OSError:
            if os.path.lexists(backup):
                os.replace(backup, dest)
            raise
    finally:
        if os.path.exists(archive):
            os.remove(archive)
        for leftover in (staging, backup):
            shutil.rmtree(leftover, ignore_errors=True)
    return entry


def linux_needs_no_sandbox(root=None):
    """Electron's sandbox needs unprivileged user namespaces or a setuid-root
    chrome-sandbox. Where neither is available (e.g. Ubuntu 24.04's AppArmor
    restriction), it refuses to start without --no-sandbox. The window only
    ever shows ClipEm's own page from 127.0.0.1."""
    helper = os.path.join(root or runtime_dir("Linux"), "chrome-sandbox")
    try:
        st = os.stat(helper)
        if st.st_uid == 0 and st.st_mode & stat.S_ISUID:
            return False
    except OSError:
        pass
    for path, blocked in (("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", "1"),
                          ("/proc/sys/kernel/unprivileged_userns_clone", "0"),
                          ("/proc/sys/user/max_user_namespaces", "0")):
        try:
            with open(path, "r", encoding="ascii") as f:
                if f.read().strip() == blocked:
                    return True
        except OSError:
            continue
    return False
