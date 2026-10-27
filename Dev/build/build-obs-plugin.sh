#!/usr/bin/env bash
# Builds clipem-obs, ClipEm's OBS plugin (source: Dev/obs-plugin/), for
# Linux and Windows: it hides OBS (its tray icon, and its window when there's
# no tray to hide to) and passes every OBS error and message on to the tray
# widget. run.py loads it through OBS_PLUGINS_PATH, so it ships in the shared
# zip, not inside OBS. Written to App/App/Shared/OBS-Plugin/ (git-ignored):
#   linux/clipem-obs.so    against Dev/obs-install (build-obs-from-source-for-linux.sh)
#                          and this machine's Qt6, like that OBS itself
#   windows/clipem-obs.dll cross-compiled with clang-cl/lld-link for the
#                          official OBS release (OBS_WINDOWS_* below)
#
# Windows needs OBS's own MSVC-built Qt and the MSVC runtime, so (all cached
# in Dev/.obs-plugin-work, downloaded once and checked against sha256 where
# upstream publishes one):
#   - the MSVC CRT and Windows SDK, via `xwin` (installed with cargo). Running
#     this accepts Microsoft's license for them (xwin --accept-license).
#   - obs-deps' prebuilt Qt6, the build OBS_WINDOWS_VERSION was made with
#   - the official OBS zip, whose obs.dll/obs-frontend-api.dll the import
#     libraries are generated from (llvm-dlltool)
#
# Requirements (Arch/CachyOS): sudo pacman -S --needed clang lld llvm qt6-base
# plus rustup/cargo, curl, unzip, and a built Dev/obs-install.
#
# Usage: ./build/build-obs-plugin.sh [linux|windows]   (default: both)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_DIR="$(dirname "$SCRIPT_DIR")"
SRC="$DEV_DIR/obs-plugin/clipem-obs.cpp"
OBS_INSTALL="$DEV_DIR/obs-install"
OUT_DIR="$(dirname "$DEV_DIR")/App/App/Shared/OBS-Plugin"
WORK="$DEV_DIR/.obs-plugin-work"

# Must match the OBS the install manifest gives Windows users (release.sh).
OBS_WINDOWS_VERSION="32.2.2"
OBS_WINDOWS_URL="https://github.com/obsproject/obs-studio/releases/download/$OBS_WINDOWS_VERSION/OBS-Studio-$OBS_WINDOWS_VERSION-Windows-x64.zip"
OBS_WINDOWS_SHA256="4d6e40e3ab155f56b30de517380566a206d74b63cdf5ad49aa596924768f97e1"
# obs-studio's CMakePresets.json at that tag: dependencies.qt6.
QT_DEPS_VERSION="2026-07-15"
QT_DEPS_URL="https://github.com/obsproject/obs-deps/releases/download/$QT_DEPS_VERSION/windows-deps-qt6-$QT_DEPS_VERSION-x64.zip"
QT_DEPS_SHA256="7c7f985711d80467bdc1795b6592275a27d5b0e5a2c7a61db1f2c1d08d6a5579"
XWIN_VERSION="0.10.0"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { for tool in "$@"; do command -v "$tool" >/dev/null 2>&1 || die "$tool is required - see this script's header."; done; }

[ -d "$OBS_INSTALL/include/obs" ] || die "$OBS_INSTALL has no headers - run build/build-obs-from-source-for-linux.sh first."
mkdir -p "$WORK"

fetch() { # url sha256 dest
    if [ ! -f "$3" ] || ! echo "$2  $3" | sha256sum -c --quiet - 2>/dev/null; then
        echo "Downloading $1"
        curl -fL --retry 3 -o "$3.part" "$1"
        echo "$2  $3.part" | sha256sum -c --quiet - || die "$1 doesn't match its pinned sha256 - investigate before building."
        mv "$3.part" "$3"
    fi
}

build_linux() {
    need g++ pkg-config
    pkg-config --exists Qt6Widgets || die "Qt6Widgets isn't installed (qt6-base)."
    mkdir -p "$OUT_DIR/linux"
    # shellcheck disable=SC2046 # pkg-config's flags are meant to split
    g++ -std=c++17 -O2 -fPIC -shared -fvisibility=hidden -Wall -Wextra -Wno-unused-parameter \
        -ffile-prefix-map="$DEV_DIR"=. \
        -I"$OBS_INSTALL/include/obs" $(pkg-config --cflags Qt6Widgets) \
        -o "$OUT_DIR/linux/clipem-obs.so" "$SRC" \
        -L"$OBS_INSTALL/lib" -lobs -lobs-frontend-api $(pkg-config --libs Qt6Widgets) \
        -Wl,--as-needed -Wl,-z,defs
    strip --strip-unneeded "$OUT_DIR/linux/clipem-obs.so"
    echo "Wrote $OUT_DIR/linux/clipem-obs.so"
}

# An import library for one of OBS's DLLs, from its export table.
import_lib() { # dll name
    {
        echo "LIBRARY $2.dll"
        echo "EXPORTS"
        llvm-readobj --coff-exports "$1" | sed -n 's/^ *Name: \(.\+\)$/\1/p'
    } > "$WORK/$2.def"
    llvm-dlltool -m i386:x86-64 -d "$WORK/$2.def" -l "$WORK/lib/$2.lib"
}

build_windows() {
    need clang-cl lld-link llvm-dlltool llvm-readobj curl unzip cargo

    local xwin="$WORK/xwin-tool/bin/xwin"
    if [ ! -x "$xwin" ]; then
        cargo install xwin --version "$XWIN_VERSION" --locked --root "$WORK/xwin-tool"
    fi
    local sdk="$WORK/xwin"
    if [ ! -d "$sdk/crt/include" ]; then
        "$xwin" --accept-license --arch x86_64 --cache-dir "$WORK/xwin-cache" splat --output "$sdk"
        rm -rf "$WORK/xwin-cache"
    fi

    fetch "$QT_DEPS_URL" "$QT_DEPS_SHA256" "$WORK/qt6-$QT_DEPS_VERSION.zip"
    local qt="$WORK/qt6-$QT_DEPS_VERSION"
    if [ ! -d "$qt/include/QtWidgets" ]; then
        rm -rf "$qt"
        unzip -q "$WORK/qt6-$QT_DEPS_VERSION.zip" -d "$qt"
    fi

    fetch "$OBS_WINDOWS_URL" "$OBS_WINDOWS_SHA256" "$WORK/obs-$OBS_WINDOWS_VERSION.zip"
    local obs="$WORK/obs-$OBS_WINDOWS_VERSION"
    if [ ! -f "$obs/bin/64bit/obs.dll" ]; then
        rm -rf "$obs"
        unzip -q "$WORK/obs-$OBS_WINDOWS_VERSION.zip" 'bin/64bit/obs.dll' 'bin/64bit/obs-frontend-api.dll' -d "$obs"
    fi
    mkdir -p "$WORK/lib"
    import_lib "$obs/bin/64bit/obs.dll" obs
    import_lib "$obs/bin/64bit/obs-frontend-api.dll" obs-frontend-api

    # obs-install's headers are the same OBS version's; only the generated
    # obsconfig.h is per platform (obs-studio's cmake/windows/obsconfig.h.in).
    local inc="$WORK/include"
    rm -rf "$inc"
    cp -r "$OBS_INSTALL/include/obs" "$inc"
    cat > "$inc/obsconfig.h" <<'EOF'
#pragma once

#define OBS_DATA_PATH "../../data"
#define OBS_PLUGIN_PATH "../../obs-plugins/64bit"
#define OBS_PLUGIN_DESTINATION "obs-plugins/64bit"

#define OBS_RELEASE_CANDIDATE 0
#define OBS_BETA 0
EOF
    # util/util_uint64.h uses MSVC's _udiv128, which clang-cl's intrin.h
    # doesn't declare: the same divide, force-included ahead of it.
    cat > "$inc/clang-cl-shim.h" <<'EOF'
#pragma once
#include <intrin.h>
static inline unsigned __int64 _udiv128(unsigned __int64 high, unsigned __int64 low, unsigned __int64 divisor,
					unsigned __int64 *remainder)
{
	unsigned __int64 quotient, rem;
	__asm__("divq %4" : "=a"(quotient), "=d"(rem) : "a"(low), "d"(high), "r"(divisor));
	*remainder = rem;
	return quotient;
}
EOF

    mkdir -p "$OUT_DIR/windows" "$WORK/obj"
    # /MD like OBS. Older MSVC runtimes crash on newer STL headers' constexpr
    # std::mutex, so that's off: the user's runtime may be older than xwin's.
    clang-cl --target=x86_64-pc-windows-msvc /nologo /c /O2 /MD /EHsc /std:c++17 /Zc:__cplusplus /permissive- \
        /W3 /utf-8 -Wno-unused-parameter "/pathmap:$DEV_DIR=." \
        /DWIN32 /D_WINDOWS /DUNICODE /D_UNICODE /DNDEBUG /DQT_NO_DEBUG \
        /DQT_CORE_LIB /DQT_GUI_LIB /DQT_WIDGETS_LIB /D_DISABLE_CONSTEXPR_MUTEX_CONSTRUCTOR \
        /imsvc "$sdk/crt/include" /imsvc "$sdk/sdk/include/ucrt" /imsvc "$sdk/sdk/include/um" \
        /imsvc "$sdk/sdk/include/shared" \
        /FI"$inc/clang-cl-shim.h" /I"$inc" /I"$qt/include" /I"$qt/include/QtCore" /I"$qt/include/QtGui" /I"$qt/include/QtWidgets" \
        "/Fo$WORK/obj/clipem-obs.obj" "$SRC"
    lld-link /nologo /dll /brepro /opt:ref /opt:icf "/out:$OUT_DIR/windows/clipem-obs.dll" \
        "/libpath:$sdk/crt/lib/x86_64" "/libpath:$sdk/sdk/lib/um/x86_64" "/libpath:$sdk/sdk/lib/ucrt/x86_64" \
        "/libpath:$WORK/lib" "/libpath:$qt/lib" \
        "$WORK/obj/clipem-obs.obj" obs.lib obs-frontend-api.lib Qt6Core.lib Qt6Gui.lib Qt6Widgets.lib
    rm -f "$OUT_DIR/windows/clipem-obs.lib" "$OUT_DIR/windows/clipem-obs.exp"
    echo "Wrote $OUT_DIR/windows/clipem-obs.dll"
}

case "${1:-all}" in
    linux) build_linux ;;
    windows) build_windows ;;
    all) build_linux; build_windows ;;
    -h|--help) sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//' ;;
    *) die "unknown target '$1' (linux, windows, or nothing for both)" ;;
esac
