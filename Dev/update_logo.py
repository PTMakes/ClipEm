#!/usr/bin/env python3
"""Regenerates every derived logo asset in the repo from the two source
files (ClipEm.png, ClipEm.webp, both 1080x1080) that live next to this
script, then re-checksums App/Shared so setup.py's integrity check doesn't
flag the new files as unexpected.

Run this after replacing Dev/ClipEm.png / Dev/ClipEm.webp with new artwork:

    python3 Dev/update_logo.py

Derived assets (all regenerated, none hand-edited - re-run this instead of
touching them directly):
  - PTMClipEm-App/App/Shared/tray-widget/ClipEm.png  (tray icon source)
  - PTMClipEm-App/App/Shared/tray-widget/ClipEm.ico  (Windows shortcut icon)
  - Dev/installer/windows/ClipEm.ico                 (NSIS installer icon)
  - PTMClipEm-Website/app/icon.png                   (Next.js favicon)
  - PTMClipEm-Website/public/logo.webp               (hero logo)
"""

import os
import subprocess
import sys

from PIL import Image

DEV_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DEV_DIR)

SOURCE_PNG = os.path.join(DEV_DIR, "ClipEm.png")
SOURCE_WEBP = os.path.join(DEV_DIR, "ClipEm.webp")

APP_SHARED_DIR = os.path.join(REPO_ROOT, "PTMClipEm-App", "App", "Shared")
TRAY_WIDGET_DIR = os.path.join(APP_SHARED_DIR, "tray-widget")
NSIS_DIR = os.path.join(DEV_DIR, "installer", "windows")
WEBSITE_APP_DIR = os.path.join(REPO_ROOT, "PTMClipEm-Website", "app")
WEBSITE_PUBLIC_DIR = os.path.join(REPO_ROOT, "PTMClipEm-Website", "public")

ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
FAVICON_SIZE = (512, 512)


def _require(path):
    if not os.path.exists(path):
        raise SystemExit(f"Missing source logo file: {path}")
    return path


def write_ico(source_img, dest_path):
    source_img.save(dest_path, sizes=ICO_SIZES)
    print(f" - Wrote {os.path.relpath(dest_path, REPO_ROOT)}", flush=True)


def write_png(img, dest_path):
    img.save(dest_path)
    print(f" - Wrote {os.path.relpath(dest_path, REPO_ROOT)}", flush=True)


def copy_bytes(src, dest_path):
    with open(src, "rb") as f:
        data = f.read()
    with open(dest_path, "wb") as f:
        f.write(data)
    print(f" - Wrote {os.path.relpath(dest_path, REPO_ROOT)}", flush=True)


def main():
    _require(SOURCE_PNG)
    _require(SOURCE_WEBP)

    source_img = Image.open(SOURCE_PNG).convert("RGBA")

    print("Updating tray widget assets...", flush=True)
    write_png(source_img, os.path.join(TRAY_WIDGET_DIR, "ClipEm.png"))
    write_ico(source_img, os.path.join(TRAY_WIDGET_DIR, "ClipEm.ico"))

    print("Updating Windows NSIS installer icon...", flush=True)
    write_ico(source_img, os.path.join(NSIS_DIR, "ClipEm.ico"))

    print("Updating website assets...", flush=True)
    favicon = source_img.resize(FAVICON_SIZE, Image.LANCZOS)
    write_png(favicon, os.path.join(WEBSITE_APP_DIR, "icon.png"))
    copy_bytes(SOURCE_WEBP, os.path.join(WEBSITE_PUBLIC_DIR, "logo.webp"))

    print("Re-checksumming App/Shared...", flush=True)
    subprocess.run(
        [sys.executable, os.path.join(DEV_DIR, "generate_checksums.py"), APP_SHARED_DIR],
        check=True,
    )

    print("Done.")


if __name__ == "__main__":
    main()
