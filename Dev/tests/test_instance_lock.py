import os
import subprocess
import sys

import clipem_common
import pytest
from clipem_common import acquire_instance_lock, pid_alive


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_no_lock_file_is_acquired(tmp_path):
    lock = tmp_path / ".tray.lock"
    assert acquire_instance_lock(str(lock))
    assert lock.read_text() == str(os.getpid())


def test_stale_lock_from_a_dead_process_is_taken_over(tmp_path):
    lock = tmp_path / ".tray.lock"
    dead = _dead_pid()
    assert not pid_alive(dead)
    lock.write_text(str(dead))
    assert acquire_instance_lock(str(lock))
    assert lock.read_text() == str(os.getpid())


def test_lock_held_by_a_live_process_is_refused(tmp_path):
    lock = tmp_path / ".tray.lock"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock.write_text(str(other.pid))
        assert not acquire_instance_lock(str(lock))
        assert lock.read_text() == str(other.pid)  # untouched
    finally:
        other.kill()
        other.wait()


def test_garbage_lock_file_is_taken_over(tmp_path):
    lock = tmp_path / ".tray.lock"
    lock.write_text("not a pid")
    assert acquire_instance_lock(str(lock))


def test_lock_is_released_at_exit_but_not_one_taken_over(tmp_path):
    """Runs in a child process so its atexit handler actually fires."""
    lock = tmp_path / ".tray.lock"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from clipem_common import acquire_instance_lock; "
        "assert acquire_instance_lock(sys.argv[2]); "
        "open(sys.argv[2], 'w').write(sys.argv[3]) if len(sys.argv) > 3 else None"
    )
    installer_dir = os.path.dirname(sys.modules["clipem_common"].__file__)
    subprocess.run([sys.executable, "-c", code, installer_dir, str(lock)], check=True)
    assert not lock.exists()

    # A newer instance overwrote the lock with its own pid: leave it be.
    subprocess.run([sys.executable, "-c", code, installer_dir, str(lock), "999999"], check=True)
    assert lock.read_text() == "999999"


def test_releasing_hands_the_lock_to_the_next_instance(tmp_path):
    """Restart ClipEm Tray and OBS: the old tray releases its lock before
    starting the new one, which must then be able to take it."""
    from clipem_common import release_instance_lock

    lock = tmp_path / ".tray.lock"
    assert acquire_instance_lock(str(lock))
    release_instance_lock(str(lock))
    assert not lock.exists()
    lock.write_text("999999")  # the next instance's lock
    release_instance_lock(str(lock))  # e.g. the old tray's exit handler
    assert lock.read_text() == "999999"


# --- Helper windows closing with the tray widget (Linux) -------------------

import time  # noqa: E402

_PARENT = r"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[1], str(os.getpid()), sys.argv[3]])
print(child.pid, flush=True)
time.sleep(60)
"""
_CHILD = r"""
import sys, threading, time
sys.path.insert(0, sys.argv[1])
from clipem_common import exit_with_parent, outlive_parent
exit_with_parent(int(sys.argv[2]))
if sys.argv[3] == "outlive":
    outlive_parent()
elif sys.argv[3] == "outlive-on-a-worker-thread":
    worker = threading.Thread(target=outlive_parent)
    worker.start()
    worker.join()
time.sleep(60)
"""


def _spawn_family(mode):
    installer_dir = os.path.dirname(sys.modules["clipem_common"].__file__)
    parent = subprocess.Popen([sys.executable, "-c", _PARENT, installer_dir, _CHILD, mode], stdout=subprocess.PIPE, text=True)
    child_pid = int(parent.stdout.readline())
    time.sleep(0.5)  # let the child set up its parent-death signal
    return parent, child_pid


def _wait_gone(pid, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_helper_dies_when_the_tray_widget_is_killed():
    parent, child_pid = _spawn_family("stay")
    parent.kill()  # e.g. the tray crashing or being SIGKILLed
    parent.wait()
    assert _wait_gone(child_pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_helper_that_opted_out_survives_the_tray_widget():
    parent, child_pid = _spawn_family("outlive")
    parent.kill()
    parent.wait()
    try:
        assert not _wait_gone(child_pid, seconds=1)
    finally:
        os.kill(child_pid, 9)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_outliving_from_a_worker_thread_doesnt_count():
    # Why settings_dialog.restart_clipem_in_background calls outlive_parent
    # on the Tk thread: the signal is per thread, the main thread's stays.
    parent, child_pid = _spawn_family("outlive-on-a-worker-thread")
    parent.kill()
    parent.wait()
    assert _wait_gone(child_pid)


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)

    warning = info


def _sleeper(tmp_path, script_name):
    """A process whose command line looks like `python <script_name>`."""
    script = tmp_path / script_name
    script.write_text("import time\ntime.sleep(60)\n")
    return subprocess.Popen([sys.executable, str(script)])


@pytest.mark.skipif(sys.platform == "win32", reason="checks the command line, which Windows doesn't")
def test_stale_tray_lock_never_stops_an_unrelated_process(tmp_path, monkeypatch):
    lock = tmp_path / ".tray.lock"
    monkeypatch.setattr(clipem_common, "TRAY_LOCK_PATH", str(lock))
    other = _sleeper(tmp_path, "something_else.py")  # took over the dead tray's pid
    tray = _sleeper(tmp_path, "tray_widget.py")
    try:
        lock.write_text(str(other.pid))
        log = _Log()
        clipem_common.stop_tray_widget(log, timeout_seconds=2)
        assert other.poll() is None and "stale lock" in log.lines[0]

        lock.write_text(str(tray.pid))
        clipem_common.stop_tray_widget(log, timeout_seconds=5)
        assert tray.wait(timeout=5) is not None
    finally:
        for proc in (other, tray):
            proc.kill()
            proc.wait()
