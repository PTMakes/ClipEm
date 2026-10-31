import os
import sys

import pytest

pytest.importorskip("tkinter")
from conftest import TRAY_WIDGET_DIR  # noqa: E402

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
        root.deiconify()
        root.update()
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
        root.deiconify()
        root.geometry(f"{width}x{height}")
        root.update()
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
