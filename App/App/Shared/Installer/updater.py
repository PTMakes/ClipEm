"""
Self-update logic for ClipEm's installed "Shared" code layer (Installer/,
tray-widget/, Scripts/, VERSION) - the app-level code that changes every
release. The pinned OBS/Python binaries under App/Windows|Linux (and the
tray's packages) are components.py's job: they can only be replaced while
nothing ClipEm runs is open, so run.py installs them at launch.

Trust chain: install-manifest.json must carry a valid Ed25519 signature
(install-manifest.json.sig, made at release time with a key that never
leaves the maintainer's machine) from the public key shipped in
signing_public_key.txt. Only then are the sha256 values it lists trusted
for the downloads themselves. A compromised web host alone can't push code.

The update is staged next to the live files and swapped in by rename, with
the previous files kept until every rename has succeeded - a failure part
way through rolls back instead of leaving a half-deleted Installer/.
User-owned files (OBS-Config, run_config.json, update_config.json) are never
touched.
"""

import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile

import ed25519_verify
from clipem_common import (
    SHARED_DIR,
    SIGNING_PUBLIC_KEY_PATH,
    load_json,
    read_version,
    sha256_of_file,
    write_json_atomic,
)

# Kept in Shared/ (not the installer dir) so it survives updates and
# reinstalls, same as run_config.json.
UPDATE_CONFIG_PATH = os.path.join(SHARED_DIR, "update_config.json")
# check_for_updates: look for a newer version when ClipEm launches and
#   offer it (never applied without asking).
# install_on_next_launch: the user chose "update during next restart" - the
#   next launch applies the update before starting OBS.
# skipped_version: the user chose "do not update" for this version - the
#   launch check stays quiet until a newer one is published.
# source_repo: the GitHub repo ("owner/name") whose releases updates come
#   from. Its manifests still have to be signed with signing_public_key.txt.
# update_channel: CHANNEL_RELEASES follows published releases,
#   CHANNEL_LATEST the rolling "dev" prerelease (Dev/release.sh --dev).
DEFAULT_SOURCE_REPO = "PTMakes/ClipEm"
CHANNEL_RELEASES, CHANNEL_LATEST = "releases", "latest"
UPDATE_CHANNELS = (CHANNEL_RELEASES, CHANNEL_LATEST)
DEV_RELEASE_TAG = "dev"
DEFAULT_UPDATE_CONFIG = {
    "check_for_updates": True,
    "install_on_next_launch": False,
    "skipped_version": None,
    "source_repo": DEFAULT_SOURCE_REPO,
    "update_channel": CHANNEL_RELEASES,
}

MANIFEST_NAME = "install-manifest.json"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REPO_URL_RE = re.compile(r"^https://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
                          re.IGNORECASE)
# Short timeout: this may run on every launch of run.py, so a hung or slow
# network shouldn't noticeably delay starting OBS.
REQUEST_TIMEOUT_SECONDS = 5
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_MANIFEST_BYTES = 256 * 1024

# Sanity limits for the shared zip (the real one is well under 1 MB / a few
# dozen files) - refuses a zip bomb even if one were ever signed.
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000

# Left untouched by apply_update() below, relative to App/Shared (with "/"
# separators) - anything a setup wizard run or the running app itself writes
# to, as opposed to code/scripts shipped fresh with every release. Top-level
# entries are simply never swapped; nested ones live inside folders that are
# swapped wholesale, so the live copy is carried over into the new folder.
# Split in three so a clean reinstall (reinstall.py) can reset the OBS or
# ClipEm settings while still keeping the rest.
OBS_SETTINGS_PATHS = {
    "OBS-Config",
    "pending_obs_settings.json",
    # Which apps are on which audio track - goes with the OBS settings that turn split tracks on.
    "app_tracks.json",
}
CLIPEM_SETTINGS_PATHS = {
    "run_config.json",
    "update_config.json",
    # Setup's "replace the clip sound" copies the user's .wav over this.
    "Scripts/replay_buffer_save_sound.wav",
}
ALWAYS_PRESERVED_PATHS = {
    # Single-instance locks of the (possibly still running) tray widget and
    # its settings dialog - losing them lets a second tray icon start.
    "tray-widget/.tray.lock",
    "tray-widget/.settings.lock",
    "tray-widget/.clip_browser.lock",
    "tray-widget/.clip_browser.url",
    # The macOS Finder alias Setup creates points at this launcher.
    "Installer/launch_obs.command",
}
PRESERVED_RELATIVE_PATHS = OBS_SETTINGS_PATHS | CLIPEM_SETTINGS_PATHS | ALWAYS_PRESERVED_PATHS

STAGING_DIR_NAME = ".update-staging"
BACKUP_DIR_NAME = ".update-backup"


class UpdateError(Exception):
    pass


def load_update_config():
    config = dict(DEFAULT_UPDATE_CONFIG)
    loaded = load_json(UPDATE_CONFIG_PATH)
    if isinstance(loaded, dict):
        # Installs from before update prompts saved {"auto_update": bool};
        # keep honouring an opt-out made there.
        legacy = loaded.pop("auto_update", None)
        if legacy is not None and "check_for_updates" not in loaded:
            loaded["check_for_updates"] = bool(legacy)
        config.update(loaded)
    return config


def save_update_config(config):
    write_json_atomic(UPDATE_CONFIG_PATH, config)


def change_update_config(**changes):
    """Saves `changes` over the current update config; returns the result."""
    config = load_update_config()
    config.update(changes)
    save_update_config(config)
    return config


def get_local_version():
    return read_version()


def is_valid_repo(repo):
    return isinstance(repo, str) and bool(_REPO_RE.match(repo)) and ".." not in repo


def repo_url(repo):
    """The repository's GitHub link, as Settings shows it."""
    return f"https://github.com/{repo}"


def repo_from_url(text):
    """The "owner/name" of a GitHub repository link the user typed
    (https://github.com/owner/name, with or without www., a trailing "/" or
    ".git") - or a bare owner/name. None if it isn't one."""
    text = (text or "").strip()
    match = _REPO_URL_RE.match(text)
    repo = match.group(1) if match else text
    return repo if is_valid_repo(repo) else None


def manifest_url(config=None):
    """Where the signed install manifest lives for the configured source repo
    and channel. GitHub's /releases/latest/ never points at a prerelease, so
    the "releases" channel can't pick up a dev build."""
    config = config or load_update_config()
    repo = config.get("source_repo") or DEFAULT_SOURCE_REPO
    if not is_valid_repo(repo):
        raise UpdateError(f"The update source repo {repo!r} isn't a GitHub \"owner/name\".")
    if config.get("update_channel") == CHANNEL_LATEST:
        return f"https://github.com/{repo}/releases/download/{DEV_RELEASE_TAG}/{MANIFEST_NAME}"
    return f"https://github.com/{repo}/releases/latest/download/{MANIFEST_NAME}"


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def require_https(url):
    if urllib.parse.urlparse(url).scheme != "https":
        raise UpdateError(f"Refusing non-HTTPS URL: {url}")
    return url


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    # GitHub release downloads redirect to its asset CDN - follow that, but
    # never a hop down to plain http.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler)


def _open_url(url, timeout):
    # Some CDNs answer urllib's default "Python-urllib/x.y" User-Agent with
    # 403 Forbidden, so identify as ClipEm instead.
    request = urllib.request.Request(require_https(url), headers={"User-Agent": "ClipEm"})
    return _opener.open(request, timeout=timeout)


def _read_url(url, timeout, max_bytes):
    with _open_url(url, timeout) as res:
        data = res.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UpdateError(f"Response from {url} is larger than expected.")
    return data


def download_file(url, dest_path, expected_sha256, timeout=DOWNLOAD_TIMEOUT_SECONDS):
    """Streams `url` to dest_path and checks its sha256. Raises UpdateError
    (and removes the partial file) on any mismatch."""
    with _open_url(url, timeout) as res, open(dest_path, "wb") as f:
        shutil.copyfileobj(res, f, 1024 * 1024)
    actual = sha256_of_file(dest_path)
    if actual != expected_sha256.lower():
        os.remove(dest_path)
        raise UpdateError(
            f"Checksum mismatch for {url} (expected {expected_sha256}, got {actual}) - "
            "the download may be corrupted or tampered with."
        )


def load_public_key():
    try:
        with open(SIGNING_PUBLIC_KEY_PATH, "r", encoding="ascii") as f:
            key = ed25519_verify.decode_key_or_signature(f.read())
    except (OSError, ValueError) as exc:
        raise UpdateError(f"No valid release signing key installed ({SIGNING_PUBLIC_KEY_PATH}): {exc}") from exc
    if len(key) != 32:
        raise UpdateError(f"Release signing key in {SIGNING_PUBLIC_KEY_PATH} is not an Ed25519 key.")
    return key


def parse_signed_manifest(manifest_bytes, signature_text, public_key=None):
    public_key = public_key or load_public_key()
    try:
        signature = ed25519_verify.decode_key_or_signature(signature_text)
    except ValueError as exc:
        raise UpdateError(f"Malformed manifest signature: {exc}") from exc
    if not ed25519_verify.verify(public_key, manifest_bytes, signature):
        raise UpdateError("Install manifest signature is INVALID - refusing to trust it.")
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or "version" not in manifest:
        raise UpdateError("Install manifest is missing its version.")
    return manifest


def fetch_manifest(config=None):
    """The install manifest, only after its Ed25519 signature checks out."""
    url = manifest_url(config)
    manifest_bytes = _read_url(url, REQUEST_TIMEOUT_SECONDS, MAX_MANIFEST_BYTES)
    signature = _read_url(url + ".sig", REQUEST_TIMEOUT_SECONDS, 1024)
    return parse_signed_manifest(manifest_bytes, signature.decode("ascii", "replace"))


_ARTIFACT_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def artifact_version(entry):
    """The version a manifest download is of, from its file name - "32.2.2"
    for .../OBS-Studio-32.2.2-Windows-x64.zip or linux-obs-install-32.2.2.tar.gz,
    "3.12.14" for cpython-3.12.14+20260924-...tar.gz. None if it has none."""
    match = _ARTIFACT_VERSION_RE.search(os.path.basename(urllib.parse.urlparse(entry.get("url", "")).path))
    return match.group(1) if match else None


def manifest_entry(manifest, platform_key, artifact):
    entry = manifest.get(platform_key, {}).get(artifact)
    if not entry or "url" not in entry or "sha256" not in entry:
        raise UpdateError(f"The install manifest has no {platform_key}/{artifact} entry.")
    return entry


# ---------------------------------------------------------------------------
# Version check
# ---------------------------------------------------------------------------

def get_remote_version():
    """Returns (version, error) - error is None on success, version is None
    on failure."""
    try:
        return fetch_manifest().get("version", "unknown"), None
    except Exception as exc:
        return None, str(exc)


def _digits(text):
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def _parse_version(version):
    """"1.2.10" -> (1, 2, 10, 1, 0); a dev build "1.2.10-dev.202610142130"
    -> (1, 2, 10, 0, 202610142130), so it sorts after 1.2.9 but before the
    1.2.10 release. Tolerant of a leading "v" or non-numeric junk in any
    segment (treated as 0) so a malformed remote version never raises - it
    just compares as very old."""
    release, _, prerelease = version.lstrip("vV").partition("-")
    parts = [_digits(part) for part in release.split(".")]
    parts += [0] * (3 - len(parts))
    return tuple(parts) + ((0, _digits(prerelease)) if prerelease else (1, 0))


def is_dev_version(version):
    return bool(version) and "-" in version


def is_newer(remote_version, local_version):
    if not remote_version or not local_version:
        return False
    if remote_version == local_version:
        return False
    return _parse_version(remote_version) > _parse_version(local_version)


def check_for_update():
    """Returns {"local_version", "remote_version", "shared_update",
    "channel_switch", "components", "update_available", "error"}:
    shared_update is a newer ClipEm (App/Shared), components what
    components.check() found outdated outside it (OBS, the bundled Python,
    the tray's packages). channel_switch: a dev build on the "releases"
    channel is offered the current release even when that's older, so
    switching back from "latest" gets off the dev build."""
    local_version = get_local_version()
    result = {
        "local_version": local_version, "remote_version": None, "shared_update": False,
        "channel_switch": False, "components": {}, "update_available": False, "error": None,
    }
    try:
        config = load_update_config()
        manifest = fetch_manifest(config)
    except Exception as exc:
        result["error"] = str(exc)
        return result
    remote_version = result["remote_version"] = manifest.get("version", "unknown")
    result["channel_switch"] = (
        config.get("update_channel") != CHANNEL_LATEST and is_dev_version(local_version)
        and not is_dev_version(remote_version) and remote_version != local_version
    )
    result["shared_update"] = result["channel_switch"] or is_newer(remote_version, local_version)
    import components  # not at the top: components imports obs_install, which imports this module

    result["components"] = components.check(manifest)
    result["update_available"] = result["shared_update"] or bool(result["components"])
    return result


def describe_update(result):
    """One line per part an update would change, for prompts."""
    lines = []
    if result["shared_update"]:
        lines.append(f"ClipEm {result['local_version']} -> {result['remote_version']}")
    import components

    lines.extend(components.describe(result["components"]))
    return lines


# ---------------------------------------------------------------------------
# Applying an update
# ---------------------------------------------------------------------------

def _platform_key():
    if sys.platform == "win32":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def validate_zip(zf, root="Shared"):
    """Every entry must live under root/, with no absolute paths or ".."
    components, and the archive must stay within sane size limits."""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UpdateError("Update package has too many entries.")
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        parts = name.split("/")
        if name.startswith("/") or ":" in parts[0] or ".." in parts or parts[0] != root:
            raise UpdateError(f"Update package contains an unsafe path: {info.filename}")
        total += info.file_size
        if total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise UpdateError("Update package is larger than expected.")


def _read_package_version(extracted_shared):
    try:
        with open(os.path.join(extracted_shared, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def carry_over_preserved(live_dir, staged_dir, preserved=None):
    """Copies each nested `preserved` (default PRESERVED_RELATIVE_PATHS) file
    that exists in the live install into the staged tree (over any shipped
    copy), so swapping in the staged folder keeps it."""
    for rel_path in sorted(PRESERVED_RELATIVE_PATHS if preserved is None else preserved):
        if "/" not in rel_path:
            continue
        src = os.path.join(live_dir, *rel_path.split("/"))
        if not os.path.isfile(src):
            continue
        dest = os.path.join(staged_dir, *rel_path.split("/"))
        if not os.path.isdir(os.path.dirname(dest)):
            continue  # that folder isn't part of this update - nothing to carry over into
        shutil.copy2(src, dest)


def swap_in(staged_dir, dest_dir, backup_dir, preserved=None, reset=()):
    """Moves every entry of staged_dir (except the top-level `preserved`
    ones, default PRESERVED_RELATIVE_PATHS) into dest_dir by rename, first
    moving any existing entry of the same name into backup_dir. Entries of
    dest_dir named in `reset` go to backup_dir too, even with nothing staged
    to replace them. On failure, every move made so far is undone before
    re-raising."""
    preserved = PRESERVED_RELATIVE_PATHS if preserved is None else preserved
    names = set(os.listdir(staged_dir)) | {name for name in reset if os.path.lexists(os.path.join(dest_dir, name))}
    moved = []  # (name, had_previous)
    os.makedirs(backup_dir, exist_ok=True)
    try:
        for name in sorted(names):
            if name in preserved:
                continue
            dest = os.path.join(dest_dir, name)
            had_previous = os.path.lexists(dest)
            if had_previous:
                os.replace(dest, os.path.join(backup_dir, name))
            moved.append((name, had_previous))
            staged = os.path.join(staged_dir, name)
            if os.path.lexists(staged):
                os.replace(staged, dest)
    except BaseException:
        for name, had_previous in reversed(moved):
            dest = os.path.join(dest_dir, name)
            if os.path.lexists(dest) and not os.path.lexists(os.path.join(staged_dir, name)):
                _remove_path(dest)
            if had_previous:
                os.replace(os.path.join(backup_dir, name), dest)
        raise


def _remove_path(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def apply_update(manifest=None, preserved=None):
    """Downloads and verifies the "shared" zip for this platform and swaps
    it into App/Shared, preserving `preserved` (default
    PRESERVED_RELATIVE_PATHS). The other PRESERVED_RELATIVE_PATHS are reset:
    replaced by the shipped copy, or removed if the package has none - how
    reinstall.py starts the OBS or ClipEm settings afresh.
    Returns (ok, message)."""
    preserved = PRESERVED_RELATIVE_PATHS if preserved is None else set(preserved)
    reset = {path for path in PRESERVED_RELATIVE_PATHS - preserved if "/" not in path}
    try:
        manifest = manifest or fetch_manifest()
        entry = manifest_entry(manifest, _platform_key(), "shared")
    except Exception as exc:
        return False, f"Could not get a trusted update manifest: {exc}"

    manifest_version = manifest["version"]
    staging_dir = os.path.join(SHARED_DIR, STAGING_DIR_NAME)
    backup_dir = os.path.join(SHARED_DIR, BACKUP_DIR_NAME)
    for leftover in (staging_dir, backup_dir):
        shutil.rmtree(leftover, ignore_errors=True)
    os.makedirs(staging_dir)
    try:
        zip_path = os.path.join(staging_dir, "shared.zip")
        download_file(entry["url"], zip_path, entry["sha256"])

        extract_dir = os.path.join(staging_dir, "extracted")
        with zipfile.ZipFile(zip_path) as zf:
            validate_zip(zf)
            zf.extractall(extract_dir)

        extracted_shared = os.path.join(extract_dir, "Shared")
        if not os.path.isdir(extracted_shared):
            return False, "Downloaded update package has an unexpected layout."

        # A package whose VERSION doesn't match the manifest would still
        # look "outdated" after installing, so every launch would re-update.
        package_version = _read_package_version(extracted_shared)
        if package_version != manifest_version:
            return False, (
                f"Update package version ({package_version}) doesn't match the "
                f"published version ({manifest_version}). Aborting update."
            )

        carry_over_preserved(SHARED_DIR, extracted_shared, preserved)
        swap_in(extracted_shared, SHARED_DIR, backup_dir, preserved, reset)
        return True, f"Updated ClipEm to version {manifest_version}."
    except Exception as exc:
        return False, f"Update failed (previous version kept): {exc}"
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)
