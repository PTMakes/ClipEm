"""
Paths, small file helpers, and logging shared by every ClipEm entry point
(setup.py, run.py, updater.py, the tray widget and its settings dialog).

Standard library only and free of side effects at import time, so anything
- including the always-running tray widget - can import it cheaply. The
tray widget reaches it by putting App/Shared/Installer on sys.path.
"""

import atexit
import hashlib
import json
import logging
import logging.handlers
import os
import sys
import tempfile
import time

INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.dirname(INSTALLER_DIR)
APP_DIR = os.path.dirname(SHARED_DIR)
ROOT_DIR = os.path.dirname(APP_DIR)

# Bumped by Dev/release.sh as part of cutting a release, and shipped with
# the rest of App/Shared so the installed copy knows its own version.
VERSION_PATH = os.path.join(SHARED_DIR, "VERSION")

# Only consulted when run.py is invoked with --boot (the OS-level autostart
# entry autostart.apply_boot_autostart creates) - a plain launch always
# starts everything. Missing/corrupt file means "start everything".
RUN_CONFIG_PATH = os.path.join(SHARED_DIR, "run_config.json")
# custom_editor_path: the clip editor's "Open in..." editor (tray Settings).
# clip_saved_notification: the tray's "Clip saved." notification. On when
# the key is missing (installs from before it existed keep what they had);
# the shipped run_config.json turns it off for new installs.
DEFAULT_RUN_CONFIG = {"autostart_obs": True, "autostart_tray": True, "custom_editor_path": "",
                      "clip_saved_notification": True}

# Written next to the macOS/Linux tray-deps by setup.install_tray_widget():
# the exact interpreter its compiled dependencies (Pillow) were installed
# for - see run.py's _resolve_tray_python().
TRAY_PYTHON_PIN_FILENAME = ".tray-python-executable"

# Base64 Ed25519 public key that clipem.app's install manifest must be
# signed with - see updater.fetch_manifest() and Dev/generate-signing-key.sh.
SIGNING_PUBLIC_KEY_PATH = os.path.join(INSTALLER_DIR, "signing_public_key.txt")

# The running tray widget's single-instance lock (its pid) - see tray_widget.LOCK_PATH.
TRAY_LOCK_PATH = os.path.join(SHARED_DIR, "tray-widget", ".tray.lock")
TRAY_WIDGET_SCRIPT_NAME = "tray_widget.py"

LOG_DIR = os.path.join(APP_DIR, "logs")
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 3

def read_version():
    try:
        with open(VERSION_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text_atomic(path, text):
    """Writes via a temp file in the same folder + os.replace, so a crash or
    full disk mid-write leaves the previous file intact instead of a
    truncated one (these are OBS's own config files - a half-written scene
    collection loses the user's whole setup)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(path, data, **dump_kwargs):
    dump_kwargs.setdefault("indent", 2)
    write_text_atomic(path, json.dumps(data, **dump_kwargs))


def load_json(path, default=None):
    """Parsed JSON at `path`, or `default` if it's missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_run_config():
    config = dict(DEFAULT_RUN_CONFIG)
    loaded = load_json(RUN_CONFIG_PATH)
    if isinstance(loaded, dict):
        config.update(loaded)
    return config


def save_run_config(config):
    write_json_atomic(RUN_CONFIG_PATH, config)


def pid_alive(pid):
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def release_instance_lock(lock_path):
    """Removes lock_path if this process holds it (not if a newer instance
    has taken it over since)."""
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            if f.read().strip() != str(os.getpid()):
                return
        os.remove(lock_path)
    except OSError:
        pass


def start_clipem():
    """Starts run.py detached, as the desktop shortcut does: applies queued
    OBS settings, then starts OBS and the tray widget (whichever aren't
    already running)."""
    import subprocess

    kwargs = {"cwd": ROOT_DIR}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.join(INSTALLER_DIR, "run.py")], **kwargs)


def process_exe_path_win32(pid):
    """Full path of pid's executable on Windows, or None."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def is_tray_widget_process(pid):
    """True only if pid is (still) a ClipEm tray widget. The lock file's pid
    can be stale - the tray crashed or was killed without removing it - and
    the OS may since have handed that pid to an unrelated program, which
    must never be stopped in its place. Anything that can't be checked
    counts as not the tray widget."""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().decode("utf-8", "replace").split("\0")
            return any(os.path.basename(arg) == TRAY_WIDGET_SCRIPT_NAME for arg in args)
        if sys.platform == "darwin":
            import subprocess

            command = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
            ).stdout
            return TRAY_WIDGET_SCRIPT_NAME in command
        if sys.platform == "win32":
            # A command line isn't readable without much more ctypes - check
            # it's this install's bundled Python running, which the tray is.
            exe = process_exe_path_win32(pid)
            if not exe:
                return False
            exe = os.path.normcase(os.path.realpath(exe))
            return exe.startswith(os.path.normcase(ROOT_DIR) + os.sep) and os.path.basename(exe) in (
                "python.exe", "pythonw.exe",
            )
    except Exception:  # gone meanwhile, no access, ps timed out...
        pass
    return False


def stop_tray_widget(log, timeout_seconds=5):
    """Ends the running tray widget (its pid is in its single-instance lock)
    so run.py can start the freshly updated one. Best-effort."""
    try:
        with open(TRAY_LOCK_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    if pid == os.getpid() or not pid_alive(pid):
        return
    if not is_tray_widget_process(pid):
        log.info(f"Restart ClipEm: the tray lock's pid {pid} isn't the tray widget any more (stale lock) - "
                 "not stopping it.")
        return
    try:
        if sys.platform == "win32":
            import ctypes

            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 0)
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log.warning(f"Restart ClipEm: couldn't stop the tray widget (pid {pid}): {exc}")
        return
    deadline = time.monotonic() + timeout_seconds
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


PR_SET_PDEATHSIG = 1  # linux/prctl.h


def _set_parent_death_signal(signum):
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return libc.prctl(PR_SET_PDEATHSIG, signum, 0, 0, 0) == 0


def exit_with_parent(parent_pid):
    """Linux: have the kernel end this process (SIGTERM) as soon as its
    parent_pid parent dies - for helper windows the tray widget opens, so
    none outlive it even if it's killed or crashes. Exits right away if the
    parent is already gone. A no-op elsewhere (the tray widget stops its
    helpers itself when it quits)."""
    if not sys.platform.startswith("linux") or not parent_pid:
        return
    import signal

    try:
        if not _set_parent_death_signal(signal.SIGTERM):
            return
    except (OSError, AttributeError):
        return
    if os.getppid() != parent_pid:  # died before the signal was set up
        os._exit(0)


def outlive_parent():
    """Undoes exit_with_parent - for a helper that's about to stop its parent
    on purpose (the settings dialog restarting ClipEm after an update).
    Call it on the thread that called exit_with_parent (the main thread):
    the parent-death signal is per thread on Linux, so clearing it on
    another one leaves the process to be killed anyway."""
    if sys.platform.startswith("linux"):
        try:
            _set_parent_death_signal(0)
        except (OSError, AttributeError):
            pass


def acquire_instance_lock(lock_path):
    """True if no other live process holds lock_path (a pid file), in which
    case it's now ours and removed again at exit. False if another process
    that's still running owns it."""
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            old_pid = int(f.read().strip())
    except (ValueError, OSError):
        old_pid = None
    if old_pid and old_pid != os.getpid() and pid_alive(old_pid):
        return False

    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    atexit.register(release_instance_lock, lock_path)
    return True


def run_pip(pip_args):
    """Runs pip, echoing its output (the GUI wizard shows print()ed text in
    its log box - a subprocess's own output would otherwise be lost). Raises
    RuntimeError with pip's last lines if it fails."""
    import subprocess

    result = subprocess.run(pip_args, capture_output=True, text=True, errors="replace")
    output = (result.stdout + result.stderr).strip()
    if output:
        print(output)
    if result.returncode != 0:
        tail = "\n".join(output.splitlines()[-8:]) or "(no output)"
        raise RuntimeError(f"pip install failed (exit {result.returncode}):\n{tail}")


def wheelhouse_dir():
    """App/<platform>/wheelhouse: the pinned packages' wheels, shipped in the
    offline install zips so Setup can install the tray widget (and, on
    Windows, Mic Filter Configuration) without internet."""
    folder = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")
    return os.path.join(APP_DIR, folder, "wheelhouse")


def install_requirements(pip_install_args, requirements):
    """`pip install -r requirements` (pip_install_args: everything up to and
    including "install", plus any --target): from the offline wheelhouse
    when there is one, else - or if it doesn't have every pinned version,
    e.g. after an update changed a pin - from PyPI."""
    wheelhouse = wheelhouse_dir()
    if os.path.isdir(wheelhouse):
        try:
            run_pip(pip_install_args + ["--no-index", "--find-links", wheelhouse, "-r", requirements])
            return
        except RuntimeError:
            print("The bundled packages don't cover these requirements - downloading them instead.")
    run_pip(pip_install_args + ["-r", requirements])


def setup_logging(name):
    """Returns a logger that writes to App/logs/<name>.log (rotated) and, when
    there is one, the console. Boot-time launches run under pythonw/autostart
    with no console at all, so without the file their failures vanish."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, f"{name}.log"),
            maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(file_handler)
    except OSError:
        pass  # read-only install folder - console only
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console)
    return logger
