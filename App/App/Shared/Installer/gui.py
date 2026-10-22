"""
Tkinter setup wizard - the same UI code runs on Windows and Linux (and
macOS, wherever tkinter happens to be present). This file only owns the
interface; every actual install step is one of the plain functions in
setup.py, passed in via the `controller` dict so this module has no
import-time dependency on setup.py (see try_launch_gui in setup.py for
why that matters).

Falls back to the text-based wizard automatically if tkinter can't be
imported at all - see setup.py's try_launch_gui.
"""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import dark_theme
import editor_install
import file_dialogs
import updater
from app_window import style_app_window, use_windows_app_id
from mic_filter_gui import MicFilterWizard
from obs_config import (
    HOTKEY_KEY_CHOICES,
    load_black_check_enabled,
    load_disk_space_warnings_enabled,
    load_stuck_check_enabled,
)
from ui_scale import UiScale, enable_dpi_awareness

WINDOW_TITLE = "ClipEm - Setup"
PAD = 16
# Initial wrap width for instructional text: fits within the window's
# declared minsize (see Wizard.__init__) minus the container's own padding.
# Once a page is shown, _fit_wraps() rewraps every such label to the page's
# actual width, so text fills the window the same way on every page.
WRAP_WIDTH = 520

# run_config.json keys for the tray menu's entries, with their checkbox text
# (the same ones as Settings > Tray Menu).
MENU_ITEMS = {
    "show_save_clip_item": "Show \"Save Clip\" in the tray menu",
    "show_clip_editor_item": "Show \"Open Clip Editor\" in the tray menu",
    "show_replay_item": "Show \"Start/Stop Replay Buffer\" in the tray menu",
    "show_recording_item": "Show \"Start/Stop Recording\" in the tray menu",
    "show_obs_item": "Show \"Show/Hide OBS\" in the tray menu",
}

# Update channel -> the name Settings shows for it (Main follows the published
# releases, Dev the rolling "dev" pre-release).
UPDATE_BRANCHES = {updater.CHANNEL_RELEASES: "Main", updater.CHANNEL_LATEST: "Dev"}

# The step list's names for the pages below.
STEP_TITLES = {
    "_show_provisioning": "Install",
    "_show_clip_folder": "Clip Folder",
    "_show_video_quality": "Display & Video",
    "_show_audio_devices": "Audio Devices",
    "_show_split_tracks": "Multitrack Audio",
    "_show_clip_quality": "Clip Quality",
    "_show_reexport": "Smaller Copy",
    "_show_hotkey": "Clip Hotkey",
    "_show_sound": "Clip Sound",
    "_show_clip_editor": "Clip Editor",
    "_show_tray_menu": "Tray Menu",
    "_show_scripts": "Detection & Warnings",
    "_show_obs_websocket": "OBS Websocket",
    "_show_auto_update": "Updates",
    "_show_autostart": "Autostart",
    "_show_finish": "Finish",
}

# The pages after Welcome, in order - numbered "Step N of 13" in the bottom bar.
STEP_PAGES = (
    "_show_provisioning",
    "_show_clip_folder",
    "_show_video_quality",
    "_show_audio_devices",
    "_show_split_tracks",
    "_show_clip_quality",
    "_show_reexport",
    "_show_hotkey",
    "_show_sound",
    "_show_clip_editor",
    "_show_tray_menu",
    "_show_scripts",
    "_show_obs_websocket",
    "_show_auto_update",
    "_show_autostart",
    "_show_finish",
)

class _StreamToQueue:
    """Redirects print() output from the wrapped setup.py functions into
    the wizard's log box instead of a console window that may not even
    be visible."""

    def __init__(self, q):
        self._q = q

    def write(self, text):
        if text:
            self._q.put(("log", text))

    def flush(self):
        pass


class _StreamCapture:
    """Mirrors writes to the real stream (so console runs still show them)
    while also appending to `sink`, a plain list the wizard keeps around for
    the whole session - see Wizard._call_logged."""

    def __init__(self, real, sink):
        self._real = real
        self._sink = sink

    def write(self, text):
        if text:
            self._real.write(text)
            self._sink.append(text)

    def flush(self):
        self._real.flush()


class Wizard(tk.Tk):
    def __init__(self, controller):
        enable_dpi_awareness()
        use_windows_app_id("PTMakes.ClipEm.Setup")
        # className: what Linux desktops match the window to (WM_CLASS).
        super().__init__(className="ClipEm")
        dark_theme.apply_dark_theme(self)  # before any widget: classic ones take their colors at creation
        # ClipEm logo (also on the folder picker and message boxes), and
        # Ctrl+A / Ctrl+Z / Ctrl+Shift+Z or R in every text field.
        style_app_window(self, log=print)
        # All pixel sizes in this file are for a 96 DPI display - see ui_scale.py.
        self.px = UiScale(self).px
        self.controller = controller
        self.title(WINDOW_TITLE)
        # Tall enough for the OBS Websocket page (the tallest ordinary page,
        # ~522px of real content - see test_gui_layout.py), with a little
        # margin.
        # 16:9, with the step list beside the page's rounded card.
        self.geometry(f"{self.px(1120)}x{self.px(630)}")
        self.minsize(self.px(1000), self.px(620))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.sidebar = tk.Frame(self, background=dark_theme.PANEL, padx=self.px(10), pady=self.px(12))
        self.sidebar.pack(side="left", fill="y")
        self.step_labels = []
        for title in ("Welcome",) + tuple(STEP_TITLES[name] for name in STEP_PAGES):
            label = tk.Label(self.sidebar, text=title, anchor="w", padx=self.px(12), pady=self.px(4),
                             background=dark_theme.PANEL, foreground=dark_theme.MUTED, width=20)
            label.pack(fill="x", pady=self.px(1))
            self.step_labels.append(label)
        right = tk.Frame(self, padx=self.px(PAD), pady=self.px(PAD))
        right.pack(side="left", fill="both", expand=True)
        card = dark_theme.Card(right, pad=self.px(PAD), radius=self.px(10), fill_height=True)
        card.pack(fill="both", expand=True)
        self.container = card.body
        self.container.bind("<Configure>", lambda _event: self._fit_wraps())

        self.step_var = tk.StringVar(value="")
        for number, name in enumerate(STEP_PAGES, 1):
            setattr(self, name, self._numbered_page(getattr(self, name), number))

        self.queue = queue.Queue()
        self.clip_folder_var = tk.StringVar(value="")
        self.clip_folder_chosen = False

        # Display/audio detection shells out (PowerShell on Windows, which
        # compiles C# on the fly) and can take several seconds - done in the
        # background so the window appears straight away. Pages that need
        # the results call _ensure_devices() first.
        self.displays = None
        self.audio_devices = None
        self.display_index_var = tk.IntVar(value=0)
        self._start_device_detection()
        saved_audio = controller["load_audio_devices"]()
        self.output_device_var = tk.StringVar(value=saved_audio["output"])
        self.input_device_var = tk.StringVar(value=saved_audio["input"])
        video_settings = controller["load_video_settings"]()
        self.resolution_var = tk.StringVar(value=f"{video_settings['width']}x{video_settings['height']}")
        self.fps_var = tk.StringVar(value=str(video_settings["fps"]))
        self.replay_buffer_var = tk.IntVar(value=controller["load_replay_buffer_seconds"]())
        self.bitrate_var = tk.IntVar(value=controller["load_recording_bitrate"]())
        # Output resolution the bitrate was last recommended for - see
        # _follow_resolution_with_bitrate.
        self._bitrate_resolution = (video_settings["width"], video_settings["height"])
        self.filesize_estimate_var = tk.StringVar(value="")
        self.split_tracks_var = tk.BooleanVar(value=controller["load_split_tracks"]())
        reexport = controller["load_clip_reexport"]()
        self.reexport_var = tk.BooleanVar(value=reexport["reexport_enabled"])
        self.crop_duration_var = tk.IntVar(
            value=min(int(reexport["crop_seconds"]), self.replay_buffer_var.get())
        )
        self.target_filesize_var = tk.StringVar(value=f"{reexport['target_mb']:g}")
        self.bitrate_preview_var = tk.StringVar(value="")
        run_config = controller["load_run_config"]()
        # The tray menu's items and the tray widget's checks - the same
        # settings as Settings > Tray Menu and Scripts.
        self.menu_item_vars = {
            key: tk.BooleanVar(value=bool(run_config.get(key, True))) for key in MENU_ITEMS
        }
        self.editors_enabled_var = tk.BooleanVar(value=not editor_install.editors_disabled(run_config))
        self.editor_kind_var = tk.StringVar(value=editor_install.editor_kind(run_config))
        self.editor_path_var = tk.StringVar(value=run_config.get("custom_editor_path") or "")
        self.stuck_check_var = tk.BooleanVar(value=self._load_flag(load_stuck_check_enabled))
        self.black_check_var = tk.BooleanVar(value=self._load_flag(load_black_check_enabled))
        self.disk_warnings_var = tk.BooleanVar(value=self._load_flag(load_disk_space_warnings_enabled))
        # One choice for both: the tray widget is required, so it always
        # starts at boot alongside OBS.
        self.autostart_var = tk.BooleanVar(value=run_config["autostart_obs"])
        # Off for new installs (the shipped run_config.json says so).
        self.clip_notification_var = tk.BooleanVar(value=bool(run_config.get("clip_saved_notification", True)))
        update_config = controller["load_update_config"]()
        self.check_updates_var = tk.BooleanVar(value=update_config["check_for_updates"])
        self.source_repo_var = tk.StringVar(value=updater.repo_url(update_config["source_repo"]))
        self.update_branch_var = tk.StringVar(
            value=UPDATE_BRANCHES.get(update_config["update_channel"], "Main"))

        ctrl0, alt0, shift0, key0 = controller["load_clip_hotkey"]()
        self.hotkey_ctrl_var = tk.BooleanVar(value=ctrl0)
        self.hotkey_alt_var = tk.BooleanVar(value=alt0)
        self.hotkey_shift_var = tk.BooleanVar(value=shift0)
        self.hotkey_key_var = tk.StringVar(value=key0)

        self.mic_filter_configured = controller["has_mic_filter_been_configured"]()

        self.sound_enabled_var = tk.BooleanVar(value=controller["load_clip_sound_enabled"]())
        self.sound_volume_var = tk.DoubleVar(value=controller["load_clip_sound_volume"]())
        self.sound_file_var = tk.StringVar(value="")

        ws_settings = controller["load_obs_websocket_settings"]()
        self.ws_port_var = tk.StringVar(value=str(ws_settings["port"]))
        self.ws_password_var = tk.StringVar(value=ws_settings["password"])

        self.launch_after_finish_var = tk.BooleanVar(value=True)
        # ClipEm autostart entries from another folder or an older version -
        # looked up when the Autostart page is first shown.
        self.other_autostarts = None
        self.clear_other_autostarts_var = tk.BooleanVar(value=False)  # opt-in

        self.provision_ok = True

        # Every OBS-config-modifying controller call made on the earlier
        # pages (save_capture_monitor, save_audio_devices, etc.) is routed
        # through _call_logged and appended here, so the finish page's log
        # box can show the full trail of what actually got written and why
        # a step may have silently failed - see _show_finish.
        self.setup_log = []

        self._show_welcome()

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _load_flag(load):
        try:
            return bool(load())
        except Exception:
            return True

    def _highlight_step(self, number):
        for index, label in enumerate(self.step_labels):
            current = index == number
            label.configure(background=dark_theme.SELECTED if current else dark_theme.PANEL,
                            foreground=dark_theme.TEXT if current else dark_theme.MUTED)

    def _start_device_detection(self):
        result = {}

        def work():
            for key, fn in (
                ("displays", "list_displays"),
                ("saved_monitor_idx", "load_capture_monitor_index"),
                ("audio_devices", "list_audio_devices"),
            ):
                try:
                    result[key] = self.controller[fn]()
                except Exception as exc:
                    print(f"Could not {fn.replace('_', ' ')}: {exc}", file=sys.stderr)

        self._device_result = result
        self._device_thread = threading.Thread(target=work, daemon=True)
        self._device_thread.start()

    def _ensure_devices(self):
        """Waits for (almost always already finished) device detection and
        fills in the display/audio state from it. Main thread only."""
        if self.displays is not None:
            return
        self._device_thread.join()
        result = self._device_result
        self.displays = result.get("displays") or [{
            "name": "Primary display", "width": self.winfo_screenwidth(),
            "height": self.winfo_screenheight(), "primary": True,
        }]
        saved_monitor_idx = result.get("saved_monitor_idx")
        default_display_idx = next((i for i, d in enumerate(self.displays) if d.get("primary")), 0)
        self.display_index_var.set(
            saved_monitor_idx
            if saved_monitor_idx is not None and saved_monitor_idx < len(self.displays)
            else default_display_idx
        )
        self.audio_devices = result.get("audio_devices") or {"output": [], "input": []}

    def _run_quietly(self, work_fn, on_done):
        """Like _run_in_background, for pages without a log box: work_fn runs
        on a worker thread, on_done(ok, result_or_error) back on this one."""
        results = queue.Queue()

        def worker():
            try:
                results.put((True, work_fn()))
            except Exception as exc:
                results.put((False, str(exc)))

        def poll():
            try:
                ok, result = results.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            on_done(ok, result)

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, poll)

    def _clear(self):
        for child in self.container.winfo_children():
            child.destroy()
        self.step_var.set("")  # Welcome; numbered pages set it (_numbered_page)
        self._highlight_step(0)
        # After the page now being built has all its labels.
        self.after_idle(self._fit_wraps)

    def _numbered_page(self, show_page, number):
        def show():
            result = show_page()
            self.step_var.set(f"Step {number} of {len(STEP_PAGES)}")
            self._highlight_step(number)
            return result
        return show

    def _fit_wraps(self):
        """Wraps each wrapping label directly on the page to the page's
        current width (it changes with the window), rather than a fixed width
        that leaves short pages' text bunched up on the left."""
        if not self.container.winfo_exists():
            return
        width = self.container.winfo_width() - self.px(PAD)
        if width < self.px(200):
            return  # not laid out yet - <Configure> calls this again when it is
        for child in self.container.winfo_children():
            if isinstance(child, (ttk.Label, tk.Label)) and str(child.cget("wraplength")) not in ("0", ""):
                if str(child.cget("wraplength")) != str(width):
                    child.configure(wraplength=width)

    def _on_close(self):
        self.destroy()
        sys.exit(0)

    def _run_in_background(self, work_fn, on_done):
        def worker():
            old_stdout = sys.stdout
            sys.stdout = _StreamToQueue(self.queue)
            try:
                result = work_fn()
                self.queue.put(("done", result))
            except Exception as exc:
                self.queue.put(("error", str(exc)))
            finally:
                sys.stdout = old_stdout

        threading.Thread(target=worker, daemon=True).start()
        self._poll_queue(on_done)

    def _call_logged(self, fn, *args, **kwargs):
        """Runs an OBS-config-modifying controller function (print()s and
        all) and keeps its output in self.setup_log for _show_finish to
        replay, in addition to letting it reach the real stdout/stderr."""
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = _StreamCapture(old_stdout, self.setup_log)
        sys.stderr = _StreamCapture(old_stderr, self.setup_log)
        try:
            return fn(*args, **kwargs)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

    def _poll_queue(self, on_done):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log_text.insert("end", payload)
                    self.log_text.see("end")
                elif kind == "done":
                    on_done(True, payload)
                    return
                elif kind == "error":
                    on_done(False, payload)
                    return
        except queue.Empty:
            pass
        self.after(100, lambda: self._poll_queue(on_done))

    def _make_bottom_bar(self, parent, back_command, back_enabled=True):
        """Every page gets a Back button pinned to the bottom-left, paired
        with whatever forward button(s) the page packs into the same bar
        with side="right"."""
        bar = ttk.Frame(parent)
        bar.pack(side="bottom", fill="x", pady=(self.px(PAD // 2), 0))
        back_btn = ttk.Button(
            bar, text="Back", command=back_command,
            state="normal" if back_enabled else "disabled",
        )
        back_btn.pack(side="left")
        ttk.Label(bar, textvariable=self.step_var, foreground="gray").pack(side="left", expand=True)
        return bar, back_btn

    def _make_log_area(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=(self.px(PAD // 2), 0))
        self.log_text = tk.Text(frame, height=14, wrap="word")
        scroll = ttk.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _make_toggle(self, parent, var, text, enabled=True, on_toggle=None):
        """A big clickable checkbox drawn with our own glyph instead of
        ttk's native indicator, which is a tiny theme-drawn image that
        doesn't grow with font size (and looks different per platform) -
        this way "bigger" actually renders the same on Windows and Linux."""
        row = ttk.Frame(parent)
        row.pack(anchor="w", pady=(0, self.px(8)))
        cursor = "hand2" if enabled else "arrow"
        glyph = tk.Label(
            row, text="☑" if var.get() else "☐", font=("", 20),
            fg=dark_theme.TEXT if enabled else dark_theme.MUTED, cursor=cursor,
        )
        glyph.pack(side="left")
        label = ttk.Label(row, text=text, cursor=cursor)
        if not enabled:
            label.configure(foreground="gray")
        label.pack(side="left", padx=(self.px(6), 0))

        def toggle(_event=None):
            var.set(not var.get())
            glyph.configure(text="☑" if var.get() else "☐")
            if on_toggle:
                on_toggle()

        if enabled:
            glyph.bind("<Button-1>", toggle)
            label.bind("<Button-1>", toggle)
        return row

    def _copy_to_clipboard(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()

    # -- pages ------------------------------------------------------------

    def _show_welcome(self):
        self._clear()
        ttk.Label(
            self.container, text="ClipEm Setup", font=("", 16, "bold")
        ).pack(anchor="w")
        ttk.Label(
            self.container, text=f"Version {self.controller['version']}"
        ).pack(anchor="w", pady=(self.px(2), 0))
        ttk.Label(
            self.container, text=f"Platform detected: {self.controller['system']}"
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))
        ttk.Label(
            self.container,
            text=(
                "This downloads and checks the portable OBS copy, sets up the profile "
                "and OBS scripts and sets up the auto run options/hotkeys selected. "
                "\n\nIt's safe to re-run any time: it reinstalls the core files without removing any clips."
            ),
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(0, self.px(PAD)))
        self.welcome_status_var = tk.StringVar(value="")
        ttk.Label(
            self.container, textvariable=self.welcome_status_var,
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w")
        bar, _ = self._make_bottom_bar(self.container, back_command=self._on_close)
        self.begin_btn = ttk.Button(bar, text="Begin Setup", command=self._begin_setup)
        self.begin_btn.pack(side="right")

    def _begin_setup(self):
        """OBS rewrites the config files this wizard edits when it exits, so
        offer to close it first (stopping its outputs so nothing is lost)."""
        if not self.controller["obs_running"]():
            self._show_provisioning()
            return
        choice = messagebox.askyesnocancel(
            "OBS is running",
            f"{self.controller['obs_running_message']}\n\n"
            "Close OBS now? Any recording in progress is stopped and saved first.\n\n"
            "Yes: close OBS and continue\nNo: continue with OBS open\nCancel: go back",
        )
        if choice is None:
            return
        if not choice:
            self._show_provisioning()
            return

        self.begin_btn.configure(state="disabled")
        self.welcome_status_var.set("Closing OBS...")

        def on_closed(ok, closed):
            self.begin_btn.configure(state="normal")
            if ok and closed:
                self.welcome_status_var.set("")
                self._show_provisioning()
                return
            self.welcome_status_var.set(
                "OBS didn't close - it may be showing a dialog. Close OBS yourself, then click Begin Setup again."
            )

        self._run_quietly(self.controller["close_obs"], on_closed)

    def _show_provisioning(self):
        self._clear()
        ttk.Label(self.container, text="Installing OBS...", font=("", 13, "bold")).pack(
            anchor="w"
        )
        bar, self.provision_back_btn = self._make_bottom_bar(
            self.container, back_command=self._show_welcome, back_enabled=False
        )
        self.provision_continue_btn = ttk.Button(
            bar, text="Continue", command=self._after_provisioning, state="disabled"
        )
        self.provision_continue_btn.pack(side="right")
        self._make_log_area(self.container)

        def on_done(ok, result):
            self.provision_back_btn.configure(state="normal")
            if not ok:
                messagebox.showerror("Setup", f"Setup failed:\n{result}")
                self.provision_continue_btn.configure(
                    text="Close", state="normal", command=self.destroy
                )
                return
            self.provision_ok = bool(result)
            if not self.provision_ok:
                self.provision_continue_btn.configure(
                    text="Close", state="normal", command=self.destroy
                )
            else:
                self.provision_continue_btn.configure(state="normal")

        self._run_in_background(self.controller["provision"], on_done)

    def _after_provisioning(self):
        if not self.provision_ok:
            self.destroy()
            return
        self._show_clip_folder()

    def _show_clip_folder(self):
        self._clear()
        ttk.Label(self.container, text="Clips folder", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Recordings and replay-buffer clips are saved here. "
            "Choose one, or type/paste a path directly:",
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        # ttk.Entry has no built-in undo (unlike tk.Text) - track changes
        # ourselves so Ctrl+Z works. Reset fresh each time this page is
        # (re)shown, and drop any trace left over from a previous visit so
        # they don't stack up across back/forward navigation.
        if getattr(self, "_clip_folder_trace_id", None):
            self.clip_folder_var.trace_remove("write", self._clip_folder_trace_id)
        self._clip_folder_undo_stack = []
        self._clip_folder_redo_stack = []
        self._clip_folder_suppress_push = False
        self._clip_folder_last_value = self.clip_folder_var.get()
        self._clip_folder_trace_id = self.clip_folder_var.trace_add(
            "write", self._on_clip_folder_var_changed
        )

        entry = ttk.Entry(self.container, textvariable=self.clip_folder_var)
        entry.pack(fill="x", pady=(0, self.px(PAD // 2)))
        entry.bind("<KeyRelease>", self._on_clip_folder_typed)
        entry.bind("<Control-z>", self._undo_clip_folder)
        entry.bind("<Control-Z>", self._undo_clip_folder)
        # Its own undo (not app_window's): it also covers Browse... and the
        # default-folder button, which change the field without typing.
        for sequence in ("<Control-Shift-Z>", "<Control-Shift-z>", "<Control-Shift-R>", "<Control-Shift-r>"):
            entry.bind(sequence, self._redo_clip_folder)

        choice_row = ttk.Frame(self.container)
        choice_row.pack(fill="x", pady=(0, self.px(PAD)))
        ttk.Button(
            choice_row,
            text="Use Current Directory + /Clips",
            command=self._use_default_clip_folder,
        ).pack(side="left")
        ttk.Button(choice_row, text="Browse...", command=self._browse_clip_folder).pack(
            side="left", padx=(self.px(PAD // 2), 0)
        )

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_provisioning)
        self.clip_folder_continue_btn = ttk.Button(
            bar, text="Continue", command=self._apply_clip_folder,
            state="normal" if self.clip_folder_chosen else "disabled",
        )
        self.clip_folder_continue_btn.pack(side="right")

    def _mark_clip_folder_chosen(self):
        self.clip_folder_chosen = True
        self.clip_folder_continue_btn.configure(state="normal")

    def _on_clip_folder_typed(self, _event=None):
        if self.clip_folder_var.get().strip():
            self._mark_clip_folder_chosen()

    def _on_clip_folder_var_changed(self, *_args):
        if self._clip_folder_suppress_push:
            return
        self._clip_folder_undo_stack.append(self._clip_folder_last_value)
        self._clip_folder_last_value = self.clip_folder_var.get()
        # A genuine new edit invalidates whatever could previously be redone.
        self._clip_folder_redo_stack.clear()

    def _undo_clip_folder(self, _event=None):
        if not self._clip_folder_undo_stack:
            return "break"
        previous = self._clip_folder_undo_stack.pop()
        self._clip_folder_redo_stack.append(self._clip_folder_last_value)
        self._clip_folder_suppress_push = True
        self.clip_folder_var.set(previous)
        self._clip_folder_suppress_push = False
        self._clip_folder_last_value = previous
        if previous.strip():
            self._mark_clip_folder_chosen()
        return "break"

    def _redo_clip_folder(self, _event=None):
        if not self._clip_folder_redo_stack:
            return "break"
        next_value = self._clip_folder_redo_stack.pop()
        self._clip_folder_undo_stack.append(self._clip_folder_last_value)
        self._clip_folder_suppress_push = True
        self.clip_folder_var.set(next_value)
        self._clip_folder_suppress_push = False
        self._clip_folder_last_value = next_value
        if next_value.strip():
            self._mark_clip_folder_chosen()
        return "break"

    def _use_default_clip_folder(self):
        self.clip_folder_var.set(self.controller["default_clip_folder"])
        self._mark_clip_folder_chosen()

    def _browse_clip_folder(self):
        chosen = filedialog.askdirectory(
            title="Select folder for OBS recordings / replay buffer clips",
            initialdir=self.clip_folder_var.get() or self.controller["default_clip_folder"],
        )
        if chosen:
            self.clip_folder_var.set(chosen)
            self._mark_clip_folder_chosen()

    def _apply_clip_folder(self):
        folder = self.clip_folder_var.get().strip() or self.controller["default_clip_folder"]
        try:
            self._call_logged(self.controller["set_clip_folder"], folder)
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not set clip folder:\n{exc}")
            return
        self._show_video_quality()

    def _show_video_quality(self):
        self._ensure_devices()
        self._clear()
        ttk.Label(self.container, text="Display & Video Quality", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Which screen to capture, and at what resolution/FPS.",
            justify="left",
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        ttk.Label(self.container, text="Display").pack(anchor="w")
        display_labels = [
            f"{d['name']} - {d['width']}x{d['height']}" + (" (primary)" if d.get("primary") else "")
            for d in self.displays
        ]
        display_combo = ttk.Combobox(self.container, values=display_labels, state="readonly")
        display_combo.current(self.display_index_var.get())
        display_combo.pack(fill="x", pady=(0, self.px(PAD // 2)))

        ttk.Label(self.container, text="Output resolution").pack(anchor="w")
        resolution_combo = ttk.Combobox(self.container, state="readonly")
        resolution_combo.pack(fill="x", pady=(0, self.px(PAD // 2)))

        def refresh_resolutions():
            display = self.displays[display_combo.current()]
            choices = self.controller["resolution_choices"](display["width"], display["height"])
            labels = [f"{w}x{h}" for w, h in choices]
            resolution_combo.configure(values=labels)
            if self.resolution_var.get() in labels:
                resolution_combo.set(self.resolution_var.get())
            else:
                resolution_combo.current(0)
                self.resolution_var.set(labels[0])

        def on_display_selected(_event=None):
            self.display_index_var.set(display_combo.current())
            refresh_resolutions()

        display_combo.bind("<<ComboboxSelected>>", on_display_selected)
        resolution_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self.resolution_var.set(resolution_combo.get())
        )
        refresh_resolutions()

        ttk.Label(self.container, text="FPS").pack(anchor="w")
        ttk.Combobox(
            self.container, textvariable=self.fps_var,
            values=[str(f) for f in self.controller["fps_choices"]], state="readonly",
        ).pack(fill="x", pady=(0, self.px(PAD // 2)))

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_clip_folder)
        ttk.Button(bar, text="Continue", command=lambda: self._apply_video_quality(display_combo)).pack(
            side="right"
        )

    def _apply_video_quality(self, display_combo):
        display = self.displays[display_combo.current()]
        try:
            out_width, out_height = (int(v) for v in self.resolution_var.get().split("x"))
            fps = int(self.fps_var.get())
        except ValueError:
            messagebox.showerror("Setup", "Please enter valid numbers for resolution and FPS.")
            return
        try:
            self._call_logged(
                self.controller["save_video_settings"],
                display["width"], display["height"], out_width, out_height, fps,
            )
            self._call_logged(
                self.controller["save_capture_monitor"],
                display_combo.current(), display.get("device_id"),
            )
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not save video settings:\n{exc}")
            return
        self._follow_resolution_with_bitrate(out_width, out_height)
        self._show_audio_devices()

    def _follow_resolution_with_bitrate(self, width, height):
        """Moves the recording bitrate to the recommended value for the new
        output resolution (18000 kbps at 1080p, scaled by pixel count) -
        unless the user chose their own (see setup.py's
        bitrate_for_new_resolution)."""
        try:
            current = self.bitrate_var.get()
        except (ValueError, tk.TclError):
            current = None
        if current is not None:
            self.bitrate_var.set(
                self.controller["bitrate_for_new_resolution"](current, self._bitrate_resolution, (width, height))
            )
        self._bitrate_resolution = (width, height)

    def _show_audio_devices(self):
        self._ensure_devices()
        self._clear()
        ttk.Label(self.container, text="Audio Devices", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Which speaker/output device OBS records desktop audio from, "
            "and which microphone it records you from.",
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        def device_labels(flow):
            return [d["name"] for d in self.audio_devices[flow]]

        def index_for(flow, var):
            ids = [d["device_id"] for d in self.audio_devices[flow]]
            return ids.index(var.get()) if var.get() in ids else 0

        ttk.Label(self.container, text="Desktop audio (speaker/output)").pack(anchor="w")
        output_combo = ttk.Combobox(self.container, values=device_labels("output"), state="readonly")
        output_combo.current(index_for("output", self.output_device_var))
        output_combo.pack(fill="x", pady=(0, self.px(PAD // 2)))

        ttk.Label(self.container, text="Microphone").pack(anchor="w")
        input_combo = ttk.Combobox(self.container, values=device_labels("input"), state="readonly")
        input_combo.current(index_for("input", self.input_device_var))
        input_combo.pack(fill="x", pady=(0, self.px(PAD // 2)))

        self.mic_filter_btn = ttk.Button(
            self.container,
            text="Redo Mic Filter Configuration" if self.mic_filter_configured
            else "Start Mic Filter Configuration",
            command=lambda: self._start_mic_filter_configuration(input_combo),
        )
        self.mic_filter_btn.pack(anchor="w", pady=(0, self.px(PAD // 2)))

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_video_quality)
        ttk.Button(
            bar, text="Continue",
            command=lambda: self._apply_audio_devices(output_combo, input_combo),
        ).pack(side="right")

    def _apply_audio_devices(self, output_combo, input_combo):
        output_id = self.audio_devices["output"][output_combo.current()]["device_id"]
        input_id = self.audio_devices["input"][input_combo.current()]["device_id"]
        self.output_device_var.set(output_id)
        self.input_device_var.set(input_id)
        try:
            self._call_logged(self.controller["save_audio_devices"], output_id, input_id)
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not save audio device settings:\n{exc}")
            return
        self._show_split_tracks()

    def _start_mic_filter_configuration(self, input_combo):
        """Installs sounddevice/numpy on first use (ensure_mic_filter_deps
        is a no-op once they're present), then opens MicFilterWizard for
        whichever mic is currently selected in the combo box - not
        necessarily the one already saved, since a user might switch mics
        and calibrate before hitting Continue."""
        device = self.audio_devices["input"][input_combo.current()]
        device_name, device_id = device["name"], device["device_id"]

        progress = tk.Toplevel(self)
        progress.title("Mic Filter Configuration")
        progress.transient(self)
        progress.resizable(False, False)
        progress.grab_set()
        ttk.Label(progress, text="Preparing microphone calibration...", padding=self.px(PAD)).pack()

        deps_queue = queue.Queue()

        def worker():
            try:
                deps_queue.put(("done", self.controller["ensure_mic_filter_deps"]()))
            except Exception as exc:
                deps_queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                kind, payload = deps_queue.get_nowait()
            except queue.Empty:
                progress.after(100, poll)
                return
            progress.destroy()
            if kind == "done" and payload:
                MicFilterWizard(
                    self, self.controller, device_name, device_id,
                    on_applied=self._on_mic_filter_applied,
                )
            else:
                detail = payload if kind == "error" else "dependency install failed"
                messagebox.showerror(
                    "Mic Filter Configuration",
                    f"Could not set up microphone calibration:\n{detail}",
                )

        progress.after(100, poll)

    def _on_mic_filter_applied(self):
        self.mic_filter_configured = True
        if self.mic_filter_btn.winfo_exists():
            self.mic_filter_btn.configure(text="Redo Mic Filter Configuration")

    def _show_split_tracks(self):
        self._clear()
        ttk.Label(self.container, text="Split Audio Tracks", font=("", 13, "bold")).pack(anchor="w")
        per_app = bool(self.controller["app_capture_supported"]())
        sources = "your mic, desktop audio and every app that plays sound" if per_app else "your mic and desktop audio"
        ttk.Label(
            self.container,
            text=(
                f"Optionally also save a copy of every clip (\"-tracks.mkv\") with {sources} "
                "each on their own audio track, for editing. The clip itself keeps "
                "everything mixed together."
            ),
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        self._make_toggle(self.container, self.split_tracks_var, "Split audio tracks")

        if not per_app:
            ttk.Label(
                self.container,
                text="Per-app tracks aren't available on this system.",
                foreground="gray", justify="left", wraplength=self.px(WRAP_WIDTH),
            ).pack(anchor="w")

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_audio_devices)
        ttk.Button(bar, text="Continue", command=self._apply_split_tracks).pack(side="right")

    def _apply_split_tracks(self):
        try:
            self._call_logged(self.controller["save_split_tracks"], self.split_tracks_var.get())
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not save the split audio tracks setting:\n{exc}")
            return
        self._show_clip_quality()

    def _show_clip_quality(self):
        self._clear()
        ttk.Label(self.container, text="Clip Length & Quality", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text=(
                "How far back the replay buffer reaches, and the recording "
                "bitrate used for saved clips."
            ),
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD // 2)))

        ttk.Label(
            self.container,
            text="Replay buffer length, seconds (how far back a clip can reach)",
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w")
        buffer_row = ttk.Frame(self.container)
        buffer_row.pack(fill="x", pady=(0, self.px(PAD // 4)))
        ttk.Spinbox(
            buffer_row, textvariable=self.replay_buffer_var, width=8,
            from_=self.controller["replay_buffer_min_seconds"],
            to=self.controller["replay_buffer_max_seconds"],
            increment=self.controller["replay_buffer_step_seconds"],
        ).pack(side="left")

        ttk.Label(self.container, text="Recording bitrate, kbps").pack(anchor="w", pady=(self.px(PAD // 4), 0))
        bitrate_row = ttk.Frame(self.container)
        bitrate_row.pack(fill="x", pady=(0, self.px(PAD // 4)))
        ttk.Spinbox(
            bitrate_row, textvariable=self.bitrate_var, width=8,
            from_=self.controller["bitrate_min_kbps"],
            to=self.controller["bitrate_max_kbps"],
            increment=self.controller["bitrate_step_kbps"],
        ).pack(side="left")
        ttk.Label(bitrate_row, textvariable=self.filesize_estimate_var, foreground="gray").pack(
            side="left", padx=(self.px(PAD // 2), 0)
        )

        def update_filesize_estimate(*_args):
            try:
                bitrate = self.bitrate_var.get()
                buffer_seconds = self.replay_buffer_var.get()
            except (ValueError, tk.TclError):
                self.filesize_estimate_var.set("")
                return
            size_mb = self.controller["compute_filesize_for_bitrate"](bitrate, buffer_seconds)
            self.filesize_estimate_var.set(f"-> ~{size_mb:.1f}MB for a full-length clip")

        self.bitrate_var.trace_add("write", update_filesize_estimate)
        self.replay_buffer_var.trace_add("write", update_filesize_estimate)
        update_filesize_estimate()

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_split_tracks)
        ttk.Button(bar, text="Continue", command=self._continue_clip_quality).pack(side="right")

    def _continue_clip_quality(self):
        try:
            self.replay_buffer_var.get()
            self.bitrate_var.get()
        except (ValueError, tk.TclError):
            messagebox.showerror(
                "Setup", "Please enter valid numbers for replay buffer length and bitrate."
            )
            return
        self._show_reexport()

    def _show_reexport(self):
        self._clear()
        ttk.Label(self.container, text="Lower-Quality Re-export", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text=(
                "Optionally save a second, smaller copy of every clip next to "
                "the original (\"-small.mp4\") - useful for uploading clips "
                "somewhere with a strict file size limit. The full-quality "
                "clip is always kept as recorded."
            ),
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        self._make_toggle(
            self.container, self.reexport_var, "Also save a smaller copy of each clip",
            on_toggle=lambda: set_lower_section_state(
                "normal" if self.reexport_var.get() else "disabled"
            ),
        )

        lower_section = ttk.Frame(self.container)
        lower_section.pack(fill="x", pady=(0, self.px(PAD // 4)))
        target_size_label = ttk.Label(lower_section, text="Target clip file size, MB")
        target_size_label.pack(anchor="w")
        size_row = ttk.Frame(lower_section)
        size_row.pack(fill="x", pady=(0, self.px(PAD // 4)))
        size_entry = ttk.Entry(size_row, textvariable=self.target_filesize_var, width=8, state="disabled")
        size_entry.pack(side="left")
        ttk.Label(size_row, textvariable=self.bitrate_preview_var, foreground="gray").pack(
            side="left", padx=(self.px(PAD // 2), 0)
        )
        recommended_row = ttk.Frame(lower_section)
        recommended_row.pack(fill="x")
        use_recommended_btn = ttk.Button(
            recommended_row, text="Use recommended", state="disabled",
            command=lambda: self.target_filesize_var.set(
                str(self.controller["default_target_filesize_mb"])
            ),
        )
        use_recommended_btn.pack(side="left")
        recommended_note_label = ttk.Label(
            recommended_row,
            text=(
                f": {self.controller['default_target_filesize_mb']}MB - "
                f"Discord's free upload limit"
            ),
            foreground="gray",
            justify="left",
        )
        recommended_note_label.pack(side="left", padx=(self.px(PAD // 4), 0))

        crop_label = ttk.Label(lower_section, text="Automatically crop the video duration to the last:")
        crop_label.pack(anchor="w", pady=(self.px(PAD // 4), 0))
        crop_row = ttk.Frame(lower_section)
        crop_row.pack(fill="x")
        crop_entry = ttk.Spinbox(
            crop_row, textvariable=self.crop_duration_var, width=6,
            from_=1, to=self.replay_buffer_var.get(), state="disabled",
        )
        crop_entry.pack(side="left", padx=(self.px(PAD // 4), self.px(PAD // 4)))
        crop_suffix_label = ttk.Label(crop_row, text="seconds")
        crop_suffix_label.pack(side="left")

        def clamp_crop_duration(*_args):
            try:
                value = self.crop_duration_var.get()
            except tk.TclError:
                return
            clamped = max(1, min(value, self.replay_buffer_var.get()))
            if clamped != value:
                self.crop_duration_var.set(clamped)

        def update_crop_bounds(*_args):
            crop_entry.configure(to=self.replay_buffer_var.get())
            clamp_crop_duration()

        self.replay_buffer_var.trace_add("write", update_crop_bounds)
        crop_entry.bind("<FocusOut>", clamp_crop_duration)
        crop_entry.bind("<Return>", clamp_crop_duration)
        update_crop_bounds()

        def set_lower_section_state(state):
            disabled = state == "disabled"
            target_size_label.configure(foreground="gray" if disabled else "")
            recommended_note_label.configure(foreground="gray")
            crop_label.configure(foreground="gray" if disabled else "")
            crop_suffix_label.configure(foreground="gray" if disabled else "")
            size_entry.configure(state=state)
            use_recommended_btn.configure(state=state)
            crop_entry.configure(state=state)

        def update_bitrate_preview(*_args):
            try:
                target_mb = float(self.target_filesize_var.get())
                crop_seconds = self.crop_duration_var.get()
            except (ValueError, tk.TclError):
                self.bitrate_preview_var.set("")
                return
            bitrate = self.controller["compute_video_bitrate_for_filesize"](
                target_mb, crop_seconds
            )
            self.bitrate_preview_var.set(f"-> ~{bitrate} kbps video bitrate")

        self.target_filesize_var.trace_add("write", update_bitrate_preview)
        self.crop_duration_var.trace_add("write", update_bitrate_preview)
        update_bitrate_preview()
        set_lower_section_state("normal" if self.reexport_var.get() else "disabled")

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_clip_quality)
        ttk.Button(bar, text="Continue", command=self._apply_clip_quality).pack(side="right")

    def _apply_clip_quality(self):
        try:
            buffer_seconds = self.replay_buffer_var.get()
            bitrate = self.bitrate_var.get()
        except (ValueError, tk.TclError):
            messagebox.showerror(
                "Setup", "Please enter valid numbers for replay buffer length and bitrate."
            )
            return
        # The re-export settings drive a separate, smaller copy of each clip
        # (Scripts/clip_reexport.py) - the recording bitrate above is
        # saved exactly as chosen either way.
        reexport_enabled = self.reexport_var.get()
        try:
            target_mb = float(self.target_filesize_var.get())
            crop_seconds = self.crop_duration_var.get()
        except (ValueError, tk.TclError):
            if reexport_enabled:
                messagebox.showerror(
                    "Setup", "Please enter a valid target file size and crop duration."
                )
                return
            target_mb, crop_seconds = self.controller["default_target_filesize_mb"], buffer_seconds
        if reexport_enabled and target_mb <= 0:
            messagebox.showerror("Setup", "The target file size must be more than 0 MB.")
            return
        crop_seconds = max(1, min(crop_seconds, buffer_seconds))
        try:
            self._call_logged(self.controller["save_replay_buffer_seconds"], buffer_seconds)
            self._call_logged(self.controller["save_recording_bitrate"], bitrate)
            self._call_logged(
                self.controller["save_clip_reexport"], reexport_enabled, target_mb, crop_seconds
            )
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not save clip quality settings:\n{exc}")
            return
        self._show_hotkey()

    def _show_hotkey(self):
        self._clear()
        ttk.Label(self.container, text="Clip Hotkey", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text=(
                'Keyboard shortcut that saves a clip ("Save Replay Buffer" in OBS) - '
                "the replay buffer length you set earlier. Defaults to Alt+~."
            ),
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        self._make_toggle(self.container, self.hotkey_ctrl_var, "Ctrl")
        self._make_toggle(self.container, self.hotkey_alt_var, "Alt")
        self._make_toggle(self.container, self.hotkey_shift_var, "Shift")

        key_row = ttk.Frame(self.container)
        key_row.pack(anchor="w", pady=(0, self.px(PAD)))
        ttk.Label(key_row, text="Key:").pack(side="left")
        ttk.Combobox(
            key_row, textvariable=self.hotkey_key_var, values=HOTKEY_KEY_CHOICES,
            state="readonly", width=10,
        ).pack(side="left", padx=(self.px(6), 0))

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_reexport)
        ttk.Button(bar, text="Continue", command=self._apply_hotkey).pack(side="right")

    def _apply_hotkey(self):
        try:
            self._call_logged(
                self.controller["save_clip_hotkey"],
                self.hotkey_ctrl_var.get(),
                self.hotkey_alt_var.get(),
                self.hotkey_shift_var.get(),
                self.hotkey_key_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not save the clip hotkey:\n{exc}")
            return
        self._show_sound()

    def _show_sound(self):
        self._clear()
        ttk.Label(self.container, text="Clip Sound", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Plays a sound when a clip is saved.",
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        self._make_toggle(
            self.container, self.sound_enabled_var, "Play a sound when a clip is saved",
            on_toggle=lambda: set_sound_section_state(self.sound_enabled_var.get()),
        )

        volume_heading = ttk.Label(self.container, text="Volume")
        volume_heading.pack(anchor="w", pady=(self.px(PAD // 2), 0))
        volume_row = ttk.Frame(self.container)
        volume_row.pack(fill="x", pady=(0, self.px(PAD)))
        self.sound_volume_label = ttk.Label(volume_row, width=5)
        scale = dark_theme.RedSlider(
            volume_row, from_=0.0, to=1.0, variable=self.sound_volume_var,
            command=lambda _val: self._update_volume_label(),
        )
        scale.pack(side="left", fill="x", expand=True)
        self.sound_volume_label.pack(side="left", padx=(self.px(PAD // 2), 0))
        play_btn = ttk.Button(volume_row, text="Play", command=self._play_sound)
        play_btn.pack(side="left", padx=(self.px(PAD // 2), 0))
        self._update_volume_label()

        file_heading = ttk.Label(self.container, text="Sound file")
        file_heading.pack(anchor="w", pady=(self.px(PAD // 2), 0))
        file_row = ttk.Frame(self.container)
        file_row.pack(fill="x", pady=(0, self.px(PAD)))
        self.sound_file_label = ttk.Label(
            file_row,
            text=os.path.basename(self.sound_file_var.get())
            or "replay_buffer_save_sound.wav - default",
        )
        self.sound_file_label.pack(side="left", fill="x", expand=True)
        replace_btn = ttk.Button(file_row, text="Replace .wav...", command=self._browse_sound_file)
        replace_btn.pack(side="left", padx=(self.px(PAD // 2), 0))

        def set_sound_section_state(enabled):
            state = "normal" if enabled else "disabled"
            fg = "" if enabled else "gray"
            volume_heading.configure(foreground=fg)
            self.sound_volume_label.configure(foreground=fg)
            file_heading.configure(foreground=fg)
            self.sound_file_label.configure(foreground=fg)
            scale.configure(state=state)
            play_btn.configure(state=state)
            replace_btn.configure(state=state)

        set_sound_section_state(self.sound_enabled_var.get())

        self._make_toggle(
            self.container, self.clip_notification_var,
            "Show a notification when a clip is saved from the tray menu",
        )

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_hotkey)
        ttk.Button(bar, text="Continue", command=self._apply_sound).pack(side="right")

    def _update_volume_label(self):
        self.sound_volume_label.configure(text=f"{self.sound_volume_var.get():.0%}")

    def _play_sound(self):
        try:
            self.controller["play_clip_sound"](
                self.sound_volume_var.get(), self.sound_file_var.get() or None
            )
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not play the clip sound:\n{exc}")

    def _browse_sound_file(self):
        chosen = filedialog.askopenfilename(
            title="Select a replacement .wav file for the clip sound",
            filetypes=[("WAV files", "*.wav")],
        )
        if chosen:
            self.sound_file_var.set(chosen)
            self.sound_file_label.configure(text=os.path.basename(chosen))

    def _apply_sound(self):
        try:
            self._call_logged(self.controller["save_clip_sound_enabled"], self.sound_enabled_var.get())
            self._call_logged(self.controller["save_clip_sound_volume"], self.sound_volume_var.get())
            if self.sound_file_var.get():
                self._call_logged(self.controller["replace_clip_sound_wav"], self.sound_file_var.get())
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not save the clip sound settings:\n{exc}")
            return
        self._show_clip_editor()

    def _show_clip_editor(self):
        self._clear()
        ttk.Label(self.container, text="Clip Editor", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Which editor the tray menu's editor entry opens. Settings > Clip Editor changes this any time.",
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))
        self._make_toggle(
            self.container, self.editors_enabled_var,
            "Enable clip editors (off: no editor in the tray menu)",
            on_toggle=lambda: show_choice(),
        )
        kind_row = ttk.Frame(self.container)
        kind_row.pack(anchor="w")
        ttk.Label(kind_row, text="Edit clips in:").pack(side="left")
        kind_buttons = [
            ttk.Radiobutton(kind_row, text="ClipEm's editor", value=editor_install.INTERNAL,
                            variable=self.editor_kind_var, command=lambda: show_choice()),
            ttk.Radiobutton(kind_row, text="External editor", value=editor_install.EXTERNAL,
                            variable=self.editor_kind_var, command=lambda: show_choice()),
        ]
        for button in kind_buttons:
            button.pack(side="left", padx=(self.px(8), 0))

        editor_row = ttk.Frame(self.container)
        editor_row.pack(anchor="w", pady=(self.px(PAD // 2), 0))
        ttk.Label(editor_row, text="External editor:").pack(side="left")
        name_var = tk.StringVar()

        def show_name(*_args):
            path = self.editor_path_var.get().strip()
            name = os.path.basename(path.rstrip("/\\")) if path else "None"
            name_var.set(name if len(name) <= 32 else name[:31] + "\u2026")

        self.editor_path_var.trace_add("write", show_name)
        show_name()
        ttk.Label(editor_row, textvariable=name_var, width=32).pack(side="left", padx=(self.px(6), 0))

        def choose():
            if sys.platform == "darwin":
                path = file_dialogs.askdirectory(parent=self, title="Choose your video editor (.app)",
                                                 initialdir="/Applications")
            else:
                filetypes = [("Programs", "*.exe")] if sys.platform == "win32" else []
                path = file_dialogs.askopenfilename(
                    parent=self, title="Choose your video editor", filetypes=filetypes + [("All files", "*")])
            if path:
                self.editor_path_var.set(path)

        ttk.Button(editor_row, text="Choose...", command=choose).pack(side="left", padx=(self.px(4), 0))
        ttk.Button(editor_row, text="Clear", command=lambda: self.editor_path_var.set("")).pack(
            side="left", padx=(self.px(4), 0))

        def show_choice():
            off = not self.editors_enabled_var.get()
            for button in kind_buttons:
                button.configure(state="disabled" if off else "normal")
            if not off and self.editor_kind_var.get() == editor_install.EXTERNAL:
                editor_row.pack(anchor="w", pady=(self.px(PAD // 2), 0))
            else:
                editor_row.pack_forget()

        show_choice()

        def next_page():
            if (self.editors_enabled_var.get() and self.editor_kind_var.get() == editor_install.EXTERNAL
                    and not self.editor_path_var.get().strip()):
                messagebox.showerror("Clip Editor", "Choose your external editor, or pick ClipEm's editor.")
                return
            self._show_tray_menu()

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_sound)
        ttk.Button(bar, text="Continue", command=next_page).pack(side="right")

    def _show_tray_menu(self):
        self._clear()
        ttk.Label(self.container, text="Tray Menu", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Choose which entries the tray icon's menu shows. Settings > Tray Menu changes these any time.",
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))
        for key, text in MENU_ITEMS.items():
            self._make_toggle(self.container, self.menu_item_vars[key], text)
        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_clip_editor)
        ttk.Button(bar, text="Continue", command=self._show_scripts).pack(side="right")

    def _show_scripts(self):
        self._clear()
        ttk.Label(self.container, text="Detection and warnings", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text="Checks the tray widget runs while OBS is open. Settings > Scripts changes these any time.",
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))
        self._make_toggle(self.container, self.stuck_check_var, "Enable stuck-screen detection")
        self._make_toggle(self.container, self.black_check_var, "Enable black-screen detection")
        self._make_toggle(self.container, self.disk_warnings_var, "Enable disk space warnings")
        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_tray_menu)
        ttk.Button(bar, text="Continue", command=self._show_obs_websocket).pack(side="right")

    def _show_obs_websocket(self):
        self._clear()
        ttk.Label(self.container, text="OBS Websocket", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            self.container,
            text=(
                "Lets tools connect to OBS locally. "
                "Copy these if you need to connect your StreamDeck/misc to OBS, otherwise just skip."
            ),
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        ttk.Label(self.container, text="Port").pack(anchor="w")
        port_row = ttk.Frame(self.container)
        port_row.pack(fill="x", pady=(0, self.px(PAD // 4)))
        ttk.Entry(port_row, textvariable=self.ws_port_var, width=10).pack(side="left")
        ttk.Button(
            port_row, text="Copy",
            command=lambda: self._copy_to_clipboard(self.ws_port_var.get()),
        ).pack(side="left", padx=(self.px(PAD // 2), 0))
        ttk.Label(
            self.container,
            text="It's suggested to only change this if you will have another OBS instance "
            "open at the same time as ClipEm's instance.",
            foreground="gray", justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(0, self.px(PAD // 2)))

        ttk.Label(self.container, text="Password").pack(anchor="w")
        password_row = ttk.Frame(self.container)
        password_row.pack(fill="x", pady=(0, self.px(PAD // 2)))
        password_entry = ttk.Entry(password_row, textvariable=self.ws_password_var, show="*")
        password_entry.pack(side="left", fill="x", expand=True)
        password_visible = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            password_row, text="\U0001F441", width=2, style="Toolbutton",
            variable=password_visible,
            command=lambda: password_entry.config(show="" if password_visible.get() else "*"),
        ).pack(side="left", padx=(self.px(PAD // 4), 0))
        ttk.Button(
            password_row, text="Copy",
            command=lambda: self._copy_to_clipboard(self.ws_password_var.get()),
        ).pack(side="left", padx=(self.px(PAD // 2), 0))

        ttk.Button(
            self.container, text="Generate Random Password",
            command=self._randomize_ws_password,
        ).pack(anchor="w", pady=(0, self.px(PAD)))

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_scripts)
        ttk.Button(bar, text="Continue", command=self._apply_obs_websocket).pack(side="right")

    def _randomize_ws_password(self):
        self.ws_password_var.set(self.controller["generate_obs_websocket_password"]())

    def _apply_obs_websocket(self):
        try:
            port = int(self.ws_port_var.get())
        except ValueError:
            messagebox.showerror("Setup", "Please enter a valid port number.")
            return
        password = self.ws_password_var.get().strip()
        if not password:
            messagebox.showerror("Setup", "Please enter a websocket password.")
            return
        try:
            self._call_logged(self.controller["save_obs_websocket_settings"], port, password)
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not save the OBS websocket settings:\n{exc}")
            return
        self._show_auto_update()

    def _show_auto_update(self):
        self._clear()
        ttk.Label(self.container, text="Updates", font=("", 13, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            self.container,
            text=(
                "ClipEm can check github.com/PTMakes/ClipEm for a newer version each time it "
                "launches. Nothing is installed automatically - when an update is "
                "found, you choose whether to install it now, at the next restart, "
                "or not at all. Updating never resets the settings from this wizard."
            ),
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        self._make_toggle(
            self.container, self.check_updates_var,
            "Allow checking for updates",
        )

        source_row = ttk.Frame(self.container)
        source_row.pack(anchor="w", pady=(self.px(PAD // 2), 0))
        ttk.Label(source_row, text="Source repo:").pack(side="left")
        ttk.Entry(source_row, textvariable=self.source_repo_var, state="readonly", width=36).pack(
            side="left", padx=(self.px(6), 0))
        ttk.Button(source_row, text="Change...", command=self._change_source_repo).pack(
            side="left", padx=(self.px(4), 0))
        ttk.Button(
            source_row, text="Reset",
            command=lambda: self.source_repo_var.set(updater.repo_url(updater.DEFAULT_SOURCE_REPO)),
        ).pack(side="left", padx=(self.px(4), 0))

        branch_row = ttk.Frame(self.container)
        branch_row.pack(anchor="w", pady=(self.px(PAD // 2), 0))
        ttk.Label(branch_row, text="Update branch:").pack(side="left")
        branch_box = ttk.Combobox(branch_row, textvariable=self.update_branch_var,
                                  values=list(UPDATE_BRANCHES.values()), state="readonly", width=8)
        branch_box.pack(side="left", padx=(self.px(6), 0))
        branch_box.bind("<<ComboboxSelected>>", self._on_branch_selected)
        self._saved_branch = self.update_branch_var.get()

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_obs_websocket)
        ttk.Button(bar, text="Continue", command=self._show_autostart).pack(side="right")

    def _change_source_repo(self):
        if not messagebox.askyesno(
            "Source Repo",
            "ClipEm downloads its updates from the releases of this GitHub repository.\n\n"
            "Only change it if you know the repository publishes ClipEm builds - updates must "
            "still be signed with the official ClipEm release key, so any other repository "
            "makes update checks fail.\n\nChange the source repo?",
        ):
            return
        text = simpledialog.askstring(
            "Source Repo", "GitHub repository link (https://github.com/owner/name):",
            initialvalue=self.source_repo_var.get(), parent=self,
        )
        if text is None:
            return
        repo = updater.repo_from_url(text)
        if repo is None:
            messagebox.showerror(
                "Source Repo",
                f"\"{text.strip()}\" isn't a GitHub repository link like https://github.com/owner/name.",
            )
            return
        self.source_repo_var.set(updater.repo_url(repo))

    def _on_branch_selected(self, _event=None):
        label = self.update_branch_var.get()
        if label == self._saved_branch:
            return
        if label == "Dev":
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
        if messagebox.askyesno("Update Branch", explanation):
            self._saved_branch = label
        else:
            self.update_branch_var.set(self._saved_branch)

    def _show_autostart(self):
        self._clear()
        ttk.Label(self.container, text="Autostart with PC boot", font=("", 13, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            self.container,
            text=(
                "Registers (or removes) a login entry with your operating system so "
                "this launches on its own at boot, in addition to the shortcut. The "
                "shortcut itself always starts everything, regardless of this."
            ),
            justify="left", wraplength=self.px(WRAP_WIDTH),
        ).pack(anchor="w", pady=(self.px(4), self.px(PAD)))

        self._make_toggle(
            self.container,
            self.autostart_var,
            "Start ClipEm (OBS and the tray icon) when the PC boots",
        )

        if self.other_autostarts is None:
            try:
                self.other_autostarts = self.controller["find_other_autostart_entries"]()
            except Exception as exc:
                print(f"Could not look for other ClipEm autostart entries: {exc}", file=sys.stderr)
                self.other_autostarts = []
        if self.other_autostarts:
            count = len(self.other_autostarts)
            self._make_toggle(
                self.container,
                self.clear_other_autostarts_var,
                f"Remove {count} autostart entr{'y' if count == 1 else 'ies'} from other ClipEm copies",
            )
            ttk.Label(
                self.container,
                text="\n".join(self.controller["describe_autostart_entry"](e) for e in self.other_autostarts),
                foreground="gray", font=("", 9), justify="left", wraplength=self.px(WRAP_WIDTH),
            ).pack(anchor="w", pady=(0, self.px(PAD // 2)))

        bar, _ = self._make_bottom_bar(self.container, back_command=self._show_auto_update)
        ttk.Button(bar, text="Continue", command=self._show_finish).pack(side="right")

    def _show_finish(self):
        self._clear()
        ttk.Label(self.container, text="Finishing up...", font=("", 13, "bold")).pack(
            anchor="w"
        )
        self._make_toggle(
            self.container, self.launch_after_finish_var, "Automatically run OBS after closing this"
        )
        bar, self.finish_back_btn = self._make_bottom_bar(
            self.container, back_command=self._show_autostart, back_enabled=False
        )
        self.finish_btn = ttk.Button(
            bar, text="Done", command=self._finish_and_close, state="disabled"
        )
        self.finish_btn.pack(side="right")
        self._make_log_area(self.container)

        if self.setup_log:
            self.log_text.insert(
                "end", "--- OBS config changes made during setup ---\n" + "".join(self.setup_log) + "\n"
            )
            self.log_text.see("end")

        def work():
            # Merged into what's there - Settings keeps more in this file.
            run_config = self.controller["load_run_config"]()
            run_config.update({
                "autostart_obs": self.autostart_var.get(),
                "autostart_tray": self.autostart_var.get(),
                "clip_saved_notification": self.clip_notification_var.get(),
                "clip_editors_disabled": not self.editors_enabled_var.get(),
                "clip_editor": self.editor_kind_var.get(),
                "custom_editor_path": self.editor_path_var.get().strip(),
                "stuck_check_enabled": self.stuck_check_var.get(),
                "black_check_enabled": self.black_check_var.get(),
                "disk_space_warnings_enabled": self.disk_warnings_var.get(),
                **{key: var.get() for key, var in self.menu_item_vars.items()},
            })
            self.controller["save_run_config"](run_config)
            channel = next(key for key, text in UPDATE_BRANCHES.items() if text == self.update_branch_var.get())
            repo = updater.repo_from_url(self.source_repo_var.get()) or updater.DEFAULT_SOURCE_REPO
            changes = {"check_for_updates": self.check_updates_var.get(),
                       "source_repo": repo, "update_channel": channel}
            saved = self.controller["load_update_config"]()
            if (repo, channel) != (saved["source_repo"], saved["update_channel"]):
                changes["skipped_version"] = None  # as in Settings: a new source offers its version again
            self.controller["change_update_config"](**changes)
            # Before this install's own entry is written - one of these may
            # sit at the same path.
            if self.other_autostarts and self.clear_other_autostarts_var.get():
                self.controller["remove_autostart_entries"](self.other_autostarts)
            self.controller["apply_boot_autostart"](self.autostart_var.get())
            ok, warning = self.controller["check_tray_prereqs"]()
            if not ok:
                print(warning)
            self.controller["install_tray"]()
            self.controller["finalize"]()
            return True

        def on_done(ok, result):
            if not ok:
                messagebox.showerror("Setup", f"Finishing steps failed:\n{result}")
            self.finish_btn.configure(state="normal")
            self.finish_back_btn.configure(state="normal")

        self._run_in_background(work, on_done)

    def _finish_and_close(self):
        if self.launch_after_finish_var.get():
            try:
                self.controller["launch_app"]()
            except Exception as exc:
                messagebox.showerror("Setup", f"Could not start OBS:\n{exc}")
        self.destroy()


def run_wizard(controller):
    app = Wizard(controller)
    app.mainloop()
