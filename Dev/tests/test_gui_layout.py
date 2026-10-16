"""
Layout regression test for the setup wizard (gui.py).

Walks every wizard page with stub controller data and checks for content
that would visibly be cut off given the wizard's fixed window size. Two
independent checks, because Tk's pack() geometry manager fails in two
different ways:

  - A text-bearing widget (Label/Button/Checkbutton) gets squeezed
    narrower/shorter than the text it needs to show. pack() clips each
    widget's box to fit its parent's cavity once the toplevel is a fixed
    size (see Wizard.geometry() in gui.py), but it does NOT wrap or
    truncate the widget's own text to match - the text just renders past
    the widget's box and gets clipped at the window edge. This is the
    plain-language "my label got cut off" bug.
  - A widget's box is positioned outside the window entirely (possible
    with place() or negative offsets, though nothing here currently does
    that) - a cheaper, complementary check.

Widgets that are meant to stretch/shrink with available space (Entry,
Combobox, Scale, Spinbox, or Text/Scrollbar which just wrap and scroll
instead of losing content) are intentionally excluded from the first
check; being narrower than some internal default is not a bug for those.

Runs each page both at the wizard's default size and again after
shrinking to its declared minsize(), since users can resize down to
that at any time.

Needs a real, running X display - tkinter can't compute real widget
geometry off-screen. Run directly:

    python3 tests/test_gui_layout.py

or under Xvfb in CI:

    xvfb-run -a python3 tests/test_gui_layout.py
"""

import os
import sys
import tkinter as tk
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "App", "App", "Shared", "Installer"
    ),
)

import gui

PAGES = (
    "_show_welcome",
    "_show_provisioning",
    "_show_clip_folder",
    "_show_video_quality",
    "_show_audio_devices",
    "_show_split_tracks",
    "_show_clip_quality",
    "_show_reexport",
    "_show_hotkey",
    "_show_sound",
    "_show_obs_websocket",
    "_show_tray_widget",
    "_show_auto_update",
    "_show_autostart",
    "_show_finish",
)

# Widgets whose whole point is text/content that can't reflow if squeezed -
# a Label/Button/Checkbutton given less room than it asked for means part of
# its text is being drawn past its own box and clipped. Entry/Combobox/Scale
# are excluded on purpose: they're routinely packed with fill="x" specifically
# so they resize with the window, and Text/Scrollbar wrap+scroll instead of
# losing content.
TEXT_BEARING_CLASSES = {"TLabel", "Label", "TButton", "Button", "TCheckbutton", "Checkbutton"}

# Tk's pack() cavity math can leave a widget a pixel or two short of its
# requested size through ordinary integer rounding (e.g. a sibling with
# fill="x" claiming the remainder first) - imperceptible and not a real
# clip. Only flag squeezes bigger than this.
SQUEEZE_TOLERANCE_PX = 2

# The real warning shown on Linux when no StatusNotifierWatcher is running
# (setup.py's check_tray_prereqs) - long enough by itself to overflow the
# wizard's fixed-width window, which is exactly the kind of content this
# test is meant to catch.
REAL_TRAY_WARNING = (
    "No system tray host found on this session (nothing implements "
    "StatusNotifierWatcher), so the tray icon won't show anywhere.\n"
    "On GNOME, install and enable the \"AppIndicator and KStatusNotifierItem "
    "Support\" extension: https://extensions.gnome.org/extension/615/appindicator-support/\n"
    "KDE Plasma, XFCE, and MATE normally have a tray host built in - if you "
    "still see this there, check your panel includes a system tray widget."
)


def make_stub_controller(tray_ok=True):
    displays = [
        {
            "name": "DELL U3419W UltraWide Curved Monitor (DisplayPort-1)",
            "width": 3440, "height": 1440, "primary": True, "device_id": "0",
        },
        {
            "name": "ASUS ProArt Secondary Display (HDMI-2)",
            "width": 1920, "height": 1080, "primary": False, "device_id": "1",
        },
    ]
    audio_devices = {
        "output": [
            {
                "name": "Realtek(R) Audio Digital Output (Optical) - High Definition Audio Device",
                "device_id": "out0",
            },
            {"name": "USB Audio CODEC - Speakers", "device_id": "out1"},
        ],
        "input": [
            {
                "name": "Blue Yeti Nano Stereo Microphone (USB Audio Device)",
                "device_id": "in0",
            },
            {"name": "Built-in Microphone Array", "device_id": "in1"},
        ],
    }
    tray_result = (True, None) if tray_ok else (False, REAL_TRAY_WARNING)

    return {
        "system": "Linux",
        "obs_running": lambda: False,
        "close_obs": lambda: True,
        "obs_running_message": "OBS is running.",
        "version": "0.0.0-test",
        "default_clip_folder": "/home/test/Clips",
        "provision": lambda: True,
        "check_tray_prereqs": lambda: tray_result,
        "set_clip_folder": lambda folder: None,
        "list_displays": lambda: displays,
        "resolution_choices": lambda w, h: [(1920, 1080), (1280, 720), (854, 480)],
        "load_video_settings": lambda: {"width": 3440, "height": 1440, "fps": 60},
        "save_video_settings": lambda *a, **k: None,
        "load_capture_monitor_index": lambda: 0,
        "save_capture_monitor": lambda *a, **k: None,
        "list_audio_devices": lambda: audio_devices,
        "load_audio_devices": lambda: {"output": "out0", "input": "in0"},
        "save_audio_devices": lambda *a, **k: None,
        "ensure_mic_filter_deps": lambda: True,
        "has_mic_filter_been_configured": lambda: False,
        "load_replay_buffer_seconds": lambda: 120,
        "save_replay_buffer_seconds": lambda *a, **k: None,
        "load_recording_bitrate": lambda: 8000,
        "save_recording_bitrate": lambda *a, **k: None,
        "compute_video_bitrate_for_filesize": lambda mb, secs: 4000,
        "compute_filesize_for_bitrate": lambda kbps, secs: 42.0,
        "default_target_filesize_mb": 25,
        "fps_choices": [30, 60, 120],
        "replay_buffer_min_seconds": 15,
        "replay_buffer_max_seconds": 300,
        "replay_buffer_step_seconds": 15,
        "bitrate_min_kbps": 1000,
        "bitrate_max_kbps": 50000,
        "bitrate_step_kbps": 500,
        "install_tray": lambda: None,
        "load_clip_hotkey": lambda: (False, True, False, "~"),
        "save_clip_hotkey": lambda *a, **k: None,
        "load_clip_sound_enabled": lambda: True,
        "save_clip_sound_enabled": lambda *a, **k: None,
        "load_clip_sound_volume": lambda: 0.8,
        "load_clip_reexport": lambda: {"reexport_enabled": True, "target_mb": 10.0, "crop_seconds": 30},
        "load_split_tracks": lambda: True,
        "save_split_tracks": lambda *a, **k: None,
        "app_slot_source_id": lambda: "wasapi_process_output_capture",
        "load_app_tracks": lambda: {"expiry_days": 14, "slots": {
            "ClipEm App A": {"exe": "Spotify.exe", "label": "Spotify", "last_heard": 0.0, "pinned": True},
            "ClipEm App B": {"exe": "Discord.exe", "label": "Discord", "last_heard": 0.0, "pinned": False},
            "ClipEm App C": None,
        }},
        "set_app_slot_pinned": lambda *a, **k: None,
        "reset_app_tracks": lambda *a, **k: None,
        "losslesscut_supported": lambda: True,
        "losslesscut_installed": lambda: False,
        "losslesscut_download_mb": 181,
        "install_losslesscut": lambda *a, **k: None,
        "save_clip_reexport": lambda *args: None,
        "save_clip_sound_volume": lambda *a, **k: None,
        "replace_clip_sound_wav": lambda *a, **k: None,
        "play_clip_sound": lambda *a, **k: None,
        "load_obs_websocket_settings": lambda: {
            "port": 4455, "password": "supersecretpassword123",
        },
        "save_obs_websocket_settings": lambda *a, **k: None,
        "generate_obs_websocket_password": lambda: "abc123",
        "load_run_config": lambda: {"autostart_obs": True, "autostart_tray": True},
        "save_run_config": lambda *a, **k: None,
        "apply_boot_autostart": lambda *a, **k: None,
        # Two leftover entries, so the Autostart page is checked with its
        # "remove other autostarts" option and listing showing.
        "find_other_autostart_entries": lambda: [
            {"path": "/home/test/.config/autostart/clipem.desktop", "command": "", "this_install": False},
            {"path": "/home/test/.config/autostart/ClipEm-old.desktop", "command": "", "this_install": False},
        ],
        "describe_autostart_entry": lambda entry: (
            os.path.basename(entry["path"]) + " (starts /home/test/Games/Recording/ClipEm-previous-install)"
        ),
        "remove_autostart_entries": lambda entries: len(entries),
        "load_update_config": lambda: {"check_for_updates": True},
        "change_update_config": lambda *a, **k: None,
        "finalize": lambda: None,
        "launch_app": lambda: None,
    }


def iter_widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from iter_widgets(child)


def describe(widget):
    cls = widget.winfo_class()
    try:
        text = widget.cget("text")
    except tk.TclError:
        text = ""
    return f"{cls}({text!r})" if text else cls


def find_squeezed_widgets(root):
    """Text-bearing widgets given less room than their own text needs -
    the direct sign that some of that text is being drawn past the
    widget's box and clipped, since Tk doesn't rewrap/truncate for you.
    Descriptions are rendered immediately since the widgets themselves
    get destroyed as soon as the caller moves on to the next page."""
    root.update_idletasks()
    problems = []
    for widget in iter_widgets(root):
        if widget.winfo_class() not in TEXT_BEARING_CLASSES:
            continue
        aw, ah = widget.winfo_width(), widget.winfo_height()
        if aw <= 0 or ah <= 0:
            continue
        rw, rh = widget.winfo_reqwidth(), widget.winfo_reqheight()
        if rw - aw > SQUEEZE_TOLERANCE_PX or rh - ah > SQUEEZE_TOLERANCE_PX:
            problems.append(
                f"{describe(widget)} wants {rw}x{rh} but only got {aw}x{ah} "
                f"- its content is being clipped"
            )
    return problems


def find_out_of_bounds_widgets(root):
    """Widgets whose box isn't fully contained within root's own box -
    catches a widget placed outside the visible window entirely (e.g. via
    place() or a bad offset), which the squeeze check above wouldn't."""
    root.update_idletasks()
    win_w, win_h = root.winfo_width(), root.winfo_height()
    root_x, root_y = root.winfo_rootx(), root.winfo_rooty()
    problems = []
    for widget in iter_widgets(root):
        if widget is root or isinstance(widget, tk.Toplevel):
            continue
        w, h = widget.winfo_width(), widget.winfo_height()
        if w <= 0 or h <= 0:
            continue
        x, y = widget.winfo_rootx() - root_x, widget.winfo_rooty() - root_y
        if x < 0 or y < 0 or x + w > win_w or y + h > win_h:
            problems.append(
                f"{describe(widget)} at ({x},{y}) size {w}x{h} "
                f"falls outside the {win_w}x{win_h} window"
            )
    return problems


def format_report(problems_by_page):
    lines = []
    for page, problems in problems_by_page.items():
        lines.append(f"{page}:")
        lines.extend(f"  {problem}" for problem in problems)
    return "\n".join(lines)


@unittest.skipUnless(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"),
    "needs a real display (run under xvfb-run in CI)",
)
class GuiLayoutTest(unittest.TestCase):
    """tray_ok=False so the stub data includes the real (long) no-tray-host
    warning from setup.py - realistic content the wizard actually shows on
    plain Linux desktops, not synthetic stress data."""

    def setUp(self):
        self.controller = make_stub_controller(tray_ok=False)
        self.wizard = gui.Wizard(self.controller)

    def tearDown(self):
        try:
            self.wizard.destroy()
        except tk.TclError:
            pass

    def _check_all_pages(self):
        problems_by_page = {}
        for page in PAGES:
            getattr(self.wizard, page)()
            problems = find_squeezed_widgets(self.wizard) + find_out_of_bounds_widgets(self.wizard)
            if problems:
                problems_by_page[page] = problems
        if problems_by_page:
            self.fail("Widgets cut off / offscreen:\n" + format_report(problems_by_page))

    def test_all_pages_fit_at_default_size(self):
        self._check_all_pages()

    def test_all_pages_fit_at_declared_minimum_size(self):
        min_w, min_h = self.wizard.wm_minsize()
        self.wizard.geometry(f"{min_w}x{min_h}")
        self.wizard.update_idletasks()
        self._check_all_pages()


if __name__ == "__main__":
    unittest.main()
