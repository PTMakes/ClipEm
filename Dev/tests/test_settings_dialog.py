import os
import sys
import time

import pytest

pytest.importorskip("tkinter")
from conftest import TRAY_WIDGET_DIR, show_window  # noqa: E402

sys.path.insert(0, TRAY_WIDGET_DIR)
import obs_config  # noqa: E402
import settings_dialog  # noqa: E402

INITIAL = {
    "websocket": {"port": 4455, "password": "old-password-123"},
    "clip_hotkey": [False, True, False, "~"],
}


def _with(**changes):
    return {**INITIAL, **changes}


def test_nothing_changed_writes_nothing(fake_obs_config, monkeypatch):
    monkeypatch.setattr(settings_dialog.obs_process, "obs_running", lambda: pytest.fail("no need to ask"))
    assert settings_dialog.save_obs_settings(dict(INITIAL), INITIAL) == "unchanged"
    assert not os.path.exists(obs_config.PENDING_OBS_SETTINGS_PATH)


def test_obs_closed_applies_straight_away(fake_obs_config, monkeypatch):
    monkeypatch.setattr(settings_dialog.obs_process, "obs_running", lambda: False)

    outcome = settings_dialog.save_obs_settings(_with(clip_hotkey=[True, False, False, "F9"]), INITIAL)

    assert outcome == "applied"
    assert obs_config.load_clip_hotkey() == (True, False, False, "F9")
    assert not os.path.exists(obs_config.PENDING_OBS_SETTINGS_PATH)


def test_obs_running_only_queues_changed_settings(fake_obs_config, monkeypatch):
    monkeypatch.setattr(settings_dialog.obs_process, "obs_running", lambda: True)

    outcome = settings_dialog.save_obs_settings(_with(clip_hotkey=[True, False, False, "F9"]), INITIAL)

    assert outcome == "queued"
    assert obs_config.load_pending_obs_settings() == {"clip_hotkey": [True, False, False, "F9"]}
    assert obs_config.load_clip_hotkey() == (False, True, False, "~")  # OBS's own file untouched


def test_queued_settings_accumulate_until_applied(fake_obs_config, monkeypatch):
    monkeypatch.setattr(settings_dialog.obs_process, "obs_running", lambda: True)
    settings_dialog.save_obs_settings(_with(clip_hotkey=[True, False, False, "F9"]), INITIAL)
    settings_dialog.save_obs_settings(_with(websocket={"port": 4460, "password": "new-password"}), INITIAL)
    assert set(obs_config.load_pending_obs_settings()) == {"clip_hotkey", "websocket"}


def test_a_second_launch_brings_the_open_dialog_to_the_front(tmp_path, monkeypatch):
    import time
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    raise_path = tmp_path / ".settings.raise"
    monkeypatch.setattr(settings_dialog, "RAISE_PATH", str(raise_path))
    monkeypatch.setattr(settings_dialog, "RAISE_POLL_MS", 20)
    raised = []
    monkeypatch.setattr(settings_dialog, "bring_to_front", lambda window: raised.append(window))
    try:
        settings_dialog.watch_for_reopen(root)
        deadline = time.time() + 0.2
        while time.time() < deadline:
            root.update()
        assert raised == []  # nobody asked
        raise_path.write_text("raise")  # what a second launch does
        deadline = time.time() + 2
        while not raised and time.time() < deadline:
            root.update()
            time.sleep(0.01)
        assert raised == [root] and not raise_path.exists()
    finally:
        root.destroy()


def test_a_second_launch_asks_the_first_and_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_dialog, "RAISE_PATH", str(tmp_path / ".settings.raise"))
    monkeypatch.setattr(settings_dialog, "acquire_instance_lock", lambda path: False)
    monkeypatch.setattr(settings_dialog, "exit_with_parent", lambda pid: None)
    monkeypatch.setattr(sys, "argv", ["settings_dialog.py"])
    settings_dialog.main()  # returns instead of opening a second dialog
    assert (tmp_path / ".settings.raise").exists()


def test_window_class_is_what_tk_reports():
    import tkinter as tk

    try:
        root = tk.Tk(className=settings_dialog.WINDOW_CLASS)
    except tk.TclError:
        pytest.skip("no display")
    assert root.winfo_class() == settings_dialog.WINDOW_CLASS  # what StartupWMClass has to match
    root.destroy()


def test_settings_gets_its_own_taskbar_entry_and_icon(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    settings_dialog.ensure_desktop_entry(str(tmp_path))
    text = (tmp_path / settings_dialog.DESKTOP_FILE).read_text(encoding="utf-8")
    assert f"StartupWMClass={settings_dialog.WINDOW_CLASS}\n" in text
    assert "ClipEmSettings.png" in text and "NoDisplay=true" in text


def _in_settings(monkeypatch, look):
    """Runs look(root) in the real Settings window, shown, then closes it."""
    import tkinter as tk

    try:
        tk.Tk().destroy()
    except tk.TclError:
        pytest.skip("no display")

    def run(root, *_args):
        show_window(root)
        look(root)
        root.destroy()

    monkeypatch.setattr(tk.Misc, "mainloop", run)
    monkeypatch.setattr(settings_dialog, "watch_for_reopen", lambda root: None)
    monkeypatch.setattr(settings_dialog, "bring_to_front", lambda root: None)
    settings_dialog.open_dialog()


def _widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _widgets(child)


def test_clip_output_has_its_three_boxes_and_recording_its_own_tab(fake_obs_config, monkeypatch):
    import tkinter as tk

    seen = {}

    def titles(root, tab):
        root.layout["show_tab"](tab)
        root.update()
        cards = [card for card in root.layout["page"].winfo_children() if card.winfo_ismapped()]
        return [next(w.cget("text") for w in _widgets(card) if isinstance(w, tk.Label)) for card in cards]

    def look(root):
        seen["output"] = titles(root, "Clip Output")
        seen["recording"] = titles(root, "Recording/Devices")

    _in_settings(monkeypatch, look)
    assert seen == {"output": ["Clip Output", "Clip Names", "Clipping Add-Ons"], "recording": ["Recording"]}


def test_every_new_setting_in_the_release_notes_can_be_highlighted(fake_obs_config, monkeypatch):
    import json

    from conftest import APP_SHARED_DIR

    with open(os.path.join(APP_SHARED_DIR, "release_notes.json"), encoding="utf-8") as f:
        keys = [item["key"] for entry in json.load(f) for item in entry.get("new_settings", ())]
    known = []
    _in_settings(monkeypatch, lambda root: known.extend(root.layout["highlights"]))
    assert keys and set(keys) <= set(known)


def test_highlighting_opens_the_tab_and_outlines_the_settings(fake_obs_config, monkeypatch):
    seen = {}

    def look(root):
        widget, _tab = root.layout["highlights"]["post_clip"]
        other, _other_tab = root.layout["highlights"]["view_logs"]
        root.highlight_settings(["post_clip", "view_logs", "no_such_setting"])
        root.update()
        seen.update(shown=widget.winfo_ismapped(), border=int(widget.cget("highlightthickness")),
                    other_border=int(other.cget("highlightthickness")))

    _in_settings(monkeypatch, look)
    assert seen["shown"] and seen["border"] > 0 and seen["other_border"] > 0


def test_highlighted_settings_get_a_new_badge_only_on_their_own_tab(fake_obs_config, monkeypatch):
    import tkinter as tk

    seen = {}

    def look(root):
        root.highlight_settings(["post_clip", "view_logs"])
        root.highlight_settings(["post_clip"])  # asked again: not a second badge
        root.update()
        badge = root.layout["highlighted"]["post_clip"]["badge"]
        widget = root.layout["highlights"]["post_clip"][0]
        other = root.layout["highlighted"]["view_logs"]["badge"]
        seen.update(badges=len([w for w in _widgets(root) if isinstance(w, tk.Label) and hasattr(w, "picture")]),
                    shown=badge.winfo_viewable(), other_shown=other.winfo_viewable(),
                    right=badge.winfo_rootx() >= widget.winfo_rootx() + widget.winfo_width(),
                    middle_off=abs(2 * badge.winfo_rooty() + badge.winfo_height()
                                   - 2 * widget.winfo_rooty() - widget.winfo_height()))
        root.layout["show_tab"]("Scripts/Troubleshooting")
        root.update()
        seen.update(switched=(badge.winfo_viewable(), other.winfo_viewable()))

    _in_settings(monkeypatch, look)
    assert seen["badges"] == 2 and seen["shown"] and not seen["other_shown"] and seen["right"]
    assert seen["middle_off"] <= 2  # doubled, so 1 pixel off: odd sizes can't center exactly
    assert seen["switched"] == (0, 1)


def test_a_badge_moves_what_is_beside_it_over_and_back(fake_obs_config, monkeypatch):
    import tkinter as tk

    seen = {}

    def look(root):
        root.layout["show_tab"]("Scripts/Troubleshooting")
        root.update()
        neighbor = next(w for w in _widgets(root) if isinstance(w, tk.Button) and w.cget("text") == "Reinstall ClipEm...")
        before = neighbor.winfo_rootx()
        root.highlight_settings(["view_logs"])
        root.update()
        badge = root.layout["highlighted"]["view_logs"]["badge"]
        seen.update(moved=neighbor.winfo_rootx() - before, clear=neighbor.winfo_rootx() >= badge.winfo_rootx() + badge.winfo_width())
        monkeypatch.setattr(settings_dialog.new_badge, "burst", lambda *args: None)
        badge.event_generate("<Button-1>")
        root.update()
        seen.update(back=neighbor.winfo_rootx() - before)

    _in_settings(monkeypatch, look)
    assert seen["moved"] > 0 and seen["clear"] and seen["back"] == 0


def test_badges_push_no_setting_out_of_the_window(fake_obs_config, monkeypatch):
    over = {}

    def look(root):
        layout = root.layout
        show_window(root, layout["size"])
        root.update()
        root.highlight_settings(list(layout["highlights"]))
        for tab in layout["tabs"]:
            layout["show_tab"](tab)
            # Settings widens itself to fit; the window manager takes a moment to do it.
            deadline = time.monotonic() + 3
            while True:
                root.update()
                widest = max((card.body.winfo_reqwidth() - card.body.winfo_width()
                              for card in layout["page"].winfo_children() if card.winfo_ismapped()), default=0)
                if widest <= 0 or time.monotonic() > deadline:
                    break
                time.sleep(0.01)
            if widest > 0:
                over[tab] = widest

    _in_settings(monkeypatch, look)
    assert over == {}


def test_clicking_a_badge_bursts_confetti_and_clears_only_that_highlight(fake_obs_config, monkeypatch):
    import time

    seen = {}
    bursts = []
    real_burst = settings_dialog.new_badge.burst

    def counted_burst(root, *args):
        before = len(root.winfo_children())
        real_burst(root, *args)
        bursts.append(len(root.winfo_children()) - before)

    monkeypatch.setattr(settings_dialog.new_badge, "burst", counted_burst)
    monkeypatch.setattr(settings_dialog.new_badge, "FRAME_MS", 1)

    def look(root):
        widget = root.layout["highlights"]["post_clip"][0]
        before = int(widget.cget("highlightthickness"))
        root.highlight_settings(["post_clip", "view_logs"])
        root.update()
        badge = root.layout["highlighted"]["post_clip"]["badge"]
        frames = len(root.winfo_children())
        badge.event_generate("<Button-1>")
        root.update()
        seen.update(left=set(root.layout["highlighted"]), border=int(widget.cget("highlightthickness")), before=before,
                    gone=not badge.winfo_exists())
        deadline = time.time() + 5
        while len(root.winfo_children()) > frames and time.time() < deadline:
            root.update()
        seen.update(cleaned_up=len(root.winfo_children()) == frames)

    _in_settings(monkeypatch, look)
    assert bursts == [settings_dialog.new_badge.PIECES] and seen["gone"] and seen["left"] == {"view_logs"}
    assert seen["border"] == seen["before"]
    assert seen["cleaned_up"]


def _text_box(widget):
    """(left, top, right, bottom) on screen of what a widget says, or None if
    it says nothing. A field's whole box counts: what's typed can fill it."""
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk

    left, top = widget.winfo_rootx(), widget.winfo_rooty()
    right, bottom = left + widget.winfo_width(), top + widget.winfo_height()
    if isinstance(widget, (tk.Entry, tk.Text, tk.Spinbox, ttk.Entry)):  # ttk's Combobox and Spinbox are Entries
        return left, top, right, bottom
    if not isinstance(widget, (tk.Label, tk.Button, tk.Checkbutton, tk.Radiobutton)) or not widget.cget("text"):
        return None
    font = tkfont.Font(root=widget, font=widget.cget("font"))
    lines = widget.cget("text").split("\n")
    wide, tall = max(font.measure(line) for line in lines), font.metrics("linespace") * len(lines)
    anchor = str(widget.cget("anchor")).replace("center", "")
    if "w" in anchor:
        text_left = left
    elif "e" in anchor:
        text_left = right - wide
    else:
        text_left = (left + right - wide) // 2
    text_top = top if "n" in anchor else bottom - tall if "s" in anchor else (top + bottom - tall) // 2
    if widget.cget("wraplength") and int(str(widget.cget("wraplength"))):
        return left, top, right, bottom  # wrapped: can't tell where its lines end
    if isinstance(widget, (tk.Checkbutton, tk.Radiobutton)):
        return text_left, text_top, right, text_top + tall  # after the box it draws
    return text_left, text_top, text_left + wide, text_top + tall


def test_new_badges_cover_no_text(fake_obs_config, monkeypatch):
    import tkinter as tk

    covered = []
    checked = []

    def look(root):
        layout = root.layout
        show_window(root, layout["size"])
        root.highlight_settings(list(layout["highlights"]))
        badges = {item["badge"]: key for key, item in layout["highlighted"].items()}
        for tab in layout["tabs"]:
            layout["show_tab"](tab)
            root.update()
            for badge, key in badges.items():
                if not badge.winfo_viewable():
                    continue
                checked.append(key)
                left, top = badge.winfo_rootx(), badge.winfo_rooty()
                right, bottom = left + badge.winfo_width(), top + badge.winfo_height()
                for widget in _widgets(root):
                    if widget in badges or not isinstance(widget, tk.Widget) or not widget.winfo_viewable():
                        continue
                    box = _text_box(widget)
                    if box and left < box[2] and box[0] < right and top < box[3] and box[1] < bottom:
                        covered.append((tab, key, str(widget), box, (left, top, right, bottom)))

    _in_settings(monkeypatch, look)
    assert set(checked) == {"post_clip", "clip_names", "view_logs", "release_notes"}
    assert not covered, "\n".join(str(item) for item in covered)


def test_the_badge_ships_as_the_asset_itself(fake_obs_config):
    import filecmp

    from conftest import APP_SHARED_DIR

    asset = os.path.join(APP_SHARED_DIR, "..", "..", "..", "Dev", "assets", "NewIcon.png")
    assert filecmp.cmp(settings_dialog.new_badge.ICON_PATH, asset, shallow=False)


def test_release_notes_window_can_show_the_new_settings(fake_obs_config, monkeypatch):
    import tkinter as tk

    seen = {}

    def look(root):
        next(w for w in _widgets(root) if isinstance(w, tk.Button) and w.cget("text") == "Release Notes").invoke()
        root.update()
        next(w for w in _widgets(root) if isinstance(w, tk.Button) and w.cget("text") == "Show New Settings").invoke()
        root.update()
        seen.update(lit=set(root.layout["highlighted"]), windows=[w for w in root.winfo_children()
                                                                  if isinstance(w, tk.Toplevel) and w.winfo_viewable()])

    _in_settings(monkeypatch, look)
    assert seen["lit"] == {"clip_names"} and not seen["windows"]


def test_highlight_argument(tmp_path):
    assert settings_dialog.highlight_from_args(["--parent-pid", "1", "--highlight", "a,b"]) == ["a", "b"]
    assert settings_dialog.highlight_from_args(["--parent-pid", "1"]) == []


def test_a_second_launch_hands_over_what_to_highlight(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_dialog, "RAISE_PATH", str(tmp_path / ".settings.raise"))
    monkeypatch.setattr(settings_dialog, "acquire_instance_lock", lambda path: False)
    monkeypatch.setattr(settings_dialog, "exit_with_parent", lambda pid: None)
    monkeypatch.setattr(sys, "argv", ["settings_dialog.py", "--highlight", "post_clip,view_logs"])
    settings_dialog.main()
    assert (tmp_path / ".settings.raise").read_text() == "post_clip,view_logs"


def test_info_buttons_are_square(fake_obs_config, monkeypatch):
    import tkinter as tk

    sizes = []

    def look(root):
        root.layout["show_tab"]("All")
        root.update()
        sizes.extend((w.winfo_reqwidth(), w.winfo_reqheight()) for w in _widgets(root)
                     if isinstance(w, tk.Button) and w.cget("text") == "i")

    _in_settings(monkeypatch, look)
    assert len(sizes) >= 3 and all(width == height for width, height in sizes)


def test_troubleshooting_has_a_view_logs_button(fake_obs_config, monkeypatch):
    import tkinter as tk

    opened = []
    monkeypatch.setattr(settings_dialog.log_viewer, "open_log_viewer", lambda *args: opened.append(args))

    def look(root):
        button = next(w for w in _widgets(root) if isinstance(w, tk.Button) and w.cget("text") == "View Logs...")
        button.invoke()

    _in_settings(monkeypatch, look)
    assert len(opened) == 1


def _tab_overflow(monkeypatch):
    """{tab: what doesn't fit} for every tab but "All", in the real Settings
    window at the size it opens at on a big enough screen."""
    import tkinter as tk

    overflow = {}

    def measure(root, *_args):
        layout = root.layout
        canvas, page = layout["canvas"], layout["page"]
        width, height = layout["size"]
        show_window(root, (width, height))
        for tab in layout["tabs"]:
            if tab == "All":
                continue
            layout["show_tab"](tab)
            root.update()
            over = {"down": page.winfo_reqheight() - canvas.winfo_height(),
                    "across": page.winfo_reqwidth() - canvas.winfo_width()}
            over = {side: f"{pixels}px" for side, pixels in over.items() if pixels > 0}
            if over:
                overflow[tab] = over
        root.destroy()

    monkeypatch.setattr(tk.Misc, "mainloop", measure)
    monkeypatch.setattr(settings_dialog, "watch_for_reopen", lambda root: None)
    monkeypatch.setattr(settings_dialog, "bring_to_front", lambda root: None)
    settings_dialog.open_dialog()
    return overflow


def test_no_settings_tab_overflows_or_scrolls(fake_obs_config, monkeypatch):
    import tkinter as tk

    try:
        tk.Tk().destroy()
    except tk.TclError:
        pytest.skip("no display")
    assert _tab_overflow(monkeypatch) == {}


def _button(root, text):
    import tkinter as tk

    return next(w for w in _widgets(root) if isinstance(w, tk.Button) and w.cget("text") == text)


def _save_with_a_new_clip_folder(monkeypatch, tmp_path, choice):
    """Presses Default, then Save, answering the clip folder question."""
    old, new = tmp_path / "old-clips", tmp_path / "new-clips"
    old.mkdir()
    (old / "a.mp4").write_bytes(b"clip")
    monkeypatch.setattr(settings_dialog.obs_config, "load_clip_folder", lambda: str(old))
    monkeypatch.setattr(settings_dialog.obs_config, "DEFAULT_CLIPS_DIR", str(new))
    monkeypatch.setattr(settings_dialog, "APP_DIR", str(tmp_path))
    asked, saved = [], []
    monkeypatch.setattr(settings_dialog.popups, "askchoice",
                        lambda title, message, buttons, default, parent=None: asked.append(message) or choice)
    monkeypatch.setattr(settings_dialog, "save_obs_settings", lambda obs, initial: saved.append(obs) or "applied")
    monkeypatch.setattr(settings_dialog.popups, "showinfo", lambda *args, **kwargs: None)

    def look(root):
        root.layout["show_tab"]("Clip Output")
        root.update()
        _button(root, "Default").invoke()
        _button(root, "Save").invoke()
        for _ in range(100):
            if not root.winfo_exists():
                return
            root.update()
            time.sleep(0.01)

    import tkinter as tk

    try:
        _in_settings(monkeypatch, look)
    except tk.TclError:
        pass  # Save closed the window
    return old, new, asked, saved


@pytest.mark.parametrize("choice, stays, arrives", [("move", False, True), ("copy", True, True), ("leave", True, False)])
def test_a_new_clip_folder_offers_the_old_ones_clips(fake_obs_config, monkeypatch, tmp_path, choice, stays, arrives):
    old, new, asked, saved = _save_with_a_new_clip_folder(monkeypatch, tmp_path, choice)
    assert len(asked) == 1 and "1 clip file" in asked[0] and saved
    assert (old / "a.mp4").exists() is stays and (new / "a.mp4").exists() is arrives


def test_cancelling_the_clip_folder_question_saves_nothing(fake_obs_config, monkeypatch, tmp_path):
    old, new, asked, saved = _save_with_a_new_clip_folder(monkeypatch, tmp_path, "cancel")
    assert asked and not saved and (old / "a.mp4").exists() and not (new / "a.mp4").exists()


def test_editor_settings_are_on_the_clip_editor_page_and_saved(fake_obs_config, monkeypatch):
    import tkinter as tk

    seen = {}

    def look(root):
        root.layout["show_tab"]("Clip Editor")
        root.update()
        seen["labels"] = {w.cget("text") for w in _widgets(root) if isinstance(w, tk.Label) and w.winfo_ismapped()}
        monkeypatch.setattr(settings_dialog, "save_obs_settings", lambda obs, initial: "unchanged")
        _button(root, "Save").invoke()

    try:
        _in_settings(monkeypatch, look)
    except tk.TclError:
        pass  # Save closed the window
    assert {"Editor Exports", "Editor Behavior", "Export encoder:", "Detected encoder:", "Open clips in:",
            "Discord export size:", "Delete unused imports after:"} <= seen["labels"]
    config = settings_dialog.load_run_config()
    assert config["editor_export"] == {"encoder": "x264", "quality": "best", "speed": "medium"}
    assert config["editor_sub_tracks"] is True and config["editor_open_in"] == "last"
    assert config["editor_discord_mb"] == 20 and config["editor_asset_days"] == 30


def test_a_request_can_open_a_page(fake_obs_config, monkeypatch):
    opened = []
    monkeypatch.setattr(sys, "argv", ["settings_dialog.py", "--highlight", "tab=Clip Editor"])
    monkeypatch.setattr(settings_dialog, "exit_with_parent", lambda pid: None)
    monkeypatch.setattr(settings_dialog, "acquire_instance_lock", lambda path: True)
    monkeypatch.setattr(settings_dialog, "ensure_desktop_entry", lambda: None)
    monkeypatch.setattr(settings_dialog, "open_dialog", lambda tab=None, highlight=(): opened.append(tab))
    settings_dialog.main()
    assert opened == ["Clip Editor"]


def _theme_combo(root):
    from tkinter import ttk

    return next(w for w in _widgets(root) if isinstance(w, ttk.Combobox) and "Halloween" in w.cget("values"))


def _choose_theme(root, label):
    combo = _theme_combo(root)
    combo.current(list(combo.cget("values")).index(label))
    combo.event_generate("<<ComboboxSelected>>")
    root.update()


def _appearance(monkeypatch, look, config=None, answers=()):
    """look(root) on UI Customization, with run_config `config`; askstring
    and askyesno answer from `answers` in turn. Returns run_config after."""
    import tkinter as tk

    if config is not None:
        settings_dialog.save_run_config(config)
    replies = list(answers)
    monkeypatch.setattr(settings_dialog.popups, "askstring", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(settings_dialog.popups, "askyesno", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(settings_dialog.popups, "showerror", lambda title, message, **k: replies.append(message))
    monkeypatch.setattr(settings_dialog, "save_obs_settings", lambda obs, initial: "unchanged")

    def run(root):
        root.layout["show_tab"]("UI Customization")
        root.update()
        look(root)

    try:
        _in_settings(monkeypatch, run)
    except tk.TclError:
        pass  # Save closed the window
    return settings_dialog.load_run_config(), replies


def test_the_theme_dropdown_offers_every_theme(fake_obs_config, monkeypatch):
    seen = {}
    config = {"saved_themes": {"Neon": {"dark": True, "colors": {"accent": "#00ff99"}}}}
    _appearance(monkeypatch, lambda root: seen.update(values=list(_theme_combo(root).cget("values")),
                                                      shown=_theme_combo(root).get()), config)
    assert seen["values"] == ["Dark Mode", "Light Mode", "Halloween", "Christmas", "Neon"]
    assert seen["shown"] == "Dark Mode"


def test_an_old_light_mode_opens_on_light_mode(fake_obs_config, monkeypatch):
    seen = []
    _appearance(monkeypatch, lambda root: seen.append(_theme_combo(root).get()), {"dark_mode": False})
    assert seen == ["Light Mode"]


def test_choosing_a_theme_shows_its_colors_and_save_keeps_it(fake_obs_config, monkeypatch):
    import tkinter as tk

    seen = {}

    def look(root):
        _choose_theme(root, "Halloween")
        entries = [w for w in _widgets(root) if isinstance(w, tk.Entry) and w.cget("font") == "TkFixedFont"]
        seen["colors"] = {e.get() for e in entries}
        _button(root, "Save").invoke()

    config, _ = _appearance(monkeypatch, look, {})
    assert settings_dialog.ui_colors.THEMES["halloween"][2]["accent"] in seen["colors"]
    assert config["theme"] == "halloween" and config["dark_mode"] is True and "custom_colors" not in config


def test_a_changed_color_is_kept_for_its_own_theme(fake_obs_config, monkeypatch):
    import tkinter as tk

    def look(root):
        _choose_theme(root, "Christmas")
        entry = next(w for w in _widgets(root) if isinstance(w, tk.Entry) and w.get()
                     == settings_dialog.ui_colors.THEMES["christmas"][2]["background"])
        entry.delete(0, "end")
        entry.insert(0, "#010203")
        _choose_theme(root, "Light Mode")  # and back: still changed
        _choose_theme(root, "Christmas")
        _button(root, "Save").invoke()

    config, _ = _appearance(monkeypatch, look, {"custom_colors": {"dark": {"wave": "#123456"}}})
    assert config["theme"] == "christmas"
    assert config["custom_colors"] == {"christmas": {"background": "#010203"}, "dark": {"wave": "#123456"}}


def test_saving_a_theme_keeps_it_even_if_settings_are_cancelled(fake_obs_config, monkeypatch):
    seen = {}

    def look(root):
        _choose_theme(root, "Halloween")
        _button(root, "Save as Theme...").invoke()
        root.update()
        seen["shown"] = _theme_combo(root).get()
        seen["delete"] = str(_button(root, "Delete Theme").cget("state"))
        _button(root, "Cancel").invoke()

    config, replies = _appearance(monkeypatch, look, {}, answers=["  Spooky  "])
    assert seen == {"shown": "Spooky", "delete": "normal"} and not replies
    saved = config["saved_themes"]["Spooky"]
    assert saved["dark"] is True and saved["colors"] == dict(
        settings_dialog.ui_colors.theme_base("halloween", {})[1])
    assert "theme" not in config  # choosing it still waits for Save


def test_a_theme_cant_take_a_built_in_name_and_replacing_one_is_asked(fake_obs_config, monkeypatch):
    seen = []

    def look(root):
        _button(root, "Save as Theme...").invoke()  # "Christmas": refused
        _button(root, "Save as Theme...").invoke()  # "Mine", already there: not replaced
        seen.append(_theme_combo(root).get())

    config, replies = _appearance(monkeypatch, look, {"saved_themes": {"Mine": {"dark": False, "colors": {}}}},
                                  answers=["christmas", "Mine", False])
    assert len(replies) == 1 and "one of ClipEm's themes" in replies[0]
    assert config["saved_themes"] == {"Mine": {"dark": False, "colors": {}}} and seen == ["Dark Mode"]


def test_deleting_a_theme(fake_obs_config, monkeypatch):
    seen = {}

    def look(root):
        seen["opened_on"] = (_theme_combo(root).get(), str(_button(root, "Delete Theme").cget("state")))
        _choose_theme(root, "Dark Mode")
        seen["delete_on_dark"] = str(_button(root, "Delete Theme").cget("state"))
        _choose_theme(root, "Paper")
        _button(root, "Delete Theme").invoke()
        seen["after"] = (_theme_combo(root).get(), list(_theme_combo(root).cget("values")))
        _button(root, "Save").invoke()

    config, _ = _appearance(monkeypatch, look, {"theme": "saved:Paper", "dark_mode": False,
                                                "saved_themes": {"Paper": {"dark": False, "colors": {}}},
                                                "custom_colors": {"saved:Paper": {"text": "#111111"}}},
                            answers=[True])
    assert seen["opened_on"] == ("Paper", "normal") and seen["delete_on_dark"] == "disabled"
    assert seen["after"] == ("Light Mode", ["Dark Mode", "Light Mode", "Halloween", "Christmas"])
    assert config["saved_themes"] == {} and config["theme"] == "light" and "custom_colors" not in config
