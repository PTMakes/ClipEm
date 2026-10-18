"""Every script a user can run answers --help (and changes nothing)."""

import os
import subprocess
import sys

import pytest
from conftest import APP_SHARED_DIR

PYTHON_SCRIPTS = ["Installer/run.py", "Installer/setup.py", "Installer/uninstall_autostart.py",
                  "tray-widget/tray_widget.py", "tray-widget/clip_browser.py", "tray-widget/settings_dialog.py"]
SHELL_SCRIPTS = ["Installer/Scripts/ClipEmSetupLinux.sh", "Installer/Scripts/UninstallAutostartLinux.sh"]
BATCH_SCRIPTS = ["Installer/Scripts/Setup_Windows.bat", "Installer/Scripts/UninstallAutostartWindows.bat"]


@pytest.mark.parametrize("script", PYTHON_SCRIPTS)
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_python_scripts_print_usage(script, flag):
    # -I: only the standard library - --help must work before the tray's
    # own dependencies (Pillow, pystray...) are even importable.
    result = subprocess.run([sys.executable, "-I", os.path.join(APP_SHARED_DIR, script), flag],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"Usage: {os.path.basename(script)}")


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_shell_scripts_print_usage(script):
    result = subprocess.run(["bash", os.path.join(APP_SHARED_DIR, script), "--help"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"Usage: {os.path.basename(script)}")


@pytest.mark.parametrize("script", BATCH_SCRIPTS)
def test_batch_scripts_handle_help_before_doing_anything(script):
    with open(os.path.join(APP_SHARED_DIR, script), encoding="utf-8") as f:
        text = f.read()
    help_at = text.index('if defined SHOW_HELP (')
    for flag in ('"%~1"=="--help"', '"%~1"=="-h"', '"%~1"=="/?"'):
        assert flag in text
    # Before any download or Python run - and without goto labels, which
    # cmd.exe can misread in a file with Unix line endings.
    assert help_at < text.index('"%PYTHON_EXE%"') and "goto" not in text


DEV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_release_script_prints_its_header_and_does_nothing_else():
    result = subprocess.run(["bash", os.path.join(DEV_DIR, "release.sh"), "--no-upload", "--help"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Builds and publishes a ClipEm release.")
    assert "Usage: ./release.sh" in result.stdout and "set -euo" not in result.stdout


def test_obspython_api_generator_documents_check():
    result = subprocess.run([sys.executable, os.path.join(DEV_DIR, "generate_obspython_api.py"), "--help"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--check" in result.stdout and "out of" in result.stdout
