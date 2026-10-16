#!/usr/bin/env bash
# Rebuilds the astral-sh python-build-standalone portable Linux Python's
# Tcl/Tk with Xft (fontconfig/freetype) support enabled.
#
# The stock PBS Linux release builds Tcl/Tk with --disable-xft for maximum
# portability (no runtime dependency on X11/fontconfig/freetype). Without
# Xft, tkinter falls back to legacy X core bitmap fonts that render at a
# fixed pixel size and ignore the display's DPI scaling entirely - this is
# why ClipEmSetupLinux.sh's setup wizard (App/Shared/Installer/gui.py) shows
# noticeably smaller text than the same wizard run under a system Python
# (whose Tk is normally Xft-enabled). This script re-links Tcl/Tk against
# system libX11/libXft/fontconfig/freetype instead - a safe assumption here
# since anything running this GUI wizard already has a working X display,
# and those libraries are ubiquitous on any Linux desktop.
#
# Which Tcl/Tk gets built depends on what the PBS release bundles:
#
#   - Tcl/Tk 8.6 (PBS releases before 20260113): a pinned, newer 8.6 release
#     (TCLTK_86_VERSION below), replacing Tcl and Tk wholesale. PBS bundled
#     8.6.14, whose Xft font backend has a point-size/DPI scaling bug where
#     explicit point sizes (e.g. gui.py's font=("", 16, "bold")) render at
#     roughly 1.5-2x the requested size and TkDefaultFont resolves to the
#     wrong nominal size entirely - confirmed fixed as of 8.6.16. Since that
#     differs from the bundled patch version, the tcl8.6/tk8.6/tcl8
#     script-library trees get replaced too (init.tcl checks for an exact
#     version match).
#   - Tcl/Tk 9.x (PBS 20260113 and later): Xft is purely a Tk build option,
#     so only Tk is rebuilt - at exactly the version PBS bundles - and only
#     libtcl9tk<ver>.so and the tk<ver> script tree are swapped. PBS's own Tcl
#     (and the Thread/itcl packages built with it) stay as shipped. Both are
#     built with ZipFS disabled, like PBS's: _tkinter finds the script
#     libraries through TCL_LIBRARY, not an archive appended to the .so
#     (which `strip` below would destroy anyway).
#
# Usage: ./build/repack-linux-python-xft.sh <cpython-version> <pbs-tag>
#   e.g. ./build/repack-linux-python-xft.sh 3.12.14 20260924
#
# The vanilla asset's sha256 is pinned in release/upstream-pins.json; the first time
# a new PBS release is used, the pin is taken from that release's own
# published SHA256SUMS (and re-checked against it on every later run).
#
# Output: Dev/dist/cpython-<version>+<tag>-x86_64-unknown-linux-gnu-install_only-xft.tar.gz
# Safe to re-run - skips all work if that file already exists (delete it,
# or the whole Dev/dist entry, to force a rebuild).
set -euo pipefail

PY_VERSION="${1:?Usage: $0 <cpython-version> <pbs-tag>}"
PBS_TAG="${2:?Usage: $0 <cpython-version> <pbs-tag>}"

# Only used for PBS releases that bundle Tcl/Tk 8.6 - see the note above.
# Bump this only after confirming a newer 8.6.x release still renders
# explicit point sizes and TkDefaultFont correctly.
TCLTK_86_VERSION="8.6.16"

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$DEV_DIR/dist"
PINS_FILE="$DEV_DIR/release/upstream-pins.json"
WORK_DIR="$DEV_DIR/.linux-python-xft-work"
mkdir -p "$DIST_DIR"

ASSET="cpython-${PY_VERSION}+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
VANILLA_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${ASSET}"
OUTPUT_TAR="$DIST_DIR/cpython-${PY_VERSION}+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only-xft.tar.gz"

if [[ -f "$OUTPUT_TAR" ]]; then
    echo "Xft-enabled Linux Python already built: $OUTPUT_TAR (delete it to force a rebuild)"
    exit 0
fi

pin_lookup() {
    python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ''))" "$PINS_FILE" "$1"
}

pin_record() {
    python3 -c "
import json, sys
sys.path.insert(0, sys.argv[4])
from release_common import write_json_atomic
path, url, sha = sys.argv[1], sys.argv[2], sys.argv[3]
pins = json.load(open(path))
pins[url] = sha
write_json_atomic(path, pins)
" "$PINS_FILE" "$1" "$2" "$DEV_DIR/release"
}

VANILLA_SHA256="$(pin_lookup "$VANILLA_URL")"
PUBLISHED_SHA256="$(curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/SHA256SUMS" \
    | awk -v asset="$ASSET" '$2 == asset {print $1}')"
if [[ -z "$PUBLISHED_SHA256" ]]; then
    echo "Could not find $ASSET in the PBS $PBS_TAG release's SHA256SUMS - check the version/tag." >&2
    exit 1
fi
if [[ -z "$VANILLA_SHA256" ]]; then
    echo "Pinning $ASSET to its published sha256 ($PUBLISHED_SHA256)"
    pin_record "$VANILLA_URL" "$PUBLISHED_SHA256"
    VANILLA_SHA256="$PUBLISHED_SHA256"
elif [[ "$VANILLA_SHA256" != "$PUBLISHED_SHA256" ]]; then
    echo "Pinned sha256 for $ASSET ($VANILLA_SHA256) doesn't match PBS's published SHA256SUMS ($PUBLISHED_SHA256)." >&2
    exit 1
fi

echo "=== Fetching vanilla portable Linux Python ($ASSET) ==="
VANILLA_TAR="$DIST_DIR/$ASSET"
if [[ ! -f "$VANILLA_TAR" ]]; then
    curl -fL --progress-bar -o "$VANILLA_TAR" "$VANILLA_URL"
fi
actual_sha256="$(sha256sum "$VANILLA_TAR" | awk '{print $1}')"
if [[ "$actual_sha256" != "$VANILLA_SHA256" ]]; then
    echo "Checksum mismatch for $VANILLA_TAR (expected $VANILLA_SHA256, got $actual_sha256)" >&2
    exit 1
fi

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/python-tree"
tar -xzf "$VANILLA_TAR" -C "$WORK_DIR/python-tree"
PYTHON_LIB_DIR="$WORK_DIR/python-tree/python/lib"

if [[ -f "$PYTHON_LIB_DIR/libtcl8.6.so" ]]; then
    TCL_SERIES="8.6"
    BUNDLED_TCLTK_VERSION="$(strings "$PYTHON_LIB_DIR/libtcl8.6.so" | grep -oE '^8\.6\.[0-9]+$' | sort -uV | tail -1)"
    TCLTK_VERSION="$TCLTK_86_VERSION"
    TK_LIB="libtk8.6.so"
    echo "PBS-bundled Tcl/Tk version: ${BUNDLED_TCLTK_VERSION:-unknown} (being replaced with $TCLTK_VERSION)"
else
    TCL_LIB_FILE="$(cd "$PYTHON_LIB_DIR" && ls libtcl9.*.so 2>/dev/null | grep -v tk | head -1 || true)"
    if [[ -z "$TCL_LIB_FILE" ]]; then
        echo "Found neither libtcl8.6.so nor libtcl9.x.so in $PYTHON_LIB_DIR - has PBS's layout changed?" >&2
        exit 1
    fi
    TCL_SERIES="${TCL_LIB_FILE#libtcl}"
    TCL_SERIES="${TCL_SERIES%.so}"
    BUNDLED_TCLTK_VERSION="$(strings "$PYTHON_LIB_DIR/$TCL_LIB_FILE" | grep -oE "^${TCL_SERIES//./\\.}\.[0-9]+\$" | sort -uV | tail -1)"
    if [[ -z "$BUNDLED_TCLTK_VERSION" ]]; then
        echo "Couldn't read the bundled Tcl version from $TCL_LIB_FILE" >&2
        exit 1
    fi
    TCLTK_VERSION="$BUNDLED_TCLTK_VERSION"
    TK_LIB="libtcl${TCL_SERIES%%.*}tk${TCL_SERIES}.so"
    if [[ ! -f "$PYTHON_LIB_DIR/$TK_LIB" ]]; then
        echo "Expected $TK_LIB next to $TCL_LIB_FILE in $PYTHON_LIB_DIR - has PBS's layout changed?" >&2
        exit 1
    fi
    echo "PBS-bundled Tcl/Tk version: $BUNDLED_TCLTK_VERSION (rebuilding its Tk with Xft)"
fi

echo
echo "=== Fetching Tcl/Tk $TCLTK_VERSION source ==="
TCL_URL="https://prdownloads.sourceforge.net/tcl/tcl${TCLTK_VERSION}-src.tar.gz"
TK_URL="https://prdownloads.sourceforge.net/tcl/tk${TCLTK_VERSION}-src.tar.gz"
TCL_TAR="$DIST_DIR/tcl${TCLTK_VERSION}-src.tar.gz"
TK_TAR="$DIST_DIR/tk${TCLTK_VERSION}-src.tar.gz"

fetch_pinned_src() {
    local url="$1" dest="$2" expected actual
    expected="$(pin_lookup "$url")"
    if [[ ! -f "$dest" ]]; then
        curl -fL --progress-bar -o "$dest" "$url"
    fi
    actual="$(sha256sum "$dest" | awk '{print $1}')"
    if [[ -z "$expected" ]]; then
        echo "  no pin yet for $url - recording sha256 $actual"
        pin_record "$url" "$actual"
    elif [[ "$actual" != "$expected" ]]; then
        echo "Checksum mismatch for $dest (expected $expected, got $actual)" >&2
        exit 1
    else
        echo "  sha256 OK"
    fi
}

fetch_pinned_src "$TCL_URL" "$TCL_TAR"
fetch_pinned_src "$TK_URL" "$TK_TAR"

tar -xzf "$TCL_TAR" -C "$WORK_DIR"
tar -xzf "$TK_TAR" -C "$WORK_DIR"

BUILD_PREFIX="$WORK_DIR/build-out"
# Where libX11 lives varies by distro (/usr/lib on Arch, a multiarch
# subdirectory on Debian/Ubuntu) - ask pkg-config rather than assuming.
X11_LIB_DIR="$(pkg-config --variable=libdir x11 2>/dev/null || echo /usr/lib)"
X11_INCLUDE_DIR="$(pkg-config --variable=includedir x11 2>/dev/null || echo /usr/include)"
mkdir -p "$BUILD_PREFIX"

# Tcl 9 defaults to appending its script library to the .so as a ZipFS
# archive; PBS builds without it (see the note at the top).
# Tcl 9's full `make install` also configures and builds its bundled
# packages (Thread, itcl, ...), which don't take --disable-zipfs - and aren't
# needed: only Tk is swapped in for 9.x, so PBS's own copies stay. Just the
# core (library, scripts, headers and tclConfig.sh) is built to compile Tk
# against.
#
# The 9.x Tk also must not pick up optional libraries from the build machine
# that users may not have: the system libtommath (Tcl bundles its own), CUPS
# (Tk 9's print support - unused) and XScreenSaver (`tk inactive` - unused),
# nor an rpath pointing into this work folder. Checked after the build.
TCL_CONFIGURE=()
TK_CONFIGURE=()
TCL_MAKE=(make -j"$(nproc)")
TCL_INSTALL=(make install)
if [[ "$TCL_SERIES" != "8.6" ]]; then
    TCL_CONFIGURE=(--disable-zipfs --disable-rpath --without-system-libtommath)
    TK_CONFIGURE=(--disable-zipfs --disable-rpath --disable-libcups --disable-xss)
    TCL_MAKE=(make -j"$(nproc)" binaries libraries)
    TCL_INSTALL=(make install-binaries install-libraries install-headers)
fi

echo
echo "=== Building Tcl $TCLTK_VERSION ==="
(cd "$WORK_DIR/tcl${TCLTK_VERSION}/unix" && \
    ./configure --prefix="$BUILD_PREFIX" --enable-shared --enable-threads "${TCL_CONFIGURE[@]}" && \
    "${TCL_MAKE[@]}" && "${TCL_INSTALL[@]}")

echo
echo "=== Building Tk $TCLTK_VERSION with Xft enabled ==="
(cd "$WORK_DIR/tk${TCLTK_VERSION}/unix" && \
    ./configure --prefix="$BUILD_PREFIX" --enable-shared --enable-threads "${TK_CONFIGURE[@]}" \
        --with-tcl="$BUILD_PREFIX/lib" --x-includes="$X11_INCLUDE_DIR" --x-libraries="$X11_LIB_DIR" && \
    make -j"$(nproc)" && make install)

if ! ldd "$BUILD_PREFIX/lib/$TK_LIB" | grep -q libXft; then
    echo "Built $TK_LIB does not link against libXft - Xft support did not actually get enabled" >&2
    exit 1
fi

# Everything the shipped Tk links must be on any Linux desktop that can show
# the wizard at all: X11/Xft/fontconfig (the point of this build), zlib and
# libc. Anything else is a build-machine library that slipped in.
if [[ "$TCL_SERIES" != "8.6" ]]; then
    unexpected="$(objdump -p "$BUILD_PREFIX/lib/$TK_LIB" | awk '/NEEDED/ {print $2}' \
        | grep -vE '^lib(Xft|fontconfig|freetype|X11|Xext|Xrender|z|m|c|dl|pthread)\.so\.[0-9]+$' || true)"
    if [[ -n "$unexpected" ]]; then
        echo "Built $TK_LIB links libraries users may not have:" $unexpected >&2
        exit 1
    fi
    if objdump -p "$BUILD_PREFIX/lib/$TK_LIB" | grep -qE 'RPATH|RUNPATH'; then
        echo "Built $TK_LIB has an rpath into the build folder" >&2
        exit 1
    fi
fi

echo
if [[ "$TCL_SERIES" == "8.6" ]]; then
    echo "=== Swapping in the Xft-enabled Tcl/Tk $TCLTK_VERSION (libraries + script trees) ==="
    chmod u+w "$BUILD_PREFIX/lib/libtcl8.6.so" "$BUILD_PREFIX/lib/libtk8.6.so"
    strip --strip-unneeded "$BUILD_PREFIX/lib/libtcl8.6.so" "$BUILD_PREFIX/lib/libtk8.6.so"
    cp "$BUILD_PREFIX/lib/libtcl8.6.so" "$PYTHON_LIB_DIR/libtcl8.6.so"
    cp "$BUILD_PREFIX/lib/libtk8.6.so" "$PYTHON_LIB_DIR/libtk8.6.so"
    # Full script-library trees, not just the .so files - init.tcl requires an
    # exact version match ("package require -exact Tcl $BUNDLED_TCLTK_VERSION")
    # and would refuse to load a same-name but different-version tree otherwise.
    rm -rf "$PYTHON_LIB_DIR/tcl8.6" "$PYTHON_LIB_DIR/tk8.6" "$PYTHON_LIB_DIR/tcl8"
    cp -r "$BUILD_PREFIX/lib/tcl8.6" "$PYTHON_LIB_DIR/tcl8.6"
    cp -r "$BUILD_PREFIX/lib/tk8.6" "$PYTHON_LIB_DIR/tk8.6"
    cp -r "$BUILD_PREFIX/lib/tcl8" "$PYTHON_LIB_DIR/tcl8"
else
    echo "=== Swapping in the Xft-enabled Tk $TCLTK_VERSION (library + tk$TCL_SERIES script tree) ==="
    chmod u+w "$BUILD_PREFIX/lib/$TK_LIB"
    strip --strip-unneeded "$BUILD_PREFIX/lib/$TK_LIB"
    cp "$BUILD_PREFIX/lib/$TK_LIB" "$PYTHON_LIB_DIR/$TK_LIB"
    rm -rf "$PYTHON_LIB_DIR/tk$TCL_SERIES"
    cp -r "$BUILD_PREFIX/lib/tk$TCL_SERIES" "$PYTHON_LIB_DIR/tk$TCL_SERIES"
fi

if [[ -n "${DISPLAY:-}" ]]; then
    echo
    echo "=== Verifying the repacked interpreter against the live display ==="
    "$WORK_DIR/python-tree/python/bin/python3" -c "
import tkinter as tk
import tkinter.font as tkfont
root = tk.Tk()
scaling = root.tk.call('tk', 'scaling')
lbl = tk.Label(root, text='x')
family = lbl.tk.call('font', 'actual', lbl.cget('font'), '-family')
assert family != 'fixed', 'still resolving to the legacy X core bitmap font - Xft did not take effect'
# The actual bug this guards against: PBS's bundled Tcl/Tk 8.6.14 resolves an
# explicit point size (e.g. gui.py's font=(\"\", 16, \"bold\")) to a font
# roughly 1.5-2x too big under Xft - catch any regression of that here
# rather than only in a human looking at the wizard.
requested = tkfont.Font(font=('', 16, 'bold'))
actual_size = requested.actual('size')
root.destroy()
assert actual_size == 16, f'requested a 16pt font but Tk resolved it to {actual_size}pt - the 8.6.14 size-inflation bug is back'
print(f'OK - tk scaling={scaling}, font family={family}, 16pt request resolved to {actual_size}pt')
"
else
    echo "No DISPLAY set - skipping live tkinter verification (ldd already confirmed Xft linkage above)"
fi

echo
echo "=== Repacking as $OUTPUT_TAR ==="
tar -czf "$OUTPUT_TAR" -C "$WORK_DIR/python-tree" python

rm -rf "$WORK_DIR"
echo "Wrote $OUTPUT_TAR"
