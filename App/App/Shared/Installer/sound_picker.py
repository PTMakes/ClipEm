"""The clip sound list in Setup and Settings: a readonly ttk.Combobox whose
drop-down is swapped for one with a small X next to each sound the user has
added (obs_config.saved_clip_sounds), which takes it off the list. The
presets have no X. Picking a sound sets the combobox and fires its
<<ComboboxSelected>>, as the stock drop-down would. Also "Add sound...":
add_sound_file, which asks how much of the file to keep."""

import os
import tkinter as tk

import dark_theme
import obs_config
import popups


def add_sound_file(parent, path, volume):
    """Adds the sound file at path to the sound list, first asking how much
    of it to play (ask_sound_length), and returns its name - or None if it
    couldn't be used (after saying why) or the user cancelled. volume() is
    the 0-1 volume previews play at."""
    toplevel = parent.winfo_toplevel()
    toplevel.configure(cursor="watch")
    toplevel.update_idletasks()
    try:
        temp, seconds = obs_config.prepare_clip_sound(path)
    except (OSError, ValueError) as exc:
        popups.showerror("Clip Sound", f"Couldn't use that sound: {exc}", parent=parent)
        return None
    finally:
        toplevel.configure(cursor="")
    try:
        def preview(cut):
            try:
                obs_config.preview_prepared_clip_sound(temp, cut, volume())
            except Exception as exc:
                popups.showerror("Clip Sound", f"Couldn't play the sound: {exc}", parent=parent)

        cut = ask_sound_length(parent, os.path.basename(path), seconds, preview)
        if cut is None:
            return None
        return obs_config.save_prepared_clip_sound(path, temp, cut)
    except (OSError, ValueError) as exc:
        popups.showerror("Clip Sound", f"Couldn't use that sound: {exc}", parent=parent)
        return None
    finally:
        obs_config.discard_prepared_clip_sound(temp)


def ask_sound_length(parent, name, seconds, preview):
    """A modal popup with a slider from MIN_CLIP_SOUND_SECONDS to the whole
    sound (where it starts); returns the length chosen, or None if
    cancelled. preview(length) plays it cut there."""
    shortest = obs_config.MIN_CLIP_SOUND_SECONDS
    if seconds <= shortest:
        return seconds
    owner = parent.winfo_toplevel()
    px = getattr(owner, "px", None) or (lambda n: n)
    dialog = tk.Toplevel(owner)
    dialog.title("Clip Sound")
    dialog.resizable(False, False)
    if owner.winfo_viewable():
        dialog.transient(owner)
    body = tk.Frame(dialog, padx=px(popups.PAD), pady=px(popups.PAD))
    body.pack(fill="both", expand=True)
    tk.Label(body, text=f"How much of {name} plays when a clip is saved:", anchor="w", justify="left",
             wraplength=popups.size(parent)[0] - 2 * px(popups.PAD)).pack(fill="x")

    length = tk.DoubleVar(dialog, value=seconds)
    slider_row = tk.Frame(body)
    slider_row.pack(fill="x", pady=(px(10), 0))
    label = tk.Label(slider_row, width=14, anchor="e")

    def snap(value):
        # Tenths of a second, but the very end is the whole sound.
        value = seconds if value >= seconds - 0.05 else max(shortest, round(value, 1))
        length.set(value)
        label.config(text=f"{value:.1f} s of {seconds:.1f} s")

    slider = dark_theme.RedSlider(slider_row, from_=shortest, to=seconds, variable=length, default=seconds,
                                  command=snap, height=px(24))
    slider.pack(side="left", fill="x", expand=True)
    label.pack(side="left", padx=(px(6), 0))
    snap(seconds)

    state = {"result": None}

    def finish(result):
        state["result"] = result
        dialog.destroy()

    row = tk.Frame(body)
    row.pack(side="bottom", anchor="e", pady=(px(12), 0))
    tk.Button(row, text="Play", width=10, command=lambda: preview(length.get())).pack(side="left")
    tk.Button(row, text="Cancel", width=10, command=lambda: finish(None)).pack(side="left", padx=(px(8), 0))
    add = tk.Button(row, text="Add", width=10, command=lambda: finish(length.get()))
    add.pack(side="left", padx=(px(8), 0))
    dialog.bind("<Return>", lambda _e: finish(length.get()))
    dialog.bind("<Escape>", lambda _e: finish(None))
    dialog.protocol("WM_DELETE_WINDOW", lambda: finish(None))

    popups.place(dialog, parent)
    dialog.grab_set()
    add.focus_set()
    dialog.wait_window()
    return state["result"]


class SoundDropdown:
    def __init__(self, combobox, on_remove, px=lambda n: n):
        """on_remove(name) runs after a saved sound is deleted, while the
        drop-down stays open."""
        self.combobox = combobox
        self.on_remove = on_remove
        self.px = px
        self.popup = None
        self.refresh()
        for sequence in ("<Button-1>", "<Down>", "<space>", "<Return>"):
            combobox.bind(sequence, self._open, add=False)

    def refresh(self):
        """Re-reads the list (the mouse wheel still steps through it)."""
        self.combobox.configure(values=obs_config.clip_sound_names())

    def _open(self, _event=None):
        if self.popup is not None:
            self.close()
            return "break"
        if self.combobox.instate(["disabled"]):
            return "break"
        self.refresh()
        self.popup = tk.Toplevel(self.combobox, background=dark_theme.LINE, borderwidth=0)
        self.popup.overrideredirect(True)
        self.popup.bind("<Escape>", lambda _e: self.close())
        self.popup.bind("<Button-1>", self._click_outside, add=True)
        self._fill()
        self.popup.update_idletasks()
        x = self.combobox.winfo_rootx()
        y = self.combobox.winfo_rooty() + self.combobox.winfo_height()
        width = max(self.combobox.winfo_width(), self.popup.winfo_reqwidth())
        self.popup.geometry(f"{width}x{self.popup.winfo_reqheight()}+{x}+{y}")
        self.popup.after_idle(self._grab)
        return "break"

    def _grab(self, tries=20):
        """Routes every click in the app to the drop-down, so one outside it
        closes it. A window can only be grabbed once it's on screen - tried
        again shortly until it is (never waited on: a window that doesn't
        appear mustn't hang the dialog)."""
        if self.popup is None:
            return
        try:
            self.popup.grab_set()
            self.popup.focus_set()
        except tk.TclError:
            if tries:
                self.popup.after(25, lambda: self._grab(tries - 1))

    def close(self):
        if self.popup is not None:
            try:
                self.popup.grab_release()
            except tk.TclError:
                pass
            self.popup.destroy()
            self.popup = None
        self.combobox.focus_set()

    def _click_outside(self, event):
        popup = self.popup
        if popup is None:
            return
        inside = (popup.winfo_rootx() <= event.x_root < popup.winfo_rootx() + popup.winfo_width()
                  and popup.winfo_rooty() <= event.y_root < popup.winfo_rooty() + popup.winfo_height())
        if not inside:
            self.close()

    def _fill(self):
        for child in self.popup.winfo_children():
            child.destroy()
        body = tk.Frame(self.popup, background=dark_theme.PANEL)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        current = self.combobox.get()
        saved = obs_config.saved_clip_sounds()
        for name in obs_config.clip_sound_names():
            self._row(body, name, removable=name in saved, selected=name == current)

    def _row(self, body, name, removable, selected):
        bg = dark_theme.SELECTED if selected else dark_theme.PANEL
        row = tk.Frame(body, background=bg)
        row.pack(fill="x")
        label = tk.Label(row, text=name, anchor="w", background=bg, foreground=dark_theme.TEXT,
                         padx=self.px(6), pady=self.px(2))
        label.pack(side="left", fill="x", expand=True)
        hover_parts = [row, label]
        if removable:
            remove = tk.Label(row, text="✕", background=bg, foreground=dark_theme.MUTED,
                              padx=self.px(6), cursor="hand2")
            remove.pack(side="right")
            remove.bind("<Enter>", lambda _e: remove.configure(foreground=dark_theme.ERROR))
            remove.bind("<Leave>", lambda _e: remove.configure(foreground=dark_theme.MUTED))
            remove.bind("<Button-1>", lambda _e: self._remove(name))
            hover_parts.append(remove)

        def paint(color):
            for part in hover_parts:
                part.configure(background=color)

        for part in (row, label):
            part.bind("<Enter>", lambda _e: paint(dark_theme.HOVER))
            part.bind("<Leave>", lambda _e: paint(bg))
            part.bind("<Button-1>", lambda _e: self._pick(name))

    def _pick(self, name):
        self.close()
        self.combobox.set(name)
        self.combobox.event_generate("<<ComboboxSelected>>")
        return "break"

    def _remove(self, name):
        obs_config.remove_clip_sound(name)
        self.refresh()
        self.on_remove(name)
        if self.popup is not None:
            self._fill()
            self.popup.update_idletasks()
            self.popup.geometry(f"{self.popup.winfo_width()}x{self.popup.winfo_reqheight()}")
        return "break"
