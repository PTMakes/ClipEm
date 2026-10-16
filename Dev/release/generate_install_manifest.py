#!/usr/bin/env python3
"""
Writes install-manifest.json: what ClipEmSetup.exe (bootstrap.ps1),
linuxinstall.sh, setup.py and the in-app updater download, and the sha256
each download must match. release.sh signs it (install-manifest.json.sig)
before publishing - clients refuse an unsigned or mis-signed manifest.

Artifact hosting:
  - Our own builds (shared zip, from-source Linux OBS, Xft-repacked Linux
    Python, ffmpeg for Linux/Windows) are assets of the GitHub release (tag v<version>, or
    "dev" for a dev build - release.sh uploads them); their sha256 comes straight from the local files.
  - Upstream artifacts (OBS for Windows/macOS, python.org's Windows Python
    and installer, the pip wheel) stay at their own URLs. Their sha256 is
    pinned in upstream-pins.json the first time it's seen - taken from the
    digest the publisher itself lists (GitHub release assets, PyPI) where
    one exists, or hashed from a download (python.org) otherwise - and
    every later release re-checks that pin against the published digest.

Usage: see parse_args() / release.sh.
"""

import argparse
import json
import sys
from pathlib import Path

from release_common import (
    SCRIPT_DIR,
    fetch_json,
    github_release_asset_digest,
    release_asset_url,
    sha256_of_file,
    sha256_of_url,
    write_json_atomic,
)

PINS_PATH = SCRIPT_DIR / "upstream-pins.json"
OBS_REPO = "obsproject/obs-studio"
LOSSLESSCUT_REPO = "mifi/lossless-cut"
# LosslessCut's release assets, per manifest platform - downloaded on request
# by the Clip Browser (Installer/editor_install.py).
LOSSLESSCUT_ASSETS = {"windows": "LosslessCut-win-x64.7z", "linux": "LosslessCut-linux-x64.tar.bz2"}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, help="Where to write install-manifest.json")
    parser.add_argument("--shared-zip", required=True)
    parser.add_argument("--linux-obs-tar", required=True)
    parser.add_argument(
        "--linux-python-tar", required=True,
        help="Xft-enabled build produced by Dev/build/repack-linux-python-xft.sh",
    )
    parser.add_argument("--linux-ffmpeg", required=True, help="Linux archive from Dev/build/build-ffmpeg.sh")
    parser.add_argument("--windows-ffmpeg", required=True, help="Windows archive from Dev/build/build-ffmpeg.sh")
    parser.add_argument(
        "--ffmpeg-source", required=True,
        help="Source tarball from Dev/build/build-ffmpeg.sh - published for the GPL, linked from clipem.app",
    )
    parser.add_argument("--windows-obs-version", required=True)
    parser.add_argument("--windows-python-version", required=True)
    parser.add_argument("--windows-pip-version", required=True)
    parser.add_argument("--macos-obs-version", required=True)
    parser.add_argument("--losslesscut-version", required=True)
    return parser.parse_args(argv)


def load_pins() -> dict:
    if not PINS_PATH.exists():
        return {}
    # Fail hard rather than returning {}: save_pins() would otherwise
    # overwrite the file with only this run's entries, silently dropping
    # every existing pin and re-trusting whatever gets downloaded next.
    try:
        return json.loads(PINS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"Could not read {PINS_PATH}: {exc} - fix or restore it before re-running.") from exc


def pinned_upstream(pins: dict, url: str, published_sha256=None) -> dict:
    if published_sha256:
        published_sha256 = published_sha256.lower()
    pinned = pins.get(url)
    if pinned and published_sha256 and pinned != published_sha256:
        raise SystemExit(
            f"Pinned sha256 for {url} ({pinned}) doesn't match the publisher's own "
            f"digest ({published_sha256}) - investigate before releasing."
        )
    if not pinned:
        if published_sha256:
            print(f"  pinning {url} to its published sha256")
            pinned = published_sha256
        else:
            print(f"  WARNING: no published digest for {url} - pinning the hash of what we download now")
            pinned = sha256_of_url(url)
        pins[url] = pinned
    return {"url": url, "sha256": pinned}


def windows_obs_asset_name(version: str) -> str:
    """OBS renamed its Windows zip when it added an arm64 build (32.0)."""
    major = int(version.split(".")[0])
    return f"OBS-Studio-{version}-Windows-x64.zip" if major >= 32 else f"OBS-Studio-{version}-Windows.zip"


def github_asset(pins: dict, repo: str, tag: str, asset: str) -> dict:
    url = f"https://github.com/{repo}/releases/download/{tag}/{asset}"
    published = None if url in pins else github_release_asset_digest(repo, tag, asset)
    return pinned_upstream(pins, url, published)


def github_obs_asset(pins: dict, version: str, asset: str) -> dict:
    return github_asset(pins, OBS_REPO, version, asset)


def losslesscut_asset(pins: dict, version: str, platform_key: str) -> dict:
    return github_asset(pins, LOSSLESSCUT_REPO, f"v{version}", LOSSLESSCUT_ASSETS[platform_key])


def pypi_wheel(pins: dict, package: str, version: str) -> dict:
    release = fetch_json(f"https://pypi.org/pypi/{package}/{version}/json")
    wheel = next(
        (f for f in release["urls"] if f["packagetype"] == "bdist_wheel" and f["filename"].endswith("-py3-none-any.whl")),
        None,
    )
    if wheel is None:
        raise SystemExit(f"No py3-none-any wheel for {package} {version} on PyPI")
    return pinned_upstream(pins, wheel["url"], wheel["digests"]["sha256"])


def own_asset(args, path_arg: str) -> dict:
    path = Path(path_arg).resolve()
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")
    return {
        "url": release_asset_url(args.version, path.name),
        "sha256": sha256_of_file(path),
    }


def main(argv=None) -> None:
    args = parse_args(argv)
    args.version = args.version.removeprefix("v")
    pins = load_pins()

    shared = own_asset(args, args.shared_zip)
    linux_obs = own_asset(args, args.linux_obs_tar)
    linux_python = own_asset(args, args.linux_python_tar)
    linux_ffmpeg = own_asset(args, args.linux_ffmpeg)
    windows_ffmpeg = own_asset(args, args.windows_ffmpeg)
    ffmpeg_source = own_asset(args, args.ffmpeg_source)

    win_obs = args.windows_obs_version
    win_py = args.windows_python_version
    mac_obs = args.macos_obs_version
    windows = {
        "obs": github_obs_asset(pins, win_obs, windows_obs_asset_name(win_obs)),
        "python": pinned_upstream(
            pins, f"https://www.python.org/ftp/python/{win_py}/python-{win_py}-embed-amd64.zip"
        ),
        # Superseded by tcltk_msi (setup.py's ensure_windows_tkinter) - kept
        # for installs whose setup.py predates it.
        "python_installer": pinned_upstream(
            pins, f"https://www.python.org/ftp/python/{win_py}/python-{win_py}-amd64.exe"
        ),
        "tcltk_msi": pinned_upstream(
            pins, f"https://www.python.org/ftp/python/{win_py}/amd64/tcltk.msi"
        ),
        "pip_wheel": pypi_wheel(pins, "pip", args.windows_pip_version),
        "ffmpeg": windows_ffmpeg,
        "shared": shared,
        "losslesscut": losslesscut_asset(pins, args.losslesscut_version, "windows"),
    }
    macos = {
        "obs_arm64": github_obs_asset(pins, mac_obs, f"OBS-Studio-{mac_obs}-macOS-Apple.dmg"),
        "obs_x86_64": github_obs_asset(pins, mac_obs, f"OBS-Studio-{mac_obs}-macOS-Intel.dmg"),
        "shared": shared,
    }
    linux_losslesscut = losslesscut_asset(pins, args.losslesscut_version, "linux")
    write_json_atomic(PINS_PATH, pins)

    # Key order matters: ClipEmSetupLinux.sh reads linux.python's fields with
    # a plain-text scan (it can't assume a JSON parser yet), expecting the
    # "linux" block, and "python" inside it, before any later "url".
    manifest = {
        "version": args.version,
        "windows": windows,
        "linux": {
            "obs": linux_obs, "python": linux_python, "shared": shared, "ffmpeg": linux_ffmpeg,
            "losslesscut": linux_losslesscut,
        },
        "macos": macos,
        # Corresponding source for the GPL binaries above (not downloaded by
        # any client - clipem.app links to it).
        "sources": {"ffmpeg": ffmpeg_source},
    }
    write_json_atomic(args.out, manifest)
    print(f"Wrote {args.out}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    sys.exit(main())
