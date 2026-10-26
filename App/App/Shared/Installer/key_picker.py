"""The "Pick..." button next to each hotkey in Setup (gui.py) and Settings
(settings_dialog.py): opens a popup, and the next key pressed with any of
Ctrl/Alt/Shift becomes the hotkey - as obs_config's (ctrl, alt, shift,
key label). Esc on its own cancels (Esc with a modifier is a hotkey)."""

import sys
import tkinter as tk

import dark_theme
import obs_config
import popups

# Tk keysym -> obs_config.HOTKEY_KEY_CHOICES label, for keys whose keysym
# isn't the label itself - including what Shift turns them into on a US
# layout (Shift+1 arrives as "exclam").
KEYSYM_LABELS = {
    "grave": "~", "asciitilde": "~", "minus": "-", "underscore": "-", "equal": "=", "plus": "=",
    "bracketleft": "[", "braceleft": "[", "bracketright": "]", "braceright": "]",
    "semicolon": ";", "colon": ";", "apostrophe": "'", "quotedbl": "'", "comma": ",", "less": ",",
    "period": ".", "greater": ".", "slash": "/", "question": "/", "backslash": "\\", "bar": "\\",
    "exclam": "1", "at": "2", "numbersign": "3", "dollar": "4", "percent": "5", "asciicircum": "6",
    "ampersand": "7", "asterisk": "8", "parenleft": "9", "parenright": "0",
    "space": "Space", "Insert": "Insert", "Delete": "Delete", "Home": "Home", "End": "End",
    "Prior": "Page Up", "Next": "Page Down", "Pause": "Pause", "Print": "Print Screen",
    "Scroll_Lock": "Scroll Lock", "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
    "Tab": "Tab", "ISO_Left_Tab": "Tab", "Return": "Enter", "BackSpace": "Backspace", "Escape": "Esc",
    "Menu": "Menu", "App": "Menu", "KP_Enter": "Num Enter",
    **{f"KP_{d}": f"Num {d}" for d in range(10)},
    "KP_Add": "Num +", "KP_Subtract": "Num -", "KP_Multiply": "Num *", "KP_Divide": "Num /",
    "KP_Decimal": "Num .", "KP_Insert": "Num 0", "KP_End": "Num 1", "KP_Down": "Num 2", "KP_Next": "Num 3",
    "KP_Left": "Num 4", "KP_Begin": "Num 5", "KP_Right": "Num 6", "KP_Home": "Num 7", "KP_Up": "Num 8",
    "KP_Prior": "Num 9", "KP_Delete": "Num .",
}
MODIFIER_KEYSYMS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R",
                    "Super_L", "Super_R", "ISO_Level3_Shift", "Caps_Lock", "Num_Lock", "Win_L", "Win_R"}
SHIFT, CONTROL = 0x1, 0x4
ALT = 0x20000 if sys.platform == "win32" else 0x10 if sys.platform == "darwin" else 0x8


def key_label(keysym):
    """The HOTKEY_KEY_CHOICES label for a Tk keysym, or None if it can't be a hotkey."""
    label = KEYSYM_LABELS.get(keysym)
    if label is None and len(keysym) == 1 and keysym.isalnum():
        label = keysym.upper()
    if label is None and keysym.upper() in {f"F{n}" for n in range(1, 25)}:
        label = keysym.upper()
    return label if label in obs_config.HOTKEY_KEY_CHOICES else None


def hotkey_from_event(keysym, state):
    """(ctrl, alt, shift, key label) for a key press, or None for a
    modifier on its own or a key that can't be a hotkey."""
    if keysym in MODIFIER_KEYSYMS:
        return None
    label = key_label(keysym)
    if label is None:
        return None
    return bool(state & CONTROL), bool(state & ALT), bool(state & SHIFT), label


def pick_hotkey(parent, title="Pick a Hotkey"):
    """Waits for a key combination in a popup; returns it, or None if cancelled."""
    owner = parent.winfo_toplevel()
    dialog = tk.Toplevel(owner)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(owner)
    px = popups._px(parent)
    body = tk.Frame(dialog, padx=px(popups.PAD), pady=px(popups.PAD))
    body.pack(fill="both", expand=True)
    tk.Label(body, text="Press the shortcut you want", font=("", 14, "bold")).pack(expand=True, side="top",
                                                                                   anchor="s")
    shown = tk.Label(body, text="Hold Ctrl, Alt or Shift too if you like.", fg=dark_theme.MUTED)
    shown.pack(expand=True, side="top", anchor="n", pady=(px(8), 0))
    buttons = tk.Frame(body)
    buttons.pack(side="bottom", anchor="e")
    state = {"result": None}

    def on_key(event):
        if event.keysym == "Escape" and not event.state & (CONTROL | ALT | SHIFT):
            dialog.destroy()
            return "break"
        hotkey = hotkey_from_event(event.keysym, event.state)
        if hotkey is None:
            if event.keysym not in MODIFIER_KEYSYMS:
                shown.configure(text=f"{event.keysym} can't be used - try another key.", fg=dark_theme.ERROR)
            return "break"
        state["result"] = hotkey
        dialog.destroy()
        return "break"

    tk.Button(buttons, text="Cancel", width=10, command=dialog.destroy).pack()
    dialog.bind("<KeyPress>", on_key)
    popups.place(dialog, parent)
    dialog.grab_set()
    dialog.focus_force()
    dialog.wait_window()
    return state["result"]
