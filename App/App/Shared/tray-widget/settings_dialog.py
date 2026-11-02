"""Small settings dialog for the ClipEm tray widget.

Launched as a separate process (see tray_widget.py's "Settings..." menu item)
rather than run in-process, since mixing tkinter's own event loop with
pystray's would be fragile. Edits the same on-disk config files setup.py's
wizard does.

OBS reads the websocket, hotkey, clip sound, clip folder and multitrack
audio settings only at startup (the clip sound's .wav file itself is read
on every clip), so while
OBS is running those are queued (obs_config.queue_obs_settings) rather than written, and the
dialog offers to restart OBS to apply them; otherwise run.py applies them
before it next starts OBS.
"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

NO_HOTKEY = "None"  # the key choice for a hotkey that isn't set (obs_config.NO_HOTKEY)

USAGE = """
Usage: settings_dialog.py

Opens ClipEm's Settings window (the tray menu's "Settings...").

  -h, --help  Show this help and exit.

The tray icon passes --parent-pid <pid> itself, so the window closes with it.
"""
if __name__ == "__main__" and {"-h", "--help"} & set(sys.argv[1:]):
    print(USAGE.strip())
    sys.exit(0)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.dirname(SCRIPT_DIR)
INSTALLER_DIR = os.path.join(SHARED_DIR, "Installer")

# Shared with setup.py/run.py rather than re-implemented here: config-file
# paths and atomic writes (clipem_common/obs_config) and the per-OS boot
# autostart entry (autostart.apply_boot_autostart). SCRIPT_DIR goes first
# because the bundled Windows Python's ._pth file keeps it off sys.path.
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(1, INSTALLER_DIR)
import bug_report  # noqa: E402
import clip_move  # noqa: E402
import color_picker  # noqa: E402
import dark_theme  # noqa: E402
import detection_help  # noqa: E402
import editor_install  # noqa: E402
import file_dialogs  # noqa: E402
import key_picker  # noqa: E402
import log_viewer  # noqa: E402
import mic_filter  # noqa: E402
import naming_form  # noqa: E402
import new_badge  # noqa: E402
import obs_config  # noqa: E402
import obs_process  # noqa: E402
import popups  # noqa: E402
import reinstall  # noqa: E402
import release_notes  # noqa: E402
import setup  # noqa: E402 - Setup's own load/save for the settings it shares with this dialog
import ui_colors  # noqa: E402
from app_window import ICON_DIR, SETTINGS_ICON, style_app_window, use_windows_app_id  # noqa: E402
from autostart import (  # noqa: E402
    SHORTCUT_BROKEN,
    SHORTCUT_DESKTOP,
    SHORTCUT_OTHER,
    SHORTCUT_OURS,
    SHORTCUT_START_MENU,
    apply_boot_autostart,
    describe_autostart_entry,
    find_autostart_entries,
    install_shortcut,
    remove_autostart_entries,
    shortcut_path,
    shortcut_state,
    uninstall_shortcut,
    window_desktop_entry,
    write_window_desktop_entry,
)
from clipem_common import (  # noqa: E402
    APP_DIR,
    OBS_RESTART_FLAG,
    REOPEN_EDITOR_FLAG,
    acquire_instance_lock,
    exit_with_parent,
    load_run_config,
    outlive_parent,
    save_run_config,
    setup_logging,
    start_clipem,
    stop_clip_editor,
    stop_tray_widget,
    write_text_atomic,
)
from mic_filter_gui import MicFilterWizard  # noqa: E402
from ui_scale import UiScale, enable_dpi_awareness  # noqa: E402

log = setup_logging("settings")
RUN_PY = os.path.join(INSTALLER_DIR, "run.py")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".settings.lock")
# Written by a second launch (the tray menu's Settings clicked again) to ask
# the open dialog to come to the front.
RAISE_PATH = os.path.join(SCRIPT_DIR, ".settings.raise")
TAB_REQUEST = "tab="  # among --highlight's keys: the page to open on
RAISE_POLL_MS = 300
# A wheel turn over a combobox this soon after scrolling the page scrolls
# the page on, instead of changing the combobox.
COMBOBOX_WHEEL_BUFFER_S = 0.6

try:
    import updater
except ImportError:
    updater = None


# Its own window class, matched by DESKTOP_FILE, for its own icon on Linux
# taskbars. Tk keeps only the first capital of what it's given, so the class
# windows report (and StartupWMClass needs) is this, not "ClipEmSettings".
WINDOW_CLASS = "Clipemsettings"
DESKTOP_FILE = "clipem-settings.desktop"


def start_run_py(extra_args=()):
    """Starts ClipEm again (run.py, detached) - see clipem_common.start_clipem."""
    start_clipem(extra_args)


def restart_obs_with_queued_settings():
    """Closes OBS (stopping its outputs first, so it doesn't stop to ask
    "are you sure?"), applies the queued settings and starts it again via
    run.py. Returns None on success, or an error message."""
    # Queued settings aren't applied yet, so the websocket settings on disk
    # are still the running OBS's - close_obs_stopping_outputs connects with those.
    if not obs_process.close_obs_stopping_outputs(log):
        return (
            "OBS didn't close (it may be showing a dialog). Your settings are saved "
            "and will apply the next time ClipEm starts OBS."
        )
    applied = obs_config.apply_pending_obs_settings()
    log.info(f"Restart OBS: applied {sorted(applied)}")
    # The tray widget keeps running: tell run.py that's expected, or it asks
    # whether to start "a second copy of ClipEm".
    start_run_py([OBS_RESTART_FLAG])
    return None


def save_obs_settings(obs_settings, initial_obs_settings):
    """Saves whichever OBS settings differ from what the dialog opened with.
    They're queued first either way, so nothing is lost if applying fails
    halfway; then applied straight away unless OBS is running. Returns
    "unchanged", "applied", or "queued" (OBS is running - offer a restart)."""
    changed = {k: v for k, v in obs_settings.items() if v != initial_obs_settings.get(k)}
    if not changed:
        return "unchanged"
    obs_config.queue_obs_settings(changed)
    if obs_process.obs_running():
        return "queued"
    obs_config.apply_pending_obs_settings()
    return "applied"


UPDATE_PENDING_MESSAGE = "The update will be installed the next time ClipEm starts."
REINSTALL_PENDING_MESSAGE = "ClipEm will be reinstalled the next time it starts."


def restart_clipem(pending_message=UPDATE_PENDING_MESSAGE):
    """To install a chosen update (or carry out a reinstall): closes OBS and
    the tray widget, then starts everything again via run.py, which does
    that before starting them. Returns None on success, or an error message
    ending with pending_message."""
    if not obs_process.close_obs_stopping_outputs(log):
        return f"OBS didn't close (it may be showing a dialog). {pending_message}"
    if obs_config.load_pending_obs_settings():
        obs_config.apply_pending_obs_settings()
    # Its code (and window) is part of the update too.
    editor_was_open = stop_clip_editor(log)
    stop_tray_widget(log)
    start_run_py([REOPEN_EDITOR_FLAG] if editor_was_open else [])
    return None


def restart_clipem_in_background(root, on_done, pending_message=UPDATE_PENDING_MESSAGE):
    """restart_clipem() on a worker thread, on_done(error_message, exc) back
    on the Tk thread. This dialog closes with the tray widget
    (exit_with_parent), which restart_clipem stops - so it opts out first,
    here on the Tk thread: Linux keeps the parent-death signal per thread,
    and clearing it on the worker left the main thread's in place, so the
    dialog died with the tray before it could start run.py."""
    outlive_parent()
    run_in_background(root, lambda: restart_clipem(pending_message), on_done)


def reveal_file(path):
    """Shows path in the system's file manager (its folder, on Linux)."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def run_in_background(root, work, on_done):
    """Runs work() on a worker thread and on_done(result, error) back on the
    Tk thread - network calls must not freeze the dialog."""
    results = queue.Queue()

    def worker():
        try:
            results.put((work(), None))
        except Exception as exc:
            results.put((None, exc))

    def poll():
        try:
            result, error = results.get_nowait()
        except queue.Empty:
            root.after(100, poll)
            return
        on_done(result, error)

    threading.Thread(target=worker, daemon=True).start()
    root.after(100, poll)


def indent_under_text(widget, checkbutton):
    """Lines up a checkbox's sub-options with its text rather than its box:
    the box's width (plus its gap) is whatever's left of the checkbox's
    width after its text and right-hand padding."""
    import tkinter.font as tkfont

    checkbutton.update_idletasks()
    font = tkfont.Font(font=checkbutton.cget("font"))
    text_width = max(font.measure(line) for line in str(checkbutton.cget("text")).splitlines())
    right = int(checkbutton.cget("padx")) + int(checkbutton.cget("bd")) + int(checkbutton.cget("highlightthickness"))
    widget.grid_configure(padx=(max(0, checkbutton.winfo_reqwidth() - text_width - right), 0))


def load_recording_settings(pending):
    """What Setup's video, clip length and audio pages last saved - or what's
    queued for OBS's next start - for the Recording/Audio Devices sections."""
    video = setup.load_video_settings()
    queued_video = pending.get("video")
    if queued_video:
        video = {"width": queued_video["output_width"], "height": queued_video["output_height"],
                 "fps": queued_video["fps"]}
    monitor = pending.get("capture_monitor")
    return {
        "width": int(video["width"]), "height": int(video["height"]), "fps": int(video["fps"]),
        "monitor_index": monitor["index"] if monitor else setup.load_capture_monitor_index(),
        "replay_buffer_seconds": int(pending.get("replay_buffer_seconds") or setup.load_replay_buffer_seconds()),
        "bitrate": int(pending.get("recording_bitrate") or setup.load_recording_bitrate()),
        "audio": pending.get("audio_devices") or setup.load_audio_devices(),
        "encoder": ((pending["recording_encoder"]["encoder"], pending["recording_encoder"].get("device"))
                    if pending.get("recording_encoder") else setup.load_recording_encoder()),
    }


def bring_to_front(root):
    root.deiconify()
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    try:
        root.focus_force()
    except tk.TclError:
        pass


def watch_for_reopen(root):
    """Brings the dialog to the front whenever another launch asks (see
    RAISE_PATH) - the same hand-over the clip editor does."""
    def check():
        if os.path.exists(RAISE_PATH):
            try:
                with open(RAISE_PATH, "r", encoding="utf-8") as f:
                    request = f.read()
                os.remove(RAISE_PATH)
            except OSError:
                request = ""
            bring_to_front(root)
            if hasattr(root, "highlight_settings"):
                keys = request.split(",")
                for key in keys:
                    if key.startswith(TAB_REQUEST):
                        root.layout["show_tab"](key[len(TAB_REQUEST):])
                root.highlight_settings(keys)  # "raise" names no setting
        root.after(RAISE_POLL_MS, check)

    root.after(RAISE_POLL_MS, check)


def parent_pid_from_args(argv):
    """The tray widget passes --parent-pid <its pid> when it opens this."""
    if "--parent-pid" in argv:
        index = argv.index("--parent-pid")
        if index + 1 < len(argv) and argv[index + 1].isdigit():
            return int(argv[index + 1])
    return None


def highlight_from_args(argv):
    """The settings to outline: --highlight key,key (the update window's
    Show New Settings, through the tray widget)."""
    if "--highlight" in argv:
        index = argv.index("--highlight")
        if index + 1 < len(argv):
            return [key for key in argv[index + 1].split(",") if key]
    return []


def desktop_entry():
    return window_desktop_entry("ClipEm Settings", os.path.abspath(__file__),
                                os.path.join(ICON_DIR, SETTINGS_ICON + ".png"), WINDOW_CLASS)


def ensure_desktop_entry(directory=None):
    """Linux: gives this window its own entry, or taskbars would show the
    main ClipEm one for it."""
    if not sys.platform.startswith("linux"):
        return
    try:
        write_window_desktop_entry(DESKTOP_FILE, desktop_entry(), directory)
    except OSError as exc:
        log.warning(f"Couldn't write {DESKTOP_FILE} for the Settings taskbar icon: {exc}")


def main():
    # Close along with the tray widget that opened this, however it ends.
    exit_with_parent(parent_pid_from_args(sys.argv[1:]))
    highlight = highlight_from_args(sys.argv[1:])
    start_tab = next((key[len(TAB_REQUEST):] for key in highlight if key.startswith(TAB_REQUEST)), None)
    if not acquire_instance_lock(LOCK_PATH):
        log.info("Settings dialog already open; asking it to come to the front.")
        try:
            write_text_atomic(RAISE_PATH, ",".join(highlight) or "raise")
        except OSError as exc:
            log.warning(f"Couldn't ask the open Settings dialog to come to the front: {exc}")
        return
    ensure_desktop_entry()
    try:
        os.remove(RAISE_PATH)  # left from a dialog that has since closed
    except OSError:
        pass
    # Saving new colors (or Dark Mode) reopens the window, built with them.
    tab = open_dialog(start_tab, highlight)
    while tab is not None:
        tab = open_dialog(tab)


def open_dialog(start_tab=None, highlight=()):
    """Builds and runs the Settings window, on start_tab (Basic Settings by
    default), outlining the settings `highlight` names. Returns the tab to reopen it on when its Save changed the
    colors, which only a newly built window shows - else None."""
    reopen_on = {"tab": None}
    ws_load_error = None
    try:
        ws_settings = obs_config.load_obs_websocket_settings()
        port, password = ws_settings["port"], ws_settings["password"]
    except Exception as exc:
        port, password = 4455, ""
        ws_load_error = str(exc)

    # Show queued (not yet applied) values where there are any, so reopening
    # Settings before OBS restarts doesn't show - and re-save - the old ones.
    pending = obs_config.load_pending_obs_settings()
    if "websocket" in pending:
        port, password = pending["websocket"]["port"], pending["websocket"]["password"]
    try:
        stuck_enabled = obs_config.load_stuck_check_enabled()
    except Exception:
        stuck_enabled = True
    try:
        black_enabled = obs_config.load_black_check_enabled()
    except Exception:
        black_enabled = True
    try:
        disk_warnings_enabled = obs_config.load_disk_space_warnings_enabled()
    except Exception:
        disk_warnings_enabled = True
    try:
        mic_check_enabled = obs_config.load_mic_check_enabled()
    except Exception:
        mic_check_enabled = True
    try:
        hotkey = tuple(pending.get("clip_hotkey") or obs_config.load_clip_hotkey())
    except Exception:
        hotkey = obs_config.DEFAULT_CLIP_HOTKEY
    try:
        toggle_hotkeys = {**obs_config.load_toggle_hotkeys(), **pending.get("toggle_hotkeys", {})}
        toggle_hotkeys = {name: list(value) if value else None for name, value in toggle_hotkeys.items()}
    except Exception:
        toggle_hotkeys = dict.fromkeys(obs_config.TOGGLE_HOTKEYS)
    try:
        clip_sound = pending.get("clip_sound") or obs_config.load_clip_sound()
    except Exception:
        clip_sound = dict(obs_config.DEFAULT_CLIP_SOUND)
    clip_folder = pending.get("clip_folder") or obs_config.load_clip_folder()
    try:
        split_tracks = bool(pending.get("split_tracks", obs_config.load_split_tracks()))
    except Exception:
        split_tracks = False
    try:
        reexport = pending.get("clip_reexport") or setup.load_clip_reexport()
        reexport = {"enabled": bool(reexport.get("enabled", reexport.get("reexport_enabled"))),
                    "target_mb": float(reexport["target_mb"]), "crop_seconds": int(reexport["crop_seconds"])}
    except Exception:
        reexport = {"enabled": False, "target_mb": float(setup.DEFAULT_TARGET_FILESIZE_MB), "crop_seconds": 30}
    try:
        replay_seconds = int(pending.get("replay_buffer_seconds") or setup.load_replay_buffer_seconds())
    except Exception:
        replay_seconds = 60
    try:
        audio_script = pending.get("audio_script") or obs_config.load_audio_script_settings()
    except Exception:
        audio_script = dict(obs_config.DEFAULT_AUDIO_SCRIPT_SETTINGS)
    try:
        post_clip = obs_config.clip_post.normalize(pending.get("post_clip") or obs_config.load_post_clip())
    except Exception:
        post_clip = dict(obs_config.clip_post.DEFAULTS)
    initial_obs_settings = {
        "websocket": {"port": port, "password": password},
        "clip_hotkey": list(hotkey),
        "toggle_hotkeys": toggle_hotkeys,
        "clip_sound": {"enabled": bool(clip_sound["enabled"]), "volume": round(float(clip_sound["volume"]), 2)},
        "clip_folder": clip_folder,
        "split_tracks": split_tracks,
        "clip_reexport": reexport,
        "audio_script": audio_script,
        "post_clip": post_clip,
    }

    run_config = load_run_config()

    enable_dpi_awareness()
    use_windows_app_id("PTMakes.ClipEm.Settings")
    # className: what Linux desktops match the window to (WM_CLASS).
    root = tk.Tk(className=WINDOW_CLASS)
    # Hidden until it's built and sized (shown at the end of main) - otherwise
    # it sits on screen as Tk's blank default-size window meanwhile.
    root.withdraw()
    dark_theme.apply_theme(root)  # before any widget: classic ones take their colors at creation
    # Settings' logo, and Ctrl+A / Ctrl+Z / Ctrl+Shift+Z or R in the text fields.
    style_app_window(root, log=log.warning, icon=SETTINGS_ICON)
    # Pixel sizes below are for a 96 DPI display - see ui_scale.py.
    px = UiScale(root).px
    root.px = px  # MicFilterWizard sizes itself with its parent's px
    root.title("ClipEm Settings")

    # Status line and Save/Cancel sit in a footer packed first, so resizing
    # the window shrinks the scrolling settings area above it, never them.
    footer = tk.Frame(root, padx=px(16), pady=px(10))
    footer.pack(side="bottom", fill="x")
    footer_line = ttk.Separator(root, orient="horizontal")
    footer_line.pack(side="bottom", fill="x")
    sidebar = tk.Frame(root, background=dark_theme.PANEL, padx=px(10), pady=px(12))
    sidebar.pack(side="left", fill="y")
    scroll_area = tk.Frame(root)
    scroll_area.pack(side="left", fill="both", expand=True)
    canvas = tk.Canvas(scroll_area, highlightthickness=0, borderwidth=0)
    scrollbar = ttk.Scrollbar(scroll_area, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    page = tk.Frame(canvas, padx=px(16), pady=px(12))
    page_window = canvas.create_window(0, 0, window=page, anchor="nw")
    page.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind(
        "<Configure>",
        lambda event: canvas.itemconfigure(page_window, width=max(event.width, page.winfo_reqwidth())),
    )

    last_page_scroll = {"time": 0.0}

    def scroll_page(event):
        last_page_scroll["time"] = time.monotonic()
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        elif sys.platform == "darwin":
            step = -event.delta
        else:
            step = -event.delta // 120
        canvas.yview_scroll(step, "units")

    def on_mouse_wheel(event):
        # bind_all also fires for other windows (dialogs) and for comboboxes,
        # which use the wheel to change their value - only scroll for the
        # settings area itself.
        if not str(event.widget).startswith(str(canvas)) or isinstance(event.widget, ttk.Combobox):
            return
        scroll_page(event)

    def on_combobox_wheel(event):
        # The page was just being scrolled: the pointer is passing over the
        # combobox - keep scrolling the page, don't change the value.
        if time.monotonic() - last_page_scroll["time"] < COMBOBOX_WHEEL_BUFFER_S:
            scroll_page(event)
            return "break"
        return None

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(sequence, on_mouse_wheel, add="+")
        # Ahead of the combobox's own wheel binding (see guard_combobox_wheel).
        root.bind_class("ComboboxWheelGuard", sequence, on_combobox_wheel)

    def guard_combobox_wheel(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Combobox) and "ComboboxWheelGuard" not in child.bindtags():
                child.bindtags(("ComboboxWheelGuard",) + child.bindtags())
            guard_combobox_wheel(child)
    # Each settings group is a rounded card in the page; the sidebar's tabs
    # choose which cards are shown. The code below adds rows to the current
    # card ("frame") - section() starts the next one.
    TAB_BASIC, TAB_OUTPUT, TAB_RECORDING = "Basic Settings", "Clip Output", "Recording/Devices"
    TAB_EDITOR = "Clip Editor"
    TAB_TRAY, TAB_SCRIPTS, TAB_WEBSOCKET = "UI Customization", "Scripts/Troubleshooting", "OBS Websocket"
    TABS = ("All", TAB_BASIC, TAB_OUTPUT, TAB_RECORDING, TAB_EDITOR, TAB_TRAY, TAB_WEBSOCKET, TAB_SCRIPTS)
    cards = []  # (card, its tab), in page order
    highlights = {}  # release_notes.json's new_settings keys -> (widget, its tab)
    highlighted = {}  # those keys while outlined -> their tab, outline options to restore and "New" badge


    def highlightable(key, widget, tab):
        """widget has to be packed side="left", for its badge - see new_badge.add_badge()."""
        highlights[key] = (widget, tab)
    frame = None
    next_row = 0

    def section(title, tab):
        nonlocal frame, next_row
        card = dark_theme.Card(page, pad=px(14), radius=px(10))
        cards.append((card, tab))
        frame = card.body
        # Rows spanning both columns (checkboxes, update text) are wider than
        # Port/Password; give that extra width to the field column so the labels
        # and fields stay flush left instead of being spread apart.
        frame.columnconfigure(1, weight=1)
        tk.Label(frame, text=title, font=("", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        next_row = 1

    info_button_image = root.info_button_image = tk.PhotoImage(width=1, height=1)  # on root: Tk drops unreferenced ones

    def info_button(parent, title, explanation):
        """The "i" next to an option, opening its explanation."""
        # An image, even this blank one, makes width and height pixels - characters aren't square.
        return tk.Button(parent, text="i", image=info_button_image, compound="center", width=px(17), height=px(17),
                         padx=0, pady=0, font=("", 9, "bold"), cursor="hand2",
                         command=lambda: popups.showinfo(title, explanation, parent=root))

    # Clip Hotkey is now the first section - no gap above it.
    section("Hotkeys", TAB_BASIC)

    def hotkey_row(title, value, choices):
        """Ctrl/Alt/Shift + key for one hotkey: returns its 4 vars."""
        nonlocal next_row
        ctrl, alt, shift, key = value or (False, False, False, NO_HOTKEY)
        hotkey_vars = (tk.BooleanVar(value=ctrl), tk.BooleanVar(value=alt), tk.BooleanVar(value=shift),
                       tk.StringVar(value=key))
        tk.Label(frame, text=title).grid(row=next_row, column=0, sticky="w")
        row = tk.Frame(frame)
        row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(2))
        for text, var in zip(("Ctrl", "Alt", "Shift"), hotkey_vars[:3], strict=True):
            tk.Checkbutton(row, text=text, variable=var).pack(side="left")
        tk.Label(row, text="+ Key:").pack(side="left", padx=(px(6), 0))
        ttk.Combobox(row, textvariable=hotkey_vars[3], values=choices, state="readonly", width=11,
                     height=16).pack(side="left", padx=(px(4), 0))

        def pick():
            picked = key_picker.pick_hotkey(root, f"Pick a Hotkey: {title.rstrip(':')}")
            if picked:
                for var, value in zip(hotkey_vars, picked, strict=True):
                    var.set(value)

        tk.Button(row, text="Pick...", command=pick).pack(side="left", padx=(px(6), 0))
        tk.Button(row, text="Clear", command=lambda: [var.set(value) for var, value in zip(
            hotkey_vars, obs_config.CLEARED_HOTKEY, strict=True)]).pack(side="left", padx=(px(4), 0))
        next_row += 1
        return hotkey_vars

    hotkey_ctrl_var, hotkey_alt_var, hotkey_shift_var, hotkey_key_var = hotkey_row(
        "Save Clip:", hotkey, [NO_HOTKEY, *obs_config.HOTKEY_KEY_CHOICES])
    # Start, or stop when pressed again (obs_config.TOGGLE_HOTKEYS).
    toggle_hotkey_vars = {
        name: hotkey_row(title, toggle_hotkeys[name], [NO_HOTKEY, *obs_config.HOTKEY_KEY_CHOICES])
        for name, title in (("recording", "Start/Stop Recording:"), ("replay_buffer", "Start/Stop Replay Buffer:"))
    }

    # --- Clip sound: on/off, volume and which .wav plays on each saved clip.
    section("Clip Sound", TAB_BASIC)
    sound_enabled_var = tk.BooleanVar(value=initial_obs_settings["clip_sound"]["enabled"])
    tk.Checkbutton(
        frame, text="Play a sound when a clip is saved", variable=sound_enabled_var,
        command=lambda: on_sound_toggle(),
    ).grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1

    tk.Label(frame, text="Volume:").grid(row=next_row, column=0, sticky="w")
    volume_row = tk.Frame(frame)
    volume_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0))
    sound_volume_var = tk.IntVar(value=round(initial_obs_settings["clip_sound"]["volume"] * 100))
    volume_scale = dark_theme.RedSlider(
        volume_row, from_=0, to=100, variable=sound_volume_var, width=px(180), height=px(24),
        default=round(obs_config.DEFAULT_CLIP_SOUND["volume"] * 100),
    )
    volume_scale.pack(side="left")
    volume_label = tk.Label(volume_row, width=5, anchor="w")
    volume_label.pack(side="left", padx=(px(4), 0))
    sound_volume_var.trace_add("write", lambda *_: volume_label.config(text=f"{sound_volume_var.get()}%"))
    volume_label.config(text=f"{sound_volume_var.get()}%")
    next_row += 1

    tk.Label(frame, text="Sound:").grid(row=next_row, column=0, sticky="w")
    sound_row = tk.Frame(frame)
    sound_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
    chosen_sound = {"path": None}  # a new .wav, copied into place on Save
    obs_config.ensure_clip_sound_wav()
    sound_name_var = tk.StringVar(value=obs_config.active_clip_sound_name() or "Custom sound")
    sound_combo = ttk.Combobox(sound_row, textvariable=sound_name_var, state="readonly", width=16,
                               values=list(obs_config.CLIP_SOUND_PRESETS))
    sound_combo.pack(side="left")

    def on_pick_preset(_event=None):
        chosen_sound["path"] = obs_config.clip_sound_preset_path(sound_name_var.get())

    sound_combo.bind("<<ComboboxSelected>>", on_pick_preset)

    def on_choose_sound():
        path = file_dialogs.askopenfilename(
            parent=root, title="Choose the clip sound", filetypes=[("WAV files", "*.wav"), ("All files", "*")],
        )
        if path:
            chosen_sound["path"] = path
            name = os.path.basename(path)
            sound_name_var.set(name if len(name) <= 16 else name[:15] + "\u2026")

    def on_play_sound():
        try:
            obs_config.play_clip_sound(sound_volume_var.get() / 100, chosen_sound["path"])
        except Exception as exc:
            popups.showerror("Clip Sound", f"Couldn't play the sound: {exc}", parent=root)

    choose_button = tk.Button(sound_row, text="Choose...", command=on_choose_sound)
    choose_button.pack(side="left", padx=(px(4), 0))
    play_button = tk.Button(sound_row, text="Play", command=on_play_sound)
    play_button.pack(side="left", padx=(px(4), 0))
    next_row += 1

    def on_sound_toggle():
        state = "normal" if sound_enabled_var.get() else "disabled"
        for widget in (volume_scale, choose_button, play_button):
            widget.config(state=state)
        sound_combo.config(state="readonly" if sound_enabled_var.get() else "disabled")

    on_sound_toggle()

    # The tray's "Clip saved." notification (Save Clip in its menu) - saved
    # with the other run_config.json settings below.
    clip_notification_var = tk.BooleanVar(value=bool(run_config.get("clip_saved_notification", True)))
    tk.Checkbutton(
        frame, text="Show a notification when a clip is saved from the tray menu",
        variable=clip_notification_var,
    ).grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1

    # --- Where clips are saved, and whether audio sources get their own tracks.
    section("Clip Output", TAB_OUTPUT)
    tk.Label(frame, text="Save clips to:").grid(row=next_row, column=0, sticky="w")
    folder_row = tk.Frame(frame)
    folder_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
    clip_folder_var = tk.StringVar(value=clip_folder)
    tk.Entry(folder_row, textvariable=clip_folder_var, state="readonly", width=28).pack(side="left")

    def on_choose_clip_folder():
        current = clip_folder_var.get()
        path = file_dialogs.askdirectory(
            parent=root, title="Choose where clips are saved",
            initialdir=current if os.path.isdir(current) else os.path.expanduser("~"),
        )
        if path:
            clip_folder_var.set(os.path.normpath(path))

    tk.Button(folder_row, text="Browse...", command=on_choose_clip_folder).pack(side="left", padx=(px(4), 0))
    tk.Button(
        folder_row, text="Default", command=lambda: clip_folder_var.set(obs_config.DEFAULT_CLIPS_DIR),
    ).pack(side="left", padx=(px(4), 0))
    next_row += 1

    # What clips and their copies are called (Scripts/clip_naming.py) - read
    # when each clip is saved, so it applies straight away.
    section("Clip Names", TAB_OUTPUT)
    naming_row = tk.Frame(frame)
    naming_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(2), 0))
    naming = naming_form.NamingForm(naming_row, run_config.get("clip_naming"), px=px,
                                    folder=lambda: clip_folder_var.get())
    naming.pack(side="left")
    highlightable("clip_names", naming, TAB_OUTPUT)

    # What's saved besides the clip, in two columns so the tab fits without scrolling.
    section("Clipping Add-Ons", TAB_OUTPUT)
    add_ons = tk.Frame(frame)
    add_ons.grid(row=next_row, column=0, columnspan=2, sticky="w")
    add_ons_left = tk.Frame(add_ons)
    add_ons_left.grid(row=0, column=0, sticky="nw")
    split_tracks_var = tk.BooleanVar(value=split_tracks)
    tk.Checkbutton(
        add_ons_left, text="Multitrack audio: also save a copy (\"-tracks.mkv\") with the OBS tracks,\n"
                    "Mix, Mic, and Desktop, and apps each on their own track, for editing",
        variable=split_tracks_var, justify="left",
    ).pack(anchor="w")

    # Post-clip (Scripts/clip_post.py): the hotkey keeps recording a little
    # longer, saved as the clip's "-extra.mkv" for the editor.
    post_row = tk.Frame(add_ons_left)
    post_row.pack(anchor="w", pady=(px(6), 0))
    post_box = tk.Frame(post_row)
    post_box.pack(side="left")
    highlightable("post_clip", post_box, TAB_OUTPUT)
    post_line = tk.Frame(post_box)
    post_line.pack(anchor="w")
    post_enabled_var = tk.BooleanVar(value=initial_obs_settings["post_clip"]["enabled"])
    post_seconds_var = tk.StringVar(value=str(initial_obs_settings["post_clip"]["seconds"]))
    post_sound_var = tk.BooleanVar(value=initial_obs_settings["post_clip"]["sound_after"])
    tk.Checkbutton(post_line, text="Post-clip: keep recording for", variable=post_enabled_var,
                   command=lambda: on_post_toggle()).pack(side="left")
    post_seconds_box = tk.Spinbox(post_line, textvariable=post_seconds_var, width=4,
                                  from_=obs_config.clip_post.MIN_SECONDS, to=obs_config.clip_post.MAX_SECONDS)
    post_seconds_box.pack(side="left", padx=(px(3), 0))
    tk.Label(post_line, text="seconds after the hotkey").pack(side="left", padx=(px(3), 0))
    info_button(post_line, "Post-clip", detection_help.POST_CLIP).pack(side="left", padx=(px(6), 0))
    post_sound_check = tk.Checkbutton(post_box, text="Play the clip sound again when the extra seconds are saved",
                                      variable=post_sound_var)
    post_sound_check.pack(anchor="w", padx=(px(22), 0))

    def on_post_toggle():
        state = "normal" if post_enabled_var.get() else "disabled"
        post_seconds_box.config(state=state)
        post_sound_check.config(state=state)

    on_post_toggle()

    # The "-small.mp4" copy (Scripts/clip_reexport.py) - as on Setup's
    # "Lower-Quality Re-export" page.
    reexport_var = tk.BooleanVar(value=reexport["enabled"])
    reexport_column = tk.Frame(add_ons)
    reexport_column.grid(row=0, column=1, sticky="nw", padx=(px(28), 0))
    reexport_check = tk.Checkbutton(
        reexport_column, text="Also save a smaller copy of each clip (\"-small.mp4\"), for sites\n"
                    "with a file size limit - the full-quality clip is always kept",
        variable=reexport_var, justify="left", command=lambda: on_reexport_toggle(),
    )
    reexport_check.grid(row=0, column=0, sticky="w")
    # Its options sit under its text, not its box - see indent_under_text().
    reexport_box = tk.Frame(reexport_column)
    reexport_box.grid(row=1, column=0, sticky="w")
    tk.Label(reexport_box, text="Target size:").grid(row=0, column=0, sticky="w")
    size_row = tk.Frame(reexport_box)
    size_row.grid(row=0, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
    target_mb_var = tk.StringVar(value=f"{reexport['target_mb']:g}")
    target_entry = tk.Entry(size_row, textvariable=target_mb_var, width=6)
    target_entry.pack(side="left")
    tk.Label(size_row, text="MB").pack(side="left", padx=(px(3), 0))
    recommended_button = tk.Button(
        size_row, text=f"Use {setup.DEFAULT_TARGET_FILESIZE_MB} MB",
        command=lambda: target_mb_var.set(str(setup.DEFAULT_TARGET_FILESIZE_MB)),
    )
    recommended_button.pack(side="left", padx=(px(6), 0))
    tk.Label(size_row, text="(Discord's free upload limit)", fg=dark_theme.MUTED).pack(side="left", padx=(px(4), 0))
    tk.Label(reexport_box, text="Keep the last:").grid(row=1, column=0, sticky="w")
    crop_row = tk.Frame(reexport_box)
    crop_row.grid(row=1, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
    crop_var = tk.StringVar(value=str(min(reexport["crop_seconds"], replay_seconds)))
    crop_spin = tk.Spinbox(crop_row, from_=1, to=max(1, replay_seconds), textvariable=crop_var, width=5)
    crop_spin.pack(side="left")
    tk.Label(crop_row, text=f"seconds (of the {replay_seconds} s replay buffer)").pack(side="left", padx=(px(3), 0))
    bitrate_var = tk.StringVar()
    tk.Label(reexport_box, textvariable=bitrate_var, fg=dark_theme.MUTED).grid(row=2, column=1, sticky="w", padx=(px(6), 0))

    def reexport_values():
        """(target MB, crop seconds), or None if either isn't a number."""
        try:
            return float(target_mb_var.get()), max(1, min(int(float(crop_var.get())), replay_seconds))
        except ValueError:
            return None

    def update_bitrate(*_args):
        values = reexport_values()
        bitrate_var.set(f"-> about {setup.compute_video_bitrate_for_filesize(*values)} kbps video"
                        if values and values[0] > 0 else "")

    def on_reexport_toggle():
        state = "normal" if reexport_var.get() else "disabled"
        for widget in (target_entry, recommended_button, crop_spin):
            widget.config(state=state)

    target_mb_var.trace_add("write", update_bitrate)
    crop_var.trace_add("write", update_bitrate)
    update_bitrate()
    on_reexport_toggle()

    # --- Recording and audio devices: what Setup's Display & Video Quality,
    # Clip Length & Quality and Audio Devices pages set. All in OBS's own
    # files, so saved like the settings above (queued while OBS runs).
    recording = load_recording_settings(pending)
    devices = {"displays": None, "audio": None}  # filled in by detect_devices()

    section("Recording", TAB_RECORDING)
    # Two columns: the recording settings on the left, the audio devices on
    # the right (under their own heading, level with the card's title).
    recording_card = frame
    recording_card.grid_slaves(row=0, column=0)[0].grid_configure(columnspan=1)
    recording_card.columnconfigure(1, weight=0)
    recording_card.columnconfigure(2, weight=1)
    frame = tk.Frame(recording_card)
    frame.grid(row=1, column=0, sticky="nw")
    next_row = 0

    def combo_row(label, width):
        nonlocal next_row
        tk.Label(frame, text=label).grid(row=next_row, column=0, sticky="w")
        combo = ttk.Combobox(frame, state="disabled", width=width)
        combo.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(2))
        next_row += 1
        return combo

    display_combo = combo_row("Display:", 34)
    display_combo.set("Detecting displays...")
    resolution_combo = combo_row("Output resolution:", 12)
    resolution_combo.set(f"{recording['width']}x{recording['height']}")
    fps_combo = combo_row("FPS:", 6)
    fps_combo.configure(values=[str(f) for f in setup.FPS_CHOICES])
    fps_combo.set(str(recording["fps"]))
    encoder_combo = combo_row("Video encoder:", 34)
    encoder_combo.set("Detecting encoders...")
    # Only saved when picked here - the list is what was found, which may not
    # have the one in use (see on_devices).
    encoder_picked = {"value": None}

    tk.Label(frame, text="Replay buffer:").grid(row=next_row, column=0, sticky="w")
    buffer_row = tk.Frame(frame)
    buffer_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(2))
    buffer_var = tk.StringVar(value=str(recording["replay_buffer_seconds"]))
    tk.Spinbox(buffer_row, textvariable=buffer_var, width=6, from_=setup.REPLAY_BUFFER_MIN_SECONDS,
               to=setup.REPLAY_BUFFER_MAX_SECONDS, increment=setup.REPLAY_BUFFER_STEP_SECONDS).pack(side="left")
    tk.Label(buffer_row, text="seconds (how far back a clip reaches)").pack(side="left", padx=(px(3), 0))
    next_row += 1

    tk.Label(frame, text="Recording bitrate:").grid(row=next_row, column=0, sticky="w")
    bitrate_row = tk.Frame(frame)
    bitrate_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(2))
    recording_bitrate_var = tk.StringVar(value=str(recording["bitrate"]))
    tk.Spinbox(bitrate_row, textvariable=recording_bitrate_var, width=7, from_=setup.BITRATE_MIN_KBPS,
               to=setup.BITRATE_MAX_KBPS, increment=setup.BITRATE_STEP_KBPS).pack(side="left")
    tk.Label(bitrate_row, text="kbps").pack(side="left", padx=(px(3), 0))
    clip_size_var = tk.StringVar()
    tk.Label(bitrate_row, textvariable=clip_size_var, fg=dark_theme.MUTED).pack(side="left", padx=(px(6), 0))
    next_row += 1

    def update_clip_size(*_args):
        try:
            size = setup.compute_filesize_for_bitrate(int(float(recording_bitrate_var.get())),
                                                      int(float(buffer_var.get())))
        except ValueError:
            clip_size_var.set("")
            return
        clip_size_var.set(f"-> ~{size:.0f} MB for a full-length clip")

    recording_bitrate_var.trace_add("write", update_clip_size)
    buffer_var.trace_add("write", update_clip_size)
    update_clip_size()

    tk.Label(recording_card, text="Audio Devices", font=("", 10, "bold")).grid(
        row=0, column=1, sticky="w", padx=(px(28), 0))
    frame = tk.Frame(recording_card)
    frame.grid(row=1, column=1, sticky="nw", padx=(px(28), 0))
    next_row = 0
    output_combo = combo_row("Desktop audio:", 34)
    input_combo = combo_row("Microphone:", 34)
    for combo in (output_combo, input_combo):
        combo.set("Detecting devices...")

    mic_reading = {"value": None}  # a MicFilterWizard reading, saved with Save
    mic_button = tk.Button(
        frame, text="Redo Mic Filter Configuration..." if setup.has_mic_filter() else "Mic Filter Configuration...",
        state="disabled", command=lambda: start_mic_filter(),
    )
    mic_button.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(2))
    next_row += 1

    def start_mic_filter():
        device = devices["audio"]["input"][input_combo.current()]
        mic_button.config(state="disabled", text="Preparing...")

        def on_deps(ok, exc):
            mic_button.config(state="normal", text="Mic Filter Configuration...")
            if exc is not None or not ok:
                popups.showerror("Mic Filter", f"Couldn't set up microphone calibration: {exc or 'install failed'}",
                                     parent=root)
                return
            controller = {
                "measure_audio_level_db": mic_filter.measure_audio_level_db,
                "compute_mic_filter_values": mic_filter.compute_mic_filter_values,
                "noise_floor_sample_seconds": mic_filter.NOISE_FLOOR_SAMPLE_SECONDS,
                "speech_sample_seconds": mic_filter.SPEECH_SAMPLE_SECONDS,
                "save_mic_filter_levels": lambda noise, speech: mic_reading.update(
                    value={"noise_floor_db": noise, "speech_level_db": speech}),
            }
            MicFilterWizard(root, controller, device["name"], device["device_id"],
                            on_applied=lambda: mic_button.config(text="Mic filter: new reading (saved with Save)"))

        run_in_background(root, mic_filter.ensure_mic_filter_deps, on_deps)

    def fill_displays():
        displays = devices["displays"]
        display_combo.configure(state="readonly", values=[
            f"{d['name']} - {d['width']}x{d['height']}" + (" (primary)" if d.get("primary") else "")
            for d in displays
        ])
        index = recording["monitor_index"]
        if index is None or not 0 <= index < len(displays):
            index = next((i for i, d in enumerate(displays) if d.get("primary")), 0)
        display_combo.current(index)
        devices["initial_display"] = index  # what "changed" is measured against
        fill_resolutions()
        resolution_combo.configure(state="readonly")
        fps_combo.configure(state="readonly")

    def fill_resolutions():
        display = devices["displays"][display_combo.current()]
        labels = [f"{w}x{h}" for w, h in setup.resolution_choices(display["width"], display["height"])]
        current = resolution_combo.get()
        resolution_combo.configure(values=labels)
        if current not in labels:
            resolution_combo.current(0)
            follow_resolution()

    resolution_shown = {"value": (recording["width"], recording["height"])}

    def follow_resolution(_event=None):
        """As Setup does: the bitrate follows the output resolution, unless
        it was set by hand (setup.bitrate_for_new_resolution)."""
        try:
            new = tuple(int(v) for v in resolution_combo.get().split("x"))
            current = int(float(recording_bitrate_var.get()))
        except ValueError:
            return
        recording_bitrate_var.set(str(setup.bitrate_for_new_resolution(current, resolution_shown["value"], new)))
        resolution_shown["value"] = new

    display_combo.bind("<<ComboboxSelected>>", lambda _e: fill_resolutions())
    resolution_combo.bind("<<ComboboxSelected>>", follow_resolution)

    def fill_audio():
        for combo, flow in ((output_combo, "output"), (input_combo, "input")):
            entries = devices["audio"][flow]
            ids = [d["device_id"] for d in entries]
            combo.configure(state="readonly", values=[d["name"] for d in entries])
            combo.current(ids.index(recording["audio"][flow]) if recording["audio"][flow] in ids else 0)
        mic_button.config(state="normal")

    def fill_encoders(found):
        choices = [(encoder, device) for encoder, device, _label in found]
        labels = [label for _encoder, _device, label in found]
        current = tuple(recording["encoder"])
        if current not in choices:
            choices.insert(0, current)
            labels.insert(0, setup.encoders.LABELS[current[0]] + " (not found on this PC)")
        encoder_combo.configure(state="readonly", values=labels)
        encoder_combo.current(choices.index(current))
        encoder_combo.bind("<<ComboboxSelected>>",
                           lambda _e: encoder_picked.update(value=choices[encoder_combo.current()]))

    def on_devices(result, exc):
        result = result if exc is None else {}
        fill_export_encoders(result.get("export_encoders") or [], result.get("detected") or [])
        fill_encoders(result.get("encoders")
                      or [(setup.encoders.X264, None, setup.encoders.LABELS[setup.encoders.X264])])
        devices["displays"] = result.get("displays") or [{
            "name": "Primary display", "width": root.winfo_screenwidth(),
            "height": root.winfo_screenheight(), "primary": True,
        }]
        devices["audio"] = result.get("audio") or {"output": [], "input": []}
        fill_displays()
        if devices["audio"]["output"] and devices["audio"]["input"]:
            fill_audio()
        else:
            for combo in (output_combo, input_combo):
                combo.set("Couldn't list devices")

    # Display/audio detection shells out (PowerShell on Windows) and can take
    # a few seconds - as in Setup, it runs while the window is up.
    def find_devices():
        ffmpeg = setup.encoders.bundled_ffmpeg(APP_DIR)
        found = setup.encoders.available()
        return {"displays": setup.list_displays(), "audio": setup.list_audio_devices(),
                "encoders": setup.list_encoders(), "detected": found,
                "export_encoders": setup.encoders.export_options(ffmpeg, found=found)}

    run_in_background(root, find_devices, on_devices)

    # --- Which editor the tray's editor item opens: ClipEm's own, the user's
    # (editor_install.py), or none at all.
    section("Clip Editor", TAB_EDITOR)
    editors_off_var = tk.BooleanVar(value=editor_install.editors_disabled(run_config))
    tk.Checkbutton(
        frame, text="Disable clip editors (no editor in the tray menu)", variable=editors_off_var,
        command=lambda: show_editor_choice(),
    ).grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1
    editor_kind_var = tk.StringVar(value=editor_install.editor_kind(run_config))
    kind_row = tk.Frame(frame)
    kind_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(2), 0))
    tk.Label(kind_row, text="Edit clips in:").pack(side="left")
    kind_buttons = [
        tk.Radiobutton(kind_row, text="ClipEm's editor", value=editor_install.INTERNAL, variable=editor_kind_var,
                       command=lambda: show_editor_choice()),
        tk.Radiobutton(kind_row, text="External editor", value=editor_install.EXTERNAL, variable=editor_kind_var,
                       command=lambda: show_editor_choice()),
    ]
    for button in kind_buttons:
        button.pack(side="left", padx=(px(6), 0))
    next_row += 1
    editor_label = tk.Label(frame, text="External editor:")
    editor_label.grid(row=next_row, column=0, sticky="w")
    editor_row = tk.Frame(frame)
    editor_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
    editor_path_var = tk.StringVar(value=run_config.get("custom_editor_path") or "")
    editor_name_var = tk.StringVar()

    def show_editor_name(*_args):
        path = editor_path_var.get().strip()
        name = os.path.basename(path.rstrip("/\\")) if path else "None"
        editor_name_var.set(name if len(name) <= 16 else name[:15] + "\u2026")

    editor_path_var.trace_add("write", show_editor_name)
    show_editor_name()
    tk.Label(editor_row, textvariable=editor_name_var, width=16, anchor="w").pack(side="left")

    def on_choose_editor():
        if sys.platform == "darwin":
            path = file_dialogs.askdirectory(parent=root, title="Choose your video editor (.app)",
                                           initialdir="/Applications")
        else:
            filetypes = [("Programs", "*.exe")] if sys.platform == "win32" else []
            path = file_dialogs.askopenfilename(
                parent=root, title="Choose your video editor", filetypes=filetypes + [("All files", "*")],
            )
        if path:
            editor_path_var.set(path)

    tk.Button(editor_row, text="Choose...", command=on_choose_editor).pack(side="left", padx=(px(4), 0))
    tk.Button(editor_row, text="Clear", command=lambda: editor_path_var.set("")).pack(
        side="left", padx=(px(4), 0)
    )
    next_row += 1

    def show_editor_choice():
        """The picker only for "External editor"; neither choice while
        editors are off."""
        off = editors_off_var.get()
        for button in kind_buttons:
            button.config(state="disabled" if off else "normal")
        if not off and editor_kind_var.get() == editor_install.EXTERNAL:
            editor_label.grid()
            editor_row.grid()
        else:
            editor_label.grid_remove()
            editor_row.grid_remove()

    show_editor_choice()

    # --- How ClipEm's editor exports and opens: read by clip_browser.py as it
    # needs them, so they apply without restarting anything.
    section("Editor Exports", TAB_EDITOR)
    saved_export = run_config.get("editor_export") if isinstance(run_config.get("editor_export"), dict) else {}
    export_detected_var = tk.StringVar(value="Detecting...")
    tk.Label(frame, text="Detected encoder:").grid(row=next_row, column=0, sticky="w")
    tk.Label(frame, textvariable=export_detected_var, fg=dark_theme.MUTED).grid(
        row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(2))
    next_row += 1
    export_encoder_combo = combo_row("Export encoder:", 34)
    export_encoder_combo.set("Detecting encoders...")
    export_choices = [setup.encoders.X264]
    tk.Label(frame, text="Hardware is faster, but makes bigger files. If it fails, the export uses x264 instead.",
             fg=dark_theme.MUTED).grid(row=next_row, column=1, sticky="w", padx=(px(6), 0))
    next_row += 1
    quality_labels = {"best": "Best (largest files)", "high": "High", "smaller": "Smaller files"}
    speed_labels = {"fast": "Fast", "medium": "Medium", "slow": "Slow (smaller files)"}
    quality_combo = combo_row("Full quality export:", 20)
    quality_combo.configure(state="readonly", values=list(quality_labels.values()))
    quality_combo.set(quality_labels.get(saved_export.get("quality"), quality_labels["best"]))
    speed_combo = combo_row("Encoding speed:", 20)
    speed_combo.configure(state="readonly", values=list(speed_labels.values()))
    speed_combo.set(speed_labels.get(saved_export.get("speed"), speed_labels["medium"]))
    discord_mb_var = tk.StringVar(value=str(run_config.get("editor_discord_mb") or obs_config.DISCORD_TARGET_MB))
    tk.Label(frame, text="Discord export size:").grid(row=next_row, column=0, sticky="w")
    discord_row = tk.Frame(frame)
    discord_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(2))
    tk.Spinbox(discord_row, textvariable=discord_mb_var, from_=1, to=500, width=5).pack(side="left")
    tk.Label(discord_row, text="MB").pack(side="left", padx=(px(4), 0))
    next_row += 1

    section("Editor Behavior", TAB_EDITOR)
    open_in_labels = {"last": "The last mode used", "trim": "Trim", "full": "Full Editor"}
    open_in_combo = combo_row("Open clips in:", 20)
    open_in_combo.configure(state="readonly", values=list(open_in_labels.values()))
    open_in_combo.set(open_in_labels.get(run_config.get("editor_open_in"), open_in_labels["last"]))
    sub_tracks_var = tk.BooleanVar(value=run_config.get("editor_sub_tracks", True) is not False)
    tk.Checkbutton(frame, text="Start the Full Editor on the sub-tracks (Mic, Desktop, apps) instead of the Mix",
                   variable=sub_tracks_var).grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1
    asset_days_var = tk.StringVar(value=str(run_config.get("editor_asset_days") or 30))
    tk.Label(frame, text="Delete unused imports after:").grid(row=next_row, column=0, sticky="w")
    asset_row = tk.Frame(frame)
    asset_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(2))
    tk.Spinbox(asset_row, textvariable=asset_days_var, from_=1, to=3650, width=5).pack(side="left")
    tk.Label(asset_row, text="days").pack(side="left", padx=(px(4), 0))
    next_row += 1

    def fill_export_encoders(options, found):
        """options: encoders.export_options(); found: what this PC has."""
        best = found[0] if found else (setup.encoders.X264, None)
        usable = {encoder for encoder, _device, _label, _ffmpeg in options}
        note = "" if best[0] in usable else " (no ffmpeg here can use it - install one with it to export with it)"
        export_detected_var.set(setup.encoders.LABELS[best[0]] + note)
        export_choices[:] = [encoder for encoder, _device, _label, _ffmpeg in options] or [setup.encoders.X264]
        labels = [setup.encoders.LABELS[encoder] for encoder in export_choices]
        export_encoder_combo.configure(state="readonly", values=labels)
        wanted = saved_export.get("encoder", setup.encoders.X264)
        export_encoder_combo.current(export_choices.index(wanted if wanted in export_choices
                                                         else setup.encoders.X264))

    section("Appearance", TAB_TRAY)
    # Each theme keeps its own changed colors (run_config "custom_colors",
    # by theme id); the rows show the theme the dropdown is on.
    # chosen_colors[theme id][key] is what each row shows for it, by
    # COLOR_SETTINGS key. Saved themes are written as soon as they're saved
    # or deleted; the choice and the colors wait for Save, like the rest.
    saved_custom = run_config.get("custom_colors") if isinstance(run_config.get("custom_colors"), dict) else {}
    chosen_colors = {}

    def theme_colors(theme_id):
        if theme_id not in chosen_colors:
            colors = ui_colors.theme_base(theme_id, run_config)[1]
            changed = saved_custom.get(theme_id) if isinstance(saved_custom.get(theme_id), dict) else {}
            colors.update({key: value.lower() for key, value in changed.items()
                           if key in colors and ui_colors.is_hex_color(value)})
            chosen_colors[theme_id] = colors
        return chosen_colors[theme_id]

    def custom_colors():
        """run_config's "custom_colors": each theme's colors that differ from
        its own - for the themes shown here, and as saved for the rest."""
        ids = {theme_id for theme_id, _label in ui_colors.theme_choices(run_config)}
        custom = {theme_id: colors for theme_id, colors in saved_custom.items()
                  if theme_id in ids and theme_id not in chosen_colors and colors}
        for theme_id, colors in chosen_colors.items():
            if theme_id not in ids:
                continue  # deleted
            base = ui_colors.theme_base(theme_id, run_config)[1]
            changed = {key: value for key, value in colors.items() if value != base[key]}
            if changed:
                custom[theme_id] = changed
        return custom

    theme_row = tk.Frame(frame)
    theme_row.grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1
    tk.Label(theme_row, text="Theme:").pack(side="left")
    theme_combo = ttk.Combobox(theme_row, state="readonly", width=28)
    theme_combo.pack(side="left", padx=(px(8), 0))
    theme_ids = []
    current_theme = {"id": ui_colors.configured_theme_id(run_config)}

    def list_themes(selected):
        choices = ui_colors.theme_choices(run_config)
        theme_ids[:] = [theme_id for theme_id, _label in choices]
        theme_combo.configure(values=[label for _id, label in choices])
        current_theme["id"] = selected if selected in theme_ids else ui_colors.mode_key(True)
        theme_combo.current(theme_ids.index(current_theme["id"]))
        show_colors()

    def on_theme_chosen(_event=None):
        current_theme["id"] = theme_ids[theme_combo.current()]
        show_colors()

    theme_combo.bind("<<ComboboxSelected>>", on_theme_chosen)

    colors_label = tk.Label(frame, fg=dark_theme.MUTED, justify="left", wraplength=px(560))
    colors_label.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
    next_row += 1
    colors_grid = tk.Frame(frame)
    colors_grid.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
    next_row += 1
    color_vars, color_swatches = {}, {}
    per_column = (len(dark_theme.COLOR_SETTINGS) + 1) // 2

    def shown_colors():
        return theme_colors(current_theme["id"])

    def show_color(key):
        color = shown_colors()[key]
        color_swatches[key].configure(background=color)
        if color_vars[key].get() != color:
            color_vars[key].set(color)

    def show_colors():
        if not color_vars:
            return  # still being built
        colors_label.config(text=f"{theme_combo.get()} colors - click a square to pick one, or type its hex "
                                 "code. Save applies them everywhere.")
        for key in color_vars:
            show_color(key)
        delete_button.configure(state="normal" if current_theme["id"].startswith(ui_colors.SAVED_PREFIX)
                                else "disabled")

    def on_hex_typed(key):
        color = color_picker.normalize_hex(color_vars[key].get())
        if color:
            shown_colors()[key] = color
            color_swatches[key].configure(background=color)

    def pick_color(key, label):
        color = color_picker.ask_color(root, shown_colors()[key], f"{label} - ClipEm", px)
        if color:
            shown_colors()[key] = color
            show_color(key)

    for index, (key, label, _name, _css) in enumerate(dark_theme.COLOR_SETTINGS):
        row, column = index % per_column, index // per_column * 3
        tk.Label(colors_grid, text=label).grid(
            row=row, column=column, sticky="w", padx=(px(28) if column else 0, px(8)), pady=px(2))
        swatch = tk.Frame(colors_grid, width=px(22), height=px(22), cursor="hand2",
                          highlightthickness=1, highlightbackground=dark_theme.MUTED)
        swatch.grid(row=row, column=column + 1, pady=px(2))
        swatch.bind("<Button-1>", lambda _e, k=key, t=label: pick_color(k, t))
        color_vars[key] = tk.StringVar()
        entry = tk.Entry(colors_grid, textvariable=color_vars[key], width=8, font="TkFixedFont")
        entry.grid(row=row, column=column + 2, sticky="w", padx=(px(6), 0), pady=px(2))
        color_vars[key].trace_add("write", lambda *_a, k=key: on_hex_typed(k))
        # An unfinished code shows the color again.
        entry.bind("<FocusOut>", lambda _e, k=key: show_color(k))
        color_swatches[key] = swatch

    def reset_colors():
        chosen_colors[current_theme["id"]] = ui_colors.theme_base(current_theme["id"], run_config)[1]
        show_colors()

    def write_saved_themes(themes):
        """Saved themes last whatever happens to the rest of this window."""
        run_config["saved_themes"] = themes
        config = load_run_config()
        config["saved_themes"] = themes
        save_run_config(config)

    def save_theme():
        name = popups.askstring("Save Theme", "A name for these colors:", parent=root)
        name = (name or "").strip()
        if not name:
            return
        if len(name) > ui_colors.MAX_THEME_NAME:
            popups.showerror("Save Theme", f"Theme names can be up to {ui_colors.MAX_THEME_NAME} characters.",
                             parent=root)
            return
        if name.lower() in {label.lower() for label, *_rest in ui_colors.THEMES.values()}:
            popups.showerror("Save Theme", f"\"{name}\" is one of ClipEm's themes - pick another name.",
                             parent=root)
            return
        themes = ui_colors.saved_themes(run_config)
        if name in themes and not popups.askyesno("Save Theme", f"Replace your theme \"{name}\"?", parent=root):
            return
        source = current_theme["id"]
        themes[name] = {"dark": ui_colors.theme_base(source, run_config)[0], "colors": dict(shown_colors())}
        try:
            write_saved_themes(themes)
        except Exception as exc:
            popups.showerror("Save Theme", f"Couldn't save the theme: {exc}", parent=root)
            return
        # The changes went into the new theme: the one they were made on
        # goes back to how it was.
        chosen_colors.pop(source, None)
        new_id = ui_colors.saved_theme_id(name)
        chosen_colors.pop(new_id, None)
        saved_custom.pop(new_id, None)
        list_themes(new_id)

    def delete_theme():
        theme_id = current_theme["id"]
        name = theme_id.removeprefix(ui_colors.SAVED_PREFIX)
        if not popups.askyesno("Delete Theme", f"Delete your theme \"{name}\"?", parent=root):
            return
        themes = ui_colors.saved_themes(run_config)
        dark = themes.get(name, {}).get("dark", True)
        themes.pop(name, None)
        try:
            write_saved_themes(themes)
        except Exception as exc:
            popups.showerror("Delete Theme", f"Couldn't delete the theme: {exc}", parent=root)
            return
        chosen_colors.pop(theme_id, None)
        list_themes(ui_colors.mode_key(dark))

    theme_buttons = tk.Frame(frame)
    theme_buttons.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
    next_row += 1
    tk.Button(theme_buttons, text="Reset to Defaults", command=reset_colors).pack(side="left")
    tk.Button(theme_buttons, text="Save as Theme...", command=save_theme).pack(side="left", padx=(px(4), 0))
    delete_button = tk.Button(theme_buttons, text="Delete Theme", command=delete_theme)
    delete_button.pack(side="left", padx=(px(4), 0))
    list_themes(current_theme["id"])
    initial_appearance = (current_theme["id"], custom_colors())

    section("Tray Menu", TAB_TRAY)
    recording_item_var = tk.BooleanVar(value=bool(run_config.get("show_recording_item", True)))
    menu_item_vars = {"show_recording_item": recording_item_var}
    for key, text in (
        ("show_save_clip_item", "Show \"Save Clip\" in the tray menu"),
        ("show_clip_editor_item", "Show \"Open Clip Editor\" in the tray menu"),
        ("show_replay_item", "Show \"Start/Stop Replay Buffer\" in the tray menu"),
        ("show_recording_item", "Show \"Start/Stop Recording\" in the tray menu"),
        ("show_obs_item", "Show \"Show/Hide OBS\" in the tray menu"),
    ):
        menu_item_vars.setdefault(key, tk.BooleanVar(value=bool(run_config.get(key, True))))
        tk.Checkbutton(frame, text=text, variable=menu_item_vars[key]).grid(
            row=next_row, column=0, columnspan=2, sticky="w")
        next_row += 1

    section("Scripts", TAB_SCRIPTS)
    stuck_var = tk.BooleanVar(value=stuck_enabled)
    black_var = tk.BooleanVar(value=black_enabled)
    disk_warnings_var = tk.BooleanVar(value=disk_warnings_enabled)
    mic_check_var = tk.BooleanVar(value=mic_check_enabled)
    for var, text, title, explanation in (
        (stuck_var, "Enable stuck-screen detection", "Stuck-Screen Detection", detection_help.STUCK_SCREEN),
        (black_var, "Enable black-screen detection", "Black-Screen Detection", detection_help.BLACK_SCREEN),
        (disk_warnings_var, "Enable disk space warnings", "Disk Space Warnings", detection_help.DISK_SPACE),
        (mic_check_var, "Enable missing-mic detection", "Missing-Mic Detection", detection_help.MIC_CHECK),
    ):
        script_row = tk.Frame(frame)
        script_row.grid(row=next_row, column=0, columnspan=2, sticky="w")
        tk.Checkbutton(script_row, text=text, variable=var).pack(side="left")
        info_button(script_row, title, explanation).pack(side="left", padx=(px(4), 0))
        next_row += 1

    # stuck_output_check.py's own options (OBS's Tools > Scripts).
    peak_guard_var = tk.BooleanVar(value=bool(audio_script["peak_guard_enabled"]))
    peak_ceiling_var = tk.StringVar(value=f"{float(audio_script['peak_ceiling_db']):g}")
    script_debug_var = tk.BooleanVar(value=bool(audio_script["debug_enabled"]))
    peak_row = tk.Frame(frame)
    peak_row.grid(row=next_row, column=0, columnspan=2, sticky="w")
    tk.Checkbutton(peak_row, text="Lower the volume of audio sources that peak", variable=peak_guard_var).pack(
        side="left")
    info_button(peak_row, "Audio Peak Guard", detection_help.PEAK_GUARD).pack(side="left", padx=(px(4), 0))
    next_row += 1
    ceiling_row = tk.Frame(frame)
    ceiling_row.grid(row=next_row, column=0, columnspan=2, sticky="w", padx=(px(24), 0))
    tk.Label(ceiling_row, text="Peak ceiling after lowering (dB):").pack(side="left")
    tk.Spinbox(ceiling_row, textvariable=peak_ceiling_var, width=6, increment=0.1,
               from_=obs_config.PEAK_CEILING_MIN_DB, to=obs_config.PEAK_CEILING_MAX_DB).pack(
        side="left", padx=(px(6), 0))
    next_row += 1
    debug_row = tk.Frame(frame)
    debug_row.grid(row=next_row, column=0, columnspan=2, sticky="w")
    tk.Checkbutton(debug_row, text="Enable debug messages in OBS's script log", variable=script_debug_var).pack(
        side="left")
    info_button(debug_row, "Debug Messages", detection_help.SCRIPT_DEBUG).pack(side="left", padx=(px(4), 0))
    next_row += 1

    section("Autostart with PC boot", TAB_BASIC)

    # The buttons act straight away (no Save needed) on the OS's autostart
    # entry, which starts ClipEm (OBS and the tray icon - one choice for
    # both, as the tray widget is required). Only the buttons that apply are
    # shown: Install when this folder has no entry, else Uninstall - and
    # one for entries left by another ClipEm folder (or an older version).
    autostart_status_var = tk.StringVar()
    autostart_row = tk.Frame(frame)
    install_button = tk.Button(autostart_row, text="Install Autostart")
    uninstall_button = tk.Button(autostart_row, text="Uninstall Autostart")
    uninstall_others_button = tk.Button(autostart_row)
    # A button's height while they're hidden for the check, so the tab is
    # already as tall as it ends up when the window is sized to fit it.
    autostart_row.configure(height=install_button.winfo_reqheight())

    def show_autostart_buttons(show_install, show_uninstall, others=0):
        install_button.pack_forget()
        uninstall_button.pack_forget()
        uninstall_others_button.pack_forget()
        if show_install:
            install_button.pack(side="left", padx=(0, px(4)))
        if show_uninstall:
            uninstall_button.pack(side="left", padx=(0, px(4)))
        if others:
            uninstall_others_button.configure(
                text=f"Uninstall {others} other detected ClipEm autostart entr{'y' if others == 1 else 'ies'}"
            )
            uninstall_others_button.pack(side="left", padx=(0, px(4)))

    def show_autostart_status(then=None):
        """Looks the entries up in the background - on Windows that runs
        PowerShell, which can take seconds to start - then shows them and
        calls then(entries) if they could be read."""
        autostart_status_var.set("Autostart entry: checking...")
        show_autostart_buttons(False, False)

        def on_entries(entries, exc):
            if exc is not None:
                autostart_status_var.set(f"Autostart entry: unknown ({exc})")
                show_autostart_buttons(True, True)
                return
            others = sum(1 for entry in entries if not entry["this_install"])
            ours = len(entries) > others
            text = "installed" if ours else "not installed"
            if others:
                text += f" (+{others} from another ClipEm folder)"
            autostart_status_var.set(f"Autostart entry: {text}")
            show_autostart_buttons(not ours, ours, others)
            if then:
                then(entries)

        run_in_background(root, find_autostart_entries, on_entries)

    def save_autostart_flags(enabled):
        """What run.py --boot starts (the entry runs it)."""
        config = load_run_config()
        config["autostart_obs"] = config["autostart_tray"] = enabled
        save_run_config(config)

    def on_install_autostart():
        try:
            save_autostart_flags(True)
            apply_boot_autostart(True)
        except Exception as exc:
            popups.showerror("Install Autostart", f"Couldn't install autostart: {exc}", parent=root)

        def check_installed(entries):
            if not any(entry["this_install"] for entry in entries):
                popups.showerror(
                    "Install Autostart", "Couldn't create the autostart entry - your system may not support it.",
                    parent=root,
                )

        show_autostart_status(then=check_installed)

    def on_uninstall_autostart():
        try:
            save_autostart_flags(False)
            remove_autostart_entries([entry for entry in find_autostart_entries() if entry["this_install"]])
        except Exception as exc:
            popups.showerror("Uninstall Autostart", f"Couldn't uninstall autostart: {exc}", parent=root)
        show_autostart_status()

    def on_uninstall_other_autostarts():
        try:
            others = [entry for entry in find_autostart_entries() if not entry["this_install"]]
        except Exception as exc:
            popups.showerror("Uninstall Autostart", f"Couldn't look up the autostart entries: {exc}", parent=root)
            return
        if others and popups.askyesno(
            "Uninstall Autostart",
            "These ClipEm autostart entries start another ClipEm folder (or an older version):\n\n"
            + "\n".join(f"  \u2022 {describe_autostart_entry(entry)}" for entry in others)
            + "\n\nRemove them?",
            parent=root,
        ):
            try:
                remove_autostart_entries(others)
            except Exception as exc:
                popups.showerror("Uninstall Autostart", f"Couldn't remove them: {exc}", parent=root)
        show_autostart_status()

    install_button.configure(command=on_install_autostart)
    uninstall_button.configure(command=on_uninstall_autostart)
    uninstall_others_button.configure(command=on_uninstall_other_autostarts)
    autostart_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
    next_row += 1
    tk.Label(frame, textvariable=autostart_status_var, fg=dark_theme.MUTED).grid(
        row=next_row, column=0, columnspan=2, sticky="w"
    )
    next_row += 1
    show_autostart_status()

    # Like autostart: each shortcut's buttons act straight away, and only
    # the ones that apply are shown.
    shortcut_kinds = [(kind, name) for kind, name in ((SHORTCUT_START_MENU, "Start Menu"),
                                                      (SHORTCUT_DESKTOP, "Desktop"))
                      if shortcut_path(kind)]
    if shortcut_kinds:
        section("Shortcuts", TAB_BASIC)
    for kind, name in shortcut_kinds:
        shortcut_status_var = tk.StringVar()
        shortcut_row = tk.Frame(frame)
        shortcut_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        next_row += 1
        tk.Label(frame, textvariable=shortcut_status_var, fg=dark_theme.MUTED).grid(
            row=next_row, column=0, columnspan=2, sticky="w")
        next_row += 1
        shortcut_install = tk.Button(shortcut_row, text=f"Install {name} Shortcut")
        shortcut_uninstall = tk.Button(shortcut_row, text=f"Uninstall {name} Shortcut")
        shortcut_row.configure(height=shortcut_install.winfo_reqheight())  # as autostart_row

        def show_shortcut_status(kind=kind, name=name, status_var=shortcut_status_var,
                                 install=shortcut_install, uninstall=shortcut_uninstall):
            """Looked up in the background - reading a Windows .lnk runs PowerShell."""
            status_var.set(f"{name} shortcut: checking...")
            install.pack_forget()
            uninstall.pack_forget()

            def on_state(state, exc):
                if exc is not None:
                    status_var.set(f"{name} shortcut: unknown ({exc})")
                    state = None
                else:
                    status_var.set(f"{name} shortcut: " + {
                        SHORTCUT_OURS: "installed",
                        SHORTCUT_OTHER: "starts another ClipEm folder",
                        SHORTCUT_BROKEN: "broken (from a ClipEm folder that's gone)",
                    }.get(state, "not installed"))
                if state != SHORTCUT_OURS:
                    install.pack(side="left", padx=(0, px(4)))
                if state in (SHORTCUT_OURS, SHORTCUT_BROKEN, None):
                    uninstall.pack(side="left", padx=(0, px(4)))

            run_in_background(root, lambda: shortcut_state(kind), on_state)

        def on_shortcut(action, kind=kind, name=name, show=show_shortcut_status):
            try:
                action(kind)
            except Exception as exc:
                popups.showerror(f"{name} Shortcut", f"Couldn't change the {name} shortcut: {exc}", parent=root)
            show()

        shortcut_install.configure(command=lambda on=on_shortcut: on(install_shortcut))
        shortcut_uninstall.configure(command=lambda on=on_shortcut: on(uninstall_shortcut))
        show_shortcut_status()

    section("OBS Websocket", TAB_WEBSOCKET)

    tk.Label(frame, text="Port:").grid(row=next_row, column=0, sticky="w", pady=px(4))
    port_var = tk.StringVar(value=str(port))
    tk.Entry(frame, textvariable=port_var, width=24).grid(
        row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4)
    )
    next_row += 1

    tk.Label(frame, text="Password:").grid(row=next_row, column=0, sticky="w", pady=px(4))
    password_var = tk.StringVar(value=password)
    password_row = tk.Frame(frame)
    password_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
    next_row += 1
    password_entry = tk.Entry(password_row, textvariable=password_var, width=20, show="*")
    password_entry.pack(side="left")

    password_visible = tk.BooleanVar(value=False)

    def on_toggle_password_visibility():
        password_entry.config(show="" if password_visible.get() else "*")

    tk.Checkbutton(
        password_row,
        text="\U0001F441",  # eye emoji - toggles the entry above between "*" and plain text
        variable=password_visible,
        command=on_toggle_password_visibility,
        indicatoron=False,
        width=2,
    ).pack(side="left", padx=(px(4), 0))

    def on_copy_password():
        root.clipboard_clear()
        root.clipboard_append(password_var.get())
        root.update()  # hand the text to the clipboard now, not at the next idle moment
        copy_button.config(text="Copied!")
        root.after(1500, lambda: copy_button.winfo_exists() and copy_button.config(text="Copy"))

    copy_button = tk.Button(password_row, text="Copy", command=on_copy_password, width=7)
    copy_button.pack(side="left", padx=(px(4), 0))
    next_row += 1
    if ws_load_error:
        tk.Label(
            frame,
            text=f"Warning: couldn't read current websocket settings ({ws_load_error})",
            fg=dark_theme.ERROR,
            wraplength=px(280),
            justify="left",
        ).grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(6), 0))

    if updater is not None:
        update_config = updater.load_update_config()
        local_version = updater.get_local_version()

        section("Updates", TAB_SCRIPTS)

        tk.Label(frame, text=f"Current version: {local_version}", fg=dark_theme.MUTED).grid(
            row=next_row, column=0, columnspan=2, sticky="w"
        )
        next_row += 1

        check_updates_var = tk.BooleanVar(value=update_config["check_for_updates"])

        def on_toggle_check_updates():
            try:
                updater.change_update_config(check_for_updates=check_updates_var.get())
            except Exception as exc:
                popups.showerror("Updates", f"Couldn't save the update setting:\n{exc}")
                check_updates_var.set(not check_updates_var.get())

        tk.Checkbutton(
            frame, text="Allow checking for updates (when ClipEm starts)", variable=check_updates_var,
            command=on_toggle_check_updates,
        ).grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        next_row += 1

        source_repo_var = tk.StringVar(value=updater.repo_url(update_config["source_repo"]))

        def save_source_repo(repo):
            try:
                updater.change_update_config(source_repo=repo, skipped_version=None)
            except Exception as exc:
                popups.showerror("Updates", f"Couldn't save the update setting:\n{exc}")
                return
            source_repo_var.set(updater.repo_url(repo))

        def on_change_source_repo():
            if not popups.askyesno(
                "Source Repo",
                "ClipEm downloads its updates from the releases of this GitHub repository.\n\n"
                "Only change it if you know the repository publishes ClipEm builds - updates must "
                "still be signed with the official ClipEm release key, so any other repository "
                "makes update checks fail.\n\nChange the source repo?",
                parent=root,
            ):
                return
            text = popups.askstring(
                "Source Repo", "GitHub repository link (https://github.com/owner/name):",
                initialvalue=source_repo_var.get(), parent=root,
            )
            if text is None:
                return
            repo = updater.repo_from_url(text)
            if repo is None:
                popups.showerror(
                    "Source Repo", f"\"{text.strip()}\" isn't a GitHub repository link like "
                    "https://github.com/owner/name.", parent=root,
                )
                return
            save_source_repo(repo)

        tk.Label(frame, text="Source repo:").grid(row=next_row, column=0, sticky="w", pady=px(4))
        source_row = tk.Frame(frame)
        source_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
        tk.Entry(source_row, textvariable=source_repo_var, state="readonly", width=32).pack(side="left")
        tk.Button(source_row, text="Change...", command=on_change_source_repo).pack(side="left", padx=(px(4), 0))
        tk.Button(
            source_row, text="Reset", command=lambda: save_source_repo(updater.DEFAULT_SOURCE_REPO),
        ).pack(side="left", padx=(px(4), 0))
        next_row += 1

        # Main follows the published releases, Dev the rolling "dev" pre-release.
        branch_labels = {updater.CHANNEL_RELEASES: "Main", updater.CHANNEL_LATEST: "Dev"}
        branch_var = tk.StringVar(value=branch_labels.get(update_config["update_channel"], "Main"))
        saved_branch = {"label": branch_var.get()}

        def on_branch_selected(_event=None):
            label = branch_var.get()
            if label == saved_branch["label"]:
                return
            channel = next(key for key, text in branch_labels.items() if text == label)
            if channel == updater.CHANNEL_LATEST:
                explanation = (
                    "The Dev branch installs development builds published for testing (the \"dev\" "
                    "pre-release on GitHub) before they become a release. They may be unfinished or "
                    "broken.\n\nSwitch to Dev?"
                )
            else:
                explanation = (
                    "The Main branch installs only published releases. If you're on a dev build, "
                    "the next update check offers the current release instead.\n\nSwitch to Main?"
                )
            if not popups.askyesno("Update Branch", explanation, parent=root):
                branch_var.set(saved_branch["label"])
                return
            try:
                updater.change_update_config(update_channel=channel, skipped_version=None)
            except Exception as exc:
                popups.showerror("Updates", f"Couldn't save the update setting:\n{exc}")
                branch_var.set(saved_branch["label"])
                return
            saved_branch["label"] = label

        tk.Label(frame, text="Update branch:").grid(row=next_row, column=0, sticky="w", pady=px(4))
        branch_box = ttk.Combobox(frame, textvariable=branch_var, values=list(branch_labels.values()),
                                  state="readonly", width=8)
        branch_box.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
        branch_box.bind("<<ComboboxSelected>>", on_branch_selected)
        next_row += 1

        def on_check_for_updates():
            check_button.config(state="disabled", text="Checking...")
            run_in_background(root, updater.check_for_update, on_update_checked)

        def on_update_checked(result, error):
            check_button.config(state="normal", text="Check for Updates")
            if error is not None:
                result = {"error": str(error)}
            if result["error"]:
                popups.showerror(
                    "Check for Updates",
                    f"Couldn't check {source_repo_var.get()} for updates:\n{result['error']}",
                )
                return
            current, latest = result["local_version"], result["remote_version"]
            if not result["update_available"]:
                popups.showinfo(
                    "Check for Updates",
                    f"Current version: {current}\nLatest version: {latest}\n\nClipEm is up to date.",
                )
                return
            changes = "\n".join(f"  \u2022 {line}" for line in updater.describe_update(result))
            notes = f"Release notes:\n{result['release_notes']}\n\n" if result["release_notes"] else ""
            if popups.askyesno(
                "Check for Updates",
                f"An update is available:\n\n{changes}\n\n{notes}Install it now? This closes OBS and the "
                "tray icon, updates ClipEm and starts them again.\n\n(Choosing No offers it again the "
                "next time ClipEm starts.)",
            ):
                try:
                    updater.change_update_config(install_on_next_launch=True)
                except Exception as exc:
                    popups.showerror("Check for Updates", f"Couldn't save the update choice:\n{exc}")
                    return
                check_button.config(state="disabled", text="Restarting...")
                restart_clipem_in_background(root, on_clipem_restarted)

        def on_clipem_restarted(error_message, exc):
            if exc is not None:
                log.warning(f"Restart ClipEm failed: {exc}")
                error_message = (f"Couldn't restart ClipEm ({exc}). The update will be installed the "
                                 "next time ClipEm starts.")
            if error_message:
                popups.showwarning("Check for Updates", error_message)
            root.destroy()

        check_button = tk.Button(frame, text="Check for Updates", command=on_check_for_updates)
        check_button.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        next_row += 1

        def on_release_notes():
            dialog = tk.Toplevel(root)
            dialog.title("Release Notes")
            dialog.transient(root)
            body = tk.Frame(dialog, padx=px(16), pady=px(12))
            body.pack(fill="both", expand=True)
            parts = release_notes.lines(release_notes.load()) or [(None, "No release notes were found.")]
            release_notes.notes_box(body, parts, width=80, height=26).pack(fill="both", expand=True)
            buttons = tk.Frame(body)
            buttons.pack(anchor="e", pady=(px(10), 0))
            # The newest version's, as every older one has been out for a while.
            added = [setting["key"] for setting in release_notes.new_settings(release_notes.load()[:1])]

            def show_new_settings():
                dialog.destroy()
                highlight_settings(added)

            if added:
                tk.Button(buttons, text="Show New Settings", command=show_new_settings).pack(side="left", padx=(0, px(8)))
            tk.Button(buttons, text="Close", width=10, command=dialog.destroy).pack(side="left")
            dialog.bind("<Escape>", lambda _event: dialog.destroy())
            popups.place(dialog, root)

        release_notes_row = tk.Frame(frame)
        release_notes_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        release_notes_button = tk.Button(release_notes_row, text="Release Notes", command=on_release_notes)
        release_notes_button.pack(side="left")
        highlightable("release_notes", release_notes_button, TAB_SCRIPTS)
        next_row += 1

    # --- Troubleshooting: a bug report (sent, or saved as a zip), and a clean reinstall.
    section("Troubleshooting", TAB_SCRIPTS)

    def on_bug_report():
        dialog = tk.Toplevel(root)
        dialog.title("Send Bug Report")
        dialog.resizable(False, False)
        dialog.transient(root)
        body = tk.Frame(dialog, padx=px(16), pady=px(12))
        body.pack(fill="both", expand=True)
        tk.Label(
            body, justify="left", wraplength=px(380),
            text="What happened? What were you doing when it went wrong?",
        ).pack(anchor="w")
        description_text = tk.Text(body, width=52, height=8, wrap="word")
        description_text.pack(anchor="w", fill="x", pady=(px(6), 0))
        tk.Label(
            body, fg=dark_theme.MUTED, justify="left", wraplength=px(380),
            text=(
                "Your description is posted publicly as a GitHub issue. ClipEm's and OBS's logs and "
                "a summary of this install (versions, settings and folder paths) are uploaded "
                "privately."
            ),
        ).pack(anchor="w", pady=(px(6), 0))

        def set_sending(sending):
            state = "disabled" if sending else "normal"
            for child in dialog_buttons.winfo_children():
                child.config(state=state)
            description_text.config(state=state)
            send_button.config(text="Sending..." if sending else "Send")

        def on_save_instead():
            dialog.destroy()
            save_bug_report_to_file()

        def on_send():
            description = description_text.get("1.0", "end").strip()
            if not description:
                popups.showwarning("Send Bug Report", "Describe what happened first.", parent=dialog)
                return
            set_sending(True)
            run_in_background(root, lambda: bug_report.send_bug_report(description), on_sent)

        def on_sent(result, error):
            if error is not None:
                log.warning(f"Sending the bug report failed: {error}")
                if not dialog.winfo_exists():
                    popups.showerror("Send Bug Report", f"Couldn't send the bug report:\n{error}", parent=root)
                    return
                set_sending(False)
                if popups.askyesno(
                    "Send Bug Report",
                    f"Couldn't send the bug report:\n{error}\n\nSave it to a file instead, to attach it "
                    "to an issue or a message yourself?",
                    parent=dialog,
                ):
                    on_save_instead()
                return
            report_id, issue_url = result
            log.info(f"Bug report sent: {report_id} ({issue_url})")
            if dialog.winfo_exists():
                dialog.destroy()
            if issue_url and popups.askyesno(
                "Send Bug Report",
                f"Thanks - the bug report was sent (report ID {report_id}).\n\nOpen the GitHub issue in your browser?",
                parent=root,
            ):
                webbrowser.open(issue_url)
            elif not issue_url:
                popups.showinfo("Send Bug Report", f"Thanks - the bug report was sent (report ID {report_id}).",
                                    parent=root)

        dialog_buttons = tk.Frame(body)
        dialog_buttons.pack(side="bottom", anchor="e", pady=(px(12), 0))
        tk.Button(dialog_buttons, text="Cancel", command=dialog.destroy, width=10).pack(side="left", padx=px(4))
        tk.Button(dialog_buttons, text="Save to File Instead...", command=on_save_instead).pack(
            side="left", padx=px(4))
        send_button = tk.Button(dialog_buttons, text="Send", command=on_send, width=10)
        send_button.pack(side="left", padx=px(4))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        popups.place(dialog, root)
        description_text.focus_set()
        dialog.grab_set()

    def save_bug_report_to_file():
        path = file_dialogs.asksaveasfilename(
            parent=root, title="Save Bug Report", initialdir=bug_report.default_save_dir(),
            initialfile=bug_report.default_file_name(), defaultextension=".zip",
            filetypes=[("Zip files", "*.zip")],
        )
        if not path:
            return
        bug_button.config(state="disabled")
        run_in_background(
            root, lambda: bug_report.create_bug_report(path),
            lambda count, error: on_bug_report_done(path, count, error),
        )

    def on_bug_report_done(path, count, error):
        bug_button.config(state="normal")
        if error is not None:
            log.warning(f"Bug report failed: {error}")
            popups.showerror("Bug Report", f"Couldn't create the bug report:\n{error}", parent=root)
            return
        log.info(f"Bug report saved: {path} ({count} log files)")
        if popups.askyesno(
            "Bug Report",
            f"Saved the bug report ({count} log files) to:\n{path}\n\n"
            "It holds ClipEm's and OBS's logs and a summary of this install (versions, settings and "
            "folder paths) - the websocket password is left out. Attach it to your bug report.\n\n"
            "Show it in its folder?",
            parent=root,
        ):
            try:
                reveal_file(path)
            except OSError as exc:
                popups.showerror("Bug Report", f"Couldn't open the folder: {exc}", parent=root)

    def on_reinstall():
        dialog = tk.Toplevel(root)
        dialog.title("Reinstall ClipEm")
        dialog.resizable(False, False)
        dialog.transient(root)
        body = tk.Frame(dialog, padx=px(16), pady=px(12))
        body.pack(fill="both", expand=True)
        tk.Label(
            body, justify="left", wraplength=px(380),
            text=(
                "Downloads ClipEm, OBS and the tray widget's packages again and replaces the "
                "installed copies - for when something is broken or missing. Your clips aren't "
                "touched.\n\nThis closes OBS and the tray icon - the current replay buffer is not saved."
            ),
        ).pack(anchor="w")
        keep_obs_var = tk.BooleanVar(value=True)
        keep_clipem_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            body, variable=keep_obs_var, justify="left",
            text="Try to keep OBS settings (scenes, capture, audio, video quality,\nclip folder, hotkey)",
        ).pack(anchor="w", pady=(px(10), 0))
        tk.Checkbutton(
            body, variable=keep_clipem_var, justify="left",
            text="Try to keep ClipEm settings (autostart, screen checks, updates,\nclip sound)",
        ).pack(anchor="w")
        tk.Label(
            body, fg=dark_theme.MUTED, justify="left", wraplength=px(380),
            text="Without the OBS settings, Setup opens afterwards to choose them again.",
        ).pack(anchor="w", pady=(px(6), 0))

        def on_confirm():
            try:
                reinstall.request_reinstall(keep_obs_var.get(), keep_clipem_var.get())
            except Exception as exc:
                popups.showerror("Reinstall ClipEm", f"Couldn't start the reinstall: {exc}", parent=dialog)
                return
            log.info(f"Reinstall requested (keep OBS settings: {keep_obs_var.get()}, "
                     f"keep ClipEm settings: {keep_clipem_var.get()})")
            dialog.destroy()
            for child in button_frame.winfo_children():
                child.config(state="disabled")
            reinstall_button.config(state="disabled")
            status_var.set("Restarting ClipEm to reinstall...")
            restart_clipem_in_background(root, on_reinstall_restarted, REINSTALL_PENDING_MESSAGE)

        dialog_buttons = tk.Frame(body)
        dialog_buttons.pack(side="bottom", anchor="e", pady=(px(12), 0))
        tk.Button(dialog_buttons, text="Cancel", command=dialog.destroy, width=10).pack(side="left", padx=px(4))
        tk.Button(dialog_buttons, text="Reinstall", command=on_confirm, width=10).pack(side="left", padx=px(4))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        popups.place(dialog, root)
        dialog.grab_set()

    def on_reinstall_restarted(error_message, exc):
        if exc is not None:
            log.warning(f"Restart ClipEm for the reinstall failed: {exc}")
            error_message = f"Couldn't restart ClipEm ({exc}). {REINSTALL_PENDING_MESSAGE}"
        if error_message:
            popups.showwarning("Reinstall ClipEm", error_message)
        root.destroy()

    troubleshooting_row = tk.Frame(frame)
    troubleshooting_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
    bug_button = tk.Button(troubleshooting_row, text="Send Bug Report...", command=on_bug_report)
    bug_button.pack(side="left")
    view_logs_button = tk.Button(troubleshooting_row, text="View Logs...",
                                 command=lambda: log_viewer.open_log_viewer(root, px, reveal_file))
    view_logs_button.pack(side="left", padx=(px(4), 0))
    highlightable("view_logs", view_logs_button, TAB_SCRIPTS)
    reinstall_button = tk.Button(troubleshooting_row, text="Reinstall ClipEm...", command=on_reinstall)
    reinstall_button.pack(side="left", padx=(px(4), 0))
    next_row += 1

    def on_save():
        if (not editors_off_var.get() and editor_kind_var.get() == editor_install.EXTERNAL
                and not editor_path_var.get().strip()):
            popups.showerror("Clip Editor", "Choose your external editor, or pick ClipEm's editor.", parent=root)
            return
        try:
            port_val = obs_config.validate_port(port_var.get().strip())
        except ValueError:
            popups.showerror("Invalid Port", "Port must be a number from 1 to 65535.")
            return
        password_val = password_var.get()
        if not password_val:
            popups.showerror("Invalid Password", "Password can't be empty.")
            return
        hotkey_val = [hotkey_ctrl_var.get(), hotkey_alt_var.get(), hotkey_shift_var.get(), hotkey_key_var.get()]
        if hotkey_val[3] == NO_HOTKEY:
            hotkey_val = list(obs_config.CLEARED_HOTKEY)  # Clear: no clip hotkey
        else:
            try:
                obs_config.hotkey_label_to_obs_key(hotkey_val[3])
            except ValueError:
                popups.showerror("Invalid Hotkey", "Pick a key for the clip hotkey.")
                return
        obs_settings = {
            "websocket": {"port": port_val, "password": password_val},
            "clip_hotkey": hotkey_val,
            "toggle_hotkeys": {
                name: None if key.get() == NO_HOTKEY else [ctrl.get(), alt.get(), shift.get(), key.get()]
                for name, (ctrl, alt, shift, key) in toggle_hotkey_vars.items()
            },
            "clip_sound": {"enabled": bool(sound_enabled_var.get()), "volume": round(sound_volume_var.get() / 100, 2)},
            "clip_folder": clip_folder_var.get(),
            "split_tracks": bool(split_tracks_var.get()),
            "clip_reexport": dict(initial_obs_settings["clip_reexport"], enabled=bool(reexport_var.get())),
        }
        try:
            ceiling = float(peak_ceiling_var.get())
        except ValueError:
            ceiling = None
        if ceiling is None or not obs_config.PEAK_CEILING_MIN_DB <= ceiling <= obs_config.PEAK_CEILING_MAX_DB:
            popups.showerror("Audio Peak Guard", f"Enter a peak ceiling from {obs_config.PEAK_CEILING_MIN_DB:g} "
                             f"to {obs_config.PEAK_CEILING_MAX_DB:g} dB.", parent=root)
            return
        obs_settings["audio_script"] = {"peak_guard_enabled": bool(peak_guard_var.get()),
                                        "peak_ceiling_db": round(ceiling, 1),
                                        "debug_enabled": bool(script_debug_var.get())}
        try:
            buffer_seconds = int(float(buffer_var.get()))
            recording_bitrate = int(float(recording_bitrate_var.get()))
        except ValueError:
            popups.showerror("Recording", "Enter numbers for the replay buffer and bitrate.", parent=root)
            return
        buffer_seconds = max(setup.REPLAY_BUFFER_MIN_SECONDS, min(buffer_seconds, setup.REPLAY_BUFFER_MAX_SECONDS))
        recording_bitrate = max(setup.BITRATE_MIN_KBPS, min(recording_bitrate, setup.BITRATE_MAX_KBPS))
        if buffer_seconds != recording["replay_buffer_seconds"]:
            obs_settings["replay_buffer_seconds"] = buffer_seconds
        try:
            post_seconds = int(float(post_seconds_var.get()))
        except ValueError:
            popups.showerror("Post-clip", "Enter how many seconds to keep recording after the hotkey.", parent=root)
            return
        obs_settings["post_clip"] = obs_config.clip_post.normalize({
            "enabled": bool(post_enabled_var.get()), "seconds": post_seconds,
            "sound_after": bool(post_sound_var.get())})
        if recording_bitrate != recording["bitrate"]:
            obs_settings["recording_bitrate"] = recording_bitrate
        if encoder_picked["value"] and tuple(encoder_picked["value"]) != tuple(recording["encoder"]):
            encoder, device = encoder_picked["value"]
            obs_settings["recording_encoder"] = {"encoder": encoder, "device": device}
        if devices["displays"] is not None:
            display = devices["displays"][display_combo.current()]
            out_width, out_height = (int(v) for v in resolution_combo.get().split("x"))
            fps = int(fps_combo.get())
            display_changed = display_combo.current() != devices["initial_display"]
            if display_changed or (out_width, out_height, fps) != (recording["width"], recording["height"],
                                                                   recording["fps"]):
                obs_settings["video"] = {"base_width": display["width"], "base_height": display["height"],
                                         "output_width": out_width, "output_height": out_height, "fps": fps}
            if display_changed:
                obs_settings["capture_monitor"] = {"index": display_combo.current(),
                                                   "device_id": display.get("device_id")}
        if devices["audio"] and devices["audio"]["output"] and devices["audio"]["input"]:
            chosen_audio = {"output": devices["audio"]["output"][output_combo.current()]["device_id"],
                            "input": devices["audio"]["input"][input_combo.current()]["device_id"]}
            if chosen_audio != recording["audio"]:
                obs_settings["audio_devices"] = chosen_audio
        if mic_reading["value"]:
            obs_settings["mic_filter"] = mic_reading["value"]

        clip_naming_settings = naming.get()
        problem = naming_form.clip_naming.validate(clip_naming_settings)
        if problem:
            popups.showerror("Clip Names", problem, parent=root)
            return
        values = reexport_values()
        if reexport_var.get() and (not values or values[0] <= 0):
            popups.showerror("Smaller Copy", "Enter a target size (MB, more than 0) and how many "
                                 "seconds to keep.", parent=root)
            return
        if values and values[0] > 0:
            # Never longer than the (possibly just changed) replay buffer.
            obs_settings["clip_reexport"].update(target_mb=values[0], crop_seconds=min(values[1], buffer_seconds))
        editor_numbers = {}
        for key, var, label in (("editor_discord_mb", discord_mb_var, "The Discord export size"),
                                ("editor_asset_days", asset_days_var, "The days to keep unused imports")):
            text = var.get().strip()
            if not text.isdigit() or int(text) < 1:
                popups.showerror("Clip Editor", f"{label} must be a whole number, 1 or more.", parent=root)
                return
            editor_numbers[key] = int(text)
        clip_transfer = None
        if obs_settings["clip_folder"] != initial_obs_settings["clip_folder"]:
            # OBS won't start the replay buffer into a folder that isn't there.
            try:
                os.makedirs(obs_settings["clip_folder"], exist_ok=True)
            except OSError as exc:
                popups.showerror("Clip Folder", f"Couldn't create the clip folder: {exc}", parent=root)
                return
            clip_transfer = ask_clip_transfer(initial_obs_settings["clip_folder"], obs_settings["clip_folder"])
            if clip_transfer == "cancel":
                return
        if chosen_sound["path"]:
            # A plain file OBS's script reads on every clip - takes effect
            # straight away, OBS running or not.
            try:
                obs_config.replace_clip_sound_wav(chosen_sound["path"])
            except Exception as exc:
                popups.showerror("Clip Sound", f"Couldn't use that sound: {exc}", parent=root)
                return
            chosen_sound["path"] = None

        try:
            # Fresh copy: the tray widget writes to this file too (see
            # obs_config.set_capture_display_user_changed).
            run_config = load_run_config()
            # Read by the tray widget before each check - see stuck_screen.py.
            run_config["stuck_check_enabled"] = bool(stuck_var.get())
            run_config["black_check_enabled"] = bool(black_var.get())
            # Read by the tray widget each poll - see disk_space.py.
            run_config["disk_space_warnings_enabled"] = bool(disk_warnings_var.get())
            run_config["mic_check_enabled"] = bool(mic_check_var.get())  # see mic_check.py
            run_config["clip_naming"] = clip_naming_settings  # read as each clip is saved
            run_config["custom_editor_path"] = editor_path_var.get().strip()
            run_config["clip_editor"] = editor_kind_var.get()
            run_config["clip_editors_disabled"] = bool(editors_off_var.get())
            quality = next(k for k, v in quality_labels.items() if v == quality_combo.get())
            speed = next(k for k, v in speed_labels.items() if v == speed_combo.get())
            encoder = (export_choices[export_encoder_combo.current()] if export_encoder_combo.current() >= 0
                       else saved_export.get("encoder", setup.encoders.X264))
            run_config["editor_export"] = {"encoder": encoder, "quality": quality, "speed": speed}
            run_config.update(editor_numbers)
            run_config["editor_open_in"] = next(k for k, v in open_in_labels.items() if v == open_in_combo.get())
            run_config["editor_sub_tracks"] = bool(sub_tracks_var.get())
            for key, var in menu_item_vars.items():  # read by the tray menu
                run_config[key] = bool(var.get())
            appearance = (current_theme["id"], custom_colors())
            run_config["theme"], run_config["custom_colors"] = appearance
            # What only knows dark from light (the tray menus, the editor's CSS) goes by this.
            run_config["dark_mode"] = ui_colors.theme_base(current_theme["id"], run_config)[0]
            if not run_config["custom_colors"]:
                del run_config["custom_colors"]
            run_config["clip_saved_notification"] = bool(clip_notification_var.get())
            save_run_config(run_config)
        except Exception as exc:
            popups.showerror("Save Failed", f"Couldn't save screen-detection and disk-space settings: {exc}")
            return
        # The editor and the tray menu pick the colors up themselves (they
        # poll run_config); this window is rebuilt with them once it closes.
        if appearance != initial_appearance:
            reopen_on["tab"] = current_tab["name"]

        try:
            outcome = save_obs_settings(obs_settings, initial_obs_settings)
        except Exception as exc:
            popups.showerror("Save Failed", f"Couldn't save settings: {exc}")
            return
        if clip_transfer:
            run_clip_transfer(clip_transfer, initial_obs_settings["clip_folder"], obs_settings["clip_folder"],
                              lambda: finish_save(outcome))
        else:
            finish_save(outcome)

    def ask_clip_transfer(old, new):
        """What to do with the clips left in the old folder: "move", "copy",
        None (leave them), or "cancel" (don't save yet)."""
        if clip_move.same_folder(old, new):
            return None
        clips = clip_move.list_clips(old)
        if not clips:
            return None
        size = clip_move.bytes_needed(clips) / 1e9
        choice = popups.askchoice(
            "Clip Folder",
            f"Your old clip folder has {len(clips)} clip file{'' if len(clips) == 1 else 's'} ({size:.1f} GB):\n"
            f"{old}\n\nMove them to the new folder, copy them there, or leave them where they are?",
            [("Move", "move"), ("Copy", "copy"), ("Leave Them", "leave"), ("Cancel", "cancel")], "move", parent=root)
        if choice in (None, "cancel"):
            return "cancel"
        if choice == "leave":
            return None
        problem = clip_move.room_problem(clips, new, choice == "move")
        if problem:
            popups.showerror("Clip Folder", problem, parent=root)
            return "cancel"
        return choice

    def run_clip_transfer(action, old, new, done):
        """Moves or copies the clips behind a status line, then done()."""
        for child in button_frame.winfo_children():
            child.config(state="disabled")
        doing = "Moving" if action == "move" else "Copying"
        shared = {"bytes": 0, "total": 0, "running": True}
        status_var.set(f"{doing} clips...")

        def show_progress():
            if not shared["running"]:
                return
            if shared["total"]:
                status_var.set(f"{doing} clips... {shared['bytes'] * 100 // shared['total']}%")
            root.after(200, show_progress)

        def work():
            def progress(count, total):
                shared.update(bytes=count, total=total)

            return clip_move.transfer(old, new, action == "move", os.path.join(APP_DIR, "cache", "edits"),
                                      os.path.join(APP_DIR, "cache", "exports.json"), progress)

        def finished(result, exc):
            shared["running"] = False
            status_var.set("")
            if exc is not None:
                popups.showwarning("Clip Folder", f"Couldn't finish: {exc}", parent=root)
            elif result["skipped"] or result["failed"]:
                lines = [f"{len(result['skipped'])} already in the new folder, so left where they are."
                         if result["skipped"] else ""]
                lines += [f"{name}: {why}" for name, why in result["failed"][:5]]
                popups.showwarning("Clip Folder", f"{result['done']} done.\n" + "\n".join(filter(None, lines)),
                                   parent=root)
            for child in button_frame.winfo_children():
                child.config(state="normal")
            done()

        run_in_background(root, work, finished)
        root.after(200, show_progress)

    def finish_save(outcome):
        if outcome == "unchanged":
            root.destroy()
            return
        if outcome == "applied":
            popups.showinfo("Saved", "Settings saved.")
            root.destroy()
            return

        if not popups.askyesno(
            "Restart OBS?",
            "OBS only picks up websocket, hotkey, clip sound, clip folder, multitrack "
            "audio and script settings when it starts. Restart OBS now to apply them?\n\n"
            "(Choosing No applies them the next time ClipEm starts OBS.)",
        ):
            root.destroy()
            return

        for child in button_frame.winfo_children():
            child.config(state="disabled")
        status_var.set("Restarting OBS...")

        def on_restarted(error_message, exc):
            if exc is not None:
                log.warning(f"Restart OBS failed: {exc}")
                error_message = (
                    f"Couldn't restart OBS ({exc}). Your settings are saved and will "
                    "apply the next time ClipEm starts OBS."
                )
            if error_message:
                popups.showwarning("Restart OBS", error_message)
            root.destroy()

        run_in_background(root, restart_obs_with_queued_settings, on_restarted)

    def on_cancel():
        root.destroy()

    status_var = tk.StringVar(value="")
    tk.Label(footer, textvariable=status_var, fg=dark_theme.MUTED).pack(side="left")

    button_frame = tk.Frame(footer)
    button_frame.pack(side="right")
    tk.Button(button_frame, text="Cancel", command=on_cancel, width=10).pack(side="left", padx=px(4))
    tk.Button(button_frame, text="Save", command=on_save, width=10).pack(side="left", padx=px(4))

    indent_under_text(reexport_box, reexport_check)

    # Sidebar: one tab per settings group. Picking one shows just its cards
    # (All shows every card); nothing is rebuilt.
    tab_labels = {}
    current_tab = {"name": None}
    window_size = {"width": 0, "height": 0}  # what it opened at, until it's shown and can be asked

    def show_tab(name):
        current_tab["name"] = name
        for card, _tab in cards:
            card.pack_forget()
        for card, tab in cards:
            if name == "All" or tab == name:
                card.pack(fill="x", pady=(0, px(10)))
        color_tabs()
        canvas.yview_moveto(0)
        if highlighted:
            fit_badges()

    def fit_badges():
        """Widens the window if badges pushed a tab's settings past its edge."""
        root.update_idletasks()
        for card, _tab in cards:
            card.fit()
        root.update_idletasks()
        needed = sidebar.winfo_reqwidth() + page.winfo_reqwidth() + scrollbar.winfo_reqwidth()
        now = max(root.winfo_width(), window_size["width"])
        if needed > now:
            window_size["width"] = min(needed, root.winfo_screenwidth() - px(40))
            root.geometry(f"{window_size['width']}x{max(root.winfo_height(), window_size['height'])}")

    def color_tabs():
        highlighted_tabs = {item["tab"] for item in highlighted.values()}
        for label_name, label in tab_labels.items():
            selected = label_name == current_tab["name"]
            label.config(background=dark_theme.SELECTED if selected else dark_theme.PANEL,
                         foreground=dark_theme.TEXT if selected else
                         dark_theme.ACCENT if label_name in highlighted_tabs else dark_theme.MUTED)

    def highlight_settings(keys):
        """Outlines the settings `keys` name and badges them "New", opens the
        first one's tab and colors the others' in the sidebar. Keys it doesn't
        know are ignored."""
        found = [key for key in keys if key in highlights]
        if not found:
            return
        for key in found:
            if key in highlighted:
                continue  # asked again while still outlined
            widget, tab = highlights[key]
            outline = {option: widget.cget(option)
                       for option in ("highlightthickness", "highlightbackground", "highlightcolor")}
            badge = new_badge.add_badge(widget, px, lambda k=key: pop_highlight(k))
            highlighted[key] = {"tab": tab, "outline": outline, "badge": badge}
            widget.config(highlightthickness=px(2), highlightbackground=dark_theme.ACCENT,
                          highlightcolor=dark_theme.ACCENT)
        show_tab(highlights[found[0]][1])

    def pop_highlight(key):
        """A "New" badge clicked: confetti, and that setting is plain again."""
        item = highlighted.pop(key)
        badge = item["badge"]
        x = badge.winfo_rootx() - root.winfo_rootx() + badge.winfo_width() // 2
        y = badge.winfo_rooty() - root.winfo_rooty() + badge.winfo_height() // 2
        badge.destroy()
        highlights[key][0].config(**item["outline"])
        color_tabs()
        new_badge.burst(root, x, y, px)

    def style_hover(label, entering):
        if label_name_of(label) != current_tab["name"]:
            label.config(background=dark_theme.HOVER if entering else dark_theme.PANEL)

    def label_name_of(label):
        return label.cget("text").strip()

    for name in TABS:
        label = tk.Label(sidebar, text=name, anchor="w", justify="left", cursor="hand2",
                         padx=px(12), pady=px(8), wraplength=px(170), background=dark_theme.PANEL)
        label.config(text=name)
        label.pack(fill="x", pady=px(2))
        label.bind("<Button-1>", lambda _e, n=name: show_tab(n))
        label.bind("<Enter>", lambda _e, w=label: style_hover(w, True))
        label.bind("<Leave>", lambda _e, w=label: style_hover(w, False))
        tab_labels[name] = label

    # Opens 16:9, wide enough for the widest card with the sidebar beside it,
    # and taller if a tab other than All needs it (fonts differ by platform);
    # All scrolls. Never bigger than the screen.
    tallest = 0
    for name in TABS:
        if name != "All":
            show_tab(name)
            root.update_idletasks()
            tallest = max(tallest, page.winfo_reqheight())
    show_tab("All")
    root.update_idletasks()
    min_width = sidebar.winfo_reqwidth() + page.winfo_reqwidth() + scrollbar.winfo_reqwidth()
    screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
    width = max(px(1100), min_width)
    # Below the page's canvas: the footer and the line above it.
    height = max(width * 9 // 16, tallest + footer.winfo_reqheight() + footer_line.winfo_reqheight())
    root.highlight_settings = highlight_settings
    root.layout = {"show_tab": show_tab, "tabs": TABS, "canvas": canvas, "page": page, "highlights": highlights,
                   "highlighted": highlighted, "size": (width, height)}  # for test_settings_dialog's overflow check
    width = min(width, screen_width - px(40))
    height = min(height, screen_height - px(80))
    window_size.update(width=width, height=height)
    root.geometry(f"{width}x{height}")
    root.minsize(min(min_width, screen_width), min(footer.winfo_reqheight() + px(160), height))
    show_tab(start_tab if start_tab in TABS else TAB_BASIC)
    highlight_settings(highlight)

    guard_combobox_wheel(page)
    # Opened from the tray menu - make sure it isn't hidden behind whatever
    # window had focus, now and whenever the menu item is clicked again.
    bring_to_front(root)
    watch_for_reopen(root)
    root.mainloop()
    return reopen_on["tab"]


if __name__ == "__main__":
    main()
