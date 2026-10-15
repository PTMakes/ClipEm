"""
Installs ClipEm's own OBS build into App/<platform> - shared by setup.py
(first install) and run.py (which reinstalls it on launch if it's gone, e.g.
after an update on an install that never had it). ClipEm never runs any
other OBS, so without this build there's nothing to launch.

Everything downloaded here is listed in clipem.app's signed install manifest
and checked against its sha256 before it's extracted (fetch_verified), and
the archive is unpacked into a staging folder first, so a failed download or
extraction never leaves a half-installed OBS that looks complete.
"""

import os
import shutil
import sys
import tarfile
import zipfile

import updater
from clipem_common import APP_DIR, load_json, sha256_of_file, write_json_atomic

# Per-machine baseline for the OBS/ffmpeg binaries fetched at install time -
# see record_local_checksums() and setup.verify_checksums().
LOCAL_CHECKSUMS_PATH = os.path.join(APP_DIR, "checksums_local.json")

# {"obs": "<version>"} - what install_obs (and components' OBS update) last
# installed, from its manifest entry. The fallback for knowing the installed
# OBS's version when OBS itself can't be asked (see components).
INSTALLED_VERSIONS_PATH = os.path.join(APP_DIR, "installed_versions.json")

STAGING_DIR_NAME = ".obs-staging"

# OBS's Windows zip ships ~76 MB of debug symbols next to its binaries -
# only useful to a debugger, so they're never extracted.
DEBUG_FILE_SUFFIXES = (".pdb",)


class ObsInstallError(Exception):
    pass


def fetch_verified(platform_key, artifact, dest_path, log=print):
    """Downloads an artifact listed in clipem.app's install manifest and
    checks its sha256 - the manifest itself is only trusted once its release
    signature verifies (updater.fetch_manifest). Raises on any failure;
    nothing unverified is ever extracted or run."""
    manifest = updater.fetch_manifest()
    entry = updater.manifest_entry(manifest, platform_key, artifact)
    log(f"Downloading {entry['url']} ...")
    updater.download_file(entry["url"], dest_path, entry["sha256"], timeout=300)
    log("  sha256 OK")
    return entry


def record_installed_version(component, entry):
    version = updater.artifact_version(entry)
    if version:
        versions = load_json(INSTALLED_VERSIONS_PATH, {}) or {}
        versions[component] = version
        write_json_atomic(INSTALLED_VERSIONS_PATH, versions)


def record_local_checksums(root_dirs, label, log=print):
    """Hashes every file under the given (absolute) root_dirs and merges
    the results into App/checksums_local.json. Called right after a fresh
    OBS/ffmpeg download - those binaries aren't in the shipped
    checksums.json, so this gives this machine its own baseline instead."""
    manifest = load_json(LOCAL_CHECKSUMS_PATH, {}) or {}
    count = 0
    for root_dir in root_dirs:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            for name in filenames:
                full_path = os.path.join(dirpath, name)
                if os.path.islink(full_path):
                    continue
                rel_path = os.path.relpath(full_path, APP_DIR).replace(os.sep, "/")
                manifest[rel_path] = sha256_of_file(full_path)
                count += 1
    write_json_atomic(LOCAL_CHECKSUMS_PATH, manifest, sort_keys=True)
    log(f"Recorded checksums for {count} {label} files (local baseline).")


# system -> (platform folder, manifest platform key, archive suffix,
#            executable relative to the platform folder, checksummed folders)
_LAYOUTS = {
    "Windows": ("Windows", "windows", ".zip", os.path.join("bin", "64bit", "obs64.exe"),
                ("bin", "data", "obs-plugins")),
    "Linux": ("Linux", "linux", ".tar.gz", os.path.join("obs-install", "bin", "obs"), ("obs-install",)),
}


def supported(system):
    return system in _LAYOUTS


def platform_dir(system):
    return os.path.join(APP_DIR, _LAYOUTS[system][0])


def obs_exe(system):
    return os.path.join(platform_dir(system), _LAYOUTS[system][3])


def _extract(archive, dest):
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if not m.lower().endswith(DEBUG_FILE_SUFFIXES)]
            zf.extractall(dest, members=members)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")


def _merge_move(src_dir, dest_dir, last=None):
    """Moves everything under src_dir into dest_dir, keeping files already
    there that the archive doesn't have (e.g. the VC++ runtime DLLs the
    Windows installer puts in bin/64bit). `last` (a path relative to
    src_dir) is moved after everything else, so its presence means the move
    finished."""
    deferred = None
    for dirpath, _dirnames, filenames in os.walk(src_dir):
        rel_dir = os.path.relpath(dirpath, src_dir)
        os.makedirs(os.path.join(dest_dir, rel_dir), exist_ok=True)
        for name in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if last and rel == os.path.normpath(last):
                deferred = rel
                continue
            os.replace(os.path.join(src_dir, rel), os.path.join(dest_dir, rel))
    if deferred:
        os.replace(os.path.join(src_dir, deferred), os.path.join(dest_dir, deferred))


def install_obs(system, log=print):
    """Downloads, verifies and installs ClipEm's OBS for `system` ("Windows"
    or "Linux") if its executable is missing. Returns the executable's path.
    Raises ObsInstallError if it can't be installed."""
    if not supported(system):
        raise ObsInstallError(f"ClipEm can't install OBS on {system} automatically.")
    exe = obs_exe(system)
    if os.path.exists(exe):
        return exe

    folder, platform_key, suffix, exe_rel, checksum_dirs = _LAYOUTS[system]
    dest = platform_dir(system)
    staging = os.path.join(dest, STAGING_DIR_NAME)
    archive = os.path.join(dest, "obs-download" + suffix)
    os.makedirs(dest, exist_ok=True)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        entry = fetch_verified(platform_key, "obs", archive, log=log)
        log("Extracting OBS...")
        _extract(archive, staging)
        if not os.path.isfile(os.path.join(staging, exe_rel)):
            raise ObsInstallError(f"the downloaded OBS package has no {exe_rel.replace(os.sep, '/')}")
        _merge_move(staging, dest, last=exe_rel)
    except ObsInstallError:
        raise
    except Exception as exc:
        raise ObsInstallError(str(exc)) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.exists(archive):
            os.remove(archive)
    if system == "Linux":
        os.chmod(exe, 0o755)

    record_installed_version("obs", entry)
    record_local_checksums([os.path.join(dest, name) for name in checksum_dirs], f"{folder} OBS", log=log)
    log("OBS installed.")
    if system == "Windows":
        warn_if_windows_vc_runtime_missing(dest, log=log)
    return exe


def remove_debug_files(platform_folder, log=print):
    """Deletes debug-symbol files (DEBUG_FILE_SUFFIXES) left by installs made
    before they were skipped, and drops them from the local checksum
    baseline so they aren't reported as missing. Returns how many went."""
    removed = []
    for name in ("bin", "data", "obs-plugins"):
        for dirpath, _dirnames, filenames in os.walk(os.path.join(platform_folder, name)):
            for filename in filenames:
                if filename.lower().endswith(DEBUG_FILE_SUFFIXES):
                    path = os.path.join(dirpath, filename)
                    try:
                        os.remove(path)
                    except OSError:
                        continue
                    removed.append(os.path.relpath(path, APP_DIR).replace(os.sep, "/"))
    if removed:
        manifest = load_json(LOCAL_CHECKSUMS_PATH, {}) or {}
        if any(rel in manifest for rel in removed):
            for rel in removed:
                manifest.pop(rel, None)
            write_json_atomic(LOCAL_CHECKSUMS_PATH, manifest, sort_keys=True)
        log(f"Removed {len(removed)} OBS debug-symbol files (not needed to run OBS).")
    return len(removed)


def warn_if_windows_vc_runtime_missing(windows_dir, log=None):
    """OBS's portable zip needs the Visual C++ runtime. ClipEmSetupWindows.exe
    puts it next to obs64.exe (Dev/installer/windows/extract_vcruntime.py);
    otherwise it has to be installed system-wide. Returns True if it looks
    present."""
    log = log or (lambda message: print(message, file=sys.stderr))
    bin_dir = os.path.join(windows_dir, "bin", "64bit")
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        if not (os.path.exists(os.path.join(bin_dir, name)) or os.path.exists(os.path.join(system32, name))):
            log(
                f"WARNING: {name} not found - OBS won't start without the Microsoft Visual C++ "
                "Redistributable (x64). Install it from https://aka.ms/vs/17/release/vc_redist.x64.exe"
            )
            return False
    return True
