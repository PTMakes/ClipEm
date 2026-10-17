#!/usr/bin/env bash
# Builds and publishes a ClipEm release.
#
#   1. Preflight - tools, signing key, version checks, lint + tests. Nothing is
#      modified if any of these fail. Then any uncommitted changes are
#      committed (you're asked for a message).
#   2. Build     - everything goes into Dev/dist; the only source-tree
#                  changes are App/Shared/VERSION + checksums.json (and new
#                  upstream pins), which belong in the release commit.
#   3. Sign      - install-manifest.json and clipem-latest.json get Ed25519
#                  signatures (see release/release_common.py).
#   4. Upload    - (asked first) every file (manifests last) becomes an asset of a draft
#                  GitHub Release v<version> of PTMakes/ClipEm. Needs the
#                  GitHub CLI, logged in (`gh auth login`).
#   5. Website   - the signed manifests are copied into
#                  Website/public - the legacy update feed for
#                  installs from before updates moved to GitHub (<= 1.3.5).
#   6. Commit    - "Update <version>", then (asked first) push main and
#                  publish the draft release, which tags the pushed commit.
#
# Usage: ./release.sh [--allow-dirty] [--no-upload] [--dev] [patch|minor|major|X.Y.Z]
#   (bump defaults to "patch")
#   --allow-dirty  build from uncommitted changes without committing them
#                  first (the build info records it)
#   --no-upload    stop after step 3 - for a dry run; nothing is published
#   --dev          a test build of HEAD for the "Latest" update channel:
#                  version <next>-dev.<UTC timestamp>, uploaded over the
#                  rolling "dev" prerelease. Nothing is committed and the
#                  website isn't touched; installs on the "Releases"
#                  channel never see it.
set -euo pipefail

ALLOW_DIRTY=0
UPLOAD=1
DEV=0
BUMP="patch"
for arg in "$@"; do
  case "$arg" in
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --no-upload) UPLOAD=0 ;;
    --dev) DEV=1 ;;
    -*) echo "Unknown option: $arg" >&2; exit 1 ;;
    *) BUMP="$arg" ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
APP_DIR="$ROOT_DIR/App"
WEBSITE_DIR="$ROOT_DIR/Website"
WEBSITE_UPDATES="$WEBSITE_DIR/public/updates"
DIST_DIR="$SCRIPT_DIR/dist"

die() { echo "ERROR: $*" >&2; exit 1; }

# Step headers: "=== [3/12] Shared resources ===", and how long the previous
# step took. STEP_TOTAL is filled in once preflight knows which optional
# steps (pre-release commit, upload) will run.
STEP_NUM=0
STEP_TOTAL=""
STEP_STARTED=$SECONDS
RELEASE_STARTED=$SECONDS
fmt_duration() { printf '%dm%02ds' $(($1 / 60)) $(($1 % 60)); }
end_step() {
  (( STEP_NUM > 0 )) && echo "   (took $(fmt_duration $((SECONDS - STEP_STARTED))))"
  return 0
}
step() {
  end_step
  STEP_NUM=$((STEP_NUM + 1))
  STEP_STARTED=$SECONDS
  echo -e "\n=== [$STEP_NUM${STEP_TOTAL:+/$STEP_TOTAL}] $* ==="
}

# --- Versions --------------------------------------------------------------
# Upstream artifact versions: setup.py is the single source of truth for
# what Windows/macOS installs fetch (setup.py and this script both read it).
SETUP_PY="$APP_DIR/App/Shared/Installer/setup.py"
read_setup_constant() {
  local value
  value="$(grep -oP "(?<=^$1 = \")[^\"]+" "$SETUP_PY" || true)"
  [[ -n "$value" ]] || die "Could not read $1 from $SETUP_PY"
  echo "$value"
}
WINDOWS_OBS_VERSION="$(read_setup_constant WINDOWS_OBS_VERSION)"
WINDOWS_PYTHON_VERSION="$(read_setup_constant WINDOWS_PYTHON_VERSION)"
MACOS_OBS_VERSION="$(read_setup_constant MACOS_OBS_VERSION)"
# pip wheel the Windows embeddable Python bootstraps itself with (pinned in
# the manifest with PyPI's own sha256). Must support WINDOWS_PYTHON_VERSION.
WINDOWS_PIP_VERSION="26.2.1"

# Linux OBS is our own build - read its version from the build itself
# (build-obs-from-source-for-linux.sh's OBS_REF), not Windows' pin.
LIBOBS_CMAKE_VERSION="$SCRIPT_DIR/obs-install/lib/cmake/libobs/libobsConfigVersion.cmake"
LINUX_OBS_VERSION="$(grep -oP '(?<=^set\(PACKAGE_VERSION ")[0-9.]+(?="\))' "$LIBOBS_CMAKE_VERSION" 2>/dev/null || true)"
[[ -n "$LINUX_OBS_VERSION" ]] || die "Could not read the Linux OBS version from $LIBOBS_CMAKE_VERSION - run Dev/build/build-obs-from-source-for-linux.sh first"

# The Linux portable Python (python-build-standalone, Xft-repacked by
# build/repack-linux-python-xft.sh). Bump both together when moving to a new PBS
# release; the repack script pins it from PBS's published SHA256SUMS. Kept
# on the same minor version as WINDOWS_PYTHON_VERSION (checked below) - the
# patch can be newer, since python.org stopped shipping Windows builds of
# 3.12 while PBS keeps building its security releases.
LINUX_PYTHON_VERSION="3.12.14"
LINUX_PYTHON_PBS_TAG="20260924"

# --- 1. Preflight ----------------------------------------------------------
step "Preflight"
required_tools=(git jq zip tar gzip openssl curl python3 makensis)
for tool in "${required_tools[@]}"; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required (see Dev/README.md)"
done
python3 "$SCRIPT_DIR/release/release_common.py" check-key
[[ "${LINUX_PYTHON_VERSION%.*}" == "${WINDOWS_PYTHON_VERSION%.*}" ]] \
  || die "Linux Python $LINUX_PYTHON_VERSION and Windows Python $WINDOWS_PYTHON_VERSION should be the same minor version"
python3 -m pytest --version >/dev/null 2>&1 \
  || die "pytest is required: python3 -m pip install 'pytest==8.*' 'ruff==0.16.*' 'Pillow==12.3.0'"
if command -v ruff >/dev/null 2>&1; then RUFF=(ruff); else RUFF=(python3 -m ruff); fi
"${RUFF[@]}" --version >/dev/null 2>&1 || die "ruff is required: python3 -m pip install 'ruff==0.16.*'"

# Setup_Windows.bat (the from-checkout path) hardcodes the embeddable
# Python's URL + sha256 - they must match setup.py and the pinned hash.
SETUP_BAT="$APP_DIR/App/Shared/Installer/Scripts/Setup_Windows.bat"
BAT_SHA256="$(grep -oP '(?<=^set "PYTHON_SHA256=)[0-9a-f]{64}' "$SETUP_BAT" || true)"
PINNED_PY_SHA256="$(jq -r --arg url "https://www.python.org/ftp/python/$WINDOWS_PYTHON_VERSION/python-$WINDOWS_PYTHON_VERSION-embed-amd64.zip" '.[$url] // empty' "$SCRIPT_DIR/release/upstream-pins.json")"
grep -q "python-$WINDOWS_PYTHON_VERSION-embed-amd64.zip" "$SETUP_BAT" \
  || die "Setup_Windows.bat doesn't fetch Python $WINDOWS_PYTHON_VERSION (setup.py's WINDOWS_PYTHON_VERSION)"
[[ -z "$PINNED_PY_SHA256" || "$BAT_SHA256" == "$PINNED_PY_SHA256" ]] \
  || die "Setup_Windows.bat's PYTHON_SHA256 ($BAT_SHA256) doesn't match upstream-pins.json ($PINNED_PY_SHA256)"

# obs-src is always "dirty" (the build script patches it in place).
is_dirty() { [[ -n "$(git -C "$ROOT_DIR" status --porcelain --ignore-submodules=dirty)" ]]; }
# commit_all <message> <pathspec...> - no-op when nothing is staged.
commit_all() {
  local msg="$1"; shift
  git -C "$ROOT_DIR" add -A -- "$@"
  if git -C "$ROOT_DIR" diff --cached --quiet; then
    echo " - nothing to commit"
  else
    git -C "$ROOT_DIR" commit -q -m "$msg"
    echo " - committed $(git -C "$ROOT_DIR" rev-parse --short HEAD) \"$msg\""
  fi
}

# Uncommitted changes get committed at the end of preflight (unless
# --allow-dirty). Ask for the message now so nothing fails after committing.
# A dev build must match a commit instead - it never commits anything.
PRE_COMMIT_MSG=""
if [[ "$ALLOW_DIRTY" == 0 ]] && is_dirty; then
  echo "Uncommitted changes:"
  git -C "$ROOT_DIR" status --short --ignore-submodules=dirty
  [[ "$DEV" == 0 ]] || die "Commit these first - a dev build is built from a commit (or pass --allow-dirty)"
  [[ -t 0 ]] || die "Uncommitted changes and no terminal to ask for a commit message - commit them first (or pass --allow-dirty)"
  read -rp "Commit message for these changes (empty to abort): " PRE_COMMIT_MSG
  [[ -n "$PRE_COMMIT_MSG" ]] || die "Aborted - nothing was modified"
fi

# Preflight, lint/tests, shared, Linux OBS, ffmpeg, Python, manifest,
# Windows installer, portable bundle, signing - plus the optional ones
# (upload; website + commit for a real release).
STEP_TOTAL=$((10 + (${#PRE_COMMIT_MSG} > 0 ? 1 : 0) + (UPLOAD == 1 ? (DEV == 1 ? 1 : 3) : 0)))

VERSION_MANIFEST="$WEBSITE_UPDATES/install-manifest.json"
[[ -f "$VERSION_MANIFEST" ]] || die "Current manifest not found: $VERSION_MANIFEST"
current_version="$(jq -r .version "$VERSION_MANIFEST")"
[[ "$current_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Current version in $VERSION_MANIFEST isn't X.Y.Z: '$current_version'"

if [[ "$BUMP" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  new_version="$BUMP"
else
  IFS='.' read -r major minor patch <<< "$current_version"
  case "$BUMP" in
    major) major=$((major + 1)); minor=0; patch=0 ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    patch) patch=$((patch + 1)) ;;
    *) die "Unknown bump type: $BUMP (use patch|minor|major or an explicit X.Y.Z)" ;;
  esac
  new_version="$major.$minor.$patch"
fi
if [[ "$DEV" == 1 ]]; then
  new_version="$new_version-dev.$(date -u +%Y%m%d%H%M)"
  TAG="dev"
else
  TAG="v$new_version"
fi

if [[ "$UPLOAD" == 1 ]]; then
  python3 "$SCRIPT_DIR/release/release_common.py" check-github
  if [[ "$DEV" == 0 ]] && python3 "$SCRIPT_DIR/release/release_common.py" release-exists "$new_version"; then
    die "Release $TAG already exists on GitHub (delete the draft there to redo it)"
  fi
fi

echo " - ClipEm version: $current_version -> $new_version"
echo " - Windows OBS $WINDOWS_OBS_VERSION, Windows Python $WINDOWS_PYTHON_VERSION (pip $WINDOWS_PIP_VERSION)"
echo " - macOS OBS $MACOS_OBS_VERSION, Linux OBS $LINUX_OBS_VERSION, Linux Python $LINUX_PYTHON_VERSION+$LINUX_PYTHON_PBS_TAG"
echo " - Release assets: $(python3 "$SCRIPT_DIR/release/release_common.py" asset-url "$new_version" "")"

step "Lint and tests"
# The OBS script tests check against obspython's real API as of our Linux
# OBS build - regenerate the snapshot whenever that build changes.
python3 "$SCRIPT_DIR/generate_obspython_api.py" --check \
  || die "Run Dev/generate_obspython_api.py and commit Dev/tests/obspython_api.json"
(cd "$APP_DIR" && "${RUFF[@]}" check .) || die "ruff found problems in App"
(cd "$SCRIPT_DIR" && "${RUFF[@]}" check .) || die "ruff found problems in Dev/"
if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  echo "WARNING: no display - the GUI layout test will be skipped (run it under xvfb-run before releasing)." >&2
fi
mkdir -p "$DIST_DIR"
TEST_LOG="$DIST_DIR/test_results.txt"
if ! (cd "$SCRIPT_DIR" && python3 -m pytest -q tests) > "$TEST_LOG" 2>&1; then
  cat "$TEST_LOG"
  die "Tests FAILED - see $TEST_LOG"
fi
tail -n 3 "$TEST_LOG"

if [[ -n "$PRE_COMMIT_MSG" ]]; then
  step "Committing pre-release changes"
  commit_all "$PRE_COMMIT_MSG" .
fi

# --- 2. Build --------------------------------------------------------------
RELEASE_DIR="$DIST_DIR/$TAG"
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

# Deterministic archives: fixed mtimes (HEAD's commit time), sorted
# entries, no owner/extra attributes.
SOURCE_DATE_EPOCH="$(git -C "$ROOT_DIR" log -1 --format=%ct)"
export SOURCE_DATE_EPOCH

step "Shared resources"
# A dev build doesn't change the source tree: VERSION and checksums.json
# are put back once the shared zip is built.
DEV_RESTORE_DIR="$RELEASE_DIR/restore"
if [[ "$DEV" == 1 ]]; then
  mkdir -p "$DEV_RESTORE_DIR"
  cp "$APP_DIR/App/Shared/VERSION" "$APP_DIR/App/Shared/checksums.json" "$DEV_RESTORE_DIR/"
fi
# Shipped inside App/Shared so the installed copy knows its own version.
echo "$new_version" > "$APP_DIR/App/Shared/VERSION"
# Checksums must be written before zipping, or the zip ships stale ones.
python3 "$SCRIPT_DIR/release/generate_checksums.py" "$APP_DIR/App/Shared"

STAGE_DIR="$RELEASE_DIR/stage"
mkdir -p "$STAGE_DIR"
git -C "$APP_DIR" ls-files -z --cached --others --exclude-standard -- App/Shared \
  | (cd "$APP_DIR/App" && sed -z 's#^App/##' | xargs -0 cp --parents -t "$STAGE_DIR")
SHARED_ZIP="$RELEASE_DIR/clipem-shared-$new_version.zip"
python3 "$SCRIPT_DIR/release/release_common.py" zip-dir "$STAGE_DIR" Shared "$SHARED_ZIP"
rm -rf "$STAGE_DIR"
if [[ "$DEV" == 1 ]]; then
  cp "$DEV_RESTORE_DIR/VERSION" "$DEV_RESTORE_DIR/checksums.json" "$APP_DIR/App/Shared/"
  rm -rf "$DEV_RESTORE_DIR"
fi
echo "Wrote $SHARED_ZIP"

step "Linux OBS (built from source)"
LINUX_OBS_TAR="$RELEASE_DIR/linux-obs-install-$LINUX_OBS_VERSION.tar.gz"
tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
  -C "$SCRIPT_DIR" -cf - obs-install \
  | python3 "$SCRIPT_DIR/release/release_common.py" pipe --label "Compressing Linux OBS" \
      --total "$(du -sb "$SCRIPT_DIR/obs-install" | cut -f1)" \
  | gzip -n > "$LINUX_OBS_TAR"
echo "Wrote $LINUX_OBS_TAR"

step "ffmpeg for clip re-export (see build/build-ffmpeg.sh; reused once built)"
"$SCRIPT_DIR/build/build-ffmpeg.sh"
FFMPEG_VERSION="$(grep -oP '(?<=^FFMPEG_TAG="n)[^"]+' "$SCRIPT_DIR/build/build-ffmpeg.sh")"
FFMPEG_VERSION="$FFMPEG_VERSION-$(grep -oP '(?<=^BUILD_REV=")[^"]+' "$SCRIPT_DIR/build/build-ffmpeg.sh")"
for f in linux-x86_64.tar.gz windows-x86_64.zip source.tar.gz; do
  cp "$DIST_DIR/ffmpeg/ffmpeg-$FFMPEG_VERSION-$f" "$RELEASE_DIR/"
done
LINUX_FFMPEG="$RELEASE_DIR/ffmpeg-$FFMPEG_VERSION-linux-x86_64.tar.gz"
WINDOWS_FFMPEG="$RELEASE_DIR/ffmpeg-$FFMPEG_VERSION-windows-x86_64.zip"
FFMPEG_SOURCE="$RELEASE_DIR/ffmpeg-$FFMPEG_VERSION-source.tar.gz"

step "Xft-enabled portable Linux Python (see build/repack-linux-python-xft.sh)"
"$SCRIPT_DIR/build/repack-linux-python-xft.sh" "$LINUX_PYTHON_VERSION" "$LINUX_PYTHON_PBS_TAG"
LINUX_PYTHON_XFT_NAME="cpython-${LINUX_PYTHON_VERSION}+${LINUX_PYTHON_PBS_TAG}-x86_64-unknown-linux-gnu-install_only-xft.tar.gz"
cp "$DIST_DIR/$LINUX_PYTHON_XFT_NAME" "$RELEASE_DIR/"
LINUX_PYTHON_XFT_TAR="$RELEASE_DIR/$LINUX_PYTHON_XFT_NAME"

step "Install manifest"
python3 "$SCRIPT_DIR/release/generate_install_manifest.py" \
  --version "$new_version" \
  --out "$RELEASE_DIR/install-manifest.json" \
  --shared-zip "$SHARED_ZIP" \
  --linux-obs-tar "$LINUX_OBS_TAR" \
  --linux-python-tar "$LINUX_PYTHON_XFT_TAR" \
  --linux-ffmpeg "$LINUX_FFMPEG" \
  --windows-ffmpeg "$WINDOWS_FFMPEG" \
  --ffmpeg-source "$FFMPEG_SOURCE" \
  --windows-obs-version "$WINDOWS_OBS_VERSION" \
  --windows-python-version "$WINDOWS_PYTHON_VERSION" \
  --windows-pip-version "$WINDOWS_PIP_VERSION" \
  --macos-obs-version "$MACOS_OBS_VERSION"

# Legacy update feed, only read by pre-1.0.22 installs.
python3 "$SCRIPT_DIR/release/generate_checksums.py" "$APP_DIR/App/Shared" --skip-checksums \
  --zip "$SHARED_ZIP" --version "$new_version" \
  --url "$(python3 "$SCRIPT_DIR/release/release_common.py" asset-url "$new_version" "$(basename "$SHARED_ZIP")")" \
  --manifest-out "$RELEASE_DIR/clipem-latest.json"

step "Windows installer"
# Built after the manifest step so the embeddable Python it bundles a pin
# for is guaranteed to be in upstream-pins.json.
"$SCRIPT_DIR/installer/windows/build.sh" "$new_version"
cp "$SCRIPT_DIR/installer/windows/dist/ClipEmSetupWindows.exe" "$RELEASE_DIR/"

step "Offline packages for the tray widget and mic filter (wheelhouses)"
TRAY_REQUIREMENTS="$APP_DIR/App/Shared/tray-widget/requirements.txt"
MIC_FILTER_REQUIREMENTS="$APP_DIR/App/Shared/Installer/mic-filter/requirements.txt"
WINDOWS_WHEELHOUSE="$RELEASE_DIR/wheelhouse-windows"
LINUX_WHEELHOUSE="$RELEASE_DIR/wheelhouse-linux"
python3 "$SCRIPT_DIR/release/release_common.py" wheelhouse --system Windows --python-version "$WINDOWS_PYTHON_VERSION" \
  --dest "$WINDOWS_WHEELHOUSE" "$TRAY_REQUIREMENTS" "$MIC_FILTER_REQUIREMENTS"
# Linux records mic levels with parecord - no mic filter packages there.
python3 "$SCRIPT_DIR/release/release_common.py" wheelhouse --system Linux --python-version "$LINUX_PYTHON_VERSION" \
  --dest "$LINUX_WHEELHOUSE" "$TRAY_REQUIREMENTS"

step "Portable Linux bundle (offline alternative to ClipEmSetupLinux.sh)"
PORTABLE_ZIP="$RELEASE_DIR/clipem-linux-portable-$new_version.zip"
# Also leaves the unpacked bundle at Dev/test/ClipEm.
python3 "$SCRIPT_DIR/release/release_common.py" portable-bundle \
  --shared-zip "$SHARED_ZIP" --obs-install "$SCRIPT_DIR/obs-install" \
  --python-tar "$LINUX_PYTHON_XFT_TAR" --ffmpeg-tar "$LINUX_FFMPEG" \
  --setup-script "$APP_DIR/App/Shared/Installer/Scripts/ClipEmSetupLinux.sh" \
  --wheelhouse "$LINUX_WHEELHOUSE" \
  --stage-dir "$SCRIPT_DIR/test" --out "$PORTABLE_ZIP" >/dev/null
echo "Wrote $PORTABLE_ZIP"

step "Portable Windows bundle (offline alternative to ClipEmSetupWindows.exe)"
WINDOWS_PORTABLE_ZIP="$RELEASE_DIR/clipem-windows-portable-$new_version.zip"
WINDOWS_INSTALLER_BUILD="$SCRIPT_DIR/installer/windows/build"
python3 "$SCRIPT_DIR/release/release_common.py" windows-offline-bundle \
  --manifest "$RELEASE_DIR/install-manifest.json" --shared-zip "$SHARED_ZIP" --ffmpeg-zip "$WINDOWS_FFMPEG" \
  --tcltk-zip "$WINDOWS_INSTALLER_BUILD/tcltk.zip" --vcruntime-zip "$WINDOWS_INSTALLER_BUILD/vcruntime.zip" \
  --setup-script "$APP_DIR/App/Shared/Installer/Scripts/Setup_Windows.bat" \
  --wheelhouse "$WINDOWS_WHEELHOUSE" --stage-dir "$RELEASE_DIR/stage-windows" --out "$WINDOWS_PORTABLE_ZIP"
rm -rf "$RELEASE_DIR/stage-windows"

# Listed in the manifest for clipem.app's download links (before signing).
python3 "$SCRIPT_DIR/release/release_common.py" add-offline --manifest "$RELEASE_DIR/install-manifest.json" \
  --version "$new_version" --windows "$WINDOWS_PORTABLE_ZIP" --linux "$PORTABLE_ZIP"

# Which exact sources this release was built from.
BUILD_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)"
jq -n \
  --arg version "$new_version" \
  --arg built "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg commit "$BUILD_COMMIT" --argjson dirty "$(is_dirty && echo true || echo false)" \
  --arg obs "$(git -C "$SCRIPT_DIR/obs-src" rev-parse HEAD)" \
  '{version: $version, built_at: $built, commit: $commit, obs_src: $obs, uncommitted_changes: $dirty}' \
  > "$RELEASE_DIR/build-info.json"

# --- 3. Sign ---------------------------------------------------------------
step "Signing manifests"
python3 "$SCRIPT_DIR/release/release_common.py" sign "$RELEASE_DIR/install-manifest.json" "$RELEASE_DIR/clipem-latest.json"
python3 "$APP_DIR/App/Shared/Installer/ed25519_verify.py" "$APP_DIR/App/Shared/Installer/signing_public_key.txt" \
  "$RELEASE_DIR/install-manifest.json" "$RELEASE_DIR/install-manifest.json.sig"

if [[ "$UPLOAD" == 0 ]]; then
  end_step
  echo -e "\n--no-upload: stopping before anything is published. Release files: $RELEASE_DIR"
  echo "Total: $(fmt_duration $((SECONDS - RELEASE_STARTED)))"
  exit 0
fi

# --- 4. Upload -------------------------------------------------------------
RELEASE_FILES=(
  "$SHARED_ZIP" "$LINUX_OBS_TAR" "$LINUX_PYTHON_XFT_TAR"
  "$LINUX_FFMPEG" "$WINDOWS_FFMPEG" "$FFMPEG_SOURCE"
  "$RELEASE_DIR/ClipEmSetupWindows.exe" "$APP_DIR/App/Shared/Installer/Scripts/ClipEmSetupLinux.sh"
  "$PORTABLE_ZIP" "$WINDOWS_PORTABLE_ZIP" "$RELEASE_DIR/build-info.json"
  "$RELEASE_DIR/install-manifest.json" "$RELEASE_DIR/install-manifest.json.sig"
  "$RELEASE_DIR/clipem-latest.json" "$RELEASE_DIR/clipem-latest.json.sig"
)
# Nothing has left this machine yet - ask before anything does.
[[ -t 0 ]] || die "No terminal to confirm the upload - nothing was published. Release files: $RELEASE_DIR"
if [[ "$DEV" == 1 ]]; then upload_target="the \"dev\" prerelease"; else upload_target="a draft GitHub release $TAG"; fi
read -rp "Upload $new_version to $upload_target now? [y/N] " answer
if [[ ! "$answer" =~ ^[Yy] ]]; then
  end_step
  echo -e "\nUpload cancelled - nothing was published. Release files: $RELEASE_DIR"
  echo "Total: $(fmt_duration $((SECONDS - RELEASE_STARTED)))"
  exit 0
fi
if [[ "$DEV" == 1 ]]; then
  step "Uploading dev build $new_version to the \"dev\" prerelease"
  python3 "$SCRIPT_DIR/release/release_common.py" upload --commit "$BUILD_COMMIT" "$new_version" "${RELEASE_FILES[@]}"
  end_step
  echo
  echo "Done. Dev build $new_version is live in $(fmt_duration $((SECONDS - RELEASE_STARTED)))."
  echo "Installs with Settings > Update channel set to \"Latest\" get it at their next update check."
  exit 0
fi

step "Uploading release assets to a draft GitHub release $TAG"
python3 "$SCRIPT_DIR/release/release_common.py" upload --commit "$BUILD_COMMIT" "$new_version" "${RELEASE_FILES[@]}"

# --- 5. Legacy update feed on the website ----------------------------------
step "Publishing manifests to the website (feed for installs up to 1.3.5)"
cp "$RELEASE_DIR/install-manifest.json" "$RELEASE_DIR/install-manifest.json.sig" \
  "$RELEASE_DIR/clipem-latest.json" "$RELEASE_DIR/clipem-latest.json.sig" "$WEBSITE_UPDATES/"

# --- 6. Commit, push and publish -------------------------------------------
step "Committing release $new_version"
# VERSION + checksums.json, the website's manifests and new upstream pins.
commit_all "Update $new_version" App/App/Shared Website/public Dev/release/upstream-pins.json

end_step
echo
echo "Built and uploaded $new_version in $(fmt_duration $((SECONDS - RELEASE_STARTED)))."
RELEASE_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)"
PUBLISH_CMDS="git -C \"$ROOT_DIR\" push origin $RELEASE_COMMIT:main && python3 \"$SCRIPT_DIR/release/release_common.py\" publish --commit $RELEASE_COMMIT $new_version"
answer=""
if [[ -t 0 ]]; then
  read -rp "Push main and publish GitHub release $TAG now? This makes it live for every install. [y/N] " answer
fi
if [[ "$answer" =~ ^[Yy] ]]; then
  git -C "$ROOT_DIR" push origin "$RELEASE_COMMIT:main"
  python3 "$SCRIPT_DIR/release/release_common.py" publish --commit "$RELEASE_COMMIT" "$new_version"
  echo "Released. Deploy Website too, so installs up to 1.3.5 see it."
else
  echo "Not published yet - the release is a draft. When ready:"
  echo "  $PUBLISH_CMDS"
  echo "then deploy Website, so installs up to 1.3.5 see it."
fi
