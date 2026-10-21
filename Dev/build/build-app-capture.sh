#!/usr/bin/env bash
# Builds clipem-app-capture.exe, the helper the tray widget records each app's
# sound with on Windows (App/Shared/tray-widget/app_recorder.py): WASAPI
# process loopback, Windows 10 2004+. Source: Dev/app-capture/main.cpp.
#
# Cross-compiled with mingw-w64 and fully static. Written to
# App/App/Shared/Tools/ (git-ignored), so release.sh's shared zip and
# checksums.json include it.
#
# Requirements (Arch/CachyOS): sudo pacman -S --needed mingw-w64-gcc
#
# Usage: ./build/build-app-capture.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$(dirname "$DEV_DIR")/App/App/Shared/Tools"
CXX="x86_64-w64-mingw32-g++"

command -v "$CXX" >/dev/null 2>&1 || { echo "ERROR: $CXX is required (mingw-w64-gcc)" >&2; exit 1; }

mkdir -p "$OUT_DIR"
# Paths are kept out of the binary (-ffile-prefix-map), so it carries nothing about this machine.
"$CXX" -std=c++17 -O2 -municode -static -static-libgcc -static-libstdc++ \
  -Wl,--no-insert-timestamp -ffile-prefix-map="$DEV_DIR"=. \
  -o "$OUT_DIR/clipem-app-capture.exe" "$DEV_DIR/app-capture/main.cpp" \
  -lole32 -luuid -lmmdevapi
x86_64-w64-mingw32-strip "$OUT_DIR/clipem-app-capture.exe"
echo "Wrote $OUT_DIR/clipem-app-capture.exe"
