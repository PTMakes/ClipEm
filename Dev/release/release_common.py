#!/usr/bin/env python3
"""
Helpers shared by the release tooling (generate_checksums.py,
generate_install_manifest.py, release.sh): hashing, atomic JSON writes,
release URLs, and Ed25519 manifest signing.

Signing uses openssl (3.0+) with a private key that lives OUTSIDE the repo:
  $CLIPEM_SIGNING_KEY, or ~/.config/clipem/signing-key.pem by default.
Create it once with Dev/generate-signing-key.sh. The matching public key is
shipped in App/App/Shared/Installer/signing_public_key.txt and
stamped into ClipEmSetupLinux.sh; clients refuse any manifest it didn't sign.

Releases are GitHub Releases of RELEASE_REPO (PTMakes/ClipEm, or
$CLIPEM_RELEASE_REPO), made with the GitHub CLI (`gh`, logged in with
`gh auth login`). A release's binaries and its signed manifests are all
assets of tag v<version>; dev builds (versions like 1.3.6-dev.202610142130,
see release.sh --dev) replace the assets of the rolling "dev" prerelease.

CLI:
  release_common.py sign <file>...             write <file>.sig for each file
  release_common.py check-key                  fail unless the private key exists and
                                               every shipped copy of the public key
                                               matches it
  release_common.py public-key                 print the base64 public key
  release_common.py check-github               fail unless gh is installed and logged in
  release_common.py release-exists <version>   exit 0 if that version's release exists
  release_common.py upload <version> <file>... create the (draft) release with these
                                               assets, or for a dev version replace the
                                               "dev" prerelease's assets with them
  release_common.py publish <version>          publish the draft release (tags the pushed HEAD)
  release_common.py asset-url <version> <name> print a release asset's download URL
  release_common.py zip-dir <base> <top> <out> deterministic zip of <base>/<top> (entries
                                               sorted, mtimes = $SOURCE_DATE_EPOCH)
  release_common.py pipe --total N --label L    copy stdin to stdout with a progress bar
  release_common.py portable-bundle ...         build the portable Linux bundle zip (see
                                               build_portable_bundle)
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEV_DIR = SCRIPT_DIR.parent
ROOT_DIR = DEV_DIR.parent
APP_INSTALLER_DIR = ROOT_DIR / "App" / "App" / "Shared" / "Installer"
PUBLIC_KEY_FILE = APP_INSTALLER_DIR / "signing_public_key.txt"
LINUX_INSTALLER_SCRIPT = APP_INSTALLER_DIR / "Scripts" / "ClipEmSetupLinux.sh"
LINUX_INSTALLER_KEY_PREFIX = 'CLIPEM_SIGNING_PUBLIC_KEY="'

# Every release file is an asset of a GitHub Release of this repo. The
# website (clipem.app) only mirrors the signed manifests, for installs from
# before updates moved to GitHub (<= 1.3.5), which only know clipem.app.
RELEASE_REPO = os.environ.get("CLIPEM_RELEASE_REPO") or "PTMakes/ClipEm"
DEV_RELEASE_TAG = "dev"
# Uploaded last, so a client never sees a manifest whose files aren't there yet.
MANIFEST_FILES = ("install-manifest.json", "install-manifest.json.sig", "clipem-latest.json", "clipem-latest.json.sig")

URL_TIMEOUT_SECONDS = 60


def sha256_of_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_url(url: str) -> str:
    print(f"  fetching {url} to hash it...")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=URL_TIMEOUT_SECONDS) as res:
        for chunk in iter(lambda: res.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_json(url: str):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=URL_TIMEOUT_SECONDS) as res:
        return json.load(res)


def write_json_atomic(path, data, **dump_kwargs) -> None:
    path = Path(path)
    dump_kwargs.setdefault("indent", 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kwargs)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ProgressBar:
    """A one-line progress bar on stderr, e.g.
      Zipping bundle  [##########----------]  50%  412/824 MB
    Drawn only when stderr is a terminal, so logs stay clean."""

    WIDTH = 28

    def __init__(self, total, label, stream=None):
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty()
        self.total = max(1, int(total))
        self.done = 0
        self.label = label
        self._last_draw = 0.0

    def advance(self, amount, label=None):
        self.done += amount
        if label:
            self.label = label
        now = time.monotonic()
        if now - self._last_draw >= 0.1:
            self._last_draw = now
            self._draw(min(self.done, self.total - 1))  # 100% only once finished

    def finish(self):
        self._draw(self.total)
        if self.enabled:
            self.stream.write("\n")
            self.stream.flush()

    def _draw(self, done):
        if not self.enabled:
            return
        fraction = done / self.total
        filled = int(fraction * self.WIDTH)
        bar = "#" * filled + "-" * (self.WIDTH - filled)
        self.stream.write(
            f"\r  {self.label:<22.22} [{bar}] {fraction:4.0%}  "
            f"{done / 1048576:,.0f}/{self.total / 1048576:,.0f} MB\x1b[K"
        )
        self.stream.flush()


def progress_pipe(total, label, src=None, dest=None) -> int:
    """Copies src to dest (default stdin -> stdout) in chunks, with a
    progress bar against `total` bytes (an estimate is fine). Returns the
    bytes copied."""
    src = src or sys.stdin.buffer
    dest = dest or sys.stdout.buffer
    bar = ProgressBar(total, label)
    copied = 0
    for chunk in iter(lambda: src.read(1024 * 1024), b""):
        dest.write(chunk)
        copied += len(chunk)
        bar.advance(len(chunk))
    dest.flush()
    bar.finish()
    return copied


def tree_size(path) -> int:
    """Bytes of every regular file under path (symlinks not followed)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                total += os.path.getsize(full)
    return total


def write_deterministic_zip(base_dir, top, out_path, epoch, progress=None) -> int:
    """Zips every file under base_dir/top (stored as "top/...") so the same
    input always gives a byte-identical zip: sorted entries, every mtime set
    to `epoch` (UTC), no owner/extra fields, and only the executable bit kept
    from the file mode. Returns the number of files written."""
    base_dir = Path(base_dir)
    # DOS timestamps can't go before 1980.
    date_time = time.gmtime(max(int(epoch), 315532800))[:6]
    files = sorted(
        (p.relative_to(base_dir).as_posix(), p) for p in (base_dir / top).rglob("*") if p.is_file()
    )
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in files:
            info = zipfile.ZipInfo(arcname, date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so external_attr's mode is honored by unzip
            info.external_attr = (0o100755 if os.access(path, os.X_OK) else 0o100644) << 16
            data = path.read_bytes()
            zf.writestr(info, data)
            if progress:
                progress(len(data))
    return len(files)


def build_portable_bundle(shared_zip, obs_install, python_tar, ffmpeg_tar, setup_script, stage_dir,
                          out_zip, epoch, wheelhouse=None) -> None:
    """The offline alternative to ClipEmSetupLinux.sh: a ClipEm/ folder with
    everything the installer would otherwise download (the shared files, the
    Linux OBS build, the portable Python and ffmpeg, and - from build_wheelhouse
    - the tray widget's packages) plus ClipEmSetupLinux.sh, zipped
    deterministically. The unpacked folder is left at stage_dir/ClipEm.
    One progress bar covers every stage, weighted by bytes."""
    stage_dir = Path(stage_dir)
    bundle = stage_dir / "ClipEm"
    linux = bundle / "App" / "Linux"
    shutil.rmtree(bundle, ignore_errors=True)
    linux.mkdir(parents=True)

    with zipfile.ZipFile(shared_zip) as zf:
        shared_members = [m for m in zf.infolist() if not m.is_dir()]
    tars = {}
    for tar_path in (python_tar, ffmpeg_tar):
        with tarfile.open(tar_path) as tf:
            tars[tar_path] = sum(m.size for m in tf if m.isfile())
    unpacked = sum(m.file_size for m in shared_members) + tree_size(obs_install) + sum(tars.values())
    bar = ProgressBar(unpacked * 2, "Unpacking shared files")  # * 2: copied in, then zipped

    with zipfile.ZipFile(shared_zip) as zf:
        for member in shared_members:
            target = Path(zf.extract(member, bundle / "App"))
            mode = (member.external_attr >> 16) & 0o777
            if mode:
                target.chmod(mode)  # zipfile doesn't restore the executable bit itself
            bar.advance(member.file_size)

    bar.advance(0, "Copying Linux OBS")
    for dirpath, _dirnames, filenames in os.walk(obs_install):
        rel = Path(dirpath).relative_to(obs_install)
        (linux / "obs-install" / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            src = Path(dirpath) / name
            shutil.copy2(src, linux / "obs-install" / rel / name, follow_symlinks=False)
            if not src.is_symlink():
                bar.advance(src.stat().st_size)

    for tar_path, label in ((python_tar, "Extracting Python"), (ffmpeg_tar, "Extracting ffmpeg")):
        bar.advance(0, label)
        with tarfile.open(tar_path) as tf:
            for member in tf:
                tf.extract(member, linux, filter="data")
                if member.isfile():
                    bar.advance(member.size)

    if wheelhouse:
        shutil.copytree(wheelhouse, linux / "wheelhouse")
    shutil.copy2(setup_script, bundle / "ClipEmSetupLinux.sh")
    (bundle / "ClipEmSetupLinux.sh").chmod(0o755)

    bar.advance(0, "Zipping bundle")
    write_deterministic_zip(stage_dir, "ClipEm", out_zip, epoch, progress=bar.advance)
    bar.finish()


DOWNLOAD_CACHE_DIR = DEV_DIR / "dist" / "cache"
# The offline zips' wheelhouse targets: the Python each platform's bundle runs.
WHEEL_PLATFORMS = {
    "Windows": ["win_amd64"],
    "Linux": ["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"],
}


def _app_installer_module(name):
    """A module from the app's Installer/ (the same code the clients run)."""
    if str(APP_INSTALLER_DIR) not in sys.path:
        sys.path.insert(0, str(APP_INSTALLER_DIR))
    return __import__(name)


def cached_download(entry, cache_dir=DOWNLOAD_CACHE_DIR) -> Path:
    """A manifest entry's file, downloaded once into cache_dir and checked
    against its sha256 every time it's used."""
    name = Path(urllib.parse.urlparse(entry["url"]).path).name
    path = Path(cache_dir) / name
    if not path.exists() or sha256_of_file(path) != entry["sha256"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(name + ".part")
        print(f"  downloading {entry['url']}")
        request = urllib.request.Request(entry["url"], headers={"User-Agent": "ClipEm-release"})
        with urllib.request.urlopen(request, timeout=URL_TIMEOUT_SECONDS) as res, open(part, "wb") as f:
            shutil.copyfileobj(res, f, 1024 * 1024)
        actual = sha256_of_file(part)
        if actual != entry["sha256"]:
            part.unlink()
            raise SystemExit(f"Checksum mismatch for {entry['url']} (expected {entry['sha256']}, got {actual})")
        part.replace(path)
    return path


def build_wheelhouse(requirement_files, system, python_version, dest) -> list:
    """Downloads the wheels for every pin in requirement_files that applies to
    `system` and `python_version` (markers evaluated by the clients' own
    components.requirement_pins - pip download would evaluate them for this
    machine instead), plus their dependencies, into dest. Returns the pins."""
    components = _app_installer_module("components")
    pins = {}
    for requirements in requirement_files:
        pins.update(components.requirement_pins(str(requirements), system, python_version))
    dest = Path(dest)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    major_minor = ".".join(python_version.split(".")[:2])
    abi = "cp" + major_minor.replace(".", "")
    command = [sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
               "--implementation", "cp", "--python-version", major_minor, "--abi", abi, "--abi", "abi3",
               "--abi", "none", "--dest", str(dest)]
    for platform_tag in WHEEL_PLATFORMS[system]:
        command += ["--platform", platform_tag]
    command += [f"{name}=={version}" for name, version in sorted(pins.items())]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    return sorted(pins)


def _extract_zip(zip_path, dest, skip_suffixes=()) -> None:
    dest = Path(dest)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if member.is_dir() or member.filename.lower().endswith(skip_suffixes):
                continue
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep):
                raise SystemExit(f"Refusing to extract {member.filename} outside {dest}")
            zf.extract(member, dest)


def build_windows_offline_bundle(manifest_path, shared_zip, ffmpeg_zip, tcltk_zip, vcruntime_zip, setup_script,
                                 wheelhouse, stage_dir, out_zip, epoch, cache_dir=DOWNLOAD_CACHE_DIR) -> None:
    """The offline alternative to ClipEmSetupWindows.exe: a ClipEm/ folder laid
    out exactly as bootstrap.ps1 + Setup leave an install - the embeddable
    Python with Tcl/Tk, site-packages and pip already set up, OBS with the
    VC++ runtime next to it, ffmpeg, the shared files and the wheelhouse -
    plus Setup_Windows.bat to run Setup from it. OBS and Python are the
    manifest's (downloaded once into cache_dir and sha256-checked)."""
    windows_python = _app_installer_module("windows_python")
    updater = _app_installer_module("updater")
    manifest = json.loads(Path(manifest_path).read_text())["windows"]
    stage_dir = Path(stage_dir)
    bundle = stage_dir / "ClipEm"
    windows = bundle / "App" / "Windows"
    python_root = windows / windows_python.PYTHON_DIR_NAME
    shutil.rmtree(bundle, ignore_errors=True)
    python_root.mkdir(parents=True)

    print("  shared files")
    _extract_zip(shared_zip, bundle / "App")
    print("  Python, Tcl/Tk and pip")
    _extract_zip(cached_download(manifest["python"], cache_dir), python_root)
    _extract_zip(tcltk_zip, python_root)
    version_info = updater._parse_version(updater.artifact_version(manifest["python"]))[:2]
    if not windows_python.own_pth_path(str(python_root), version_info):
        raise SystemExit(f"The embeddable Python has no python{version_info[0]}{version_info[1]}._pth")
    windows_python.enable_site(str(python_root), version_info)
    windows_python.add_lib_to_pth(str(python_root), version_info)
    # A pure-Python wheel installs by unpacking it into site-packages.
    _extract_zip(cached_download(manifest["pip_wheel"], cache_dir), python_root / "Lib" / "site-packages")
    print("  OBS and the VC++ runtime")
    _extract_zip(cached_download(manifest["obs"], cache_dir), windows, skip_suffixes=(".pdb",))
    if not (windows / "bin" / "64bit" / "obs64.exe").is_file():
        raise SystemExit("The Windows OBS zip has no bin/64bit/obs64.exe")
    _extract_zip(vcruntime_zip, windows / "bin" / "64bit")
    print("  ffmpeg and the wheelhouse")
    _extract_zip(ffmpeg_zip, windows)
    shutil.copytree(wheelhouse, windows / "wheelhouse")
    shutil.copy2(setup_script, bundle / "Setup_Windows.bat")
    # See obs_install.INSTALLED_VERSIONS_PATH.
    write_json_atomic(bundle / "App" / "installed_versions.json", {"obs": updater.artifact_version(manifest["obs"])})

    print("  zipping")
    write_deterministic_zip(stage_dir, "ClipEm", out_zip, epoch)


def add_offline_downloads(manifest_path, version, zips: dict) -> dict:
    """Lists the offline zips ({platform: path}) in the manifest - for
    clipem.app's download links, not read by any client."""
    manifest = json.loads(Path(manifest_path).read_text())
    manifest["offline"] = {
        platform: {
            "url": release_asset_url(version, Path(path).name),
            "sha256": sha256_of_file(path),
            "size": Path(path).stat().st_size,
        }
        for platform, path in sorted(zips.items())
    }
    write_json_atomic(manifest_path, manifest)
    return manifest["offline"]


def is_dev_version(version: str) -> bool:
    return "-" in version


def release_tag(version: str) -> str:
    return DEV_RELEASE_TAG if is_dev_version(version) else f"v{version}"


def release_asset_url(version: str, name: str) -> str:
    return f"https://github.com/{RELEASE_REPO}/releases/download/{release_tag(version)}/{name}"


def github_release_asset_digest(owner_repo: str, tag: str, asset_name: str):
    """sha256 GitHub itself publishes for a release asset (the API's
    "digest" field), or None if the release predates that field or the
    lookup fails. Used to cross-check a first-time upstream pin instead of
    trusting whatever the network returned (trust on first use)."""
    try:
        release = fetch_json(f"https://api.github.com/repos/{owner_repo}/releases/tags/{tag}")
    except Exception as exc:
        print(f"  (could not query GitHub for {owner_repo}@{tag}: {exc})")
        return None
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            digest = asset.get("digest") or ""
            if digest.startswith("sha256:"):
                return digest.split(":", 1)[1]
    # Older releases have no API digest; OBS (among others) lists
    # "<asset>: <sha256>" in the release notes instead.
    match = re.search(rf"^\s*{re.escape(asset_name)}:\s*([0-9a-fA-F]{{64}})\s*$", release.get("body") or "", re.M)
    return match.group(1).lower() if match else None


# ---------------------------------------------------------------------------
# GitHub Releases (gh CLI)
# ---------------------------------------------------------------------------

def _gh(*args, capture=False) -> subprocess.CompletedProcess:
    if not shutil.which("gh"):
        raise SystemExit("The GitHub CLI (gh) is required: https://cli.github.com, then `gh auth login`.")
    return subprocess.run(["gh", *args, "--repo", RELEASE_REPO], text=True, check=False,
                          capture_output=capture)


def _gh_ok(*args) -> str:
    result = _gh(*args, capture=True)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{(result.stderr or result.stdout).strip()}")
    return result.stdout


def check_github() -> None:
    if not shutil.which("gh"):
        raise SystemExit("The GitHub CLI (gh) is required: https://cli.github.com, then `gh auth login`.")
    if subprocess.run(["gh", "auth", "status"], capture_output=True, check=False).returncode != 0:
        raise SystemExit("gh isn't logged in - run `gh auth login`.")
    result = subprocess.run(["gh", "repo", "view", RELEASE_REPO, "--json", "name"], capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        raise SystemExit(f"Can't reach the GitHub repo {RELEASE_REPO}:\n{result.stderr.strip()}")


def release_exists(tag: str) -> bool:
    result = _gh("release", "view", tag, "--json", "tagName", capture=True)
    if result.returncode == 0:
        return True
    if "not found" in (result.stderr or "").lower():
        return False
    raise SystemExit(f"Couldn't check GitHub for release {tag}:\n{result.stderr.strip()}")


def release_asset_names(tag: str) -> set:
    assets = json.loads(_gh_ok("release", "view", tag, "--json", "assets"))["assets"]
    return {asset["name"] for asset in assets}


def _ordered_uploads(files) -> list:
    """Manifests (and their signatures) last."""
    return sorted((Path(f) for f in files), key=lambda f: f.name in MANIFEST_FILES)


def upload_release(version: str, files, commit: str) -> None:
    """A release: a draft release v<version> with every file as an asset -
    published (tagging the release commit) by publish_release once that is
    pushed. `commit` is what the files were built from. A dev version: the rolling "dev" prerelease's assets are
    replaced by these files, manifests last, then stale ones removed."""
    files = _ordered_uploads(files)
    for f in files:
        print(f" - {f.name} ({f.stat().st_size / 1048576:,.1f} MB)", flush=True)
    tag = release_tag(version)
    if not is_dev_version(version):
        # The release commit may not be pushed yet - publish_release points
        # the tag at it once it is.
        _gh_ok("release", "create", tag, "--draft", "--title", f"ClipEm {version}",
               "--notes", f"ClipEm {version}", *[str(f) for f in files])
        return
    notes = f"Development build {version} (commit {commit}) - for testing, not a release."
    if not release_exists(tag):
        _gh_ok("release", "create", tag, "--prerelease", "--title", "ClipEm dev build", "--notes", notes,
               *[str(f) for f in files])
        return
    _gh_ok("release", "edit", tag, "--prerelease", "--notes", notes)
    payload = [f for f in files if f.name not in MANIFEST_FILES]
    manifests = [f for f in files if f.name in MANIFEST_FILES]
    for batch in (payload, manifests):
        if batch:
            _gh_ok("release", "upload", tag, "--clobber", *[str(f) for f in batch])
    for stale in sorted(release_asset_names(tag) - {f.name for f in files}):
        print(f" - removing stale {stale}")
        _gh_ok("release", "delete-asset", tag, stale, "--yes")


def publish_release(version: str, commit: str) -> None:
    _gh_ok("release", "edit", release_tag(version), "--target", commit, "--draft=false", "--latest")


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def signing_key_path() -> Path:
    return Path(os.environ.get("CLIPEM_SIGNING_KEY") or Path.home() / ".config" / "clipem" / "signing-key.pem")


def public_key_from_private(key_path: Path) -> str:
    der = subprocess.run(
        ["openssl", "pkey", "-in", str(key_path), "-pubout", "-outform", "DER"],
        check=True, capture_output=True,
    ).stdout
    if len(der) != 44:
        raise SystemExit(f"{key_path} is not an Ed25519 private key.")
    return base64.b64encode(der[-32:]).decode("ascii")


def shipped_public_keys() -> dict:
    """Every place the public key is embedded -> the value found there."""
    keys = {str(PUBLIC_KEY_FILE): PUBLIC_KEY_FILE.read_text().strip() if PUBLIC_KEY_FILE.exists() else ""}
    value = ""
    for line in LINUX_INSTALLER_SCRIPT.read_text().splitlines():
        if line.startswith(LINUX_INSTALLER_KEY_PREFIX):
            value = line[len(LINUX_INSTALLER_KEY_PREFIX):].rstrip('"')
    keys[str(LINUX_INSTALLER_SCRIPT)] = value
    return keys


def check_key() -> str:
    key_path = signing_key_path()
    if not key_path.exists():
        raise SystemExit(
            f"Release signing key not found at {key_path}.\n"
            "Create one with Dev/generate-signing-key.sh (or point CLIPEM_SIGNING_KEY at it)."
        )
    public_key = public_key_from_private(key_path)
    mismatched = [where for where, value in shipped_public_keys().items() if value != public_key]
    if mismatched:
        raise SystemExit(
            "The shipped public key doesn't match the signing key in "
            f"{key_path}:\n  " + "\n  ".join(mismatched) +
            "\nRun Dev/generate-signing-key.sh --stamp-only to update them."
        )
    return public_key


def sign_file(path) -> Path:
    """Writes <path>.sig: base64 of the raw 64-byte Ed25519 signature over
    the file's exact bytes."""
    path = Path(path)
    with tempfile.TemporaryDirectory() as tmp:
        raw_sig = Path(tmp) / "sig.bin"
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(signing_key_path()), "-rawin",
             "-in", str(path), "-out", str(raw_sig)],
            check=True,
        )
        signature = raw_sig.read_bytes()
    if len(signature) != 64:
        raise SystemExit(f"Unexpected signature length for {path}")
    sig_path = path.with_name(path.name + ".sig")
    sig_path.write_text(base64.b64encode(signature).decode("ascii") + "\n")
    return sig_path


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sign = sub.add_parser("sign")
    sign.add_argument("files", nargs="+")
    sub.add_parser("check-key")
    sub.add_parser("public-key")
    sub.add_parser("check-github")
    exists = sub.add_parser("release-exists")
    exists.add_argument("version")
    upload = sub.add_parser("upload")
    upload.add_argument("--commit", required=True)
    upload.add_argument("version")
    upload.add_argument("files", nargs="+")
    publish = sub.add_parser("publish")
    publish.add_argument("--commit", required=True)
    publish.add_argument("version")
    asset_url = sub.add_parser("asset-url")
    asset_url.add_argument("version")
    asset_url.add_argument("name")
    zip_dir = sub.add_parser("zip-dir")
    zip_dir.add_argument("base")
    zip_dir.add_argument("top")
    zip_dir.add_argument("out")
    pipe = sub.add_parser("pipe")
    pipe.add_argument("--total", type=int, required=True)
    pipe.add_argument("--label", required=True)
    bundle = sub.add_parser("portable-bundle")
    for option in ("--shared-zip", "--obs-install", "--python-tar", "--ffmpeg-tar", "--setup-script",
                   "--stage-dir", "--out"):
        bundle.add_argument(option, required=True)
    bundle.add_argument("--wheelhouse")
    windows_bundle = sub.add_parser("windows-offline-bundle")
    for option in ("--manifest", "--shared-zip", "--ffmpeg-zip", "--tcltk-zip", "--vcruntime-zip",
                   "--setup-script", "--wheelhouse", "--stage-dir", "--out"):
        windows_bundle.add_argument(option, required=True)
    wheels = sub.add_parser("wheelhouse")
    wheels.add_argument("--system", required=True, choices=sorted(WHEEL_PLATFORMS))
    wheels.add_argument("--python-version", required=True)
    wheels.add_argument("--dest", required=True)
    wheels.add_argument("requirements", nargs="+")
    offline = sub.add_parser("add-offline")
    offline.add_argument("--manifest", required=True)
    offline.add_argument("--version", required=True)
    offline.add_argument("--windows", required=True)
    offline.add_argument("--linux", required=True)
    args = parser.parse_args(argv)

    if args.command == "sign":
        check_key()
        for file in args.files:
            print(f" - Signed {file} -> {sign_file(file)}")
    elif args.command == "check-key":
        print(f"Signing key OK ({signing_key_path()}), public key {check_key()}")
    elif args.command == "public-key":
        print(public_key_from_private(signing_key_path()))
    elif args.command == "check-github":
        check_github()
        print(f"GitHub OK ({RELEASE_REPO})")
    elif args.command == "release-exists":
        return 0 if release_exists(release_tag(args.version)) else 1
    elif args.command == "upload":
        upload_release(args.version, args.files, args.commit)
    elif args.command == "publish":
        publish_release(args.version, args.commit)
        print(f" - https://github.com/{RELEASE_REPO}/releases/tag/{release_tag(args.version)}")
    elif args.command == "asset-url":
        print(release_asset_url(args.version, args.name))
    elif args.command == "zip-dir":
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if not epoch:
            raise SystemExit("Set SOURCE_DATE_EPOCH (release.sh uses the App repo's HEAD commit time).")
        count = write_deterministic_zip(args.base, args.top, args.out, epoch)
        print(f" - Zipped {count} files from {Path(args.base) / args.top} -> {args.out}")
    elif args.command == "pipe":
        progress_pipe(args.total, args.label)
    elif args.command == "portable-bundle":
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if not epoch:
            raise SystemExit("Set SOURCE_DATE_EPOCH (release.sh uses the App repo's HEAD commit time).")
        build_portable_bundle(args.shared_zip, args.obs_install, args.python_tar, args.ffmpeg_tar,
                              args.setup_script, args.stage_dir, args.out, epoch, wheelhouse=args.wheelhouse)
        print(f" - Wrote {args.out}")
    elif args.command == "windows-offline-bundle":
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if not epoch:
            raise SystemExit("Set SOURCE_DATE_EPOCH (release.sh uses the App repo's HEAD commit time).")
        build_windows_offline_bundle(args.manifest, args.shared_zip, args.ffmpeg_zip, args.tcltk_zip,
                                     args.vcruntime_zip, args.setup_script, args.wheelhouse, args.stage_dir,
                                     args.out, epoch)
        print(f" - Wrote {args.out}")
    elif args.command == "wheelhouse":
        pins = build_wheelhouse(args.requirements, args.system, args.python_version, args.dest)
        print(f" - {args.system} wheelhouse for Python {args.python_version}: {', '.join(pins)} (+ dependencies)")
    elif args.command == "add-offline":
        for platform, entry in add_offline_downloads(
            args.manifest, args.version.removeprefix("v"), {"windows": args.windows, "linux": args.linux}
        ).items():
            print(f" - {platform}: {entry['url']} ({entry['size'] // (1024 * 1024)} MB)")


if __name__ == "__main__":
    sys.exit(main())
