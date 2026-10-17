"""Small settings dialog for the ClipEm tray widget.

Launched as a separate process (see tray_widget.py's "Settings..." menu item)
rather than run in-process, since mixing tkinter's own event loop with
pystray's would be fragile. Edits the same on-disk config files setup.py's
wizard does.

OBS reads the websocket, hotkey and clip sound settings only at startup
(the clip sound's .wav file itself is read on every clip), so while
OBS is running those are queued (obs_config.queue_obs_settings) rather than written, and the
dialog offers to restart OBS to apply them; otherwise run.py applies them
before it next starts OBS.
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.dirname(SCRIPT_DIR)
INSTALLER_DIR = os.path.join(SHARED_DIR, "Installer")

# Shared with setup.py/run.py rather than re-implemented here: config-file
# paths and atomic writes (clipem_common/obs_config) and the per-OS boot
# autostart entry (autostart.apply_boot_autostart).
sys.path.insert(0, INSTALLER_DIR)
import bug_report  # noqa: E402
import obs_config  # noqa: E402
import obs_process  # noqa: E402
import reinstall  # noqa: E402
from app_window import style_app_window, use_windows_app_id  # noqa: E402
from autostart import (  # noqa: E402
    apply_boot_autostart,
    describe_autostart_entry,
    find_autostart_entries,
    remove_autostart_entries,
)
from clipem_common import (  # noqa: E402
    acquire_instance_lock,
    exit_with_parent,
    load_run_config,
    outlive_parent,
    save_run_config,
    setup_logging,
    start_clipem,
    stop_tray_widget,
)
from ui_scale import UiScale, enable_dpi_awareness  # noqa: E402

log = setup_logging("settings")
RUN_PY = os.path.join(INSTALLER_DIR, "run.py")
LOCK_PATH = os.path.join(SCRIPT_DIR, ".settings.lock")

try:
    import updater
except ImportError:
    updater = None


def start_run_py():
    """Starts ClipEm again (run.py, detached) - see clipem_common.start_clipem."""
    start_clipem()


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
    start_run_py()
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
    stop_tray_widget(log)
    start_run_py()
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
        log.info("Settings dialog already open; not opening another.")
        return
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
    initial_obs_settings = {
        "websocket": {"port": port, "password": password},
        "clip_hotkey": list(hotkey),
        "clip_sound": {"enabled": bool(clip_sound["enabled"]), "volume": round(float(clip_sound["volume"]), 2)},
    }

    run_config = load_run_config()

    enable_dpi_awareness()
    use_windows_app_id("PTMakes.ClipEm.Settings")
    # className: what Linux desktops match the window to (WM_CLASS).
    root = tk.Tk(className="ClipEm")
    # ClipEm logo, and Ctrl+A / Ctrl+Z / Ctrl+Shift+Z or R in the text fields.
    style_app_window(root, log=log.warning)
    # Pixel sizes below are for a 96 DPI display - see ui_scale.py.
    px = UiScale(root).px
    root.title("ClipEm Settings")
    # Opened from the tray menu - make sure it isn't hidden behind whatever
    # window had focus.
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))

    # Status line and Save/Cancel sit in a footer packed first, so resizing
    # the window shrinks the scrolling settings area above it, never them.
    footer = tk.Frame(root, padx=px(16), pady=px(10))
    footer.pack(side="bottom", fill="x")
    ttk.Separator(root, orient="horizontal").pack(side="bottom", fill="x")
    scroll_area = tk.Frame(root)
    scroll_area.pack(side="top", fill="both", expand=True)
    canvas = tk.Canvas(scroll_area, highlightthickness=0, borderwidth=0)
    scrollbar = ttk.Scrollbar(scroll_area, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    frame = tk.Frame(canvas, padx=px(16), pady=px(12))
    frame_window = canvas.create_window(0, 0, window=frame, anchor="nw")
    frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind(
        "<Configure>",
        lambda event: canvas.itemconfigure(frame_window, width=max(event.width, frame.winfo_reqwidth())),
    )

    def on_mouse_wheel(event):
        # bind_all also fires for other windows (dialogs) and for comboboxes,
        # which use the wheel to change their value - only scroll for the
        # settings area itself.
        if not str(event.widget).startswith(str(canvas)) or isinstance(event.widget, ttk.Combobox):
            return
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        elif sys.platform == "darwin":
            step = -event.delta
        else:
            step = -event.delta // 120
        canvas.yview_scroll(step, "units")

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(sequence, on_mouse_wheel, add="+")
    # Rows spanning both columns (checkboxes, update text) are wider than
    # Port/Password; give that extra width to the field column so the labels
    # and fields stay flush left instead of being spread apart.
    frame.columnconfigure(1, weight=1)

    # Clip Hotkey is now the first section - no gap above it.
    tk.Label(frame, text="Clip Hotkey", font=("", 10, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w"
    )
    hotkey_ctrl_var = tk.BooleanVar(value=hotkey[0])
    hotkey_alt_var = tk.BooleanVar(value=hotkey[1])
    hotkey_shift_var = tk.BooleanVar(value=hotkey[2])
    hotkey_key_var = tk.StringVar(value=hotkey[3])
    hotkey_row = tk.Frame(frame)
    hotkey_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=px(4))
    for text, var in (("Ctrl", hotkey_ctrl_var), ("Alt", hotkey_alt_var), ("Shift", hotkey_shift_var)):
        tk.Checkbutton(hotkey_row, text=text, variable=var).pack(side="left")
    tk.Label(hotkey_row, text="+ Key:").pack(side="left", padx=(px(6), 0))
    ttk.Combobox(
        hotkey_row, textvariable=hotkey_key_var, values=obs_config.HOTKEY_KEY_CHOICES,
        state="readonly", width=5,
    ).pack(side="left", padx=(px(4), 0))

    # --- Clip sound: on/off, volume and which .wav plays on each saved clip.
    next_row = 2
    tk.Label(frame, text="Clip Sound", font=("", 10, "bold")).grid(
        row=next_row, column=0, columnspan=2, sticky="w", pady=(px(14), 0)
    )
    next_row += 1
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
    volume_scale = tk.Scale(
        volume_row, from_=0, to=100, orient="horizontal", variable=sound_volume_var,
        showvalue=False, length=px(150),
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
    sound_name_var = tk.StringVar(value="Current sound")
    tk.Label(sound_row, textvariable=sound_name_var, width=16, anchor="w").pack(side="left")

    def on_choose_sound():
        path = filedialog.askopenfilename(
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

    on_sound_toggle()

    # --- The clip editor's "Open in..." button: any editor the user has.
    tk.Label(frame, text="Clip Editor", font=("", 10, "bold")).grid(
        row=next_row, column=0, columnspan=2, sticky="w", pady=(px(14), 0)
    )
    next_row += 1
    tk.Label(frame, text="External editor:").grid(row=next_row, column=0, sticky="w")
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
            path = filedialog.askdirectory(parent=root, title="Choose your video editor (.app)",
                                           initialdir="/Applications")
        else:
            filetypes = [("Programs", "*.exe")] if sys.platform == "win32" else []
            path = filedialog.askopenfilename(
                parent=root, title="Choose your video editor", filetypes=filetypes + [("All files", "*")],
            )
        if path:
            editor_path_var.set(path)

    tk.Button(editor_row, text="Choose...", command=on_choose_editor).pack(side="left", padx=(px(4), 0))
    tk.Button(editor_row, text="Clear", command=lambda: editor_path_var.set("")).pack(
        side="left", padx=(px(4), 0)
    )
    next_row += 1

    tk.Label(frame, text="Scripts", font=("", 10, "bold")).grid(
        row=next_row, column=0, columnspan=2, sticky="w", pady=(px(14), 0)
    )
    next_row += 1
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

    # One choice for both: the tray widget is required, so it always starts
    # at boot alongside OBS.
    autostart_var = tk.BooleanVar(value=run_config["autostart_obs"])
    tk.Label(frame, text="Autostart with PC boot", font=("", 10, "bold")).grid(
        row=next_row, column=0, columnspan=2, sticky="w", pady=(px(14), 0)
    )
    next_row += 1

    tk.Checkbutton(
        frame, text="Start ClipEm (OBS and the tray icon) when the PC boots", variable=autostart_var
    ).grid(row=next_row, column=0, columnspan=2, sticky="w")
    next_row += 1

    # Install/Uninstall act straight away (no Save needed) on the OS's
    # autostart entry itself - including ones left by another ClipEm folder.
    # Only the button that applies is shown: Install when this folder has
    # no entry, Uninstall when there's one to remove (both only when the
    # only entries start another ClipEm folder).
    autostart_status_var = tk.StringVar()
    autostart_row = tk.Frame(frame)
    install_button = tk.Button(autostart_row, text="Install Autostart")
    uninstall_button = tk.Button(autostart_row, text="Uninstall Autostart")

    def show_autostart_buttons(show_install, show_uninstall):
        install_button.pack_forget()
        uninstall_button.pack_forget()
        if show_install:
            install_button.pack(side="left", padx=(0, px(4)))
        if show_uninstall:
            uninstall_button.pack(side="left", padx=(0, px(4)))

    def show_autostart_status():
        try:
            entries = find_autostart_entries()
        except Exception as exc:
            autostart_status_var.set(f"Autostart entry: unknown ({exc})")
            show_autostart_buttons(True, True)
            return
        others = sum(1 for entry in entries if not entry["this_install"])
        ours = len(entries) > others
        text = "installed" if ours else "not installed"
        if others:
            text += f" (+{others} from another ClipEm folder)"
        autostart_status_var.set(f"Autostart entry: {text}")
        show_autostart_buttons(not ours, bool(entries))

    def save_autostart_flags():
        config = load_run_config()
        config["autostart_obs"] = config["autostart_tray"] = bool(autostart_var.get())
        save_run_config(config)

    def on_install_autostart():
        autostart_var.set(True)
        try:
            save_autostart_flags()
            apply_boot_autostart(True)
        except Exception as exc:
            messagebox.showerror("Install Autostart", f"Couldn't install autostart: {exc}", parent=root)
        show_autostart_status()
        if not any(entry["this_install"] for entry in find_autostart_entries()):
            messagebox.showerror(
                "Install Autostart", "Couldn't create the autostart entry - your system may not support it.",
                parent=root,
            )

    def on_uninstall_autostart():
        entries = find_autostart_entries()
        to_remove = [entry for entry in entries if entry["this_install"]]
        others = [entry for entry in entries if not entry["this_install"]]
        if others and messagebox.askyesno(
            "Uninstall Autostart",
            "These ClipEm autostart entries start another ClipEm folder (or an older version):\n\n"
            + "\n".join(f"  \u2022 {describe_autostart_entry(entry)}" for entry in others)
            + "\n\nRemove them too?",
            parent=root,
        ):
            to_remove += others
        autostart_var.set(False)
        try:
            save_autostart_flags()
            remove_autostart_entries(to_remove)
        except Exception as exc:
            messagebox.showerror("Uninstall Autostart", f"Couldn't uninstall autostart: {exc}", parent=root)
        show_autostart_status()

    install_button.configure(command=on_install_autostart)
    uninstall_button.configure(command=on_uninstall_autostart)
    autostart_row.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
    next_row += 1
    tk.Label(frame, textvariable=autostart_status_var, fg="gray40").grid(
        row=next_row, column=0, columnspan=2, sticky="w"
    )
    next_row += 1
    show_autostart_status()

    tk.Label(frame, text="OBS Websocket", font=("", 10, "bold")).grid(
        row=next_row, column=0, columnspan=2, sticky="w", pady=(px(14), 0)
    )
    next_row += 1

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

    if updater is not None:
        update_config = updater.load_update_config()
        local_version = updater.get_local_version()

        tk.Label(frame, text="Updates", font=("", 10, "bold")).grid(
            row=next_row, column=0, columnspan=2, sticky="w", pady=(px(14), 0)
        )
        next_row += 1

        tk.Label(frame, text=f"Current version: {local_version}", fg="gray40").grid(
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

        channel_labels = {updater.CHANNEL_RELEASES: "Releases", updater.CHANNEL_LATEST: "Latest (dev builds)"}
        channel_var = tk.StringVar(value=channel_labels.get(update_config["update_channel"], "Releases"))
        saved_channel = {"label": channel_var.get()}

        def on_channel_selected(_event=None):
            label = channel_var.get()
            if label == saved_channel["label"]:
                return
            channel = next(key for key, text in channel_labels.items() if text == label)
            if channel == updater.CHANNEL_LATEST:
                explanation = (
                    "The Latest channel installs development builds published for testing (the \"dev\" "
                    "pre-release on GitHub) before they become a release. They may be unfinished or "
                    "broken.\n\nSwitch to Latest?"
                )
            else:
                explanation = (
                    "The Releases channel installs only published releases. If you're on a dev build, "
                    "the next update check offers the current release instead.\n\nSwitch to Releases?"
                )
            if not messagebox.askyesno("Update Channel", explanation, parent=root):
                channel_var.set(saved_channel["label"])
                return
            try:
                updater.change_update_config(update_channel=channel, skipped_version=None)
            except Exception as exc:
                messagebox.showerror("Updates", f"Couldn't save the update setting:\n{exc}")
                channel_var.set(saved_channel["label"])
                return
            saved_channel["label"] = label

        tk.Label(frame, text="Update channel:").grid(row=next_row, column=0, sticky="w", pady=px(4))
        channel_box = ttk.Combobox(frame, textvariable=channel_var, values=list(channel_labels.values()),
                                   state="readonly", width=18)
        channel_box.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
        channel_box.bind("<<ComboboxSelected>>", on_channel_selected)
        next_row += 1

        source_repo_var = tk.StringVar(value=update_config["source_repo"])

        def save_source_repo(repo):
            try:
                updater.change_update_config(source_repo=repo, skipped_version=None)
            except Exception as exc:
                messagebox.showerror("Updates", f"Couldn't save the update setting:\n{exc}")
                return
            source_repo_var.set(repo)

        def on_change_source_repo():
            if not messagebox.askyesno(
                "Update Source",
                "ClipEm downloads its updates from the releases of this GitHub repository.\n\n"
                "Only change it if you know the repository publishes ClipEm builds - updates must "
                "still be signed with the official ClipEm release key, so any other repository "
                "makes update checks fail.\n\nChange the update source?",
                parent=root,
            ):
                return
            repo = simpledialog.askstring(
                "Update Source", "GitHub repository (owner/name):", initialvalue=source_repo_var.get(), parent=root,
            )
            if repo is None:
                return
            repo = repo.strip()
            if not updater.is_valid_repo(repo):
                messagebox.showerror("Update Source", f"\"{repo}\" isn't a GitHub repository like owner/name.")
                return
            save_source_repo(repo)

        tk.Label(frame, text="Update source:").grid(row=next_row, column=0, sticky="w", pady=px(4))
        source_row = tk.Frame(frame)
        source_row.grid(row=next_row, column=1, sticky="w", padx=(px(6), 0), pady=px(4))
        tk.Entry(source_row, textvariable=source_repo_var, state="readonly", width=22).pack(side="left")
        tk.Button(source_row, text="Change...", command=on_change_source_repo).pack(side="left", padx=(px(4), 0))
        tk.Button(
            source_row, text="Reset", command=lambda: save_source_repo(updater.DEFAULT_SOURCE_REPO),
        ).pack(side="left", padx=(px(4), 0))
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
    tk.Label(frame, text="Troubleshooting", font=("", 10, "bold")).grid(
        row=next_row, column=0, columnspan=2, sticky="w", pady=(px(14), 0)
    )
    next_row += 1

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
            body, fg="gray40", justify="left", wraplength=px(380),
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
        path = filedialog.asksaveasfilename(
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
            body, fg="gray40", justify="left", wraplength=px(380),
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

    tk.Label(
        frame,
        text=(
            "Autostart, screen-detection, update settings and a new clip sound file\n"
            "apply right away. OBS only reads the websocket, hotkey and clip sound\n"
            "on/off and volume when it starts - if it's running, you'll be offered\n"
            "a restart."
        ),
        fg="gray40",
        justify="left",
    ).grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(12), 0))
    next_row += 1

    if ws_load_error:
        tk.Label(
            frame,
            text=f"Warning: couldn't read current websocket settings ({ws_load_error})",
            fg="red",
            wraplength=px(280),
            justify="left",
        ).grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(px(6), 0))
        next_row += 1

    def on_save():
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
        }
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
            run_config["autostart_obs"] = run_config["autostart_tray"] = bool(autostart_var.get())
            # Read by the tray widget before each check - see stuck_screen.py.
            run_config["stuck_check_enabled"] = bool(stuck_var.get())
            run_config["black_check_enabled"] = bool(black_var.get())
            # Read by the tray widget each poll - see disk_space.py.
            run_config["disk_space_warnings_enabled"] = bool(disk_warnings_var.get())
            run_config["custom_editor_path"] = editor_path_var.get().strip()
            save_run_config(run_config)
            apply_boot_autostart(run_config["autostart_obs"])
        except Exception as exc:
            messagebox.showerror("Save Failed", f"Couldn't save autostart, screen-detection and disk-space settings: {exc}")
            return

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
            "OBS only picks up websocket, hotkey and clip sound settings when it "
            "starts. Restart OBS now to apply them?\n\n"
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
    tk.Label(footer, textvariable=status_var, fg="gray40").pack(side="left")

    button_frame = tk.Frame(footer)
    button_frame.pack(side="right")
    tk.Button(button_frame, text="Cancel", command=on_cancel, width=10).pack(side="left", padx=px(4))
    tk.Button(button_frame, text="Save", command=on_save, width=10).pack(side="left", padx=px(4))

    # Size to fit the settings' width, but only a limited height - the rest
    # scrolls. Never taller than the screen, so the footer stays on it.
    root.update_idletasks()
    width = frame.winfo_reqwidth() + scrollbar.winfo_reqwidth()
    content_height = frame.winfo_reqheight() + footer.winfo_reqheight() + px(2)
    max_height = root.winfo_screenheight() - px(80)
    height = min(content_height, px(520), max_height)
    root.geometry(f"{width}x{height}")
    root.minsize(width, min(footer.winfo_reqheight() + px(120), max_height))
    root.maxsize(root.winfo_screenwidth(), content_height)

    root.mainloop()


if __name__ == "__main__":
    main()
