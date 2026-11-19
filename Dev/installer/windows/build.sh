#!/usr/bin/env bash
# Compiles ClipEmSetup.nsi -> dist/ClipEmSetupWindows.exe.
#
# Usage: ./build.sh <version>      (release.sh calls this with the release version)
#        ./build.sh --help         show this help and exit
#
# Bundles, into the installer, everything bootstrap.ps1 needs to verify a
# release before downloading it (see bootstrap.ps1's header):
#   build/ed25519_verify.py        - from App
#   build/signing_public_key.txt   - from App (must be set - see
#                                    Dev/generate-signing-key.sh)
#   build/bootstrap-pins.json      - URL + pinned sha256 of the Windows
#                                    Python (python-build-standalone's, with
#                                    Tcl/Tk and pip - setup.py's
#                                    WINDOWS_PYTHON_VERSION/_PBS_TAG, upstream-pins.json)
#   build/vcruntime.zip            - the VC++ runtime DLLs OBS needs, from a
#                                    pinned VC_redist.x64.exe (extract_vcruntime.py)
#
# Optional Authenticode signing (strongly recommended - unsigned installers
# trigger SmartScreen warnings): set CLIPEM_CODESIGN_PFX (path to a .pfx)
# and CLIPEM_CODESIGN_PASSWORD, and install osslsigncode.
#
# Requires makensis (compiles natively on Linux, no Wine needed) and 7z:
#   Arch/CachyOS:   sudo pacman -S nsis 7zip
#   Debian/Ubuntu:  sudo apt install nsis 7zip
set -euo pipefail

case "${1:-}" in
  -h|--help)
    # This file's header comment, up to the first line of code.
    sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'
    exit 0 ;;
esac

VERSION="${1:?Usage: $0 <version>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALLER_SRC="$DEV_DIR/../App/App/Shared/Installer"
BUILD_DIR="$SCRIPT_DIR/build"
OUTPUT="$SCRIPT_DIR/dist/ClipEmSetupWindows.exe"

if ! command -v makensis >/dev/null 2>&1; then
  echo "makensis not found - install NSIS first (see comment at the top of this script)." >&2
  exit 1
fi
if ! command -v 7z >/dev/null 2>&1; then
  echo "7z not found - install 7zip first (see comment at the top of this script)." >&2
  exit 1
fi
if [[ ! -s "$INSTALLER_SRC/signing_public_key.txt" ]]; then
  echo "No release signing public key in $INSTALLER_SRC/signing_public_key.txt - run Dev/generate-signing-key.sh first." >&2
  exit 1
fi

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$SCRIPT_DIR/dist"
cp "$INSTALLER_SRC/ed25519_verify.py" "$INSTALLER_SRC/signing_public_key.txt" "$BUILD_DIR/"

python3 - "$INSTALLER_SRC/setup.py" "$DEV_DIR/release/upstream-pins.json" "$BUILD_DIR/bootstrap-pins.json" <<'PYEOF'
import json, re, sys
setup_py, pins_path, out_path = sys.argv[1:4]
source = open(setup_py).read()
version = re.search(r'^WINDOWS_PYTHON_VERSION = "([^"]+)"', source, re.M).group(1)
tag = re.search(r'^WINDOWS_PYTHON_PBS_TAG = "([^"]+)"', source, re.M).group(1)
url = (f"https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/"
       f"cpython-{version}+{tag}-x86_64-pc-windows-msvc-install_only.tar.gz")
sha256 = json.load(open(pins_path)).get(url)
if not sha256:
    sys.exit(f"{url} isn't pinned in {pins_path} - run generate_install_manifest.py (release.sh) first.")
json.dump({"python": {"url": url, "sha256": sha256}}, open(out_path, "w"), indent=2)
print(f"Bundling Python pin: {url} ({sha256})")
PYEOF

# Downloads <url> to <out> and checks it against its sha256 in
# upstream-pins.json - fails if it isn't pinned or doesn't match. A dropped
# connection is retried; a good download is kept in cache/ (by its sha256),
# so the next build doesn't fetch it again.
fetch_pinned() {
  python3 - "$1" "$DEV_DIR/release/upstream-pins.json" "$2" "$SCRIPT_DIR/cache" <<'PYEOF'
import hashlib, json, os, shutil, sys, time, urllib.request
url, pins_path, out_path, cache_dir = sys.argv[1:5]
sha256 = json.load(open(pins_path)).get(url)
if not sha256:
    sys.exit(f"{url} isn't pinned in {pins_path}.")

def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

cached = os.path.join(cache_dir, sha256)
if os.path.isfile(cached) and digest(cached) == sha256:
    shutil.copyfile(cached, out_path)
    print(f"Using cached {url} (sha256 OK)")
    sys.exit(0)
os.makedirs(cache_dir, exist_ok=True)
partial = cached + ".part"
attempts = 5
for attempt in range(1, attempts + 1):
    try:
        with urllib.request.urlopen(url, timeout=300) as response, open(partial, "wb") as f:
            shutil.copyfileobj(response, f, 1 << 20)
            expected = response.headers.get("Content-Length")
        # A dropped connection can end the read early without an error.
        if expected and os.path.getsize(partial) != int(expected):
            raise OSError(f"got {os.path.getsize(partial)} of {expected} bytes")
        actual = digest(partial)
        if actual != sha256:
            raise OSError(f"checksum mismatch: expected {sha256}, got {actual}")
        break
    except Exception as exc:  # IncompleteRead, timeouts, resets, a cut-short file...
        if os.path.exists(partial):
            os.remove(partial)
        if attempt == attempts:
            sys.exit(f"Couldn't download {url} after {attempts} tries: {exc}")
        print(f"Download of {url} failed ({exc}) - retrying ({attempt}/{attempts - 1})...", file=sys.stderr)
        time.sleep(5 * attempt)
os.replace(partial, cached)
shutil.copyfile(cached, out_path)
print(f"Downloaded {url} (sha256 OK)")
PYEOF
}

# The Visual C++ runtime OBS needs (MSVCP140.dll, VCRUNTIME140_1.dll...),
# deployed app-local next to obs64.exe - see extract_vcruntime.py. A fixed
# Microsoft download URL (the path's hex segment is the file's own sha256);
# to update, resolve https://aka.ms/vs/17/release/vc_redist.x64.exe and pin
# the new URL in upstream-pins.json.
VC_REDIST_URL="https://download.visualstudio.microsoft.com/download/pr/bd1c8d9d-ba95-4eee-bc6e-df1fcc876373/CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/VC_redist.x64.exe"
fetch_pinned "$VC_REDIST_URL" "$BUILD_DIR/VC_redist.x64.exe"
python3 "$SCRIPT_DIR/extract_vcruntime.py" "$BUILD_DIR/VC_redist.x64.exe" "$BUILD_DIR/vcruntime.zip"

# VIProductVersion takes X.X.X.X only - a dev version's "-dev.<stamp>"
# stays in the text version fields.
(cd "$SCRIPT_DIR" && makensis -V2 -DVERSION="$VERSION" -DVI_VERSION="${VERSION%%-*}.0" ClipEmSetup.nsi)

if [[ -n "${CLIPEM_CODESIGN_PFX:-}" ]]; then
  osslsigncode sign -pkcs12 "$CLIPEM_CODESIGN_PFX" -pass "${CLIPEM_CODESIGN_PASSWORD:-}" \
    -n "ClipEm Setup" -i "https://clipem.app" -t http://timestamp.digicert.com \
    -in "$OUTPUT" -out "$OUTPUT.signed"
  mv "$OUTPUT.signed" "$OUTPUT"
  echo "Authenticode-signed $OUTPUT"
else
  echo "Note: CLIPEM_CODESIGN_PFX not set - the installer is unsigned (SmartScreen will warn users)." >&2
fi
echo "Built $OUTPUT"
