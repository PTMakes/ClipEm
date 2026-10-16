"""
Updates for the parts of ClipEm outside App/Shared, which
updater.apply_update() never touches: ClipEm's OBS, its bundled Python, and
the Python packages the tray widget (and, on Windows, Mic Filter
Configuration) were installed with.

check() compares each with the signed install manifest:
- OBS: the version in the manifest entry's file name vs. the installed OBS's
  own version (falling back to obs_install.INSTALLED_VERSIONS_PATH).
- Python: the same, vs. the bundled interpreter's version.
- Packages: the exact pins in requirements.txt vs. what's installed - only
  for sets that were installed at all (the tray widget is optional).
Only ever upgrades, never downgrades.

install() must run with OBS and every ClipEm Python process closed - run.py
does it at launch, before starting anything. Each part is downloaded and
fully prepared in a staging folder next to the live one, then swapped in by
rename (updater.swap_in rolls back on failure), so a failed download or
extraction leaves the working install alone.

No user data lives in these folders: OBS's settings are in
App/Shared/OBS-Config and only reach OBS through links that
portable_launch_helper.prepare() recreates on every launch - the links in
the old OBS folder are removed before it's deleted, so nothing can follow
them into OBS-Config. The bundled Python on Windows can't be renamed while
run.py runs from it, so that swap is finished by a small PowerShell script
after run.py exits, which then starts ClipEm again.
"""

import importlib.metadata
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile

import obs_install
import updater
import windows_python
from clipem_common import (
    APP_DIR,
    INSTALLER_DIR,
    LOG_DIR,
    ROOT_DIR,
    SHARED_DIR,
    TRAY_PYTHON_PIN_FILENAME,
    install_requirements,
    load_json,
)
from portable_launch_helper import _is_junction

TRAY_REQUIREMENTS = os.path.join(SHARED_DIR, "tray-widget", "requirements.txt")
MIC_FILTER_REQUIREMENTS = os.path.join(INSTALLER_DIR, "mic-filter", "requirements.txt")
# Packages that show each set was installed (requirements names, canonical).
TRAY_MARKER_PACKAGES = ("websocket-client", "pillow")
MIC_FILTER_MARKER_PACKAGES = ("sounddevice",)

STAGING_SUFFIX = ".update-staging"
BACKUP_SUFFIX = ".update-backup"

# What install() asks of its caller once it's done.
CONTINUE = None  # nothing - carry on starting ClipEm
RESTART = "restart"  # run run.py again (Linux: it's now on the new Python)
HANDOFF = "handoff"  # exit now: the Windows Python swap starts ClipEm again

# Visual C++ runtime DLLs ClipEmSetupWindows.exe puts next to obs64.exe (OBS's
# zip doesn't have them) - carried over into a new OBS's bin\64bit.
_VC_RUNTIME_RE = re.compile(r"^(msvcp|vcruntime|concrt|vccorlib|vcomp)140.*\.dll$", re.IGNORECASE)


def _system():
    if sys.platform == "win32":
        return "Windows"
    if sys.platform.startswith("linux"):
        return "Linux"
    return None  # macOS: OBS.app/system Python aren't managed by the manifest


def _platform_key(system):
    return "windows" if system == "Windows" else "linux"


# ---------------------------------------------------------------------------
# What's installed
# ---------------------------------------------------------------------------

def windows_python_root():
    """App/Windows/Python, where python.exe is (flat, as bootstrap.ps1 and
    this module extract it; older installs may have it one level down)."""
    base = os.path.join(APP_DIR, "Windows", windows_python.PYTHON_DIR_NAME)
    for dirpath, _dirnames, filenames in os.walk(base):
        if "python.exe" in filenames:
            return dirpath
    return None


def python_exe(system):
    if system == "Windows":
        root = windows_python_root()
        return os.path.join(root, "python.exe") if root else None
    exe = os.path.join(APP_DIR, "Linux", "python", "bin", "python3")
    return exe if os.path.exists(exe) else None


def _query_python_version(exe):
    if os.path.dirname(os.path.realpath(exe)) == os.path.dirname(os.path.realpath(sys.executable)):
        return platform.python_version()  # this one - pythonw.exe and python.exe share a folder
    try:
        result = subprocess.run(
            [exe, "-c", "import platform; print(platform.python_version())"],
            capture_output=True, text=True, timeout=30,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def installed_python_version(system):
    exe = python_exe(system)
    return _query_python_version(exe) if exe else None


def _file_version_win32(path):
    """"32.2.2" from an executable's version resource, or None."""
    import ctypes
    from ctypes import wintypes

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
            "dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags",
            "dwFileOS", "dwFileType", "dwFileSubtype", "dwFileDateMS", "dwFileDateLS",
        )]

    version_dll = ctypes.windll.version
    size = version_dll.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(path, 0, size, data):
        return None
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version_dll.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)) or not length.value:
        return None
    info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    return f"{info.dwFileVersionMS >> 16}.{info.dwFileVersionMS & 0xFFFF}.{info.dwFileVersionLS >> 16}"


def installed_obs_version(system):
    exe = obs_install.obs_exe(system)
    if not os.path.exists(exe):
        return None
    version = None
    try:
        if system == "Windows":
            version = _file_version_win32(exe)
        else:
            # "OBS Studio - 32.2.2-modified"
            result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
            match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
            version = match.group(1) if match else None
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        version = None
    if version in (None, "0.0.0"):
        version = (load_json(obs_install.INSTALLED_VERSIONS_PATH, {}) or {}).get("obs")
    return version


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

def _canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower().strip()


_MARKER_CLAUSE_RE = re.compile(r"""^\s*(\w+)\s*(==|!=|<=|>=|<|>)\s*["']([^"']*)["']\s*$""")
_COMPARE = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
}


def _version_tuple(version):
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _marker_applies(marker, system, python_version):
    """Evaluates the environment markers requirements.txt uses (sys_platform,
    platform_system, python_version; joined with "and"). Anything else
    raises ValueError - requirements.txt mustn't use what this can't check."""
    values = {
        "sys_platform": "win32" if system == "Windows" else "linux",
        "platform_system": system,
        "python_version": ".".join(python_version.split(".")[:2]),
    }
    for clause in marker.split(" and "):
        match = _MARKER_CLAUSE_RE.match(clause)
        if not match or match.group(1) not in values:
            raise ValueError(f"Unsupported requirement marker: {marker.strip()}")
        name, op, wanted = match.groups()
        actual = values[name]
        if name == "python_version":
            actual, wanted = _version_tuple(actual), _version_tuple(wanted)
        elif op not in ("==", "!="):
            raise ValueError(f"Unsupported requirement marker: {marker.strip()}")
        if not _COMPARE[op](actual, wanted):
            return False
    return True


def requirement_pins(path, system, python_version):
    """{canonical name: version} of the exact pins in a requirements file
    that apply to this platform and Python."""
    pins = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            requirement, _, marker = line.partition(";")
            name, sep, version = requirement.partition("==")
            if not sep:
                raise ValueError(f"Not an exact pin in {os.path.basename(path)}: {line}")
            if marker and not _marker_applies(marker, system, python_version):
                continue
            pins[_canonical(name)] = version.strip()
    return pins


def installed_packages(path):
    """{canonical name: version} of the packages installed in folder `path`."""
    if not path or not os.path.isdir(path):
        return {}
    found = {}
    for dist in importlib.metadata.distributions(path=[path]):
        name = dist.metadata["Name"]
        if name:
            found[_canonical(name)] = dist.version
    return found


def _linux_tray_deps_dir():
    return os.path.join(APP_DIR, "Linux", "tray-deps")


def _linux_tray_python():
    """The interpreter the tray widget's packages were installed for (see
    run._resolve_tray_python)."""
    try:
        with open(os.path.join(_linux_tray_deps_dir(), TRAY_PYTHON_PIN_FILENAME), "r", encoding="utf-8") as f:
            pinned = f.read().strip()
        if pinned and os.path.exists(pinned):
            return pinned
    except OSError:
        pass
    return python_exe("Linux") or sys.executable


def _windows_site_packages(python_root):
    return os.path.join(python_root, "Lib", "site-packages")


def installed_package_sets(system):
    """The requirements files of the package sets installed on this machine."""
    if system == "Linux":
        return [TRAY_REQUIREMENTS] if installed_packages(_linux_tray_deps_dir()) else []
    root = windows_python_root()
    installed = installed_packages(_windows_site_packages(root)) if root else {}
    sets = []
    if any(name in installed for name in TRAY_MARKER_PACKAGES):
        sets.append(TRAY_REQUIREMENTS)
    if any(name in installed for name in MIC_FILTER_MARKER_PACKAGES):
        sets.append(MIC_FILTER_REQUIREMENTS)
    return sets


def outdated_package_sets(system):
    if system == "Linux":
        folder, python_version = _linux_tray_deps_dir(), _query_python_version(_linux_tray_python())
    else:
        root = windows_python_root()
        folder = _windows_site_packages(root) if root else None
        python_version = installed_python_version(system)
    if not folder or not python_version:
        return []
    installed = installed_packages(folder)
    outdated = []
    for requirements in installed_package_sets(system):
        pins = requirement_pins(requirements, system, python_version)
        if any(installed.get(name) != version for name, version in pins.items()):
            outdated.append(requirements)
    return outdated


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def check(manifest, system=None):
    """What's outdated compared with `manifest`, e.g.
    {"obs": ("32.2.2", "32.3.0"), "python": ("3.12.10", "3.12.11"),
     "packages": ["<requirements.txt path>", ...]} - only the keys that are."""
    system = system or _system()
    if system is None:
        return {}
    key = _platform_key(system)
    found = {}
    for name, installed in (("obs", installed_obs_version), ("python", installed_python_version)):
        entry = manifest.get(key, {}).get(name)
        if not entry:
            continue
        remote = updater.artifact_version(entry)
        local = installed(system)
        if updater.is_newer(remote, local):
            found[name] = (local, remote)
    packages = outdated_package_sets(system)
    if packages:
        found["packages"] = packages
    return found


def describe(plan):
    lines = []
    if "obs" in plan:
        lines.append("OBS {} -> {}".format(*plan["obs"]))
    if "python" in plan:
        lines.append("ClipEm's Python {} -> {}".format(*plan["python"]))
    if "packages" in plan:
        names = ["tray widget" if path == TRAY_REQUIREMENTS else "mic filter" for path in plan["packages"]]
        lines.append(f"Updated {' and '.join(names)} packages")
    return lines


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def _download(manifest, system, artifact, dest_path, log):
    entry = updater.manifest_entry(manifest, _platform_key(system), artifact)
    log(f"Downloading {entry['url']} ...")
    updater.download_file(entry["url"], dest_path, entry["sha256"], timeout=600)
    return entry


def _remove_links(folder):
    """Removes every symlink/junction inside `folder` (without following
    them), so deleting the folder afterwards can't reach what they point at."""
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                os.unlink(path)
            elif _is_junction(path):
                os.rmdir(path)
        dirnames[:] = [name for name in dirnames if os.path.isdir(os.path.join(dirpath, name))]


def _clean(*paths):
    for path in paths:
        if os.path.isdir(path):
            _remove_links(path)
        shutil.rmtree(path, ignore_errors=True)


def install_obs(manifest, system, log):
    """Replaces OBS's program folders (App/Linux/obs-install, or
    App/Windows/bin+data+obs-plugins) with the manifest's OBS."""
    folder, _key, suffix, exe_rel, program_dirs = obs_install._LAYOUTS[system]
    dest = obs_install.platform_dir(system)
    staging, backup = os.path.join(dest, ".obs" + STAGING_SUFFIX), os.path.join(dest, ".obs" + BACKUP_SUFFIX)
    archive = os.path.join(dest, "obs-update" + suffix)
    _clean(staging, backup)
    os.makedirs(staging)
    try:
        entry = _download(manifest, system, "obs", archive, log)
        log("Extracting OBS...")
        obs_install._extract(archive, staging)
        os.remove(archive)
        if not os.path.isfile(os.path.join(staging, exe_rel)):
            raise updater.UpdateError(f"the downloaded OBS package has no {exe_rel.replace(os.sep, '/')}")
        unexpected = sorted(set(os.listdir(staging)) - set(program_dirs))
        if unexpected:
            raise updater.UpdateError(f"the downloaded OBS package has unexpected folders: {', '.join(unexpected)}")
        if system == "Windows":
            old_bin, new_bin = os.path.join(dest, "bin", "64bit"), os.path.join(staging, "bin", "64bit")
            for name in os.listdir(old_bin) if os.path.isdir(old_bin) else []:
                if _VC_RUNTIME_RE.match(name) and not os.path.exists(os.path.join(new_bin, name)):
                    shutil.copy2(os.path.join(old_bin, name), os.path.join(new_bin, name))
        else:
            os.chmod(os.path.join(staging, exe_rel), 0o755)
        # Linux: obs-install/config/obs-studio links to OBS-Config - recreated
        # by portable_launch_helper.prepare() on the next launch.
        for name in program_dirs:
            if os.path.isdir(os.path.join(dest, name)):
                _remove_links(os.path.join(dest, name))
        updater.swap_in(staging, dest, backup)
    finally:
        if os.path.exists(archive):
            os.remove(archive)
        _clean(staging, backup)
    obs_install.record_installed_version("obs", entry)
    obs_install.record_local_checksums([os.path.join(dest, name) for name in program_dirs], f"{folder} OBS", log=log)
    log(f"OBS updated to {updater.artifact_version(entry)}.")


def _pip_install(python, requirements, log, target=None, force=False):
    args = [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location"]
    if force:
        args.append("--force-reinstall")
    if target:
        args += ["--target", target]
    log(f"Installing {os.path.relpath(requirements, SHARED_DIR)}...")
    install_requirements(args, requirements)


def _build_linux_tray_deps(python, dest, log):
    """A fresh tray-deps folder for `python` at dest (pip --target into an
    existing folder doesn't replace what's there)."""
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    _pip_install(python, TRAY_REQUIREMENTS, log, target=dest)


def _write_tray_python_pin(deps_dir, python):
    with open(os.path.join(deps_dir, TRAY_PYTHON_PIN_FILENAME), "w", encoding="utf-8") as f:
        f.write(os.path.realpath(python))


def install_packages(system, requirement_sets, log, force=False):
    """force: reinstall even what's already at the pinned version (a clean
    reinstall). Linux's tray-deps folder is always built afresh anyway."""
    if system == "Linux":
        linux_dir = os.path.join(APP_DIR, "Linux")
        staging = os.path.join(linux_dir, ".tray-deps" + STAGING_SUFFIX)
        backup = os.path.join(linux_dir, ".tray-deps" + BACKUP_SUFFIX)
        _clean(staging, backup)
        python = _linux_tray_python()
        try:
            _build_linux_tray_deps(python, os.path.join(staging, "tray-deps"), log)
            _write_tray_python_pin(os.path.join(staging, "tray-deps"), python)
            updater.swap_in(staging, linux_dir, backup)
        finally:
            _clean(staging, backup)
        return
    python = python_exe(system)
    for requirements in requirement_sets:
        _pip_install(python, requirements, log, force=force)


def install_linux_python(manifest, log):
    """Swaps in the manifest's Python (and, if the tray widget is set up, a
    tray-deps built for it). Running processes keep their old files open;
    run.py restarts itself afterwards to move onto the new one."""
    linux_dir = os.path.join(APP_DIR, "Linux")
    staging = os.path.join(linux_dir, ".python" + STAGING_SUFFIX)
    backup = os.path.join(linux_dir, ".python" + BACKUP_SUFFIX)
    archive = os.path.join(linux_dir, "python-update.tar.gz")
    _clean(staging, backup)
    os.makedirs(staging)
    rebuild_tray = TRAY_REQUIREMENTS in installed_package_sets("Linux")
    try:
        entry = _download(manifest, "Linux", "python", archive, log)
        log("Extracting Python...")
        obs_install._extract(archive, staging)
        os.remove(archive)
        new_python = os.path.join(staging, "python", "bin", "python3")
        if not os.path.exists(new_python) or sorted(os.listdir(staging)) != ["python"]:
            raise updater.UpdateError("the downloaded Python package has an unexpected layout")
        if rebuild_tray:
            _build_linux_tray_deps(new_python, os.path.join(staging, "tray-deps"), log)
        updater.swap_in(staging, linux_dir, backup)
    finally:
        if os.path.exists(archive):
            os.remove(archive)
        _clean(staging, backup)
    if rebuild_tray:
        _write_tray_python_pin(_linux_tray_deps_dir(), os.path.join(linux_dir, "python", "bin", "python3"))
    log(f"Python updated to {updater.artifact_version(entry)}.")


WINDOWS_PYTHON_SWAP_SCRIPT = r"""
param([int]$WaitPid, [string]$WindowsDir, [string]$PythonDirName, [string]$RootDir, [string]$RunArgs, [string]$LogPath)
function Log($Message) { Add-Content -LiteralPath $LogPath -Value "$(Get-Date -Format s) $Message" }
Log "Waiting for ClipEm (pid $WaitPid) to exit"
Wait-Process -Id $WaitPid -Timeout 60 -ErrorAction SilentlyContinue
$live = Join-Path $WindowsDir $PythonDirName
$staged = Join-Path $WindowsDir ".python__STAGING__"
$backupName = ".python__BACKUP__"
$backup = Join-Path $WindowsDir $backupName
$swapped = $false
for ($attempt = 1; $attempt -le 30 -and -not $swapped; $attempt++) {
    try {
        if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
        Rename-Item -LiteralPath $live -NewName $backupName
        try {
            Rename-Item -LiteralPath $staged -NewName $PythonDirName
            $swapped = $true
        } catch {
            Rename-Item -LiteralPath $backup -NewName $PythonDirName
            throw
        }
    } catch {
        Log "Attempt $($attempt): $($_.Exception.Message)"
        Start-Sleep -Seconds 1
    }
}
if ($swapped) {
    Log "Python swapped in"
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Log "Couldn't swap Python in - keeping the previous one"
}
# "|"-separated (no Windows path contains one); each quoted for Start-Process,
# which joins its arguments with plain spaces.
$arguments = @($RunArgs -split '\|' | ForEach-Object { '"' + $_ + '"' })
Start-Process -FilePath (Join-Path $live "pythonw.exe") -ArgumentList $arguments -WorkingDirectory $RootDir
""".replace("__STAGING__", STAGING_SUFFIX).replace("__BACKUP__", BACKUP_SUFFIX)


def stage_windows_python(manifest, log):
    """Prepares the manifest's Python completely in App/Windows/
    .python.update-staging: Tcl/Tk, site-packages and pip, and every package
    set the current Python has. Returns its folder."""
    windows_dir = os.path.join(APP_DIR, "Windows")
    staging = os.path.join(windows_dir, ".python" + STAGING_SUFFIX)
    archive = os.path.join(windows_dir, "python-update.zip")
    package_sets = installed_package_sets("Windows")
    _clean(staging)
    os.makedirs(staging)
    try:
        entry = _download(manifest, "Windows", "python", archive, log)
        log("Extracting Python...")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging)
        os.remove(archive)
        new_python = os.path.join(staging, "python.exe")
        if not os.path.exists(new_python):
            raise updater.UpdateError("the downloaded Python package has no python.exe")
        version_info = updater._parse_version(updater.artifact_version(entry))[:2]
        windows_python.enable_site(staging, version_info)
        if not windows_python.install_tcltk(staging):
            raise updater.UpdateError("couldn't add Tcl/Tk to the new Python (see the log)")
        windows_python.add_lib_to_pth(staging, version_info)
        windows_python.bootstrap_pip(new_python, log)
        for requirements in package_sets:
            _pip_install(new_python, requirements, log)
    except BaseException:
        _clean(staging)
        raise
    finally:
        if os.path.exists(archive):
            os.remove(archive)
    log(f"Python {updater.artifact_version(entry)} is ready - swapping it in once ClipEm exits.")
    return staging


def start_windows_python_swap(run_args):
    """Starts the PowerShell script that swaps the staged Python in once
    this process has exited, then runs run.py `run_args` on it."""
    windows_dir = os.path.join(APP_DIR, "Windows")
    script = os.path.join(windows_dir, ".python-update.ps1")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(WINDOWS_PYTHON_SWAP_SCRIPT)
    os.makedirs(LOG_DIR, exist_ok=True)
    run_py = os.path.join(INSTALLER_DIR, "run.py")
    arguments = "|".join([run_py, *run_args])
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script,
         "-WaitPid", str(os.getpid()), "-WindowsDir", windows_dir, "-PythonDirName", windows_python.PYTHON_DIR_NAME,
         "-RootDir", ROOT_DIR, "-RunArgs", arguments, "-LogPath", os.path.join(LOG_DIR, "python-update.log")],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        cwd=ROOT_DIR,
    )


def install(manifest, plan, log, run_args=()):
    """Installs everything in `plan` (from check()). Returns CONTINUE,
    RESTART or HANDOFF (see their definitions). Raises on failure - what
    was already swapped in stays, the rest keeps the previous version."""
    system = _system()
    if not plan or system is None:
        return CONTINUE
    if "obs" in plan:
        install_obs(manifest, system, log)
    if "python" in plan:
        # Reinstalls every package set for the new Python too.
        if system == "Linux":
            install_linux_python(manifest, log)
            return RESTART
        stage_windows_python(manifest, log)
        start_windows_python_swap(run_args)
        return HANDOFF
    if "packages" in plan:
        install_packages(system, plan["packages"], log)
    return CONTINUE
