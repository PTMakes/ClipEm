"""The small helpers everything else stands on: files that must never end up
half-written, config that must never stop a launch, and processes that must
never be mistaken for another one's."""

import ctypes
import hashlib
import logging
import logging.handlers
import os
import subprocess
import sys
import types
import uuid

import clipem_common
import pytest

log = logging.getLogger("test")


def test_a_missing_version_file_reads_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(clipem_common, "VERSION_PATH", str(tmp_path / "VERSION"))
    assert clipem_common.read_version() == "unknown"
    (tmp_path / "VERSION").write_text(" 1.2.3\n")
    assert clipem_common.read_version() == "1.2.3"


def test_sha256_covers_the_whole_file(tmp_path):
    data = b"x" * (1024 * 1024 + 7)  # more than one read
    path = tmp_path / "file"
    path.write_bytes(data)
    assert clipem_common.sha256_of_file(str(path)) == hashlib.sha256(data).hexdigest()


def test_atomic_writes_make_the_folder_and_keep_line_endings(tmp_path):
    path = tmp_path / "new" / "folder" / "file.ini"
    clipem_common.write_text_atomic(str(path), "a\r\nb\n")
    assert path.read_bytes() == b"a\r\nb\n"
    assert os.listdir(path.parent) == ["file.ini"]


def test_atomic_writes_work_in_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clipem_common.write_text_atomic("file.txt", "text")
    assert (tmp_path / "file.txt").read_text() == "text"


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch, failing):
    path = tmp_path / "scene.json"
    path.write_text("the user's whole setup")

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(clipem_common.os, failing, fail)
    with pytest.raises(OSError, match="disk full"):
        clipem_common.write_text_atomic(str(path), "half of it")
    monkeypatch.undo()
    assert path.read_text() == "the user's whole setup"
    assert os.listdir(tmp_path) == ["scene.json"]


def test_a_temp_file_that_cant_be_removed_doesnt_hide_the_real_error(tmp_path, monkeypatch):
    def fail(*_args):
        raise OSError("disk full")

    def no_remove(_path):
        raise PermissionError("in use")

    monkeypatch.setattr(clipem_common.os, "replace", fail)
    monkeypatch.setattr(clipem_common.os, "remove", no_remove)
    with pytest.raises(OSError, match="disk full"):
        clipem_common.write_text_atomic(str(tmp_path / "file"), "x")


def test_json_is_pretty_unless_asked_otherwise(tmp_path):
    path = tmp_path / "file.json"
    clipem_common.write_json_atomic(str(path), {"a": 1})
    assert path.read_text() == '{\n  "a": 1\n}'
    clipem_common.write_json_atomic(str(path), {"a": 1}, indent=None)
    assert path.read_text() == '{"a": 1}'


def test_unreadable_json_is_the_default(tmp_path):
    path = tmp_path / "file.json"
    assert clipem_common.load_json(str(path), "fallback") == "fallback"
    assert clipem_common.load_json(str(path)) is None
    path.write_bytes(b"\xff\xfe not text")
    assert clipem_common.load_json(str(path), "fallback") == "fallback"
    path.write_text("{broken")
    assert clipem_common.load_json(str(path), "fallback") == "fallback"
    path.write_text("[1]")
    assert clipem_common.load_json(str(path), "fallback") == [1]


@pytest.mark.parametrize("saved, expected", [
    ('{"autostart_obs": false}', False),
    ("{broken", True),  # a broken file starts everything
    ("[1, 2]", True),
    ("null", True),
])
def test_run_config_fills_in_defaults(tmp_path, monkeypatch, saved, expected):
    path = tmp_path / "run_config.json"
    path.write_text(saved)
    monkeypatch.setattr(clipem_common, "RUN_CONFIG_PATH", str(path))
    config = clipem_common.load_run_config()
    assert config["autostart_obs"] is expected and config["autostart_tray"] is True


def test_run_config_defaults_arent_changed_by_a_load(tmp_path, monkeypatch):
    monkeypatch.setattr(clipem_common, "RUN_CONFIG_PATH", str(tmp_path / "run_config.json"))
    clipem_common.load_run_config()["autostart_obs"] = False
    assert clipem_common.DEFAULT_RUN_CONFIG["autostart_obs"] is True


def test_saved_run_config_is_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(clipem_common, "RUN_CONFIG_PATH", str(tmp_path / "run_config.json"))
    clipem_common.save_run_config({"autostart_tray": False})
    assert clipem_common.load_run_config()["autostart_tray"] is False


def test_a_process_owned_by_someone_else_is_alive(monkeypatch):
    def not_allowed(_pid, _signal):
        raise PermissionError("not yours")

    monkeypatch.setattr(clipem_common.sys, "platform", "linux")
    monkeypatch.setattr(clipem_common.os, "kill", not_allowed)
    assert clipem_common.pid_alive(1) is True


def test_pid_alive_and_locks_answer_with_real_booleans(tmp_path):
    dead = 2 ** 22 + 1
    assert clipem_common.pid_alive(dead) is False
    assert clipem_common.holds_lock(dead, str(tmp_path / "a.lock")) is False
    lock = tmp_path / "b.lock"
    lock.write_text(str(os.getppid()))
    assert clipem_common.acquire_instance_lock(str(lock)) is False


def test_a_second_log_folder_use_is_fine(tmp_path, monkeypatch, fresh_logger):
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(clipem_common, "LOG_DIR", str(tmp_path / "logs"))
    logger = clipem_common.setup_logging(fresh_logger)
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers)


def test_a_process_with_no_proc_entry_isnt_a_zombie():
    assert clipem_common._is_zombie(2 ** 22 + 1) is False


def _fake_kernel32(monkeypatch, **functions):
    kernel32 = types.SimpleNamespace(CloseHandle=lambda handle: None, **functions)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel32), raising=False)
    monkeypatch.setattr(clipem_common.sys, "platform", "win32")


def _exit_code(codes):
    """A fake GetExitCodeProcess: handle -> exit code, None for a failed call."""
    def get(handle, pointer):
        if codes[handle] is None:
            return 0
        pointer._obj.value = codes[handle]
        return 1
    return get


def test_windows_asks_for_a_handle_to_see_a_process_is_alive(monkeypatch):
    closed = []
    _fake_kernel32(monkeypatch, OpenProcess=lambda access, inherit, pid: 5 if pid == 42 else 0,
                   GetExitCodeProcess=_exit_code({5: 259}))  # STILL_ACTIVE
    ctypes.windll.kernel32.CloseHandle = lambda handle: closed.append(handle)
    assert clipem_common.pid_alive(42) is True
    assert clipem_common.pid_alive(43) is False
    assert closed == [5]  # and a handle isn't leaked on every check


def test_on_windows_an_exited_process_still_open_somewhere_is_dead(monkeypatch):
    closed = []
    _fake_kernel32(monkeypatch, OpenProcess=lambda access, inherit, pid: pid, GetExitCodeProcess=_exit_code({7: 0, 8: None}))
    ctypes.windll.kernel32.CloseHandle = lambda handle: closed.append(handle)
    assert clipem_common.pid_alive(7) is False
    assert clipem_common.pid_alive(8) is True  # no exit code to go on: an openable process counts as alive
    assert closed == [7, 8]


def test_a_lock_gone_since_it_was_read_is_held_by_nobody(tmp_path):
    lock = tmp_path / "gone.lock"
    assert clipem_common.holds_lock(os.getpid(), str(lock), process_start=lambda pid: 0) is False


def test_lock_file_times_get_two_seconds_of_leeway(tmp_path):
    lock = tmp_path / "app.lock"
    lock.write_text("1")
    written = lock.stat().st_mtime
    assert clipem_common.holds_lock(os.getpid(), str(lock), process_start=lambda pid: written + 2)
    assert not clipem_common.holds_lock(os.getpid(), str(lock), process_start=lambda pid: written + 3)


def test_outside_windows_a_live_pid_is_enough(tmp_path, monkeypatch):
    # pid_alive is faked: on real Windows its os.kill(pid, 0) would be a Ctrl+C.
    monkeypatch.setattr(clipem_common.sys, "platform", "linux")
    monkeypatch.setattr(clipem_common, "pid_alive", lambda pid: True)
    assert clipem_common.holds_lock(os.getpid(), str(tmp_path / "no.lock")) is True


def test_releasing_a_lock_thats_gone_is_fine(tmp_path):
    clipem_common.release_instance_lock(str(tmp_path / "gone.lock"))


def test_taking_a_lock_again_in_the_same_process_works(tmp_path):
    lock = tmp_path / "app.lock"
    lock.write_text(str(os.getpid()))
    assert clipem_common.acquire_instance_lock(str(lock))


@pytest.mark.parametrize("url, expected", [
    ("http://127.0.0.1:8123/clipem-editor/#tok", "http://127.0.0.1:8123/tok/api/quit"),
    ("http://127.0.0.1:8123/clipem-editor/#a b", "http://127.0.0.1:8123/a%20b/api/quit"),
    ("http://evil.example:8123/clipem-editor/#tok", None),  # only ever the local editor
    ("http://127.0.0.1/clipem-editor/#tok", None),
    ("http://127.0.0.1:8123/clipem-editor/", None),
    ("", None),
    (None, None),
])
def test_the_editors_quit_url(url, expected):
    assert clipem_common.clip_editor_quit_url(url) == expected


@pytest.fixture
def editor(tmp_path, monkeypatch):
    """A running clip editor's lock and URL files; alive: whether its pid is
    (a list: one answer per check, the last repeating)."""
    lock, url = tmp_path / ".clip_browser.lock", tmp_path / ".clip_browser.url"
    lock.write_text("4242")
    url.write_text("http://127.0.0.1:8123/clipem-editor/#tok\n")
    monkeypatch.setattr(clipem_common, "CLIP_EDITOR_LOCK_PATH", str(lock))
    monkeypatch.setattr(clipem_common, "CLIP_EDITOR_URL_PATH", str(url))
    monkeypatch.setattr(clipem_common.time, "sleep", lambda _s: None)
    monkeypatch.setattr(clipem_common.sys, "platform", "linux")
    asked, killed = [], []
    state = types.SimpleNamespace(lock=lock, url=url, asked=asked, killed=killed, alive=[True, False], refuse=False)

    def urlopen(request, timeout):
        asked.append(request.full_url)
        if state.refuse:
            raise OSError("connection refused")
        return types.SimpleNamespace(close=lambda: None)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(clipem_common, "pid_alive",
                        lambda pid: state.alive.pop(0) if len(state.alive) > 1 else state.alive[0])
    monkeypatch.setattr(clipem_common.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    return state


def test_a_running_editor_is_asked_to_quit(editor):
    editor.alive = [True, True, False]
    assert clipem_common.stop_clip_editor(log) is True
    assert editor.asked == ["http://127.0.0.1:8123/tok/api/quit"] and editor.killed == []


def test_an_editor_that_wont_quit_is_ended(editor):
    editor.alive = [True]
    assert clipem_common.stop_clip_editor(log, timeout_seconds=-2) is True
    assert [pid for pid, _sig in editor.killed] == [4242]


def test_an_editor_that_doesnt_answer_is_left_alone(editor):
    """Nothing answered: a stale lock, and its pid may be some other program's."""
    editor.refuse = True
    assert clipem_common.stop_clip_editor(log) is False
    assert editor.killed == []


@pytest.mark.parametrize("change", [
    lambda e: e.lock.unlink(),
    lambda e: e.lock.write_text("not a pid"),
    lambda e: e.lock.write_text(str(os.getpid())),
    lambda e: setattr(e, "alive", [False]),
    lambda e: e.url.unlink(),
    lambda e: e.url.write_text("http://evil.example:1/#tok"),
])
def test_nothing_to_stop_asks_nothing(editor, change):
    change(editor)
    assert clipem_common.stop_clip_editor(log) is False
    assert editor.asked == [] and editor.killed == []


def _popen_args(monkeypatch, platform):
    started = []
    monkeypatch.setattr(clipem_common.sys, "platform", platform)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: started.append((args, kwargs)))
    return started


def test_starting_clipem_detaches_it(monkeypatch):
    started = _popen_args(monkeypatch, "linux")
    clipem_common.start_clipem(["--reopen-editor"])
    args, kwargs = started[0]
    assert args == [sys.executable, os.path.join(clipem_common.INSTALLER_DIR, "run.py"), "--reopen-editor"]
    assert kwargs == {"cwd": clipem_common.ROOT_DIR, "start_new_session": True}


def test_starting_clipem_on_windows_uses_a_detached_process(monkeypatch):
    started = _popen_args(monkeypatch, "win32")
    clipem_common.start_clipem()
    assert started[0][1]["creationflags"] == 8 | 512


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_a_pid_with_no_process_isnt_the_tray_widget():
    assert clipem_common.is_tray_widget_process(2 ** 22 + 1) is False


def test_macos_reads_the_command_line_from_ps(monkeypatch):
    monkeypatch.setattr(clipem_common.sys, "platform", "darwin")
    out = {"stdout": ""}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=out["stdout"]))
    assert clipem_common.is_tray_widget_process(1) is False
    out["stdout"] = "/python /app/tray-widget/tray_widget.py"
    assert clipem_common.is_tray_widget_process(1) is True


def test_a_ps_that_hangs_counts_as_not_the_tray_widget(monkeypatch):
    def hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ps", 5)

    monkeypatch.setattr(clipem_common.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", hang)
    assert clipem_common.is_tray_widget_process(1) is False


def test_an_unknown_platform_never_stops_anything(monkeypatch):
    monkeypatch.setattr(clipem_common.sys, "platform", "sunos5")
    assert clipem_common.is_tray_widget_process(os.getpid()) is False


@pytest.mark.parametrize("exe, expected", [
    ("{root}/App/Windows/python/pythonw.exe", True),
    ("{root}/App/Windows/python/python.exe", True),
    ("{root}/App/Windows/bin/obs64.exe", False),
    ("/elsewhere/python.exe", False),  # somebody else's Python
    (None, False),
])
def test_windows_recognises_this_installs_python(monkeypatch, tmp_path, exe, expected):
    monkeypatch.setattr(clipem_common, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(clipem_common.sys, "platform", "win32")
    monkeypatch.setattr(clipem_common, "process_exe_path_win32", lambda pid: exe and exe.format(root=tmp_path))
    assert clipem_common.is_tray_widget_process(1) is expected


def _tray_lock(tmp_path, monkeypatch, text):
    lock = tmp_path / ".tray.lock"
    lock.write_text(text)
    monkeypatch.setattr(clipem_common, "TRAY_LOCK_PATH", str(lock))
    killed = []
    monkeypatch.setattr(clipem_common.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    return killed


@pytest.mark.parametrize("text", ["not a pid", ""])
def test_a_broken_tray_lock_stops_nothing(tmp_path, monkeypatch, text):
    killed = _tray_lock(tmp_path, monkeypatch, text)
    clipem_common.stop_tray_widget(log)
    assert killed == []


def test_the_tray_never_stops_itself(tmp_path, monkeypatch):
    killed = _tray_lock(tmp_path, monkeypatch, str(os.getpid()))
    clipem_common.stop_tray_widget(log)
    assert killed == []


def test_a_tray_that_cant_be_stopped_is_reported(tmp_path, monkeypatch):
    lock = tmp_path / ".tray.lock"
    lock.write_text("4242")
    monkeypatch.setattr(clipem_common, "TRAY_LOCK_PATH", str(lock))
    monkeypatch.setattr(clipem_common, "pid_alive", lambda pid: True)
    monkeypatch.setattr(clipem_common, "is_tray_widget_process", lambda pid: True)
    monkeypatch.setattr(clipem_common.sys, "platform", "linux")

    def refuse(_pid, _sig):
        raise PermissionError("not allowed")

    monkeypatch.setattr(clipem_common.os, "kill", refuse)
    warnings = []
    monkeypatch.setattr(log, "warning", warnings.append)
    clipem_common.stop_tray_widget(log)
    assert "couldn't stop the tray widget" in warnings[0]


def test_windows_terminates_the_tray_by_handle(tmp_path, monkeypatch):
    lock = tmp_path / ".tray.lock"
    lock.write_text("4242")
    monkeypatch.setattr(clipem_common, "TRAY_LOCK_PATH", str(lock))
    terminated = []
    _fake_kernel32(monkeypatch, OpenProcess=lambda access, inherit, pid: pid + 1,
                   TerminateProcess=lambda handle, code: terminated.append(handle))
    alive = iter([True, False])
    monkeypatch.setattr(clipem_common, "pid_alive", lambda pid: next(alive))
    monkeypatch.setattr(clipem_common, "is_tray_widget_process", lambda pid: True)
    clipem_common.stop_tray_widget(log)
    assert terminated == [4243]


def test_exit_with_parent_does_nothing_outside_linux_or_without_a_parent(monkeypatch):
    monkeypatch.setattr(clipem_common, "_set_parent_death_signal", lambda _s: pytest.fail("not here"))
    monkeypatch.setattr(clipem_common.sys, "platform", "win32")
    clipem_common.exit_with_parent(1)
    clipem_common.outlive_parent()
    monkeypatch.setattr(clipem_common.sys, "platform", "linux")
    clipem_common.exit_with_parent(0)


def test_a_parent_already_gone_ends_the_helper(monkeypatch):
    exits = []
    monkeypatch.setattr(clipem_common.sys, "platform", "linux")
    monkeypatch.setattr(clipem_common, "_set_parent_death_signal", lambda _s: True)
    monkeypatch.setattr(clipem_common.os, "_exit", exits.append)
    monkeypatch.setattr(clipem_common.os, "getppid", lambda: 1)
    clipem_common.exit_with_parent(4242)
    assert exits == [0]
    clipem_common.exit_with_parent(1)
    assert exits == [0]


@pytest.mark.parametrize("outcome", [False, OSError("no prctl"), AttributeError("no libc")])
def test_no_parent_death_signal_leaves_the_helper_running(monkeypatch, outcome):
    def set_signal(_signum):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(clipem_common.sys, "platform", "linux")
    monkeypatch.setattr(clipem_common, "_set_parent_death_signal", set_signal)
    monkeypatch.setattr(clipem_common.os, "_exit", lambda _c: pytest.fail("the parent may be fine"))
    clipem_common.exit_with_parent(4242)
    if outcome is not False:
        clipem_common.outlive_parent()


def _pip(monkeypatch, *results):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return results[len(calls) - 1]

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _result(code=0, out="", err=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


def test_pip_output_is_echoed(monkeypatch, capsys):
    _pip(monkeypatch, _result(out="Installed\n"))
    clipem_common.run_pip(["pip", "install", "x"])
    assert "Installed" in capsys.readouterr().out


def test_a_pip_failure_quotes_its_last_lines(monkeypatch, capsys):
    lines = "\n".join(f"line {n}" for n in range(20))
    _pip(monkeypatch, _result(3, err=lines))
    with pytest.raises(RuntimeError) as exc:
        clipem_common.run_pip(["pip"])
    assert "exit 3" in str(exc.value) and "line 19" in str(exc.value)
    assert "line 12" in str(exc.value) and "line 11" not in str(exc.value)


def test_a_silent_pip_failure_still_says_something(monkeypatch):
    _pip(monkeypatch, _result(1))
    with pytest.raises(RuntimeError, match=r"\(no output\)"):
        clipem_common.run_pip(["pip"])


@pytest.mark.parametrize("platform, folder", [("win32", "Windows"), ("darwin", "macOS"), ("linux", "Linux")])
def test_the_wheelhouse_is_in_the_platforms_folder(monkeypatch, platform, folder):
    monkeypatch.setattr(clipem_common.sys, "platform", platform)
    assert clipem_common.wheelhouse_dir() == os.path.join(clipem_common.APP_DIR, folder, "wheelhouse")


def test_requirements_come_from_the_wheelhouse_when_it_has_them(tmp_path, monkeypatch):
    monkeypatch.setattr(clipem_common, "wheelhouse_dir", lambda: str(tmp_path))
    calls = _pip(monkeypatch, _result())
    clipem_common.install_requirements(["pip", "install"], "req.txt")
    assert calls == [["pip", "install", "--no-index", "--find-links", str(tmp_path), "-r", "req.txt"]]


def test_requirements_the_wheelhouse_lacks_are_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(clipem_common, "wheelhouse_dir", lambda: str(tmp_path))
    calls = _pip(monkeypatch, _result(1), _result())
    clipem_common.install_requirements(["pip", "install"], "req.txt")
    assert calls[1] == ["pip", "install", "-r", "req.txt"]


def test_requirements_without_a_wheelhouse_are_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(clipem_common, "wheelhouse_dir", lambda: str(tmp_path / "none"))
    calls = _pip(monkeypatch, _result())
    clipem_common.install_requirements(["pip", "install"], "req.txt")
    assert calls == [["pip", "install", "-r", "req.txt"]]


@pytest.fixture
def fresh_logger():
    name = f"clipem-test-{uuid.uuid4()}"
    yield name
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _own_handlers(logger):
    """Its handlers without pytest's own."""
    return [type(h) for h in logger.handlers if type(h).__module__.startswith("logging")]


def test_logs_go_to_their_own_file_once(tmp_path, monkeypatch, fresh_logger):
    monkeypatch.setattr(clipem_common, "LOG_DIR", str(tmp_path / "logs"))
    logger = clipem_common.setup_logging(fresh_logger)
    assert clipem_common.setup_logging(fresh_logger) is logger
    assert len(_own_handlers(logger)) == 2 and logger.propagate is False
    logger.info("hello")
    assert "INFO hello" in (tmp_path / "logs" / f"{fresh_logger}.log").read_text()


def test_a_read_only_install_still_logs_to_the_console(tmp_path, monkeypatch, fresh_logger):
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    monkeypatch.setattr(clipem_common, "LOG_DIR", str(blocked / "logs"))
    logger = clipem_common.setup_logging(fresh_logger)
    assert _own_handlers(logger) == [logging.StreamHandler]


def test_no_console_at_all_is_fine(tmp_path, monkeypatch, fresh_logger):
    """pythonw has no sys.stderr."""
    monkeypatch.setattr(clipem_common, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(clipem_common.sys, "stderr", None)
    logger = clipem_common.setup_logging(fresh_logger)
    assert _own_handlers(logger) == [logging.handlers.RotatingFileHandler]
