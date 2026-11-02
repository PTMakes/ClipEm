"""Runs on the Windows test VM, in its desktop session (see test-windows.sh):

    pythonw test_windows_helper.py close|pytest

Each writes its output to results/<command>.txt and, when it's finished, its
exit status to results/<command>.done - pythonw has no console to write to.

close: closes the installed ClipEm, as run.py's Kill Existing does, noting
what was running for test-windows.sh to start again. pytest: runs Dev/tests
with the packages and tools in ~/ClipEmTest, which ClipEm's bundled Python
can't be pointed at by PYTHONPATH (it has a ._pth file).
"""

import json
import logging
import os
import subprocess
import sys
import traceback

TEST_DIR = os.path.join(os.path.expanduser("~"), "ClipEmTest")
RESULTS_DIR = os.path.join(TEST_DIR, "results")
STATE_PATH = os.path.join(TEST_DIR, "was_running.json")
INSTALL_DIR = os.path.join(os.environ["LOCALAPPDATA"], "ClipEm")
INSTALLER_DIR = os.path.join(INSTALL_DIR, "App", "Shared", "Installer")
TOOL_DIRS = (
    # A full ffmpeg first (winget's Gyan.FFmpeg): ClipEm's has no lavfi, ffprobe
    # or libx264, which the tests make their clips with.
    os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "WinGet", "Links"),
    os.path.join(INSTALL_DIR, "App", "Windows", "ffmpeg", "bin"),
    os.path.join(os.environ["ProgramFiles"], "Git", "cmd"),
    os.path.join(os.environ["ProgramFiles"], "Git", "usr", "bin"),  # bash, openssl
)


def _waiting_launch(clipem_common):
    """The pid of a launch still showing its update prompt, which holds the
    run lock and would make the one after the tests ask what to do. This uses
    what every installed version has: the VM may be on an old one."""
    try:
        with open(os.path.join(clipem_common.APP_DIR, ".run.lock"), encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    # A live pid running this install's Python, not one reused since.
    return pid if clipem_common.pid_alive(pid) and clipem_common.is_tray_widget_process(pid) else None


def close():
    sys.path.insert(0, INSTALLER_DIR)
    import clipem_common
    import obs_process

    logging.basicConfig(level=logging.INFO, force=True)
    log = logging.getLogger("close")
    # One already noted is a run that never got to start it again.
    launch = _waiting_launch(clipem_common)
    was_running = obs_process.obs_running() or bool(launch) or _was_running()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"was_running": was_running}, f)
    if not obs_process.close_obs_stopping_outputs(log, timeout_seconds=30):
        obs_process.kill_obs(log)
    clipem_common.stop_clip_editor(log)
    clipem_common.stop_tray_widget(log)
    if launch:
        subprocess.run(["taskkill", "/PID", str(launch), "/F"], capture_output=True, check=False)
    return 0


def _was_running():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)["was_running"]
    except (OSError, ValueError, KeyError):
        return False


def run_pytest():
    sys.path.insert(0, os.path.join(TEST_DIR, "deps"))
    os.environ["PATH"] = os.pathsep.join([*TOOL_DIRS, os.environ["PATH"]])
    os.chdir(os.path.join(TEST_DIR, "repo", "Dev"))
    import pytest

    return int(pytest.main(["-q", "-rfEsxX", "-p", "no:cacheprovider", f"--basetemp={os.path.join(TEST_DIR, 'tmp')}",
                            f"--junitxml={os.path.join(RESULTS_DIR, 'junit.xml')}", "tests"]))


def main(command):
    status = 1
    with open(os.path.join(RESULTS_DIR, f"{command}.txt"), "w", encoding="utf-8", buffering=1) as output:
        os.dup2(output.fileno(), 1)
        os.dup2(output.fileno(), 2)
        sys.stdout = sys.stderr = output
        try:
            status = {"close": close, "pytest": run_pytest}[command]()
        except BaseException:  # the .done file must get written whatever happened
            traceback.print_exc()
    with open(os.path.join(RESULTS_DIR, f"{command}.done"), "w", encoding="ascii") as f:
        f.write(str(status))


if __name__ == "__main__":
    main(sys.argv[1])
