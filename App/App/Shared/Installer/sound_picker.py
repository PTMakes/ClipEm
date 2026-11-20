"""The clip sound list in Setup and Settings: a readonly ttk.Combobox whose
drop-down is swapped for one with a small X next to each sound the user has
added (obs_config.saved_clip_sounds), which takes it off the list. The
presets have no X. Picking a sound sets the combobox and fires its
<<ComboboxSelected>>, as the stock drop-down would. Also "Add sound..."
(add_sound_file) and SoundTrim, the start and end handles shown to the
right of the list."""

import tkinter as tk

import dark_theme
import obs_config
import popups


def add_sound_file(parent, path):
    """Adds the sound file at path to the sound list and returns its name,
    or None if it couldn't be used (after saying why)."""
    toplevel = parent.winfo_toplevel()
    toplevel.configure(cursor="watch")
    toplevel.update_idletasks()
    try:
        return obs_config.add_clip_sound(path)
    except (OSError, ValueError) as exc:
        popups.showerror("Clip Sound", f"Couldn't use that sound: {exc}", parent=parent)
        return None
    finally:
        toplevel.configure(cursor="")


def save_sound_choice(chosen, in_use, trim):
    """Saves the sound list's state on Save/Continue: every changed trim,
    and the chosen sound as the clip sound when it isn't already (as now
    trimmed). in_use: the listed sound the clip sound was, or None."""
    names = obs_config.clip_sound_names()
    for name in list(trim.edits):
        if name != chosen and name in names and trim.edited(name):
            obs_config.save_clip_sound_trim(name, *trim.trim(name))
    if chosen in names and (chosen != in_use or trim.edited(chosen)):
        obs_config.use_clip_sound(chosen, *trim.trim(chosen))
    trim.forget()


class SoundTrim(tk.Frame):
    """Start and end handles over the selected saved sound, with the span
    they keep - for cutting dead air off its start or a tail off its end.
    Disabled for the presets. Changes are held (edits) until the dialog
    saves them with use_clip_sound(name, *trim(name))."""

    def __init__(self, master, px, width=180):
        super().__init__(master)
        self.edits = {}  # name: (start, end) as moved, not yet saved
        self.name = None
        self._seconds = 0.0
        self.slider = dark_theme.RangeSlider(
            self, width=px(width), height=px(24), min_gap=obs_config.MIN_CLIP_SOUND_SECONDS, command=self._moved,
        )
        self.slider.pack(side="left")
        self.label = tk.Label(self, width=11, anchor="w")
        self.label.pack(side="left", padx=(px(4), 0))
        self._enabled = True

    def show(self, name):
        """Shows name's trim - its edit, else what's saved - or a disabled
        slider for a preset (or anything not in the list)."""
        self.name = name
        path = obs_config.saved_clip_sounds().get(name)
        try:
            self._seconds = obs_config.wav_seconds(path) if path else 0.0
        except (OSError, EOFError, ValueError):
            self._seconds = 0.0
        if not self._seconds:
            self.slider.set_range(0, 1, 0, 1)
            self.label.config(text="")
            self._apply_state()
            return
        start, end = self.edits.get(name) or obs_config.clip_sound_trim(name)
        self.slider.set_range(0, self._seconds, start, self._seconds if end is None else end)
        self._apply_state()
        self._show_label()

    def set_enabled(self, enabled):
        self._enabled = enabled
        self._apply_state()

    def _apply_state(self):
        usable = self._enabled and self._seconds > obs_config.MIN_CLIP_SOUND_SECONDS
        self.slider.configure(state="normal" if usable else "disabled")
        self.label.config(foreground=dark_theme.TEXT if usable else dark_theme.MUTED)

    def _moved(self, start, end):
        if self.name is not None and self._seconds:
            self.edits[self.name] = (round(start, 2), round(end, 2))
            self._show_label()

    def _show_label(self):
        start, end = self.slider.get()
        self.label.config(text=f"{start:.2f}\u2013{end:.2f} s")

    def trim(self, name):
        """(start, end) for use_clip_sound - end None where it's the end of
        the sound - or (None, None) when name's trim wasn't changed."""
        if name not in self.edits:
            return None, None
        start, end = self.edits[name]
        try:
            seconds = obs_config.wav_seconds(obs_config.saved_clip_sounds()[name])
        except (KeyError, OSError, EOFError, ValueError):
            return None, None
        return start, (None if end >= seconds - 0.005 else end)

    def edited(self, name):
        if name not in self.edits:
            return False
        return self.trim(name) != obs_config.clip_sound_trim(name)

    def forget(self, name=None):
        """Drops name's held edit (or all of them, once saved)."""
        if name is None:
            self.edits.clear()
        else:
            self.edits.pop(name, None)

    def play(self, volume):
        """Plays the selected sound as it would be saved."""
        start, end = self.trim(self.name) if self.name in self.edits else (None, None)
        obs_config.preview_clip_sound(self.name, volume, start, end)


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
