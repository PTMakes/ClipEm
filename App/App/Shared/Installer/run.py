"""
Universal run entry point for ClipEm.

RunWindows.bat, RunMac.command, and RunLinux.sh are all thin shims that
just find a Python interpreter and exec this file - all the actual
platform-dispatch logic (which OBS binary to start, how to wire in the
shared config, whether to also start the tray widget) lives here, once,
instead of being duplicated across three shell dialects.

Started detached: OBS (and the tray widget, if its dependencies are
installed) keep running after this script exits.
"""

import os
import platform
import subprocess
import sys
import threading
import time

USAGE = """
Usage: run.py [--boot]

Starts ClipEm - OBS, recording into its replay buffer, and the tray icon.
This is what the ClipEm shortcut runs. A chosen update is installed first,
and new ones are offered afterwards.

  --boot      Start only what Settings' "Autostart with PC boot" allows
              (the boot autostart entry passes this).
  -h, --help  Show this help and exit.

ClipEm passes these itself: --restarted, --post-update, --restart-obs.
"""
if __name__ == "__main__" and {"-h", "--help"} & set(sys.argv[1:]):
    print(USAGE.strip())
    sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_window  # noqa: E402
import autostart  # noqa: E402
import components  # noqa: E402
import obs_config  # noqa: E402
import obs_install  # noqa: E402
import obs_process  # noqa: E402
import portable_launch_helper as helper  # noqa: E402
import reinstall  # noqa: E402
import updater  # noqa: E402
from clipem_common import (  # noqa: E402
    APP_DIR,
    DEFAULT_RUN_CONFIG,
    INSTALLER_DIR,
    OBS_RESTART_FLAG,
    ROOT_DIR,
    SHARED_DIR,
    TRAY_LOCK_PATH,
    TRAY_PYTHON_PIN_FILENAME,
    acquire_instance_lock,
    is_tray_widget_process,
    load_run_config,
    pid_alive,
    setup_logging,
    stop_tray_widget,
)

TRAY_WIDGET_SCRIPT = os.path.join(SHARED_DIR, "tray-widget", "tray_widget.py")

# Set for the tray widget when OBS was started without --startreplaybuffer
# because the clips folder was missing - see _obs_launch_flags.
REPLAY_BUFFER_DEFERRED_ENV = "CLIPEM_REPLAY_BUFFER_DEFERRED"

POST_UPDATE_FLAG = "--post-update"
# Added by _restart_self: this launch replaces the one that started it, which
# may not have exited quite yet - not a second copy of ClipEm.
RESTARTED_FLAG = "--restarted"
# Held (pid) for as long as a launch runs - including while it shows the
# update prompt or installs an update. In App/, which updates never replace.
RUN_LOCK_PATH = os.path.join(APP_DIR, ".run.lock")
RESTART_WAIT_SECONDS = 15

# Windows process-creation flags for everything run.py starts, so nothing
# is tied to (or pops up) a console window: the desktop shortcut runs this
# under pythonw.exe, and a python.exe child would otherwise open a console
# of its own - one that, when closed, takes that child down with it.
if sys.platform == "win32":
    _NO_CONSOLE_FLAGS = subprocess.CREATE_NO_WINDOW
    _DETACHED_FLAGS = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def _windowless_python(python_exe):
    """pythonw.exe beside python_exe on Windows (same interpreter, no
    console), else python_exe unchanged."""
    if sys.platform == "win32":
        pythonw = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
        if os.path.exists(pythonw):
            return pythonw
    return python_exe

log = setup_logging("run")


def _clear_sentinel(obs_studio_config_dir):
    sentinel_dir = os.path.join(obs_studio_config_dir, ".sentinel")
    if not os.path.isdir(sentinel_dir):
        return
    for name in os.listdir(sentinel_dir):
        try:
            os.remove(os.path.join(sentinel_dir, name))
        except OSError:
            pass


def _resolve_tray_python(tray_deps_dir, fallback_python_exe):
    """Returns the interpreter to run the tray widget with.

    Prefers the interpreter setup.py pinned in tray_deps_dir (see
    TRAY_PYTHON_PIN_FILENAME above) over fallback_python_exe (whatever
    "python3" resolves to right now), so a system Python upgrade since Setup
    ran doesn't hand tray-deps' compiled extensions to an interpreter they
    weren't built for. Falls back if there's no pin (older installs from
    before this existed, or tray_deps_dir wasn't given - Windows never has
    one) or the pinned interpreter no longer exists on disk (e.g. it was
    since uninstalled) - _start_tray_widget's own prereq check will then
    catch and report the same mismatch this is trying to avoid.
    """
    if not tray_deps_dir:
        return fallback_python_exe
    try:
        with open(os.path.join(tray_deps_dir, TRAY_PYTHON_PIN_FILENAME), "r", encoding="utf-8") as f:
            pinned = f.read().strip()
    except OSError:
        return fallback_python_exe
    return pinned if pinned and os.path.exists(pinned) else fallback_python_exe


def _start_tray_widget(python_exe, extra_env=None, tray_deps_dir=None):
    if _tray_widget_running():  # e.g. Settings restarting just OBS
        log.info("The tray widget is already running.")
        return
    python_exe = _resolve_tray_python(tray_deps_dir, python_exe)
    check_env = dict(os.environ)
    if tray_deps_dir:
        existing = check_env.get("PYTHONPATH", "")
        check_env["PYTHONPATH"] = tray_deps_dir + (os.pathsep + existing if existing else "")
    if extra_env:
        check_env.update(extra_env)
    # Linux talks StatusNotifierItem/dbusmenu directly over D-Bus (dbus-next)
    # instead of through pystray's GTK/AppIndicator backend - see
    # tray-widget/sni_tray.py and requirements.txt.
    #
    # Imports PIL.Image specifically, not just `import PIL` - PIL/__init__.py
    # doesn't itself touch Pillow's compiled _imaging extension, so a bare
    # `import PIL` still succeeds even when that extension is unusable (e.g.
    # tray-deps was pip-installed under a different Python version than the
    # one now running this - its ABI-tagged .so won't load under a new one).
    # tray_widget.py's own `from PIL import Image, ImageDraw` would hit that
    # same failure moments after being launched - check for it here instead,
    # so a stale/mismatched tray-deps is reported instead of silently
    # spawning a subprocess that's already going to crash.
    tray_backend_module = "dbus_next" if sys.platform == "linux" else "pystray"
    check = subprocess.run(
        [python_exe, "-c", f"import {tray_backend_module}, websocket\nfrom PIL import Image, ImageDraw"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=check_env,
        text=True,
        **({"creationflags": _NO_CONSOLE_FLAGS} if sys.platform == "win32" else {}),
    )
    if check.returncode != 0:
        log.info(
            "(Tray widget dependencies aren't usable with this Python - skipping it. "
            "Run Setup again and reinstall the tray widget to fix this.)"
        )
        if check.stderr:
            log.info(check.stderr.strip().splitlines()[-1])
        return

    kwargs = {}
    if sys.platform == "win32":
        # Its own windowless process, not sharing whatever console launched
        # run.py - closing that console used to kill the tray widget too.
        python_exe = _windowless_python(python_exe)
        kwargs["creationflags"] = _DETACHED_FLAGS
    subprocess.Popen([python_exe, TRAY_WIDGET_SCRIPT], env=check_env, **kwargs)
    log.info("Started the tray widget.")


def _enable_windows_tk():
    """The embeddable Windows Python has no registry entries telling Tcl/Tk
    where their script libraries are - point them at the copies Setup placed
    beside python.exe (same as setup.configure_tcltk_env)."""
    if sys.platform != "win32":
        return
    python_root = os.path.dirname(sys.executable)
    for var, sub in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        path = os.path.join(python_root, "tcl", sub)
        if os.path.isdir(path):
            os.environ.setdefault(var, path)


def _show_error(message):
    """Best-effort error dialog - run.py usually has no console (the
    shortcut and boot autostart run it windowless), so a log line alone
    would go unnoticed."""
    try:
        _enable_windows_tk()
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk(className="ClipEm")
        root.withdraw()
        app_window.set_window_icon(root)
        messagebox.showerror("ClipEm", message, parent=root)
        root.destroy()
    except Exception:
        pass


def _fatal(message):
    log.error(message)
    _show_error(message)
    sys.exit(message)


def _run_with_notice(text, work):
    """Runs work() while showing a small window with `text` (when there's a
    display to show one on), so a long download isn't a silent pause after
    clicking the shortcut. Returns work()'s result; re-raises its exception."""
    try:
        _enable_windows_tk()
        import tkinter

        root = tkinter.Tk(className="ClipEm")
    except Exception:
        return work()
    app_window.set_window_icon(root)

    result = {}

    def worker():
        try:
            result["value"] = work()
        except BaseException as exc:
            result["error"] = exc

    root.title("ClipEm")
    root.resizable(False, False)
    tkinter.Label(root, text=text, padx=24, pady=18, justify="left").pack()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def poll():
        if thread.is_alive():
            root.after(200, poll)
        else:
            root.destroy()

    root.after(200, poll)
    root.mainloop()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _ensure_obs(system):
    """ClipEm's own OBS for this platform, downloaded first if it's missing
    (an install that never had it, or a deleted/partial one). ClipEm never
    runs any other OBS, so if it can't be installed, this stops with an
    error."""
    exe = obs_install.obs_exe(system)
    if os.path.exists(exe):
        return exe
    log.info("ClipEm's OBS is missing - downloading it.")
    try:
        _run_with_notice(
            "ClipEm's copy of OBS is missing, so it's being downloaded now.\n"
            "This only happens once and may take a few minutes.",
            lambda: obs_install.install_obs(system, log=log.info),
        )
    except obs_install.ObsInstallError as exc:
        _fatal(
            f"ClipEm's copy of OBS is missing and couldn't be downloaded ({exc}).\n\n"
            "Check your internet connection and start ClipEm again, or re-run the ClipEm installer."
        )
    return exe


def _obs_launch_flags():
    """(OBS's flags after the portable one, extra env for the tray widget).
    With the clips folder missing - e.g. on a cloud drive that isn't mounted
    yet at login - --startreplaybuffer would only get OBS's "path is
    invalid" error dialog, so OBS starts without it and the tray widget
    starts the replay buffer once the folder is there
    (tray-widget/clip_folder.py)."""
    clips_dir = obs_config.load_clip_folder()
    if os.path.isdir(clips_dir):
        return ["--startreplaybuffer", "--minimize-to-tray", "--multi"], {}
    log.warning(f"Clips folder {clips_dir} isn't there - starting OBS without the replay buffer; "
                "the tray widget starts it once the folder is.")
    return ["--minimize-to-tray", "--multi"], {REPLAY_BUFFER_DEFERRED_ENV: "1"}


def run_windows(config):
    platform_dir = obs_install.platform_dir("Windows")
    exe = _ensure_obs("Windows")
    obs_install.warn_if_windows_vc_runtime_missing(platform_dir, log=log.warning)

    helper.prepare(platform_dir)
    _clear_sentinel(os.path.join(platform_dir, "config", "obs-studio"))

    obs_flags, tray_env = _obs_launch_flags()
    if config["autostart_obs"]:
        subprocess.Popen(
            [exe, "--portable", *obs_flags],
            cwd=os.path.dirname(exe),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        log.info("Started OBS.")
    else:
        log.info("Boot autostart for OBS is off - config wired, start it yourself.")

    if config["autostart_tray"]:
        python_exe = sys.executable  # the bundled embeddable Python running this script
        _start_tray_widget(python_exe, extra_env=tray_env)
    else:
        log.info("Boot autostart for the tray widget is off.")


def run_macos(config):
    platform_dir = os.path.join(APP_DIR, "macOS")
    exe = os.path.join(platform_dir, "OBS.app", "Contents", "MacOS", "OBS")
    if not os.path.exists(exe):
        sys.exit("OBS.app not found. Run SetupMac.command first.")

    helper.prepare(platform_dir)
    _clear_sentinel(os.path.join(platform_dir, "config", "obs-studio"))

    obs_flags, tray_env = _obs_launch_flags()
    if config["autostart_obs"]:
        subprocess.Popen([exe, "--portable", *obs_flags])
        log.info("Started OBS.")
    else:
        log.info("Boot autostart for OBS is off - config wired, start it yourself.")

    if config["autostart_tray"]:
        _start_tray_widget(sys.executable, extra_env=tray_env, tray_deps_dir=os.path.join(platform_dir, "tray-deps"))
    else:
        log.info("Boot autostart for the tray widget is off.")


def run_linux(config):
    platform_dir = obs_install.platform_dir("Linux")
    # Only ever ClipEm's own build (Dev/build/build-obs-from-source-for-linux.sh:
    # ENABLE_RELOCATABLE + ENABLE_PORTABLE_CONFIG), never a system-wide OBS.
    exe = _ensure_obs("Linux")
    obs_install_dir = os.path.dirname(os.path.dirname(exe))

    # OBS's -p portable mode reads <install prefix>/config (sibling of bin/),
    # so the config link goes in obs-install/config. platform_dir stays
    # App/Linux so the helper still finds App/Shared as its parent's Shared/.
    helper.prepare(platform_dir, config_link_name=os.path.join("obs-install", "config"))
    _clear_sentinel(os.path.join(obs_install_dir, "config", "obs-studio"))
    obs_flags, tray_env = _obs_launch_flags()
    if config["autostart_obs"]:
        subprocess.Popen(
            [exe, "-p", *obs_flags],
            cwd=os.path.dirname(exe),
        )
        log.info("Started OBS.")
    else:
        log.info("Boot autostart for OBS is off - config wired, start it yourself.")

    if config["autostart_tray"]:
        tray_env["PYSTRAY_BACKEND"] = "appindicator"
        _start_tray_widget(sys.executable, extra_env=tray_env, tray_deps_dir=os.path.join(platform_dir, "tray-deps"))
    else:
        log.info("Boot autostart for the tray widget is off.")


def _install_pending_update():
    """Before OBS starts: installs an update the user chose at a launch's
    prompt (or in Settings). Nothing ClipEm runs is open yet, so OBS, the
    bundled Python and the tray's packages can be replaced too, not just
    App/Shared.

    App/Shared goes first, then this script re-execs (--post-update, the
    choice still pending) so the new release's own code installs the rest -
    it knows what that release needs. The choice is cleared before that
    part, so a failure can't repeat on every launch; it's offered again
    afterwards instead."""
    try:
        if not updater.load_update_config()["install_on_next_launch"]:
            return
        manifest = updater.fetch_manifest()  # offline: keep the choice for next launch
    except Exception as exc:
        log.warning(f"(Couldn't install the chosen update yet - will try again next launch: {exc})")
        return
    try:
        result = updater.check_for_update()
        post_update = POST_UPDATE_FLAG in sys.argv[1:]
        if result["shared_update"] and not post_update:
            log.info(f"Installing ClipEm {result['remote_version']}...")
            ok, message = _run_with_notice("Updating ClipEm...", lambda: updater.apply_update(manifest))
            log.info(message)
            if not ok:
                updater.change_update_config(install_on_next_launch=False)
                _show_error(message)
                return
            _restart_self([POST_UPDATE_FLAG])
        updater.change_update_config(install_on_next_launch=False)
        plan = result["components"]
        if not plan:
            return
        log.info(f"Updating: {'; '.join(components.describe(plan))}")
        args = [arg for arg in sys.argv[1:] if arg != POST_UPDATE_FLAG] + [POST_UPDATE_FLAG]
        outcome = _run_with_notice(
            "Updating ClipEm - downloading the new versions of:\n  "
            + "\n  ".join(components.describe(plan))
            + "\n\nThis can take a few minutes.",
            lambda: components.install(manifest, plan, log.info, run_args=args),
        )
        if outcome == components.HANDOFF:
            log.info("Exiting so the new Python can be swapped in - it starts ClipEm again.")
            sys.exit(0)
        if outcome == components.RESTART:
            _restart_self([POST_UPDATE_FLAG])
    except Exception as exc:
        log.exception("Installing the update failed")
        _show_error(f"ClipEm couldn't finish updating ({exc}).\n\nIt will keep using the current version "
                    "and offer the update again.")


def _reinstall_if_requested():
    """Before OBS starts: carries out a clean reinstall chosen in Settings
    (see reinstall.py). The request is cleared before anything that could
    fail on every launch, so a failure is reported once, not repeatedly.
    Exits instead of returning when Setup takes over."""
    request = reinstall.load_request()
    if not request:
        return
    try:
        manifest = updater.fetch_manifest()
    except Exception as exc:
        reinstall.clear_request()
        log.warning(f"Reinstall: couldn't get the update manifest: {exc}")
        _show_error(f"ClipEm couldn't download a fresh copy to reinstall from ({exc}).\n\n"
                    "Nothing was changed - starting ClipEm as it is.")
        return
    try:
        if request["stage"] == reinstall.STAGE_SHARED:
            log.info(f"Reinstalling ClipEm {manifest['version']} (request: {request})...")
            ok, message = _run_with_notice(
                "Reinstalling ClipEm...", lambda: reinstall.reinstall_shared(request, manifest),
            )
            log.info(message)
            if not ok:
                reinstall.clear_request()
                _show_error(message)
                return
            _restart_self([])  # the freshly installed run.py does the rest
        reinstall.clear_request()
        plan = reinstall.component_plan(manifest)
        if plan:
            parts = reinstall.describe_plan(plan)
            log.info(f"Reinstalling: {'; '.join(parts)}")
            _run_with_notice(
                "Reinstalling ClipEm - downloading:\n  " + "\n  ".join(parts) + "\n\nThis can take a few minutes.",
                lambda: reinstall.reinstall_components(manifest, plan, log.info),
            )
        log.info("Reinstall finished.")
    except Exception as exc:
        log.exception("Reinstalling failed")
        _show_error(f"ClipEm couldn't finish reinstalling ({exc}).\n\nWhat was already reinstalled stays; "
                    "the rest keeps the previous version.")
        return
    if not request.get("keep_obs_settings"):
        _start_setup()


def _start_setup():
    """Opens Setup (its finish page starts ClipEm again) and exits."""
    log.info("OBS settings were reset - starting Setup instead of OBS.")
    kwargs = {"cwd": ROOT_DIR}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_FLAGS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.join(INSTALLER_DIR, "setup.py")], **kwargs)
    sys.exit(0)


UPDATE_NOW, UPDATE_NEXT_LAUNCH, UPDATE_SKIP = "now", "next_launch", "skip"


def _ask_about_update(changes):
    """Shows the update prompt listing `changes` (one line each) and returns
    UPDATE_NOW, UPDATE_NEXT_LAUNCH, UPDATE_SKIP, or None if it was closed
    without choosing (or can't be shown) - then it's simply offered again at
    the next launch."""
    try:
        _enable_windows_tk()
        import tkinter

        root = tkinter.Tk(className="ClipEm")
    except Exception as exc:
        log.info(f"(Couldn't show the update prompt: {exc})")
        return None
    app_window.set_window_icon(root)
    root.title("ClipEm Update Available")
    root.resizable(False, False)
    # Launched at boot or from a shortcut - don't open hidden behind
    # whatever window has focus.
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))

    choice = {}

    def choose(value):
        choice["value"] = value
        root.destroy()

    frame = tkinter.Frame(root, padx=20, pady=16)
    frame.pack()
    tkinter.Label(
        frame, justify="left",
        text=(
            "An update for ClipEm is available:\n\n"
            + "\n".join(f"  \u2022 {line}" for line in changes)
            + "\n\nUpdating keeps all of your settings. Restarting closes and reopens\n"
            "OBS and the tray icon - clips not saved yet are lost."
        ),
    ).pack(anchor="w", pady=(0, 12))
    for text, value in (
        ("Update and restart now", UPDATE_NOW),
        ("Update and install during next restart", UPDATE_NEXT_LAUNCH),
        ("Do not update", UPDATE_SKIP),
    ):
        tkinter.Button(frame, text=text, command=lambda v=value: choose(v)).pack(fill="x", pady=2)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return choice.get("value")


def _offer_update():
    """After OBS has started (so a boot launch isn't held up waiting for an
    answer): when checking for updates is allowed, looks for a newer version
    and asks what to do with it. Nothing is ever installed without asking -
    and only ever by _install_pending_update, at a launch. Best-effort and
    silent about network failures - a launch should never be broken by
    clipem.app being unreachable."""
    if POST_UPDATE_FLAG in sys.argv[1:]:
        return
    try:
        config = updater.load_update_config()
        if not config["check_for_updates"]:
            return
        result = updater.check_for_update()
        if not result["update_available"]:
            return
        remote_version = result["remote_version"]
        changes = updater.describe_update(result)
        if remote_version == config["skipped_version"]:
            log.info(f"An update is available ({'; '.join(changes)}), but the user chose not to install it.")
            return
        log.info(f"An update is available ({'; '.join(changes)}) - asking the user.")
        choice = _ask_about_update(changes)
        log.info(f"Update prompt answer: {choice}")
        if choice == UPDATE_SKIP:
            updater.change_update_config(skipped_version=remote_version)
        elif choice == UPDATE_NEXT_LAUNCH:
            updater.change_update_config(install_on_next_launch=True)
        elif choice == UPDATE_NOW:
            updater.change_update_config(install_on_next_launch=True)
            _restart_clipem_to_update()
    except Exception as exc:
        log.warning(f"(Update check failed - continuing without updating: {exc})")


def _restart_clipem_to_update():
    """Closes OBS and the tray widget and runs this script again, which
    installs the chosen update before starting them back up. If OBS won't
    close, the update stays chosen for the next launch."""
    if not obs_process.close_obs_stopping_outputs(log):
        _show_error("OBS didn't close (it may be showing a dialog), so the update will be installed "
                    "the next time ClipEm starts.")
        return
    stop_tray_widget(log)
    _restart_self([])


def _read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _what_is_already_open():
    """Why this would be a second copy of ClipEm (another launch still
    running - e.g. showing its update prompt or installing an update - or
    this install's OBS or tray widget already open), or None. Takes the
    launch lock when it's free."""
    if RESTARTED_FLAG in sys.argv[1:]:
        # Started by the launch it replaces - give that one time to exit.
        previous = _read_pid(RUN_LOCK_PATH)
        deadline = time.monotonic() + RESTART_WAIT_SECONDS
        while previous and previous != os.getpid() and pid_alive(previous) and time.monotonic() < deadline:
            time.sleep(0.2)
    if not acquire_instance_lock(RUN_LOCK_PATH):
        return "ClipEm is still starting up or installing an update."
    if RESTARTED_FLAG in sys.argv[1:]:
        return None  # the restarts close OBS and the tray widget first
    if obs_process.obs_running():
        return "ClipEm's OBS is already running."
    if OBS_RESTART_FLAG in sys.argv[1:]:
        return None  # Settings' "Restart OBS": the tray widget stays, on purpose
    if _tray_widget_running():
        return "ClipEm's tray icon is already running."
    return None


def _tray_widget_running():
    tray_pid = _read_pid(TRAY_LOCK_PATH)
    return bool(tray_pid and tray_pid != os.getpid() and pid_alive(tray_pid) and is_tray_widget_process(tray_pid))


def _ask_already_open(reason):
    """True for Proceed Anyway; False for Cancel, closing the window, or
    when no window can be shown."""
    try:
        _enable_windows_tk()
        import tkinter

        root = tkinter.Tk(className="ClipEm")
    except Exception as exc:
        log.info(f"(Couldn't ask whether to start a second copy: {exc})")
        return False
    app_window.set_window_icon(root)
    root.title("ClipEm Is Already Open")
    root.resizable(False, False)
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    choice = {"proceed": False}

    def proceed():
        choice["proceed"] = True
        root.destroy()

    frame = tkinter.Frame(root, padx=20, pady=16)
    frame.pack()
    tkinter.Label(
        frame, justify="left",
        text=(
            f"ClipEm is already open. {reason}\n\n"
            "Starting it again opens a second OBS, which records the same\n"
            "screen and audio twice. Use the tray icon to show OBS instead."
        ),
    ).pack(anchor="w", pady=(0, 12))
    buttons = tkinter.Frame(frame)
    buttons.pack(fill="x")
    tkinter.Button(buttons, text="Proceed Anyway", command=proceed).pack(side="right")
    cancel = tkinter.Button(buttons, text="Cancel", command=root.destroy)
    cancel.pack(side="right", padx=(0, 8))
    cancel.focus_set()
    root.bind("<Escape>", lambda _event: root.destroy())
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return choice["proceed"]


def _claim_launch():
    """Asks before starting a second copy of ClipEm (Cancel exits). Returns
    whether this launch may install or offer updates - not while another
    launch is running, which could be doing the same."""
    reason = _what_is_already_open()
    if reason is None:
        return True
    log.info(f"Already open: {reason} Asking the user.")
    if not _ask_already_open(reason):
        log.info("Not starting a second copy of ClipEm.")
        sys.exit(0)
    log.info("Starting another copy anyway (Proceed Anyway).")
    return not reason.startswith("ClipEm is still starting")


def _restart_self(extra_args):
    """Re-runs this script so freshly-updated modules take over. POSIX exec
    passes argv through untouched; Windows' os.execv joins argv without
    quoting (a path with a space splits in two), so spawn and exit there."""
    extra_args = list(extra_args) + [RESTARTED_FLAG]
    args = [arg for arg in sys.argv[1:] if arg not in extra_args] + extra_args
    argv = [sys.executable, os.path.abspath(__file__)] + args
    if sys.platform == "win32":
        subprocess.Popen(argv, creationflags=_DETACHED_FLAGS)
        sys.exit(0)
    os.execv(sys.executable, argv)


def main():
    # A plain double-click of the desktop shortcut always starts
    # everything (unchanged from before boot-autostart existed) - only the
    # OS-level autostart entry setup.py's apply_boot_autostart() creates
    # invokes this with --boot, and only then do the saved preferences
    # (what to launch automatically at boot) apply.
    may_update = _claim_launch()
    if may_update:
        _reinstall_if_requested()
        _install_pending_update()
    # Before OBS ever starts: the shipped obs-websocket password is public
    # (same file in every install), so replace it if Setup hasn't yet.
    try:
        obs_config.ensure_obs_websocket_password()
    except OSError as exc:
        log.warning(f"Could not randomize the obs-websocket password: {exc}")
    # Settings changed in the tray's Settings dialog while OBS was running
    # are queued until OBS is closed (see obs_config's "Settings waiting for
    # OBS to be closed") - apply them now, before OBS reads its config.
    # Skipped if this install's OBS is somehow still running (it would
    # overwrite the scene collection on exit).
    if obs_config.load_pending_obs_settings():
        if obs_process.obs_running():
            log.info("Queued OBS settings not applied yet - OBS is still running.")
        else:
            try:
                applied = obs_config.apply_pending_obs_settings()
                log.info(f"Applied queued OBS settings: {', '.join(sorted(applied))}")
            except Exception as exc:
                log.warning(f"Could not apply queued OBS settings: {exc}")
    if not obs_process.obs_running():
        try:
            if obs_config.migrate_discord_target_size():
                log.info(f"Smaller-copy target moved to Discord's new {obs_config.DISCORD_TARGET_MB} MB limit.")
        except Exception as exc:
            log.warning(f"Could not update the smaller-copy target size: {exc}")
    # Before the tray widget starts: it only names its notifications "ClipEm"
    # once this entry exists (tray_widget.use_clipem_notification_name).
    try:
        path = autostart.ensure_start_menu_entry()
        if path:
            log.info(f"Added ClipEm to the Start Menu: {path}")
    except Exception as exc:
        log.warning(f"Could not add ClipEm to the Start Menu: {exc}")
    boot = "--boot" in sys.argv[1:]
    config = load_run_config() if boot else dict(DEFAULT_RUN_CONFIG)
    system = platform.system()
    if system == "Windows":
        run_windows(config)
    elif system == "Darwin":
        run_macos(config)
    elif system == "Linux":
        run_linux(config)
    else:
        sys.exit(f"Unsupported platform: {system}")
    if may_update:
        _offer_update()


if __name__ == "__main__":
    main()
