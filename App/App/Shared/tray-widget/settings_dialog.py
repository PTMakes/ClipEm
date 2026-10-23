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
from tkinter import messagebox, simpledialog, ttk

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
# autostart entry (autostart.apply_boot_autostart).
sys.path.insert(0, INSTALLER_DIR)
import bug_report  # noqa: E402
import color_picker  # noqa: E402
import dark_theme  # noqa: E402
import editor_install  # noqa: E402
import file_dialogs  # noqa: E402
import mic_filter  # noqa: E402
import obs_config  # noqa: E402
import obs_process  # noqa: E402
import reinstall  # noqa: E402
import setup  # noqa: E402 - Setup's own load/save for the settings it shares with this dialog
from app_window import style_app_window, use_windows_app_id  # noqa: E402
from autostart import (  # noqa: E402
    apply_boot_autostart,
    describe_autostart_entry,
    find_autostart_entries,
    remove_autostart_entries,
)
from clipem_common import (  # noqa: E402
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
RAISE_POLL_MS = 300
# A wheel turn over a combobox this soon after scrolling the page scrolls
# the page on, instead of changing the combobox.
COMBOBOX_WHEEL_BUFFER_S = 0.6

try:
    import updater
except ImportError:
    updater = None


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
                os.remove(RAISE_PATH)
            except OSError:
                pass
            bring_to_front(root)
        root.after(RAISE_POLL_MS, check)

    root.after(RAISE_POLL_MS, check)


def parent_pid_from_args(argv):
    """The tray widget passes --parent-pid <its pid> when it opens this."""
    if "--parent-pid" in argv:
        index = argv.index("--parent-pid")
        if index + 1 < len(argv) and argv[index + 1].isdigit():
            return int(argv[index + 1])
    return None


def main():
    # Close along with the tray widget that opened this, however it ends.
    exit_with_parent(parent_pid_from_args(sys.argv[1:]))
    if not acquire_instance_lock(LOCK_PATH):
        log.info("Settings dialog already open; asking it to come to the front.")
        try:
            write_text_atomic(RAISE_PATH, "raise")
        except OSError as exc:
            log.warning(f"Couldn't ask the open Settings dialog to come to the front: {exc}")
        return
    try:
        os.remove(RAISE_PATH)  # left from a dialog that has since closed
    except OSError:
        pass
    # Saving new colors (or Dark Mode) reopens the window, built with them.
    tab = None
    while True:
        tab = open_dialog(tab)
        if tab is None:
            break


def open_dialog(start_tab=None):
    """Builds and runs the Settings window, on start_tab (Basic Settings by
    default). Returns the tab to reopen it on when its Save changed the
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
        hotkey = tuple(pending.get("clip_hotkey") or obs_config.load_clip_hotkey())
    except Exception:
        hotkey = obs_config.DEFAULT_CLIP_HOTKEY
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
    initial_obs_settings = {
        "websocket": {"port": port, "password": password},
        "clip_hotkey": list(hotkey),
        "clip_sound": {"enabled": bool(clip_sound["enabled"]), "volume": round(float(clip_sound["volume"]), 2)},
        "clip_folder": clip_folder,
        "split_tracks": split_tracks,
        "clip_reexport": reexport,
    }

    run_config = load_run_config()

    enable_dpi_awareness()
    use_windows_app_id("PTMakes.ClipEm.Settings")
    # className: what Linux desktops match the window to (WM_CLASS).
    root = tk.Tk(className="ClipEm")
    # Hidden until it's built and sized (shown at the end of main) - otherwise
    # it sits on screen as Tk's blank default-size window meanwhile.
    root.withdraw()
    dark_theme.apply_theme(root)  # before any widget: classic ones take their colors at creation
    # ClipEm logo, and Ctrl+A / Ctrl+Z / Ctrl+Shift+Z or R in the text fields.
    style_app_window(root, log=log.warning)
    # Pixel sizes below are for a 96 DPI display - see ui_scale.py.
    px = UiScale(root).px
    root.px = px  # MicFilterWizard sizes itself with its parent's px
    root.title("ClipEm Settings")

    # Status line and Save/Cancel sit in a footer packed first, so resizing
    # the window shrinks the scrolling settings area above it, never them.
    footer = tk.Frame(root, padx=px(16), pady=px(10))
    footer.pack(side="bottom", fill="x")
    ttk.Separator(root, orient="horizontal").pack(side="bottom", fill="x")
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
    TAB_BASIC, TAB_OUTPUT, TAB_EDITOR = "Basic Settings", "Clip Output/Devices", "Clip Editor"
    TAB_TRAY, TAB_SCRIPTS, TAB_WEBSOCKET = "UI Customization", "Scripts/Troubleshooting", "OBS Websocket"
    TABS = ("All", TAB_BASIC, TAB_OUTPUT, TAB_EDITOR, TAB_TRAY, TAB_WEBSOCKET, TAB_SCRIPTS)
    cards = []  # (card, its tab), in page order
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

    # Clip Hotkey is now the first section - no gap above it.
    section("Clip Hotkey", TAB_BASIC)
    hotkey_ctrl_var = tk.BooleanVar(value=hotkey[0])
    hotkey_alt_var = tk.BooleanVar(value=hotkey[1])
    hotkey_shift_var = tk.BooleanVar(value=hotkey[2])
    hotkey_key_var = tk.StringVar(value=hotkey[3])
    hotkey_row = tk.Frame(frame)
    hotkey_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=px(4))
    for text, var in (("Ctrl", hotkey_ctrl_var), ("Alt", hotkey_alt_var), ("Shift", hotkey_shift_var)):
        tk.Checkbutton(hotkey_row, text=text, variable=var).pack(side="left")
    tk.Label(hotkey_row, text="+ Key:").pack(side="left", padx=(px(6), 0))
    ttk.Combobox(
        hotkey_row, textvariable=hotkey_key_var, values=obs_config.HOTKEY_KEY_CHOICES,
        state="readonly", width=5,
    ).pack(side="left", padx=(px(4), 0))

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
            messagebox.showerror("Clip Sound", f"Couldn't play the sound: {exc}", parent=root)

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

    split_tracks_var = tk.BooleanVar(value=split_tracks)
    tk.Checkbutton(
        frame, text="Multitrack audio: also save a copy (\"-tracks.mkv\") with the OBS tracks,\n"
                    "Mix, Mic, and Desktop, and apps each on their own track, for editing",
        variable=split_tracks_var, justify="left",
    ).grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1

    # The "-small.mp4" copy (Scripts/clip_reexport.py) - as on Setup's
    # "Lower-Quality Re-export" page.
    reexport_var = tk.BooleanVar(value=reexport["enabled"])
    reexport_check = tk.Checkbutton(
        frame, text="Also save a smaller copy of each clip (\"-small.mp4\"), for sites\n"
                    "with a file size limit - the full-quality clip is always kept",
        variable=reexport_var, justify="left", command=lambda: on_reexport_toggle(),
    )
    reexport_check.grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1
    # Its options sit under its text, not its box - see indent_under_text().
    reexport_box = tk.Frame(frame)
    reexport_box.grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1
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

    section("Recording", TAB_OUTPUT)

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

    section("Audio Devices", TAB_OUTPUT)
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
                messagebox.showerror("Mic Filter", f"Couldn't set up microphone calibration: {exc or 'install failed'}",
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

    def on_devices(result, exc):
        result = result if exc is None else {}
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
    run_in_background(root, lambda: {"displays": setup.list_displays(), "audio": setup.list_audio_devices()},
                      on_devices)

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

    section("Appearance", TAB_TRAY)
    saved_dark, saved_colors = dark_theme.theme_from_config(run_config)
    dark_mode_var = tk.BooleanVar(value=saved_dark)
    tk.Checkbutton(
        frame, text="Use Dark Mode", variable=dark_mode_var, command=lambda: show_colors(),
    ).grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1

    # Each mode keeps its own colors; the rows show the one Use Dark Mode is
    # set to. chosen_colors[dark][key] is what each row shows, by
    # dark_theme.COLOR_SETTINGS key.
    def colors_by_key(dark, custom=None):
        palette = dark_theme.colors_for(dark, custom)
        return {key: palette[name] for key, _label, name, _css in dark_theme.COLOR_SETTINGS}

    default_colors = {dark: colors_by_key(dark) for dark in (True, False)}
    chosen_colors = {dark: colors_by_key(dark, saved_colors) for dark in (True, False)}

    def custom_colors():
        """run_config's "custom_colors" for what the rows show: only the
        colors that differ from the defaults."""
        custom = {}
        for dark in (True, False):
            changed = {key: value for key, value in chosen_colors[dark].items()
                       if value != default_colors[dark][key]}
            if changed:
                custom[dark_theme.mode_key(dark)] = changed
        return custom

    initial_appearance = (saved_dark, custom_colors())
    colors_label = tk.Label(frame, fg=dark_theme.MUTED, justify="left", wraplength=px(560))
    colors_label.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
    next_row += 1
    colors_grid = tk.Frame(frame)
    colors_grid.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
    next_row += 1
    color_vars, color_swatches = {}, {}
    per_column = (len(dark_theme.COLOR_SETTINGS) + 1) // 2

    def shown_mode():
        return bool(dark_mode_var.get())

    def show_color(key):
        color = chosen_colors[shown_mode()][key]
        color_swatches[key].configure(background=color)
        if color_vars[key].get() != color:
            color_vars[key].set(color)

    def show_colors():
        colors_label.config(text=f"{'Dark' if shown_mode() else 'Light'} Mode colors - click a square to pick "
                                 "one, or type its hex code. Save applies them everywhere.")
        for key in color_vars:
            show_color(key)

    def on_hex_typed(key):
        color = color_picker.normalize_hex(color_vars[key].get())
        if color:
            chosen_colors[shown_mode()][key] = color
            color_swatches[key].configure(background=color)

    def pick_color(key, label):
        color = color_picker.ask_color(root, chosen_colors[shown_mode()][key], f"{label} - ClipEm", px)
        if color:
            chosen_colors[shown_mode()][key] = color
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
        chosen_colors[shown_mode()] = dict(default_colors[shown_mode()])
        show_colors()

    tk.Button(frame, text="Reset to Defaults", command=reset_colors).grid(
        row=next_row, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
    next_row += 1
    show_colors()

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
    tk.Checkbutton(frame, text="Enable stuck-screen detection", variable=stuck_var).grid(
        row=next_row, column=0, columnspan=2, sticky="w"
    )
    next_row += 1
    black_var = tk.BooleanVar(value=black_enabled)
    tk.Checkbutton(frame, text="Enable black-screen detection", variable=black_var).grid(
        row=next_row, column=0, columnspan=2, sticky="w"
    )
    next_row += 1
    disk_warnings_var = tk.BooleanVar(value=disk_warnings_enabled)
    tk.Checkbutton(frame, text="Enable disk space warnings", variable=disk_warnings_var).grid(
        row=next_row, column=0, columnspan=2, sticky="w"
    )
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
            messagebox.showerror("Install Autostart", f"Couldn't install autostart: {exc}", parent=root)

        def check_installed(entries):
            if not any(entry["this_install"] for entry in entries):
                messagebox.showerror(
                    "Install Autostart", "Couldn't create the autostart entry - your system may not support it.",
                    parent=root,
                )

        show_autostart_status(then=check_installed)

    def on_uninstall_autostart():
        try:
            save_autostart_flags(False)
            remove_autostart_entries([entry for entry in find_autostart_entries() if entry["this_install"]])
        except Exception as exc:
            messagebox.showerror("Uninstall Autostart", f"Couldn't uninstall autostart: {exc}", parent=root)
        show_autostart_status()

    def on_uninstall_other_autostarts():
        try:
            others = [entry for entry in find_autostart_entries() if not entry["this_install"]]
        except Exception as exc:
            messagebox.showerror("Uninstall Autostart", f"Couldn't look up the autostart entries: {exc}", parent=root)
            return
        if others and messagebox.askyesno(
            "Uninstall Autostart",
            "These ClipEm autostart entries start another ClipEm folder (or an older version):\n\n"
            + "\n".join(f"  \u2022 {describe_autostart_entry(entry)}" for entry in others)
            + "\n\nRemove them?",
            parent=root,
        ):
            try:
                remove_autostart_entries(others)
            except Exception as exc:
                messagebox.showerror("Uninstall Autostart", f"Couldn't remove them: {exc}", parent=root)
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
                messagebox.showerror("Updates", f"Couldn't save the update setting:\n{exc}")
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
                messagebox.showerror("Updates", f"Couldn't save the update setting:\n{exc}")
                return
            source_repo_var.set(updater.repo_url(repo))

        def on_change_source_repo():
            if not messagebox.askyesno(
                "Source Repo",
                "ClipEm downloads its updates from the releases of this GitHub repository.\n\n"
                "Only change it if you know the repository publishes ClipEm builds - updates must "
                "still be signed with the official ClipEm release key, so any other repository "
                "makes update checks fail.\n\nChange the source repo?",
                parent=root,
            ):
                return
            text = simpledialog.askstring(
                "Source Repo", "GitHub repository link (https://github.com/owner/name):",
                initialvalue=source_repo_var.get(), parent=root,
            )
            if text is None:
                return
            repo = updater.repo_from_url(text)
            if repo is None:
                messagebox.showerror(
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
            if not messagebox.askyesno("Update Branch", explanation, parent=root):
                branch_var.set(saved_branch["label"])
                return
            try:
                updater.change_update_config(update_channel=channel, skipped_version=None)
            except Exception as exc:
                messagebox.showerror("Updates", f"Couldn't save the update setting:\n{exc}")
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
                messagebox.showerror(
                    "Check for Updates",
                    f"Couldn't check {source_repo_var.get()} for updates:\n{result['error']}",
                )
                return
            current, latest = result["local_version"], result["remote_version"]
            if not result["update_available"]:
                messagebox.showinfo(
                    "Check for Updates",
                    f"Current version: {current}\nLatest version: {latest}\n\nClipEm is up to date.",
                )
                return
            changes = "\n".join(f"  \u2022 {line}" for line in updater.describe_update(result))
            if messagebox.askyesno(
                "Check for Updates",
                f"An update is available:\n\n{changes}\n\nInstall it now? This closes OBS and the "
                "tray icon, updates ClipEm and starts them again.\n\n(Choosing No offers it again the "
                "next time ClipEm starts.)",
            ):
                try:
                    updater.change_update_config(install_on_next_launch=True)
                except Exception as exc:
                    messagebox.showerror("Check for Updates", f"Couldn't save the update choice:\n{exc}")
                    return
                check_button.config(state="disabled", text="Restarting...")
                restart_clipem_in_background(root, on_clipem_restarted)

        def on_clipem_restarted(error_message, exc):
            if exc is not None:
                log.warning(f"Restart ClipEm failed: {exc}")
                error_message = (f"Couldn't restart ClipEm ({exc}). The update will be installed the "
                                 "next time ClipEm starts.")
            if error_message:
                messagebox.showwarning("Check for Updates", error_message)
            root.destroy()

        check_button = tk.Button(frame, text="Check for Updates", command=on_check_for_updates)
        check_button.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        next_row += 1

    # --- Troubleshooting: a bug report (sent, or saved as a zip), and a clean reinstall.
    section("Troubleshooting", TAB_SCRIPTS)

    def on_bug_report():
        dialog = tk.Toplevel(root)
        dialog.title("Send Bug Report")
        dialog.resizable(False, False)
        dialog.transient(root)
        body = tk.Frame(dialog, padx=px(16), pady=px(12))
        body.pack()
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
                messagebox.showwarning("Send Bug Report", "Describe what happened first.", parent=dialog)
                return
            set_sending(True)
            run_in_background(root, lambda: bug_report.send_bug_report(description), on_sent)

        def on_sent(result, error):
            if error is not None:
                log.warning(f"Sending the bug report failed: {error}")
                if not dialog.winfo_exists():
                    messagebox.showerror("Send Bug Report", f"Couldn't send the bug report:\n{error}", parent=root)
                    return
                set_sending(False)
                if messagebox.askyesno(
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
            if issue_url and messagebox.askyesno(
                "Send Bug Report",
                f"Thanks - the bug report was sent (report ID {report_id}).\n\nOpen the GitHub issue in your browser?",
                parent=root,
            ):
                webbrowser.open(issue_url)
            elif not issue_url:
                messagebox.showinfo("Send Bug Report", f"Thanks - the bug report was sent (report ID {report_id}).",
                                    parent=root)

        dialog_buttons = tk.Frame(body)
        dialog_buttons.pack(anchor="e", pady=(px(12), 0))
        tk.Button(dialog_buttons, text="Cancel", command=dialog.destroy, width=10).pack(side="left", padx=px(4))
        tk.Button(dialog_buttons, text="Save to File Instead...", command=on_save_instead).pack(
            side="left", padx=px(4))
        send_button = tk.Button(dialog_buttons, text="Send", command=on_send, width=10)
        send_button.pack(side="left", padx=px(4))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
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
            messagebox.showerror("Bug Report", f"Couldn't create the bug report:\n{error}", parent=root)
            return
        log.info(f"Bug report saved: {path} ({count} log files)")
        if messagebox.askyesno(
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
                messagebox.showerror("Bug Report", f"Couldn't open the folder: {exc}", parent=root)

    def on_reinstall():
        dialog = tk.Toplevel(root)
        dialog.title("Reinstall ClipEm")
        dialog.resizable(False, False)
        dialog.transient(root)
        body = tk.Frame(dialog, padx=px(16), pady=px(12))
        body.pack()
        tk.Label(
            body, justify="left", wraplength=px(380),
            text=(
                "Downloads ClipEm, OBS and the tray widget's packages again and replaces the "
                "installed copies - for when something is broken or missing. Your clips aren't "
                "touched.\n\nThis closes OBS and the tray icon - clips not saved yet are lost."
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
                messagebox.showerror("Reinstall ClipEm", f"Couldn't start the reinstall: {exc}", parent=dialog)
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
        dialog_buttons.pack(anchor="e", pady=(px(12), 0))
        tk.Button(dialog_buttons, text="Cancel", command=dialog.destroy, width=10).pack(side="left", padx=px(4))
        tk.Button(dialog_buttons, text="Reinstall", command=on_confirm, width=10).pack(side="left", padx=px(4))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()

    def on_reinstall_restarted(error_message, exc):
        if exc is not None:
            log.warning(f"Restart ClipEm for the reinstall failed: {exc}")
            error_message = f"Couldn't restart ClipEm ({exc}). {REINSTALL_PENDING_MESSAGE}"
        if error_message:
            messagebox.showwarning("Reinstall ClipEm", error_message)
        root.destroy()

    troubleshooting_row = tk.Frame(frame)
    troubleshooting_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
    bug_button = tk.Button(troubleshooting_row, text="Send Bug Report...", command=on_bug_report)
    bug_button.pack(side="left")
    reinstall_button = tk.Button(troubleshooting_row, text="Reinstall ClipEm...", command=on_reinstall)
    reinstall_button.pack(side="left", padx=(px(4), 0))
    next_row += 1

    def on_save():
        if (not editors_off_var.get() and editor_kind_var.get() == editor_install.EXTERNAL
                and not editor_path_var.get().strip()):
            messagebox.showerror("Clip Editor", "Choose your external editor, or pick ClipEm's editor.", parent=root)
            return
        try:
            port_val = obs_config.validate_port(port_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Port", "Port must be a number from 1 to 65535.")
            return
        password_val = password_var.get()
        if not password_val:
            messagebox.showerror("Invalid Password", "Password can't be empty.")
            return
        hotkey_val = [hotkey_ctrl_var.get(), hotkey_alt_var.get(), hotkey_shift_var.get(), hotkey_key_var.get()]
        try:
            obs_config.hotkey_label_to_obs_key(hotkey_val[3])
        except ValueError:
            messagebox.showerror("Invalid Hotkey", "Pick a key for the clip hotkey.")
            return
        obs_settings = {
            "websocket": {"port": port_val, "password": password_val},
            "clip_hotkey": hotkey_val,
            "clip_sound": {"enabled": bool(sound_enabled_var.get()), "volume": round(sound_volume_var.get() / 100, 2)},
            "clip_folder": clip_folder_var.get(),
            "split_tracks": bool(split_tracks_var.get()),
            "clip_reexport": dict(initial_obs_settings["clip_reexport"], enabled=bool(reexport_var.get())),
        }
        try:
            buffer_seconds = int(float(buffer_var.get()))
            recording_bitrate = int(float(recording_bitrate_var.get()))
        except ValueError:
            messagebox.showerror("Recording", "Enter numbers for the replay buffer and bitrate.", parent=root)
            return
        buffer_seconds = max(setup.REPLAY_BUFFER_MIN_SECONDS, min(buffer_seconds, setup.REPLAY_BUFFER_MAX_SECONDS))
        recording_bitrate = max(setup.BITRATE_MIN_KBPS, min(recording_bitrate, setup.BITRATE_MAX_KBPS))
        if buffer_seconds != recording["replay_buffer_seconds"]:
            obs_settings["replay_buffer_seconds"] = buffer_seconds
        if recording_bitrate != recording["bitrate"]:
            obs_settings["recording_bitrate"] = recording_bitrate
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

        values = reexport_values()
        if reexport_var.get() and (not values or values[0] <= 0):
            messagebox.showerror("Smaller Copy", "Enter a target size (MB, more than 0) and how many "
                                 "seconds to keep.", parent=root)
            return
        if values and values[0] > 0:
            # Never longer than the (possibly just changed) replay buffer.
            obs_settings["clip_reexport"].update(target_mb=values[0], crop_seconds=min(values[1], buffer_seconds))
        if obs_settings["clip_folder"] != initial_obs_settings["clip_folder"]:
            # OBS won't start the replay buffer into a folder that isn't there.
            try:
                os.makedirs(obs_settings["clip_folder"], exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Clip Folder", f"Couldn't create the clip folder: {exc}", parent=root)
                return
        if chosen_sound["path"]:
            # A plain file OBS's script reads on every clip - takes effect
            # straight away, OBS running or not.
            try:
                obs_config.replace_clip_sound_wav(chosen_sound["path"])
            except Exception as exc:
                messagebox.showerror("Clip Sound", f"Couldn't use that sound: {exc}", parent=root)
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
            run_config["custom_editor_path"] = editor_path_var.get().strip()
            run_config["clip_editor"] = editor_kind_var.get()
            run_config["clip_editors_disabled"] = bool(editors_off_var.get())
            for key, var in menu_item_vars.items():  # read by the tray menu
                run_config[key] = bool(var.get())
            appearance = (bool(dark_mode_var.get()), custom_colors())
            run_config["dark_mode"], run_config["custom_colors"] = appearance
            if not run_config["custom_colors"]:
                del run_config["custom_colors"]
            run_config["clip_saved_notification"] = bool(clip_notification_var.get())
            save_run_config(run_config)
        except Exception as exc:
            messagebox.showerror("Save Failed", f"Couldn't save screen-detection and disk-space settings: {exc}")
            return
        # The editor and the tray menu pick the colors up themselves (they
        # poll run_config); this window is rebuilt with them once it closes.
        if appearance != initial_appearance:
            reopen_on["tab"] = current_tab["name"]

        try:
            outcome = save_obs_settings(obs_settings, initial_obs_settings)
        except Exception as exc:
            messagebox.showerror("Save Failed", f"Couldn't save settings: {exc}")
            return
        if outcome == "unchanged":
            root.destroy()
            return
        if outcome == "applied":
            messagebox.showinfo("Saved", "Settings saved.")
            root.destroy()
            return

        if not messagebox.askyesno(
            "Restart OBS?",
            "OBS only picks up websocket, hotkey, clip sound, clip folder and multitrack "
            "audio settings when it starts. Restart OBS now to apply them?\n\n"
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
                messagebox.showwarning("Restart OBS", error_message)
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

    def show_tab(name):
        current_tab["name"] = name
        for card, _tab in cards:
            card.pack_forget()
        for card, tab in cards:
            if name == "All" or tab == name:
                card.pack(fill="x", pady=(0, px(10)))
        for label_name, label in tab_labels.items():
            selected = label_name == name
            label.config(background=dark_theme.SELECTED if selected else dark_theme.PANEL,
                         foreground=dark_theme.TEXT if selected else dark_theme.MUTED)
        canvas.yview_moveto(0)

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

    # Opens 16:9, wide enough for the widest card with the sidebar beside it;
    # the page scrolls when a tab is taller. Never bigger than the screen.
    show_tab("All")
    root.update_idletasks()
    min_width = sidebar.winfo_reqwidth() + page.winfo_reqwidth() + scrollbar.winfo_reqwidth()
    screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
    width = min(max(px(1100), min_width), screen_width - px(40))
    height = min(width * 9 // 16, screen_height - px(80))
    root.geometry(f"{width}x{height}")
    root.minsize(min(min_width, screen_width), min(footer.winfo_reqheight() + px(160), height))
    show_tab(start_tab if start_tab in TABS else TAB_BASIC)

    guard_combobox_wheel(page)
    # Opened from the tray menu - make sure it isn't hidden behind whatever
    # window had focus, now and whenever the menu item is clicked again.
    bring_to_front(root)
    watch_for_reopen(root)
    root.mainloop()
    return reopen_on["tab"]


if __name__ == "__main__":
    main()
