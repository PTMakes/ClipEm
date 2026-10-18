#!/usr/bin/env bash
# Builds the ffmpeg binary ClipEm ships for re-exporting smaller copies of
# saved clips (App/Shared/Scripts/clip_reexport.py), splitting their audio
# tracks, and the clip editor (App/Shared/tray-widget/clip_edit.py), from
# official source:
#   - FFmpeg  https://github.com/FFmpeg/FFmpeg          (pinned tag + commit)
#   - x264    https://code.videolan.org/videolan/x264   (pinned commit)
#
# Targets (both fully static - no runtime dependencies on the user's system):
#   linux-x86_64    native build
#   windows-x86_64  cross-compiled with mingw-w64
#
# The build is deliberately minimal: the ffmpeg CLI only, H.264 via x264,
# FFmpeg's native AAC encoder, the demuxers/decoders OBS recordings can
# contain, MP4/MKV output, JPEG thumbnails, raw PCM (waveforms) and the
# filters the clip editor's exports use. No network, no hardware
# acceleration, no autodetected system libraries - so the result is
# reproducible and small.
#
# Output (in Dev/dist/ffmpeg/, reused if already built for these pins):
#   ffmpeg-<ver>-<rev>-linux-x86_64.tar.gz   bin/ffmpeg + LICENSE/SOURCES
#   ffmpeg-<ver>-<rev>-windows-x86_64.zip    bin/ffmpeg.exe + LICENSE/SOURCES
#   ffmpeg-<ver>-<rev>-source.tar.gz         the exact FFmpeg + x264 sources built
#                                        (published alongside the binaries -
#                                        the GPL requires offering them)
#
# Requirements (Arch/CachyOS):
#   sudo pacman -S --needed base-devel git nasm mingw-w64-gcc
#
# Usage: ./build/build-ffmpeg.sh [--force]
#   --force     rebuild even if the archives for these pins already exist
#   -h, --help show this help and exit
set -euo pipefail

case "${1:-}" in
  -h|--help)
    # This file's header comment, up to the first line of code.
    sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'
    exit 0 ;;
esac

FFMPEG_TAG="n9.0.2"
FFMPEG_COMMIT="946fcce07b6dcd0331c8cc609192aeff5e1924f8"
X264_COMMIT="b35605ace3ddf7c1a5d67a2eb553f034aef41d55"  # x264 "stable" branch
# ClipEm's own build revision: bump it whenever FFMPEG_COMMON_FLAGS change, so
# the archives get new names (and hashes) and installs pick the new build up.
BUILD_REV="r3"

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$DEV_DIR/dist/ffmpeg"
WORK_DIR="$DEV_DIR/.ffmpeg-build"
VERSION="${FFMPEG_TAG#n}"
LINUX_ARCHIVE="$OUT_DIR/ffmpeg-$VERSION-$BUILD_REV-linux-x86_64.tar.gz"
WINDOWS_ARCHIVE="$OUT_DIR/ffmpeg-$VERSION-$BUILD_REV-windows-x86_64.zip"
SOURCE_ARCHIVE="$OUT_DIR/ffmpeg-$VERSION-$BUILD_REV-source.tar.gz"
JOBS="$(nproc)"

if [[ "${1:-}" != "--force" && -f "$LINUX_ARCHIVE" && -f "$WINDOWS_ARCHIVE" && -f "$SOURCE_ARCHIVE" ]]; then
  echo "ffmpeg $VERSION ($BUILD_REV) already built in $OUT_DIR (pass --force to rebuild)"
  exit 0
fi

for tool in git make nasm pkg-config gcc x86_64-w64-mingw32-gcc zip tar; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "$tool is required - see the Requirements comment at the top of this script." >&2
    exit 1
  }
done

# --- Sources ---------------------------------------------------------------
fetch() {  # <url> <commit> <dir>
  local url="$1" commit="$2" dir="$3"
  if [[ ! -d "$dir/.git" ]]; then
    git init -q "$dir"
    git -C "$dir" remote add origin "$url"
  fi
  git -C "$dir" fetch -q --depth 1 origin "$commit"
  git -C "$dir" checkout -q --force FETCH_HEAD
  git -C "$dir" clean -qfdx
  [[ "$(git -C "$dir" rev-parse HEAD)" == "$commit" ]] || {
    echo "$url: checked out $(git -C "$dir" rev-parse HEAD), expected $commit" >&2
    exit 1
  }
}

mkdir -p "$WORK_DIR" "$OUT_DIR"
echo "=== Fetching FFmpeg $FFMPEG_TAG and x264 ==="
fetch https://github.com/FFmpeg/FFmpeg.git "$FFMPEG_COMMIT" "$WORK_DIR/src/ffmpeg"
fetch https://code.videolan.org/videolan/x264.git "$X264_COMMIT" "$WORK_DIR/src/x264"

# Guard against the tag having been moved upstream.
tag_commit="$(git ls-remote https://github.com/FFmpeg/FFmpeg.git "refs/tags/$FFMPEG_TAG^{}" | awk '{print $1}')"
[[ "$tag_commit" == "$FFMPEG_COMMIT" ]] || {
  echo "FFmpeg tag $FFMPEG_TAG now points at $tag_commit, not the pinned $FFMPEG_COMMIT - investigate." >&2
  exit 1
}

# --- Build -----------------------------------------------------------------
# Everything OBS can write to a recording (H.264/HEVC/AV1 video, AAC/Opus
# audio, in MP4/MOV/MKV/FLV/TS) must be readable; output is H.264+AAC in
# MP4/MKV, JPEG thumbnails, and raw PCM for the editor's waveforms.
# shellcheck disable=SC2054  # commas are part of the configure values
FFMPEG_COMMON_FLAGS=(
  --enable-gpl --enable-libx264
  --disable-autodetect --disable-network --disable-doc --disable-debug
  --disable-ffplay --disable-ffprobe
  --enable-static --disable-shared
  --disable-everything
  --enable-protocol=file,pipe
  --enable-demuxer=mov,matroska,flv,mpegts,aac,h264,hevc
  --enable-decoder=h264,hevc,av1,aac,aac_latm,opus,mp3,pcm_s16le,pcm_f32le
  --enable-parser=h264,hevc,av1,aac,aac_latm,opus
  --enable-encoder=libx264,aac,mjpeg,pcm_s16le
  --enable-muxer=mp4,matroska,null,pcm_s16le,image2
  --enable-bsf=aac_adtstoasc,h264_mp4toannexb,extract_extradata
  --enable-filter=scale,format,aresample,aformat,null,anull,trim,atrim,setpts,asetpts
  --enable-filter=concat,volume,amix,adelay,apad,afade,asplit,split
  --enable-filter=loudnorm,alimiter,pan,channelsplit,join,agate
  --enable-swscale --enable-swresample
)

build_target() {  # <name> <host-triplet-or-empty>
  local name="$1" host="$2" prefix="$WORK_DIR/$1/prefix"
  echo -e "\n=== Building x264 ($name) ==="
  rm -rf "${WORK_DIR:?}/${name:?}"
  mkdir -p "$WORK_DIR/$name"
  cp -a "$WORK_DIR/src/x264" "$WORK_DIR/$name/x264"
  cp -a "$WORK_DIR/src/ffmpeg" "$WORK_DIR/$name/ffmpeg"
  # What official release tarballs ship - otherwise the shallow checkout
  # makes `ffmpeg -version` report a commit hash instead of the release.
  echo "$VERSION" > "$WORK_DIR/$name/ffmpeg/VERSION"

  local x264_flags=(--prefix="$prefix" --enable-static --enable-pic --disable-cli --disable-opencl --bit-depth=8)
  local ffmpeg_flags=(--prefix="$prefix" --pkg-config-flags=--static
    --extra-cflags="-I$prefix/include" --extra-ldflags="-L$prefix/lib")
  if [[ -n "$host" ]]; then
    x264_flags+=(--host="$host" --cross-prefix="$host-")
    # Plain pkg-config: the distro's <host>-pkg-config wrapper overrides
    # PKG_CONFIG_LIBDIR and would miss the x264 built into $prefix.
    ffmpeg_flags+=(--target-os=mingw32 --arch=x86_64 --cross-prefix="$host-"
      --pkg-config=pkg-config --extra-ldflags="-static -static-libgcc")
  else
    ffmpeg_flags+=(--extra-ldflags="-static" --extra-ldexeflags="-static")
  fi

  (cd "$WORK_DIR/$name/x264" && ./configure "${x264_flags[@]}" && make -j"$JOBS" && make install)

  echo -e "\n=== Building FFmpeg $FFMPEG_TAG ($name) ==="
  (cd "$WORK_DIR/$name/ffmpeg" \
    && PKG_CONFIG_PATH="$prefix/lib/pkgconfig" PKG_CONFIG_LIBDIR="$prefix/lib/pkgconfig" \
       ./configure "${FFMPEG_COMMON_FLAGS[@]}" "${ffmpeg_flags[@]}" \
    && make -j"$JOBS")
}

write_notices() {  # <dir>
  cp "$WORK_DIR/src/ffmpeg/COPYING.GPLv3" "$1/LICENSE.txt"
  cat > "$1/SOURCES.txt" <<EOF
This ffmpeg was built for ClipEm from unmodified official sources:
  FFmpeg $FFMPEG_TAG  https://github.com/FFmpeg/FFmpeg  commit $FFMPEG_COMMIT
  x264             https://code.videolan.org/videolan/x264  commit $X264_COMMIT
Licensed under the GNU GPL (see LICENSE.txt). The exact source code is
published next to this download as ffmpeg-$VERSION-$BUILD_REV-source.tar.gz, and the
build script is Dev/build/build-ffmpeg.sh in the ClipEm repository.
EOF
}

package() {  # <name> <binary> <archive>
  local stage="$WORK_DIR/$1/package"
  rm -rf "$stage"
  mkdir -p "$stage/ffmpeg/bin"
  cp "$WORK_DIR/$1/ffmpeg/$2" "$stage/ffmpeg/bin/"
  write_notices "$stage/ffmpeg"
  find "$stage" -exec touch -h -d "@$(git -C "$WORK_DIR/src/ffmpeg" log -1 --format=%ct)" {} +
  rm -f "$3"
  if [[ "$3" == *.zip ]]; then
    (cd "$stage" && find ffmpeg -type f | LC_ALL=C sort | zip -X -q "$3" -@)
  else
    tar --sort=name --owner=0 --group=0 --numeric-owner -C "$stage" -cf - ffmpeg | gzip -n > "$3"
  fi
  echo "Wrote $3"
}

build_target linux-x86_64 ""
strip "$WORK_DIR/linux-x86_64/ffmpeg/ffmpeg"
check_has() {  # <-encoders|-muxers|-filters> <name>...
  local kind="$1" listing
  shift
  listing="$("$WORK_DIR/linux-x86_64/ffmpeg/ffmpeg" -hide_banner "$kind" 2>/dev/null)"
  for name in "$@"; do
    grep -qE "^ *[A-Z.|]+ +$name( |$)" <<<"$listing" || { echo "Linux build has no $name ($kind)" >&2; exit 1; }
  done
}
check_has -encoders libx264 aac mjpeg pcm_s16le
check_has -muxers mp4 matroska null s16le image2
check_has -filters concat volume amix adelay apad afade asplit split scale loudnorm alimiter pan channelsplit join agate crop
if ldd "$WORK_DIR/linux-x86_64/ffmpeg/ffmpeg" >/dev/null 2>&1; then
  echo "Linux build is dynamically linked - expected a static binary" >&2
  exit 1
fi
package linux-x86_64 ffmpeg "$LINUX_ARCHIVE"

build_target windows-x86_64 x86_64-w64-mingw32
x86_64-w64-mingw32-strip "$WORK_DIR/windows-x86_64/ffmpeg/ffmpeg.exe"
package windows-x86_64 ffmpeg.exe "$WINDOWS_ARCHIVE"

echo -e "\n=== Source archive (GPL) ==="
rm -f "$SOURCE_ARCHIVE"
tar --sort=name --owner=0 --group=0 --numeric-owner --exclude=.git \
  -C "$WORK_DIR/src" -cf - ffmpeg x264 | gzip -n > "$SOURCE_ARCHIVE"
echo "Wrote $SOURCE_ARCHIVE"

rm -rf "$WORK_DIR/linux-x86_64" "$WORK_DIR/windows-x86_64"
echo -e "\nDone: ffmpeg $VERSION ($BUILD_REV) for Linux and Windows in $OUT_DIR"
