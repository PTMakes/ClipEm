#!/usr/bin/env bash
# Builds OBS Studio from source with a genuinely relocatable/portable
# config layout (per https://github.com/obsproject/obs-studio/wiki/Build-Instructions-For-Linux)
# and installs it into ./obs-install, right beside this script - matching
# how Windows/macOS bundle a real OBS binary inside their own App/ folder,
# instead of borrowing your system's `obs` + redirecting XDG_CONFIG_HOME.
#
# IMPORTANT CAVEATS, read before running:
#
# - This is written for Arch/CachyOS (pacman [+ yay for AUR packages]).
#   Translated from the OBS wiki's Debian/Fedora dependency lists to Arch
#   package names from general knowledge - NOT verified against a live
#   build. If `cmake --preset` or the build itself complains about a
#   missing dependency, install the named package and re-run this script
#   (every step here is safe to re-run).
# - "Relocatable" means OBS's own config/data paths are relative to
#   ./obs-install, not that the binary is statically linked. It still
#   dynamically links against *this machine's* Qt6/ffmpeg/pipewire/etc at
#   whatever versions pacman gives you right now - it will NOT necessarily
#   run unmodified on a different machine/distro without rebuilding there.
#   For a single CachyOS box, that's fine.
# - Builds with ENABLE_BROWSER=OFF (skips needing the prebuilt CEF
#   framework - not needed for gaming/clipping), ENABLE_AJA=OFF (no AJA
#   capture card support), and ENABLE_NVENC=OFF (needs NVIDIA's
#   nv-codec-headers, which isn't packaged for Arch/CachyOS - NVENC users
#   still get software x264 by default; AMF/QSV hardware encoders are
#   unaffected). All three can be re-enabled later; see the cmake
#   invocation below.
# - Needs `sudo` once, to install build dependencies system-wide (there's
#   no way around that for a source build). It does NOT install OBS
#   itself system-wide - that goes into ./obs-install only.
# - Does not refresh pacman's package databases (a refresh without a full
#   upgrade is Arch's unsupported "partial upgrade"). Run `sudo pacman -Syu`
#   yourself first if your system is out of date.
# - The default ref is pinned to an exact commit (OBS_PINNED_COMMIT below),
#   so a moved upstream tag can't silently change what gets built. Any local
#   changes inside obs-src are discarded on each run - the patches below are
#   re-applied to a clean checkout every time.
# - Expect this to take a while: a full clone with submodules, then a
#   20-60+ minute compile depending on your hardware.
set -euo pipefail

OBS_PINNED_REF="32.2.2"
OBS_PINNED_COMMIT="ba2f32bdf791005443988a4955e963663e16b1ed"
OBS_REF="${1:-$OBS_PINNED_REF}"
# Lives in Dev/build/, but builds into Dev/ - where release.sh packages obs-install from.
DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${DEV_DIR}/obs-src"
BUILD_DIR="${SRC_DIR}/build_ubuntu"
INSTALL_DIR="${DEV_DIR}/obs-install"
# Per-app audio capture on Linux ("Split audio tracks" in Setup) - OBS has
# no built-in PipeWire application audio source, so this third-party plugin
# is built against the OBS above and ships inside the same obs-install tree.
PW_AUDIO_VERSION="1.2.1"
PW_AUDIO_URL="https://github.com/dimtpap/obs-pipewire-audio-capture/archive/refs/tags/${PW_AUDIO_VERSION}.tar.gz"
PW_AUDIO_SHA256="70107cafd2020437fc0b663fdf6a94598b474942a0582b24719a0e76a9f73a50"
PW_AUDIO_DIR="${DEV_DIR}/.pipewire-audio-work"

if ! command -v pacman >/dev/null 2>&1; then
    echo "This script is written for Arch/CachyOS (pacman). For other distros," >&2
    echo "follow https://github.com/obsproject/obs-studio/wiki/Build-Instructions-For-Linux" >&2
    echo "directly and adjust the cmake invocation at the bottom of this script." >&2
    exit 1
fi

echo "=== Installing build dependencies (pacman) ==="
echo "You'll be asked to confirm before anything installs."
sudo pacman -S --needed \
    base-devel cmake extra-cmake-modules ninja git clang ccache curl \
    ffmpeg x264 mbedtls mbedtls3 mesa libglvnd jansson luajit python \
    libx11 libxcb libxcomposite libxinerama libxss \
    wayland wayland-protocols pciutils pipewire \
    qt6-base qt6-svg qt6-wayland qt6-imageformats \
    alsa-lib fontconfig freetype2 jack2 libpulse speexdsp \
    systemd v4l-utils libva vlc libdrm nlohmann-json swig srt

if command -v yay >/dev/null 2>&1; then
    echo "=== Installing AUR-only dependencies (yay) ==="
    if ! yay -S --needed librist websocketpp asio uthash simde qrcodegencpp; then
        echo "Installing AUR dependencies failed - fix that (or install them manually) and re-run." >&2
        exit 1
    fi
else
    echo "yay not found. If the build complains about a missing package, these" >&2
    echo "are AUR-only and need an AUR helper: librist websocketpp asio uthash simde qrcodegencpp" >&2
fi

# pacman has occasionally reported success while leaving a --needed package
# (observed with swig) not actually on PATH - catch that here instead of
# 20+ minutes into a build that then fails in cmake configure.
echo
echo "=== Verifying critical build tools are actually on PATH ==="
missing=()
for tool in cmake ninja git clang curl swig; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Missing after install: ${missing[*]} - retrying explicitly..." >&2
    sudo pacman -S --needed "${missing[@]}"
    for tool in "${missing[@]}"; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "Still missing: $tool. Install it manually and re-run this script." >&2
            exit 1
        }
    done
fi

echo
echo "=== Fetching source ==="
if [ ! -d "$SRC_DIR" ]; then
    git clone --recursive https://github.com/obsproject/obs-studio.git "$SRC_DIR"
fi
cd "$SRC_DIR"
git fetch --tags
# Start from a clean tree: drops the previous run's patches (re-applied below).
git reset --hard --quiet
git submodule foreach --recursive --quiet git reset --hard --quiet
git checkout --quiet "$OBS_REF"
git submodule update --init --recursive
if [[ "$OBS_REF" == "$OBS_PINNED_REF" && "$(git rev-parse HEAD)" != "$OBS_PINNED_COMMIT" ]]; then
    echo "Tag $OBS_REF now points at $(git rev-parse HEAD), not the pinned $OBS_PINNED_COMMIT." >&2
    echo "Upstream moved the tag - investigate before building from it." >&2
    exit 1
fi

# Each patch below must either apply or already be present - a silent
# no-op would only surface 20+ minutes later as an obscure build failure.
patch_failed() {
    echo "Patch $1 no longer applies to $OBS_REF - obs-studio upstream changed that code." >&2
    echo "Update the patch in this script (or drop it if fixed upstream)." >&2
    exit 1
}

echo
echo "=== Patching for toolchain versions newer than $OBS_REF was written against ==="
echo "(verified needed on CachyOS with Qt 6.11 / FFmpeg 8.1 / GCC 15; safe no-ops"
echo " once fixed upstream or if your versions don't hit these)"

# 1. Qt6's CMake packaging requires explicitly requesting the "Private"
#    component to get the Qt::GuiPrivate target - obs-studio only asks for
#    "Gui", which happens to still work against Ubuntu's Qt6 packaging (CI's
#    target) but not Arch's. OBS renamed UI/ to frontend/ between 31.x and
#    32.x, so check both locations.
if [ -f UI/cmake/ui-qt.cmake ]; then
    UI_QT_CMAKE=UI/cmake/ui-qt.cmake
else
    UI_QT_CMAKE=frontend/cmake/ui-qt.cmake
fi
sed -i 's/find_package(Qt6 REQUIRED Gui DBus)/find_package(Qt6 REQUIRED Gui GuiPrivate DBus)/' \
    "$UI_QT_CMAKE"
grep -q 'find_package(Qt6 REQUIRED Gui GuiPrivate DBus)' "$UI_QT_CMAKE" || patch_failed 1

# 2. FFmpeg renamed FF_PROFILE_*/FF_LEVEL_* to AV_PROFILE_*/AV_LEVEL_* (same
#    values, just namespaced) - recent FFmpeg releases dropped the old names.
FFMPEG_PATCHED_FILES=(plugins/obs-ffmpeg/obs-ffmpeg-vaapi.c plugins/obs-outputs/rtmp-av1.c)
sed -i -E 's/\bFF_PROFILE_/AV_PROFILE_/g; s/\bFF_LEVEL_/AV_LEVEL_/g' "${FFMPEG_PATCHED_FILES[@]}"
if grep -qE '\bFF_(PROFILE|LEVEL)_' "${FFMPEG_PATCHED_FILES[@]}"; then patch_failed 2; fi

# 3. Recent libstdc++ stopped transitively pulling in <cstdint> from other
#    standard headers, and this file uses uint8_t without including it.
if ! grep -q '#include <cstdint>' deps/json11/json11.cpp; then
    sed -i '/#include <cassert>/a #include <cstdint>' deps/json11/json11.cpp
fi
grep -q '#include <cstdint>' deps/json11/json11.cpp || patch_failed 3

# 4. obs-outputs/librtmp still uses MbedTLS's old (pre-4.0) API. It forces
#    CMake's Config-mode search for MbedTLS, which on Arch/CachyOS resolves
#    straight to the system "mbedtls" package's own CMake config - now
#    MbedTLS 4.x, an incompatible API/ABI bump. Force Module mode instead so
#    it falls through to obs-studio's own pkg-config-based finder, which
#    (with PKG_CONFIG_PATH set below) picks up the "mbedtls3" compat package.
sed -i '/set(CMAKE_FIND_PACKAGE_PREFER_CONFIG TRUE)/{N;s/set(CMAKE_FIND_PACKAGE_PREFER_CONFIG TRUE)\nfind_package(MbedTLS REQUIRED)/find_package(MbedTLS REQUIRED)/}' \
    plugins/obs-outputs/CMakeLists.txt
if grep -A1 'set(CMAKE_FIND_PACKAGE_PREFER_CONFIG TRUE)' plugins/obs-outputs/CMakeLists.txt \
    | grep -q 'find_package(MbedTLS REQUIRED)'; then
    patch_failed 4
fi
grep -q 'find_package(MbedTLS REQUIRED)' plugins/obs-outputs/CMakeLists.txt || patch_failed 4

# 5. libobs/obs-nix.c's add_default_module_paths() adds a plugin directory
#    computed relative to the running binary, then separately adds another
#    one built from OBS_INSTALL_PREFIX (this build's -DCMAKE_INSTALL_PREFIX,
#    baked into the binary at compile time) - skipping the latter only when
#    it's identical to the former. That assumes the binary always runs from
#    the same prefix it was built with. It doesn't for us: release.sh copies
#    this whole obs-install directory into each distribution bundle
#    (Dev/test/ClipEm/App/Linux/obs-install, a portable install's App/Linux,
#    etc), so the binary ends up running from a different path than
#    OBS_INSTALL_PREFIX pointed at when it was built here. The two paths
#    then differ, both get added, and - since this original obs-install
#    build directory is still sitting right here on the machine that built
#    it, with an identical plugin set - OBS ends up scanning both and
#    loading every plugin (obs-websocket included) twice in one process.
#    Patches the function to skip its own OBS_INSTALL_PREFIX-based entry
#    once it already knows (from the same comparison already being made)
#    that OBS_INSTALL_PREFIX doesn't match where the binary is actually
#    running from.
python3 - <<'PYEOF'
import re

path = "libobs/obs-nix.c"
with open(path) as f:
    src = f.read()

if "skip_stale_install_prefix_path" in src:
    # Already applied - this script re-checks out $OBS_REF into the same
    # obs-src clone on every rerun rather than starting from a fresh one,
    # so a previous run's patch is still sitting in the working tree.
    raise SystemExit(0)

old = '''void add_default_module_paths(void)
{
	char *module_bin_path = os_get_executable_path_ptr("../" OBS_PLUGIN_PATH);
	char *module_data_path = os_get_executable_path_ptr("../" OBS_DATA_PATH "/obs-plugins/%module%");

	if (module_bin_path && module_data_path) {
		char *abs_module_bin_path = os_get_abs_path_ptr(module_bin_path);
		char *abs_module_install_path = os_get_abs_path_ptr(OBS_INSTALL_PREFIX "/" OBS_PLUGIN_DESTINATION);

		if (abs_module_bin_path &&
		    (!abs_module_install_path || strcmp(abs_module_bin_path, abs_module_install_path) != 0)) {
			obs_add_module_path(module_bin_path, module_data_path);
		}
		bfree(abs_module_install_path);
		bfree(abs_module_bin_path);
	}

	bfree(module_bin_path);
	bfree(module_data_path);

	for (int i = 0; i < module_patterns_size; i++) {
		obs_add_module_path(module_bin[i], module_data[i]);
	}
}'''

new = '''void add_default_module_paths(void)
{
	char *module_bin_path = os_get_executable_path_ptr("../" OBS_PLUGIN_PATH);
	char *module_data_path = os_get_executable_path_ptr("../" OBS_DATA_PATH "/obs-plugins/%module%");

	/* See build-obs-from-source-for-linux.sh patch #5: if this directory
	 * gets copied elsewhere after being built, OBS_INSTALL_PREFIX (baked in
	 * at compile time) no longer matches where the binary actually runs
	 * from. skip_stale_install_prefix_path tracks that mismatch so the loop
	 * below can skip its own now-stale OBS_INSTALL_PREFIX entry instead of
	 * scanning it (and every plugin in it) a second time. */
	bool skip_stale_install_prefix_path = false;

	if (module_bin_path && module_data_path) {
		char *abs_module_bin_path = os_get_abs_path_ptr(module_bin_path);
		char *abs_module_install_path = os_get_abs_path_ptr(OBS_INSTALL_PREFIX "/" OBS_PLUGIN_DESTINATION);

		if (abs_module_bin_path &&
		    (!abs_module_install_path || strcmp(abs_module_bin_path, abs_module_install_path) != 0)) {
			obs_add_module_path(module_bin_path, module_data_path);
			skip_stale_install_prefix_path = true;
		}
		bfree(abs_module_install_path);
		bfree(abs_module_bin_path);
	}

	bfree(module_bin_path);
	bfree(module_data_path);

	for (int i = 0; i < module_patterns_size; i++) {
		if (skip_stale_install_prefix_path &&
		    strcmp(module_bin[i], OBS_INSTALL_PREFIX "/" OBS_PLUGIN_DESTINATION) == 0)
			continue;
		obs_add_module_path(module_bin[i], module_data[i]);
	}
}'''

if old not in src:
    raise SystemExit(
        "build-obs-from-source-for-linux.sh patch #5: add_default_module_paths() in "
        "libobs/obs-nix.c doesn't match what this patch expects - obs-studio upstream "
        "must have changed it. Update the patch (or drop it if fixed upstream)."
    )

with open(path, "w") as f:
    f.write(src.replace(old, new))
PYEOF

echo
echo "=== Configuring (relocatable, portable config, installing to $INSTALL_DIR) ==="
# obs-outputs/librtmp still uses MbedTLS's old (pre-4.0) API. Arch/CachyOS
# moved the "mbedtls" package to MbedTLS 4.x (breaking API/ABI change) and
# split the old API out into "mbedtls3", whose pkg-config file lives outside
# the default search path - point pkg-config at it first so CMake resolves
# the compatible headers/libs instead of the system default v4 ones.
export PKG_CONFIG_PATH="/usr/lib/mbedtls3/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
cmake --preset ubuntu \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$INSTALL_DIR" \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DENABLE_RELOCATABLE=ON \
    -DENABLE_PORTABLE_CONFIG=ON \
    -DENABLE_BROWSER=OFF \
    -DENABLE_AJA=OFF \
    -DENABLE_NVENC=OFF \
    -DOBS_COMPILE_DEPRECATION_AS_WARNING=ON

echo
echo "=== Building (this will take a while) ==="
cmake --build "$BUILD_DIR"

echo
echo "=== Installing into $INSTALL_DIR ==="
cmake --install "$BUILD_DIR"

echo
echo "=== Building obs-pipewire-audio-capture $PW_AUDIO_VERSION against it ==="
rm -rf "$PW_AUDIO_DIR"
mkdir -p "$PW_AUDIO_DIR"
curl -fsSL -o "$PW_AUDIO_DIR/src.tar.gz" "$PW_AUDIO_URL"
echo "$PW_AUDIO_SHA256  $PW_AUDIO_DIR/src.tar.gz" | sha256sum -c --quiet - || {
    echo "obs-pipewire-audio-capture $PW_AUDIO_VERSION doesn't match its pinned sha256 - investigate before building." >&2
    exit 1
}
tar xzf "$PW_AUDIO_DIR/src.tar.gz" -C "$PW_AUDIO_DIR" --strip-components=1
cmake -S "$PW_AUDIO_DIR" -B "$PW_AUDIO_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$INSTALL_DIR" \
    -DCMAKE_INSTALL_PREFIX="$INSTALL_DIR" \
    -DCMAKE_INSTALL_LIBDIR=lib
cmake --build "$PW_AUDIO_DIR/build"
cmake --install "$PW_AUDIO_DIR/build"
[ -f "$INSTALL_DIR/lib/obs-plugins/linux-pipewire-audio.so" ] || {
    echo "linux-pipewire-audio.so didn't land in $INSTALL_DIR/lib/obs-plugins." >&2
    exit 1
}

echo
echo "Done. OBS is installed at: $INSTALL_DIR"
echo "Portable launch command: $INSTALL_DIR/bin/obs -p"
echo "release.sh packages it, and run.py uses it automatically when it's present."
