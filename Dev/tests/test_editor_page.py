"""The clip editor's page itself (tray-widget/clip_browser/*.js), run in
ClipEm's own editor window - the pinned Electron Setup installs
(Installer/editor_window.py), running tray-widget/editor-window/ - against the
real server: what the Python tests of the server can't see, such as which file
the page actually plays. Headless on Linux (Ozone's headless platform); on
Windows it opens on the desktop, so the Windows VM runs these in the
logged-in session (Dev/test-windows.sh). Skipped where no editor window is
installed: CLIPEM_EDITOR_WINDOW can point at one (Dev/devtools.py)."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest
from conftest import SCRIPTS_DIR, TRAY_WIDGET_DIR

sys.path.insert(0, TRAY_WIDGET_DIR)
sys.path.insert(1, SCRIPTS_DIR)

import clip_browser  # noqa: E402
import replace_file  # noqa: E402

import devtools  # noqa: E402 - Dev/devtools.py

EDITOR_WINDOW = devtools.editor_window_path()
FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not (EDITOR_WINDOW and FFMPEG),
                                reason="needs ClipEm's editor window (run Setup) and ffmpeg")


@pytest.fixture
def page(tmp_path):
    """open(folder) -> DevTools on the editor's page, served from folder."""
    opened = []

    def open_page(folder):
        srv = clip_browser.ClipBrowserServer(lambda: str(folder), token="tok")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        opened.append((srv, None))
        try:
            window, tools = devtools.start_editor_window(srv.url, str(tmp_path / f"profile{len(opened)}"),
                                                         EDITOR_WINDOW)
        except RuntimeError as exc:
            pytest.fail(str(exc))
        opened.append((None, window))
        opened.append((None, tools))
        assert tools.wait_for("typeof state !== 'undefined' && state.clips.length > 0"), "the page listed no clips"
        return tools

    yield open_page
    for srv, other in reversed(opened):
        if isinstance(other, devtools.DevTools):
            other.close()
        elif other is not None:
            devtools.stop(other)
        else:
            srv.shutdown()
            srv.server_close()


def post_clip_save(folder, seconds=8, press=5):
    """A post-clip save as clip_post leaves it: the clip, cut at the press,
    and the "-extra.mkv" running on past it, with every track."""
    extra = folder / "Replay-x-extra.mkv"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s=160x90:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-map", "0:v", "-map", "1:a", "-map", "1:a",
                    "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-metadata", f"CLIPEM_MARK={press:.3f}", "-metadata:s:a:0", "title=Mix",
                    "-metadata:s:a:1", "title=Mic", "-metadata:s:a:2", "title=Desktop", str(extra)], check=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(extra), "-t", str(press), "-map", "0:v", "-map", "0:a:0",
                    "-c", "copy", str(folder / "Replay-x.mp4")], check=True)
    return "Replay-x.mp4"


def test_the_editor_plays_past_a_post_clip_press(tmp_path, page):
    """The timeline runs on to the "-extra.mkv"'s end, so that's what the
    player has to play - the clip itself stops at the press."""
    name = post_clip_save(tmp_path)
    tools = page(tmp_path)
    tools.js(f"select({json.dumps(name)})")
    assert tools.wait_for("ed.info && player.readyState >= 1 && isFinite(player.duration)"), "nothing loaded"
    timeline, mark = tools.js("[ed.info.duration, ed.info.mark]")
    assert mark == pytest.approx(5, abs=0.05) and timeline > 7.5
    assert tools.js("player.duration") == pytest.approx(timeline, abs=0.15)  # the file the editor works on
    reached = tools.js("new Promise((done) => { player.onseeked = () => done(player.currentTime);"
                       " player.currentTime = 7; setTimeout(() => done(player.currentTime), 5000); })")
    assert reached == pytest.approx(7, abs=0.1)


def test_a_plain_clip_plays_itself(tmp_path, page):
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=30:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "Replay-y.mp4")], check=True)
    tools = page(tmp_path)
    tools.js("select('Replay-y.mp4')")
    assert tools.wait_for("player.readyState >= 1 && isFinite(player.duration)")
    assert tools.js("player.currentSrc").endswith("/media/Replay-y.mp4")


@pytest.mark.parametrize("copy", ["extra", "tracks"])
def test_a_clip_opened_before_its_copy_is_finished_is_opened_again_from_it(tmp_path, page, copy):
    """The clip is listed (and may be opened - the newest is, by itself)
    before its "-extra.mkv" or "-tracks.mkv" is finished, then the clip is
    cut down to the press: the editor has to move over to the copy."""
    staging = tmp_path / "staging"
    staging.mkdir()
    folder = tmp_path / "clips"
    folder.mkdir()
    post_clip_save(staging)  # Replay-x.mp4 (cut at 5 s) and Replay-x-extra.mkv (8 s)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(staging / "Replay-x-extra.mkv"), "-map", "0:v",
                    "-map", "0:a:0", "-c", "copy", str(folder / "Replay-x.mp4")], check=True)  # as OBS saved it
    tools = page(folder)
    assert tools.wait_for("state.selected === 'Replay-x.mp4' && player.readyState >= 1"), "it didn't open"
    assert tools.js("player.currentSrc").endswith("/media/Replay-x.mp4")
    if copy == "extra":
        # As ClipEm finishes it (Scripts/replace_file.py): the editor may be reading the clip.
        replace_file.replace(staging / "Replay-x-extra.mkv", folder / "Replay-x-extra.mkv")
        replace_file.replace(staging / "Replay-x.mp4", folder / "Replay-x.mp4")  # cut at the press
        wanted, mark = "/media/Replay-x-extra.mkv", 5
    else:
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(staging / "Replay-x-extra.mkv"), "-map", "0", "-c", "copy",
                        "-metadata", "CLIPEM_MARK=", str(folder / "Replay-x-tracks.mkv")], check=True)
        wanted, mark = None, None
    # The list is polled every few seconds.
    if not tools.wait_for("ed.info && ed.info.tracks.length === 3 && player.readyState >= 1", seconds=20):
        pytest.fail("the editor stayed on the clip: " + tools.js(
            "JSON.stringify({selected: state.selected, src: player.currentSrc, clips: state.clips,"
            " tracks: ed.info && ed.info.tracks.length, status: document.getElementById('status').textContent})"))
    assert tools.js("state.selected") == "Replay-x.mp4"
    if wanted:
        assert tools.js("player.currentSrc").endswith(wanted)
        assert tools.js("ed.info.mark") == pytest.approx(mark, abs=0.05)
        assert tools.js("player.duration") == pytest.approx(tools.js("ed.info.duration"), abs=0.15)


def test_trim_and_the_full_editor_warn_of_sub_track_edits_the_mix_track_covers(tmp_path, page):
    """Mic's edits aren't heard or in the exported mix while the recorded
    Mix track is used - Trim always uses it, and going back to Trim hands
    the Full Editor's Mix / Sub-tracks choice and any solo back."""
    name = post_clip_save(tmp_path)
    tools = page(tmp_path)
    tools.js(f"select({json.dumps(name)})")
    assert tools.wait_for("ed.info && ed.info.split && ed.name === " + json.dumps(name)), "no split clip loaded"
    note = "$('discord-note').textContent"
    tools.js("setMode('trim')")
    assert "Mic" not in tools.js(note)
    tools.js("change(() => { trackOf(1).volume_db = -6; })")
    assert "Mic has edits that Trim doesn't play or export" in tools.js(note)

    tools.js("setMode('multi'); chooseSources(() => { trackOf(0).muted = false; })")
    assert "while the Mix track is used - pick Sub-tracks" in tools.js(note)
    tools.js("chooseSources(() => { trackOf(0).muted = true; })")
    assert "Mic" not in tools.js(note)

    tools.js("ed.solo.add(1); setMode('trim')")
    assert tools.js("[trackOf(0).muted, ed.sources, ed.solo.size]") == [False, "auto", 0]
    assert "Mic has edits that Trim doesn't play or export" in tools.js(note)


def test_the_filters_narrow_the_clip_list(tmp_path, page):
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=30:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "source.mp4")], check=True)
    now = time.time()
    files = {  # name: age in hours
        "Replay-2026-10-25-21-50-07-Deadlock.mp4": 1, "Replay-2026-10-25-21-50-07-Deadlock-tracks.mkv": 1,
        "Replay-2026-10-25-21-50-07-Deadlock-discord.mp4": 0.5,
        "Replay-2026-10-20-10-00-00-Sober.mp4": 24 * 5, "Replay-2026-10-20-10-00-00-Sober-small.mp4": 24 * 5,
    }
    for name, hours in files.items():
        shutil.copy(tmp_path / "source.mp4", tmp_path / name)
        os.utime(tmp_path / name, (now - hours * 3600, now - hours * 3600))
    (tmp_path / "source.mp4").unlink()
    tools = page(tmp_path)
    shown = "[...document.querySelectorAll('#clip-list .clip')].map((li) => li.dataset.name)"
    assert tools.js(shown) == ["Replay-2026-10-25-21-50-07-Deadlock-discord.mp4",
                               "Replay-2026-10-25-21-50-07-Deadlock.mp4", "Replay-2026-10-20-10-00-00-Sober.mp4"]
    assert tools.js("[...$('filter-game').options].map((o) => o.text)") == ["All", "Deadlock (2)", "Sober (1)"]
    groups = tools.js("[...$('filter-date').querySelectorAll('optgroup')].map((g) => [g.label, g.children.length])")
    assert [label for label, _count in groups] == ["Last 24 hours", "Days"]  # hours and days with clips

    def pick(control, value):
        tools.js(f"(() => {{ const c = $('{control}'); if (c.type === 'checkbox') c.checked = {json.dumps(value)};"
                 f" else c.value = {json.dumps(value)}; c.dispatchEvent(new Event('change')); }})()")

    pick("filter-game", "Sober")
    assert tools.js(shown) == ["Replay-2026-10-20-10-00-00-Sober.mp4"]
    assert tools.js("$('clip-count').textContent") == "1 of 3"
    pick("filter-game", "")
    assert tools.js("$('filter-exports').checked") is True  # exports are listed by default
    pick("filter-exports", False)
    assert tools.js(shown) == ["Replay-2026-10-25-21-50-07-Deadlock.mp4", "Replay-2026-10-20-10-00-00-Sober.mp4"]
    assert tools.js("$('clip-count').textContent") == "2 of 3"
    pick("filter-game", "Deadlock")
    assert tools.js(shown) == ["Replay-2026-10-25-21-50-07-Deadlock.mp4"]
    pick("filter-exports", True)
    assert tools.js(shown) == ["Replay-2026-10-25-21-50-07-Deadlock-discord.mp4",
                               "Replay-2026-10-25-21-50-07-Deadlock.mp4"]
    pick("filter-game", "")
    hour = tools.js("$('filter-date').querySelector('optgroup').children[0].value")
    pick("filter-date", hour)
    assert "Replay-2026-10-20-10-00-00-Sober.mp4" not in tools.js(shown)
    old_day = tools.js("[...$('filter-date').querySelectorAll('optgroup')[1].children].at(-1).value")
    pick("filter-date", old_day)
    assert tools.js(shown) == ["Replay-2026-10-20-10-00-00-Sober.mp4"]


def test_the_game_filter_lists_clips_without_a_game_as_not_detected(tmp_path, page):
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=30:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "Replay-2026-10-25-21-50-07-Deadlock.mp4")],
                   check=True)
    shutil.copy(tmp_path / "Replay-2026-10-25-21-50-07-Deadlock.mp4", tmp_path / "Replay-2026-10-25-22-00-00.mp4")
    tools = page(tmp_path)
    assert tools.js("[...$('filter-game').options].map((o) => o.text)") == ["All", "Deadlock (1)", "Not Detected (1)"]
    tools.js("(() => { const c = $('filter-game'); c.value = 'none:'; c.dispatchEvent(new Event('change')); })()")
    assert tools.js("[...document.querySelectorAll('#clip-list .clip')].map((li) => li.dataset.name)") == [
        "Replay-2026-10-25-22-00-00.mp4"]


def test_typing_a_new_name_renames_the_clip_and_its_copies(tmp_path, page):
    clip = tmp_path / "Replay-2026-10-25-21-50-07-Deadlock.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=30:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    shutil.copy(clip, tmp_path / "Replay-2026-10-25-21-50-07-Deadlock-small.mp4")
    for path in tmp_path.iterdir():
        os.utime(path, (time.time() - 3600, time.time() - 3600))
    tools = page(tmp_path)
    assert tools.wait_for("ed.info && $('out-name').value === 'Replay-2026-10-25-21-50-07-Deadlock'")
    assert tools.js("$('out-name-ext').textContent") == ".mp4"
    rename = ("(() => { const box = $('out-name'); box.focus(); box.value = %s;"
              " box.dispatchEvent(new KeyboardEvent('keydown', { key: %s })); box.dispatchEvent(new Event('change')); })()")
    tools.js(rename % (json.dumps("Replay-2026-10-25-21-50-07-Deadlock-Test"), json.dumps("Enter")))
    assert tools.wait_for("state.selected === 'Replay-2026-10-25-21-50-07-Deadlock-Test.mp4' && ed.info"), \
        tools.js("$('status').textContent")
    assert tools.js("$('out-name').value") == "Replay-2026-10-25-21-50-07-Deadlock-Test"
    assert tools.js("state.clips[0].game") == "Deadlock-Test"
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".mp4") == [
        "Replay-2026-10-25-21-50-07-Deadlock-Test-small.mp4", "Replay-2026-10-25-21-50-07-Deadlock-Test.mp4"]


def test_an_error_on_the_status_line_can_be_copied_and_reported(tmp_path, page, monkeypatch):
    started = []
    monkeypatch.setattr(clip_browser, "open_bug_report", lambda title, message: started.append((title, message)))
    post_clip_save(tmp_path)
    tools = page(tmp_path)
    assert tools.js("$('status-actions').hidden")
    tools.js("setStatus('Export failed: ffmpeg failed (analysing)', true)")
    assert not tools.js("$('status-actions').hidden")
    tools.js("$('status-copy').click()", gesture=True)
    assert tools.wait_for("$('status-copy').textContent !== 'Copy'"), "Copy did nothing"
    assert tools.js("$('status-copy').textContent") == "Copied"
    tools.js("$('status-report').click()")
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started == [("ClipEm Editor", "Export failed: ffmpeg failed (analysing)")]
    tools.js("setStatus('Exported.')")
    assert tools.js("$('status-actions').hidden")  # not for news that isn't an error


def test_an_mpeg_ts_clip_plays_from_its_preview_copy(tmp_path, page):
    """The window can't open MPEG-TS (or FLV) itself: it's sent a copy it can."""
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=30:d=3", "-f", "lavfi",
                    "-i", "sine=d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(tmp_path / "Replay-t.ts")], check=True)
    tools = page(tmp_path)
    tools.js("select('Replay-t.ts')")
    assert tools.wait_for("player.readyState >= 1 && isFinite(player.duration) || !!player.error", seconds=30)
    assert tools.js("player.error && player.error.message") is None
    assert tools.js("player.duration") == pytest.approx(3, abs=0.2) and tools.js("player.videoWidth") == 160
