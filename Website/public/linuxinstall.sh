#!/bin/bash
# ClipEm's Linux installer - also hosted as-is at https://clipem.app/linuxinstall.sh
# for `curl -fsSL https://clipem.app/linuxinstall.sh | bash`.
#
# Downloads (with sha256 verification against
# https://clipem.app/updates/install-manifest.json, whose Ed25519 release
# signature is checked first) whichever of a portable Python, OBS, and the
# shared ClipEm resources aren't already present, then hands off to the same
# App/Shared/Installer/setup.py every platform uses.
# Safe to re-run any time - every step here only fetches what's missing.
#
# Usage:
#   curl -fsSL https://clipem.app/linuxinstall.sh | bash
#   ./Setup_Linux.sh [install-dir]
set -euo pipefail

MANIFEST_URL="${CLIPEM_MANIFEST_URL:-https://clipem.app/updates/install-manifest.json}"

# Base64 Ed25519 public key the manifest must be signed with. Stamped in by
# Dev/generate-signing-key.sh - must match App/Shared/Installer/signing_public_key.txt.
CLIPEM_SIGNING_PUBLIC_KEY="qVRRlix2EQKA14sroLDWymLKDYVH0PYLClc88D5A/zA="
# Test suites only (tests/test_linux_installer.py): trust a throwaway key
# instead, so the whole script can run against a locally signed manifest.
# Anyone able to set this could equally edit this script.
if [ -n "${CLIPEM_TEST_SIGNING_PUBLIC_KEY:-}" ]; then
    echo "WARNING: CLIPEM_TEST_SIGNING_PUBLIC_KEY is set - trusting a test signing key." >&2
    CLIPEM_SIGNING_PUBLIC_KEY="$CLIPEM_TEST_SIGNING_PUBLIC_KEY"
fi

# Under `curl ... | bash` stdin is the script itself, not the keyboard -
# prompts (here and in setup.py) read from the terminal directly when there
# is one.
HAVE_TTY=0
if [ -t 0 ]; then
    HAVE_TTY=1
elif (exec </dev/tty) 2>/dev/null; then
    HAVE_TTY=2
fi

# ${BASH_SOURCE[0]} isn't a real file when this is piped straight into bash
# (`curl ... | bash`) - fall back to the current directory in that case
# rather than trying to derive a script location that doesn't exist.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    SCRIPT_DIR="$PWD"
fi

if [ -n "${1:-}" ]; then
    # Explicit install dir on the command line - use it as-is, no prompt,
    # so scripted/automated installs keep working.
    ROOT_DIR="$1"
else
    if [ -f "${SCRIPT_DIR}/App/Shared/Installer/setup.py" ]; then
        DEFAULT_DIR="$SCRIPT_DIR"   # existing checkout/distribution - install in place
    elif [ -f "${SCRIPT_DIR}/ClipEm/App/Shared/Installer/setup.py" ]; then
        DEFAULT_DIR="$SCRIPT_DIR/ClipEm"   # existing checkout/distribution - install in place
    else
        DEFAULT_DIR="${SCRIPT_DIR}/ClipEm"   # fresh install - create a new folder
    fi

    if [ "$HAVE_TTY" != "0" ]; then
        read -r -p "Install directory [$DEFAULT_DIR]: " ROOT_DIR </dev/tty
        ROOT_DIR="${ROOT_DIR:-$DEFAULT_DIR}"
    else
        # No terminal at all (e.g. a scripted install) - can't prompt, so
        # fall back to the auto-detected default silently.
        ROOT_DIR="$DEFAULT_DIR"
    fi
fi

mkdir -p "$ROOT_DIR"
ROOT_DIR="$(cd "$ROOT_DIR" && pwd)"

APP_DIR="${ROOT_DIR}/App"
echo "ClipEm install directory: $ROOT_DIR"

# --- 1. The install manifest ----------------------------------------------
# Only fetched once something actually has to be downloaded (ensure_manifest),
# so the offline zip - which already has everything - installs without
# internet. manifest_field() below (used for OBS/shared) parses it with
# Python for robustness, but that's not available yet for the entry Python
# itself needs, hence manifest_linux_python_field()'s plain-text scan instead.

MANIFEST_JSON=""
MANIFEST_SIG=""
trap 'rm -f "$MANIFEST_JSON" "$MANIFEST_SIG"' EXIT

ensure_manifest() {
    if [ -n "$MANIFEST_JSON" ]; then
        return
    fi
    MANIFEST_JSON="$(mktemp)"
    MANIFEST_SIG="$(mktemp)"
    echo "Fetching install manifest ($MANIFEST_URL)..."
    curl -fsSL "$MANIFEST_URL" -o "$MANIFEST_JSON"
    curl -fsSL "$MANIFEST_URL.sig" -o "$MANIFEST_SIG"
    verify_manifest_signature
}

verify_manifest_signature() {
    # Everything below trusts the sha256 values in the manifest, so the
    # manifest itself must first be proven to come from a ClipEm release.
    if [ -z "$CLIPEM_SIGNING_PUBLIC_KEY" ]; then
        echo "This installer has no release signing key configured - refusing to continue." >&2
        exit 1
    fi
    if ! command -v openssl >/dev/null 2>&1; then
        echo "openssl (3.0 or newer) is needed to verify ClipEm's release signature." >&2
        echo "Install it with your package manager and re-run." >&2
        exit 1
    fi
    local pub_der sig_bin ok=0
    pub_der="$(mktemp)"
    sig_bin="$(mktemp)"
    # Raw 32-byte Ed25519 key -> DER SubjectPublicKeyInfo (fixed 12-byte prefix).
    { printf '\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00'; printf '%s' "$CLIPEM_SIGNING_PUBLIC_KEY" | base64 -d; } > "$pub_der"
    if base64 -d "$MANIFEST_SIG" > "$sig_bin" 2>/dev/null \
        && openssl pkeyutl -verify -pubin -keyform DER -inkey "$pub_der" -rawin \
            -in "$MANIFEST_JSON" -sigfile "$sig_bin" >/dev/null 2>&1; then
        ok=1
    fi
    rm -f "$pub_der" "$sig_bin"
    if [ "$ok" != "1" ]; then
        echo "The install manifest's release signature is INVALID (or openssl is older than 3.0)." >&2
        echo "Refusing to download anything it lists." >&2
        exit 1
    fi
    echo "  release signature OK"
}

manifest_linux_python_field() {
    # $1 = field (url/sha256). Relies on generate_install_manifest.py's fixed
    # key order (version, windows, linux; obs, python, shared within each).
    awk -v field="\"$1\"" '
        /"linux": \{/ { in_linux=1 }
        in_linux && /"python": \{/ { in_python=1; next }
        in_linux && in_python && $0 ~ field {
            line=$0
            sub(/^[^:]*:[ ]*"/, "", line)
            sub(/",?$/, "", line)
            print line
            exit
        }
    ' "$MANIFEST_JSON"
}

sha256_verify() {
    local file="$1" expected="$2" actual
    actual="$(sha256sum "$file" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
        echo "Checksum mismatch for $file" >&2
        echo "  expected: $expected" >&2
        echo "  actual:   $actual" >&2
        echo "The download may be corrupted or tampered with - aborting." >&2
        exit 1
    fi
    echo "  sha256 OK"
}

# --- 2. Portable Python ----------------------------------------------------
# Always ClipEm's own (python-build-standalone, with an Xft-enabled Tk - see
# Dev/repack-linux-python-xft.sh), never the system python3: that one may be
# too old, lack tkinter entirely (so no graphical wizard), or change version
# under the tray widget's compiled dependencies with the next distro upgrade.

PYTHON_BIN="${APP_DIR}/Linux/python/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "Fetching ClipEm's portable Python (one-time, ~30MB, stays inside App/Linux/python)..."
    ensure_manifest
    URL="$(manifest_linux_python_field url)"
    EXPECTED_SHA256="$(manifest_linux_python_field sha256)"

    mkdir -p "${APP_DIR}/Linux"
    TMP_PY="$(mktemp)"
    curl -L --fail -o "$TMP_PY" "$URL"
    sha256_verify "$TMP_PY" "$EXPECTED_SHA256"
    tar -xzf "$TMP_PY" -C "${APP_DIR}/Linux"
    rm -f "$TMP_PY"
fi
echo "Using Python: $PYTHON_BIN"

# --- 3. manifest_field()/fetch_and_verify(), now that Python is available -

manifest_field() {
    # $1 = artifact (obs/python/shared), $2 = field (url/sha256)
    "$PYTHON_BIN" -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data["linux"][sys.argv[2]][sys.argv[3]])
' "$MANIFEST_JSON" "$1" "$2"
}

fetch_and_verify() {
    # $1 = artifact (obs/shared/ffmpeg), $2 = destination file path
    local url sha256
    ensure_manifest
    url="$(manifest_field "$1" url)"
    sha256="$(manifest_field "$1" sha256)"
    echo "Downloading $url ..."
    curl -fL --progress-bar -o "$2" "$url"
    sha256_verify "$2" "$sha256"
}

# --- 4. OBS - ClipEm's own self-contained Linux build ----------------------
# Always this one, even if OBS is installed system-wide: ClipEm never runs a
# system OBS.

OBS_FRESHLY_FETCHED=0
if [ ! -x "${APP_DIR}/Linux/obs-install/bin/obs" ]; then
    echo "Fetching OBS (self-contained Linux build)..."
    TMP_OBS="$(mktemp)"
    fetch_and_verify obs "$TMP_OBS"
    mkdir -p "${APP_DIR}/Linux"
    tar -xzf "$TMP_OBS" -C "${APP_DIR}/Linux"
    rm -f "$TMP_OBS"
    OBS_FRESHLY_FETCHED=1
fi

# --- 4b. ffmpeg - ClipEm's own static build (clip re-export) --------------

if [ ! -x "${APP_DIR}/Linux/ffmpeg/bin/ffmpeg" ]; then
    echo "Fetching ffmpeg (used to save smaller copies of clips)..."
    TMP_FFMPEG="$(mktemp)"
    fetch_and_verify ffmpeg "$TMP_FFMPEG"
    mkdir -p "${APP_DIR}/Linux"
    tar -xzf "$TMP_FFMPEG" -C "${APP_DIR}/Linux"
    rm -f "$TMP_FFMPEG"
fi

# --- 5. Shared ClipEm resources (config/scripts/tray-widget/setup logic) -

if [ ! -f "${APP_DIR}/Shared/Installer/setup.py" ]; then
    echo "Fetching shared ClipEm resources..."
    TMP_SHARED="$(mktemp)"
    fetch_and_verify shared "$TMP_SHARED"
    mkdir -p "$APP_DIR"
    if command -v unzip >/dev/null 2>&1; then
        unzip -q -o "$TMP_SHARED" -d "$APP_DIR"
    else
        "$PYTHON_BIN" -c "import zipfile, sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$TMP_SHARED" "$APP_DIR"
    fi
    rm -f "$TMP_SHARED"
fi

# Baseline hashes for a freshly-fetched OBS build, the same way
# setup_windows()/setup_macos() do for their own fresh OBS fetch - see
# record_local_checksums() in setup.py for why (nothing committed to
# compare a freshly-fetched binary against otherwise). Needs setup.py,
# which is only guaranteed present after the shared-resources fetch above.
if [ "$OBS_FRESHLY_FETCHED" = "1" ]; then
    "$PYTHON_BIN" -c '
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "Shared", "Installer"))
import setup
setup.record_local_checksums([os.path.join(sys.argv[1], "Linux", "obs-install")], "Linux OBS")
' "$APP_DIR"
fi

# --- 6. Hand off to the real cross-platform setup logic -------------------

# exec replaces this process without running the EXIT trap above, so clean
# up the manifest temp file manually first.
rm -f "$MANIFEST_JSON" "$MANIFEST_SIG"
if [ "$HAVE_TTY" = "2" ]; then
    exec "$PYTHON_BIN" "${APP_DIR}/Shared/Installer/setup.py" </dev/tty
fi
exec "$PYTHON_BIN" "${APP_DIR}/Shared/Installer/setup.py"
