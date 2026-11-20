#!/usr/bin/env python3
"""Puts the logo artwork in Dev/assets (ClipEm.png, ClipEm.webp,
ClipEmSettings.png, ClipEmEditor.png, NewIcon.png) everywhere it ships, then
re-checksums App/Shared so setup.py's integrity check doesn't flag the new
files as unexpected.

Run this after replacing any of them with new artwork:

    python3 Dev/update_logo.py

The PNGs and the WebP are copied byte for byte - never re-encoded, resized
or touched up - so what ships is exactly the artwork in Dev/assets:
  - App/App/Shared/tray-widget/ClipEm.png          (tray icon source)
  - App/App/Shared/tray-widget/ClipEmSettings.png  (Settings window)
  - App/App/Shared/tray-widget/ClipEmEditor.png    (clip editor window)
  - App/App/Shared/tray-widget/NewIcon.png         (the "new" badge)
  - Website/app/icon.png                           (Next.js favicon)
  - Website/public/logo.webp                       (hero logo)

Windows needs .ico files, which can't hold the artwork as is (256px at
most), so these are built from the PNGs - the only files this makes rather
than copies:
  - App/App/Shared/tray-widget/ClipEm.ico          (Windows shortcut icon)
  - App/App/Shared/tray-widget/ClipEmSettings.ico
  - App/App/Shared/tray-widget/ClipEmEditor.ico
  - Dev/installer/windows/ClipEm.ico               (NSIS installer icon)
"""

import os
import shutil
import subprocess
import sys

from PIL import Image

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DEV_DIR)
ASSETS_DIR = os.path.join(DEV_DIR, "assets")

APP_SHARED_DIR = os.path.join(REPO_ROOT, "App", "App", "Shared")
TRAY_WIDGET_DIR = os.path.join(APP_SHARED_DIR, "tray-widget")
NSIS_DIR = os.path.join(DEV_DIR, "installer", "windows")
WEBSITE_APP_DIR = os.path.join(REPO_ROOT, "Website", "app")
WEBSITE_PUBLIC_DIR = os.path.join(REPO_ROOT, "Website", "public")

# (source in Dev/assets, destination) - copied byte for byte.
COPIES = [
    ("ClipEm.png", os.path.join(TRAY_WIDGET_DIR, "ClipEm.png")),
    ("ClipEmSettings.png", os.path.join(TRAY_WIDGET_DIR, "ClipEmSettings.png")),
    ("ClipEmEditor.png", os.path.join(TRAY_WIDGET_DIR, "ClipEmEditor.png")),
    ("NewIcon.png", os.path.join(TRAY_WIDGET_DIR, "NewIcon.png")),
    ("ClipEm.png", os.path.join(WEBSITE_APP_DIR, "icon.png")),
    ("ClipEm.webp", os.path.join(WEBSITE_PUBLIC_DIR, "logo.webp")),
]
# (source in Dev/assets, destination) - .ico files built from the PNG.
ICONS = [
    ("ClipEm.png", os.path.join(TRAY_WIDGET_DIR, "ClipEm.ico")),
    ("ClipEmSettings.png", os.path.join(TRAY_WIDGET_DIR, "ClipEmSettings.ico")),
    ("ClipEmEditor.png", os.path.join(TRAY_WIDGET_DIR, "ClipEmEditor.ico")),
    ("ClipEm.png", os.path.join(NSIS_DIR, "ClipEm.ico")),
]

ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _source(name):
    path = os.path.join(ASSETS_DIR, name)
    if not os.path.exists(path):
        raise SystemExit(f"Missing source logo file: {path}")
    return path


def _wrote(dest_path):
    print(f" - Wrote {os.path.relpath(dest_path, REPO_ROOT)}", flush=True)


def main():
    # Check every source first, so a missing one changes nothing.
    for name, _ in COPIES + ICONS:
        _source(name)

    print("Copying artwork...", flush=True)
    for name, dest_path in COPIES:
        shutil.copyfile(_source(name), dest_path)
        _wrote(dest_path)

    print("Building Windows icons...", flush=True)
    for name, dest_path in ICONS:
        with Image.open(_source(name)) as img:
            img.save(dest_path, sizes=ICO_SIZES)
        _wrote(dest_path)

    print("Re-checksumming App/Shared...", flush=True)
    subprocess.run(
        [sys.executable, os.path.join(DEV_DIR, "release", "generate_checksums.py"), APP_SHARED_DIR],
        check=True,
    )

    print("Done.")


if __name__ == "__main__":
    main()
