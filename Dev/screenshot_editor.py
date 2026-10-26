#!/usr/bin/env python3
"""Takes the clip editor screenshot shown on the website (Website/public/
editor-screenshot.webp) and on the setup wizard's Clip Editor page
(App/App/Shared/Installer/editor-screenshot*.png).

The real editor (tray-widget/clip_browser.py's server and page) is run on a
few generated sample clips and captured with headless Chromium - nothing of
the user's own clips, settings or caches is read or written.

  --if-changed  only retake it when the editor's page (clip_browser/*, clip_browser.py) or
                this script changed since the last screenshot - release.sh
                runs it this way. EDITOR_HASH_FILE records what it was taken of.

Needs ffmpeg, Chromium (or Chrome) and Pillow."""

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(DEV_DIR)
SHARED_DIR = os.path.join(ROOT_DIR, "App", "App", "Shared")
PAGE_DIR = os.path.join(SHARED_DIR, "tray-widget", "clip_browser")
WEBSITE_OUT = os.path.join(ROOT_DIR, "Website", "public", "editor-screenshot.webp")
# Width for each display scale - matching gui.py's EDITOR_SCREENSHOTS. 520 px
# fits under the Clip Editor page's options in the wizard's window at 100%.
INSTALLER_OUTS = {
    os.path.join(SHARED_DIR, "Installer", "editor-screenshot.png"): 520,
    os.path.join(SHARED_DIR, "Installer", "editor-screenshot@1.5x.png"): 780,
    os.path.join(SHARED_DIR, "Installer", "editor-screenshot@2x.png"): 1040,
}
EDITOR_HASH_FILE = os.path.join(DEV_DIR, "assets", "editor-screenshot.sha256")

WINDOW = (1600, 900)
SCALE = 2  # captured at 2x, so the website's copy is sharp on HiDPI screens
WEBSITE_WIDTH = 2000
SHOWN_FOLDER = "~/Videos/Clips"

# (video source, minutes past 21:00) - the newest clip is the one opened.
CLIPS = [
    ("mandelbrot=size=1280x720:rate=30:end_scale=0.05", 50),
    ("life=size=1280x720:rate=30:mold=10:life_color=#2080ec:death_color=#15151a:ratio=0.2", 41),
    ("cellauto=size=1280x720:rate=30:rule=110", 33),
    ("gradients=size=1280x720:rate=30:speed=0.02", 24),
    ("testsrc2=size=1280x720:rate=30", 12),
]
SECONDS = 20
# The newest clip's split audio tracks - its "-tracks.mkv" copy, as ClipEm
# saves it (clip_edit.SPLIT_TITLES, then each app), each shaped to look like
# what it's named. The clips themselves have the mixed track only.
MIC = "0.6*sin(2*PI*180*t)*gt(sin(2.1*t)+sin(3.3*t),0.4)*(0.5+0.5*sin(17*t))"
DESKTOP = "0.3*sin(2*PI*440*t)*(0.6+0.4*sin(1.3*t))"
GAME = "0.7*(random(0)-0.5)*exp(-3*mod(t,2.3))"
MIX = f"({MIC})*0.5+({DESKTOP})*0.5+({GAME})*0.5"
TRACKS = [("Mix", MIX), ("Mic", MIC), ("Desktop", DESKTOP), ("Game", GAME)]


def editor_hash():
    """Of what the screenshot shows: the page's files, the server that fills
    it in, and this script."""
    page = sorted(name for name in os.listdir(PAGE_DIR) if name.endswith((".html", ".css", ".js")))
    paths = [os.path.join(PAGE_DIR, name) for name in page]
    paths += [os.path.join(SHARED_DIR, "tray-widget", "clip_browser.py"), os.path.abspath(__file__)]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(os.path.basename(path).encode())
        with open(path, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


def find_chromium():
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("Chromium or Google Chrome is needed to take the editor screenshot.")


def encode(path, source, tracks, minute):
    command = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source]
    for _title, expression in tracks:
        command += ["-f", "lavfi", "-i", f"aevalsrc='{expression}':s=48000:d={SECONDS}"]
    command += ["-map", "0:v"]
    for track, (title, _expression) in enumerate(tracks):
        command += ["-map", f"{track + 1}:a", f"-metadata:s:a:{track}", f"title={title}"]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-t", str(SECONDS), path]
    subprocess.run(command, check=True)
    stamp = datetime.datetime(2026, 10, 25, 21, minute, 7).timestamp()  # the clip list shows these
    os.utime(path, (stamp, stamp))


def make_clips(folder):
    for index, (source, minute) in enumerate(CLIPS):
        name = f"Replay 2026-10-25 21-{minute:02d}-07"
        encode(os.path.join(folder, name + ".mp4"), source, TRACKS[:1], minute)
        if index == 0:
            encode(os.path.join(folder, name + "-tracks.mkv"), source, TRACKS, minute)


def capture(work, out_png):
    sys.path.insert(0, os.path.join(SHARED_DIR, "tray-widget"))
    import clip_browser

    clips = os.path.join(work, "Clips")
    os.makedirs(clips)
    make_clips(clips)
    clip_browser.page_theme = lambda: {"theme": "dark", "css": ""}  # not this machine's Settings

    send_json = clip_browser.Handler._json

    def json_with_shown_folder(handler, data, status=200):
        if isinstance(data, dict) and data.get("folder") == clips:
            data = {**data, "folder": SHOWN_FOLDER}
        return send_json(handler, data, status)

    clip_browser.Handler._json = json_with_shown_folder
    cache = os.path.join(work, "cache")
    server = clip_browser.ClipBrowserServer(
        lambda: clips,
        thumbnailer=clip_browser.Thumbnailer(cache_dir=os.path.join(cache, "thumbs")),
        editor=clip_browser.Editor(edits_dir=os.path.join(cache, "edits"), track_dir=os.path.join(cache, "tracks"),
                                   peaks_dir=os.path.join(cache, "peaks"),
                                   exports_path=os.path.join(cache, "exports.json")),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chromium = find_chromium()
    try:
        # The first load makes the thumbnails and waveforms, the second shows them all.
        for _ in range(2):
            subprocess.run([
                chromium, "--headless=new", "--no-sandbox", "--hide-scrollbars", "--mute-audio",
                f"--user-data-dir={os.path.join(work, 'profile')}", f"--window-size={WINDOW[0]},{WINDOW[1]}",
                f"--force-device-scale-factor={SCALE}", "--virtual-time-budget=15000",
                f"--screenshot={out_png}", server.launch_url,
            ], check=True, timeout=180, capture_output=True)
    finally:
        server.shutdown()


def save(png):
    from PIL import Image

    with Image.open(png) as shot:
        shot = shot.convert("RGB")
        website = shot.resize((WEBSITE_WIDTH, round(shot.height * WEBSITE_WIDTH / shot.width)), Image.LANCZOS)
        website.save(WEBSITE_OUT, "WEBP", quality=85, method=6)
        for path, width in INSTALLER_OUTS.items():
            shot.resize((width, round(shot.height * width / shot.width)), Image.LANCZOS).save(path, "PNG", optimize=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--if-changed", action="store_true")
    args = parser.parse_args()
    current = editor_hash()
    if args.if_changed and all(os.path.exists(path) for path in [WEBSITE_OUT, *INSTALLER_OUTS]):
        try:
            with open(EDITOR_HASH_FILE, encoding="utf-8") as f:
                if f.read().strip() == current:
                    print("Editor unchanged - keeping its screenshot.")
                    return
        except OSError:
            pass
    with tempfile.TemporaryDirectory(prefix="clipem-screenshot-") as work:
        png = os.path.join(work, "editor.png")
        capture(work, png)
        save(png)
    with open(EDITOR_HASH_FILE, "w", encoding="utf-8") as f:
        f.write(current + "\n")
    for path in [WEBSITE_OUT, *INSTALLER_OUTS]:
        print(f"Wrote {os.path.relpath(path, ROOT_DIR)}")


if __name__ == "__main__":
    main()
