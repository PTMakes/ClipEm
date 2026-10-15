#!/usr/bin/env bash
# Clean-installs the newest Linux build release.sh made (Dev/dist/v*/
# clipem-linux-portable-*.zip) into a folder, for testing it like a user
# would: the folder is wiped (rm -rf) first, then the bundle is unpacked and
# its setup run.
#
# Usage: ./install-latest-build.sh [options] [target-dir]
#   target-dir     where to install (default: /home/pt/clipem)
#   --no-setup     just unpack; don't run ClipEmSetupLinux.sh afterwards
#   --wipe-clips   also delete target-dir/Clips (kept by default - it's
#                  where saved clips go unless Setup was told otherwise)
#   --force        wipe target-dir even if it doesn't look like a ClipEm
#                  install
#   --kill         stop anything still running from target-dir (OBS, the
#                  tray widget...) instead of refusing - asks OBS/Python to
#                  exit (SIGTERM), then forces it after 5 seconds
set -euo pipefail

TARGET="/home/pt/clipem"
RUN_SETUP=1
KEEP_CLIPS=1
FORCE=0
KILL=0
for arg in "$@"; do
  case "$arg" in
    --no-setup) RUN_SETUP=0 ;;
    --wipe-clips) KEEP_CLIPS=0 ;;
    --force) FORCE=1 ;;
    --kill) KILL=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    -*) echo "Unknown option: $arg" >&2; exit 1 ;;
    *) TARGET="$arg" ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "ERROR: $*" >&2; exit 1; }

ZIP="$(ls "$SCRIPT_DIR"/dist/v*/clipem-linux-portable-*.zip 2>/dev/null | sort -V | tail -1 || true)"
[[ -n "$ZIP" ]] || die "No build found in $SCRIPT_DIR/dist - run Dev/release.sh (e.g. with --no-upload) first."

# --- Refuse anything that isn't obviously safe to rm -rf -----------------
TARGET="$(realpath -m -- "$TARGET")"
case "$TARGET" in
  /|/home|"$HOME"|/tmp|/usr|/usr/*|/etc|/etc/*|/var|/opt) die "Refusing to wipe $TARGET." ;;
esac
[[ "$TARGET" == "$SCRIPT_DIR"* || "$SCRIPT_DIR" == "$TARGET"* ]] && die "Refusing to wipe $TARGET - it overlaps the repo."
if [[ -d "$TARGET" && -n "$(ls -A "$TARGET")" && ! -d "$TARGET/App" && "$FORCE" == 0 ]]; then
  die "$TARGET isn't empty and doesn't look like a ClipEm install (no App/) - pass --force to wipe it anyway."
fi

# Files in use can't be replaced cleanly, and a running OBS would keep
# writing into the folder being deleted.
running_pids() {
  local proc exe
  for proc in /proc/[0-9]*; do
    exe="$(readlink "$proc/exe" 2>/dev/null || true)"
    [[ "$exe" == "$TARGET"/* ]] && echo "${proc#/proc/}"
  done
  return 0
}
if [[ -d "$TARGET" ]]; then
  pids="$(running_pids)"
  if [[ -n "$pids" ]]; then
    echo "Still running from $TARGET:" >&2
    for pid in $pids; do
      echo "  $pid: $(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" >&2
    done
    [[ "$KILL" == 1 ]] || die "Quit ClipEm + OBS (tray icon > Quit), or re-run with --kill."
    kill $pids 2>/dev/null || true
    for _ in {1..50}; do [[ -z "$(running_pids)" ]] && break; sleep 0.1; done
    pids="$(running_pids)"
    if [[ -n "$pids" ]]; then
      echo "Forcing: $pids" >&2
      kill -9 $pids 2>/dev/null || true
      sleep 0.5
    fi
    [[ -z "$(running_pids)" ]] || die "Couldn't stop everything running from $TARGET."
  fi
fi

echo "Build:  $ZIP"
echo "Target: $TARGET"

# --- Wipe, keeping Clips unless asked not to ------------------------------
CLIPS_BACKUP=""
if [[ "$KEEP_CLIPS" == 1 && -d "$TARGET/Clips" ]]; then
  CLIPS_BACKUP="$(mktemp -d "$(dirname "$TARGET")/.clipem-clips-XXXXXX")"
  mv "$TARGET/Clips" "$CLIPS_BACKUP/Clips"
  echo "Kept:   $TARGET/Clips (moved aside during the wipe)"
fi
rm -rf -- "$TARGET"

# --- Unpack the bundle (it contains a top-level ClipEm/ folder) -----------
UNPACK_DIR="$(mktemp -d "$(dirname "$TARGET")/.clipem-unpack-XXXXXX")"
trap 'rm -rf -- "$UNPACK_DIR"' EXIT
unzip -q "$ZIP" -d "$UNPACK_DIR"
[[ -x "$UNPACK_DIR/ClipEm/ClipEmSetupLinux.sh" ]] || die "$ZIP has no ClipEm/ClipEmSetupLinux.sh - is it a portable build?"
mv "$UNPACK_DIR/ClipEm" "$TARGET"

if [[ -n "$CLIPS_BACKUP" ]]; then
  mv "$CLIPS_BACKUP/Clips" "$TARGET/Clips"
  rmdir "$CLIPS_BACKUP"
fi

VERSION="$(cat "$TARGET/App/Shared/VERSION" 2>/dev/null || echo unknown)"
echo "Installed ClipEm $VERSION into $TARGET"

rm -rf -- "$UNPACK_DIR"
trap - EXIT

if [[ "$RUN_SETUP" == 1 ]]; then
  echo "Running setup..."
  exec "$TARGET/ClipEmSetupLinux.sh" "$TARGET"
fi
echo "Run setup with: $TARGET/ClipEmSetupLinux.sh \"$TARGET\""
