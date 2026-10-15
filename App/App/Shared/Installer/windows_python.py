"""
ClipEm's bundled Windows Python (App/Windows/Python - python.org's
embeddable distribution) and what ClipEm adds to it: site-packages + pip
(for the tray widget's packages) and Tcl/Tk (tkinter, for the setup wizard
and the tray's dialogs).

Used by setup.py for the interpreter it runs on, and by components.py to
fully prepare a replacement Python in a staging folder before it's swapped
in - so every function takes the Python to work on rather than assuming
sys.executable.
"""

import os
import shutil
import subprocess
import sys

import updater

PYTHON_DIR_NAME = "Python"  # App/Windows/Python

# What tcltk.msi's administrative install puts under its TARGETDIR (checked
# against the 3.10 and 3.12 MSIs' Directory/File tables), and where each
# piece goes in the embeddable Python. zlib1.dll: tcl86t.dll links it from
# 3.12 on (it's absent from - and not needed by - the 3.10 MSI).
TCLTK_DLLS = ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll")  # DLLs\ -> python root
TCLTK_TCL_MARKER = os.path.join("tcl", "tcl8.6", "init.tcl")  # tcl\ -> python root
TCLTK_TKINTER_PKG = os.path.join("Lib", "tkinter")  # Lib\tkinter -> python root\Lib


def _tcltk_log(message):
    print(f"  [tkinter] {message}")


def own_pth_path(python_root, version_info=None):
    """The ._pth file this embeddable Python actually reads -
    python<major><minor>._pth. A folder can hold another version's too (an
    install made over an older one), and editing that one does nothing."""
    version_info = version_info or sys.version_info
    path = os.path.join(python_root, f"python{version_info[0]}{version_info[1]}._pth")
    return path if os.path.exists(path) else None


def _edit_pth(python_root, version_info, edit):
    pth_path = own_pth_path(python_root, version_info)
    if not pth_path:
        return
    with open(pth_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    lines = edit(lines)
    with open(pth_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def enable_site(python_root, version_info=None):
    """The embeddable Python ships with site (and so site-packages) disabled."""
    _edit_pth(python_root, version_info,
              lambda lines: ["import site" if line.strip() == "#import site" else line for line in lines])


def add_lib_to_pth(python_root, version_info=None):
    """Lib\\ holds the tkinter package taken from tcltk.msi."""
    _edit_pth(python_root, version_info, lambda lines: lines if "Lib" in lines else lines + ["Lib"])


def bootstrap_pip(python_exe, log=print):
    """Installs pip into python_exe's site-packages from the pinned wheel in
    the signed manifest (rather than get-pip.py, which is unversioned and
    changes under us). pip runs straight from its wheel on sys.path. Not as
    `python pip-X.whl/pip`: on Windows pip refuses to install over itself
    ("To modify pip, please run...") whenever sys.argv[0] is named pip."""
    manifest = updater.fetch_manifest()
    entry = updater.manifest_entry(manifest, "windows", "pip_wheel")
    wheel_path = os.path.join(os.path.dirname(python_exe), os.path.basename(entry["url"]))
    log(f"Downloading {entry['url']} ...")
    updater.download_file(entry["url"], wheel_path, entry["sha256"], timeout=300)
    try:
        subprocess.run(
            [python_exe, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); "
             "from pip._internal.cli.main import main; "
             "sys.exit(main(['install', '--no-index', '--no-warn-script-location', sys.argv[1]]))",
             wheel_path],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        tail = "\n".join((exc.stdout + exc.stderr).strip().splitlines()[-8:])
        raise RuntimeError(f"Installing pip failed:\n{tail}") from exc
    finally:
        os.remove(wheel_path)


def tcltk_installed(python_root):
    return os.path.exists(os.path.join(python_root, TCLTK_TCL_MARKER)) and os.path.exists(
        os.path.join(python_root, "_tkinter.pyd")
    )


def _print_msiexec_log_tail(log_path, lines=40):
    """msiexec writes its /l*v log as UTF-16; the useful part (the error
    and "Return value 3") is near the end."""
    try:
        with open(log_path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        _tcltk_log(f"no msiexec log at {log_path} ({exc})")
        return
    text = raw.decode("utf-16", errors="replace") if raw[:2] == b"\xff\xfe" else raw.decode("utf-8", errors="replace")
    _tcltk_log(f"last {lines} lines of the msiexec log ({log_path}):")
    for line in text.splitlines()[-lines:]:
        print(f"      {line}")


def _list_tree(root, max_depth=2):
    """For the log when expected files are missing - shows what msiexec
    actually produced."""
    if not os.path.isdir(root):
        _tcltk_log(f"{root} does not exist")
        return
    _tcltk_log(f"contents of {root}:")
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.count(os.sep) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        indent = "      " + "  " * depth
        print(f"{indent}{os.path.basename(dirpath) or dirpath}\\")
        for name in sorted(filenames)[:20]:
            print(f"{indent}  {name}")
        if len(filenames) > 20:
            print(f"{indent}  ... and {len(filenames) - 20} more files")


def _msiexec_quote(value):
    # msiexec parses its own command line: a property value with spaces
    # (C:\Users\First Last\...) must be PROPERTY="value", which
    # subprocess's list quoting ("PROPERTY=value") doesn't produce.
    return f'"{value}"'


def install_tcltk(python_root):
    """Pulls tkinter's pieces out of python.org's tcltk.msi (pinned in the
    signed manifest, built for the manifest's Python) into python_root, via
    an administrative install (`msiexec /a` - extracts files only: no
    registry entries, no Start Menu shortcuts, no admin rights needed).
    Returns True on success. Every step is logged with a [tkinter] prefix;
    on failure the work folder is left for troubleshooting."""
    _tcltk_log(f"python root: {python_root}")
    work_dir = os.path.join(python_root, "_tcltk_bootstrap")
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)
    msiexec_log = os.path.join(work_dir, "msiexec.log")
    ok = False
    try:
        msi_path = os.path.join(work_dir, "tcltk.msi")
        manifest = updater.fetch_manifest()
        entry = updater.manifest_entry(manifest, "windows", "tcltk_msi")
        updater.download_file(entry["url"], msi_path, entry["sha256"], timeout=300)
        _tcltk_log(f"downloaded {msi_path} ({os.path.getsize(msi_path)} bytes)")

        extract_dir = os.path.join(work_dir, "extract")
        # A string, not a list - see _msiexec_quote.
        command = (
            f"msiexec /a {_msiexec_quote(msi_path)} /qn "
            f"TARGETDIR={_msiexec_quote(extract_dir)} /l*v {_msiexec_quote(msiexec_log)}"
        )
        _tcltk_log(f"running: {command}")
        result = subprocess.run(command)
        _tcltk_log(f"msiexec exit code: {result.returncode}")
        if result.returncode != 0:
            _print_msiexec_log_tail(msiexec_log)
            return False

        missing = [
            rel for rel in
            [os.path.join("DLLs", name) for name in TCLTK_DLLS]
            + [TCLTK_TCL_MARKER, os.path.join(TCLTK_TKINTER_PKG, "__init__.py")]
            if not os.path.exists(os.path.join(extract_dir, rel))
        ]
        if missing:
            _tcltk_log("msiexec succeeded but these expected files are missing: " + ", ".join(missing))
            _list_tree(extract_dir)
            _print_msiexec_log_tail(msiexec_log)
            return False

        for name in TCLTK_DLLS:
            shutil.copy2(os.path.join(extract_dir, "DLLs", name), os.path.join(python_root, name))
            _tcltk_log(f"copied {name}")
        shutil.copytree(os.path.join(extract_dir, "tcl"), os.path.join(python_root, "tcl"), dirs_exist_ok=True)
        _tcltk_log("copied tcl\\ (Tcl/Tk script libraries)")
        lib_dir = os.path.join(python_root, "Lib")
        os.makedirs(lib_dir, exist_ok=True)
        shutil.copytree(
            os.path.join(extract_dir, TCLTK_TKINTER_PKG), os.path.join(lib_dir, "tkinter"), dirs_exist_ok=True
        )
        _tcltk_log("copied Lib\\tkinter")
        ok = True
        return True
    finally:
        if ok:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            _tcltk_log(f"left {work_dir} in place for troubleshooting (it's retried and replaced next run)")


def verify_tkinter(python_exe, python_root):
    """Imports tkinter and starts a Tcl interpreter in a fresh process with
    the same environment the GUI will get - catches a missing DLL or script
    library here, with the actual error, rather than as a vague GUI fallback."""
    env = dict(os.environ)
    env["TCL_LIBRARY"] = os.path.join(python_root, "tcl", "tcl8.6")
    env["TK_LIBRARY"] = os.path.join(python_root, "tcl", "tk8.6")
    result = subprocess.run(
        [python_exe, "-c",
         "import sys; sys.path.append(sys.argv[1]); import tkinter; "
         "tkinter.Tcl(); print(tkinter.TclVersion)",
         os.path.join(python_root, "Lib")],
        capture_output=True, text=True, env=env,
    )
    if result.returncode == 0:
        _tcltk_log(f"tkinter imports and Tcl {result.stdout.strip()} starts")
        return True
    _tcltk_log(f"tkinter test import failed (exit code {result.returncode}):")
    for line in (result.stdout + result.stderr).strip().splitlines():
        print(f"      {line}")
    return False
