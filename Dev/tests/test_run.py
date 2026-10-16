import os
import subprocess
import sys
import threading

import pytest
import run


def test_post_update_launch_never_offers_again(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", run.POST_UPDATE_FLAG])

    def fail(*_args):
        raise AssertionError("no update check right after an update")

    monkeypatch.setattr(run.updater, "check_for_update", fail)
    run._offer_update()


class Restarted(BaseException):
    """What the faked _restart_self raises: a real one never returns."""


@pytest.fixture
def update_flow(tmp_path, monkeypatch):
    """_offer_update()/_install_pending_update() with update_config.json in
    tmp_path, ClipEm 1.1.0 "published" (set_available() changes that) and
    nothing installed or restarted for real. Returns
    (config(), calls, set_answer, set_available)."""
    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.setattr(run.updater, "UPDATE_CONFIG_PATH", str(tmp_path / "update_config.json"))
    available = {"shared_update": True, "remote_version": "1.1.0", "components": {}}

    def check_for_update():
        result = {"local_version": "1.0.0", "error": None, **available}
        result["update_available"] = result["shared_update"] or bool(result["components"])
        return result

    monkeypatch.setattr(run.updater, "check_for_update", check_for_update)
    monkeypatch.setattr(run.updater, "fetch_manifest", lambda: {"version": available["remote_version"]})
    calls = []
    answer = {"value": None}
    monkeypatch.setattr(run, "_run_with_notice", lambda _text, work: work())
    monkeypatch.setattr(run.updater, "apply_update", lambda manifest=None: calls.append("apply") or (True, "ok"))
    monkeypatch.setattr(run.components, "install",
                        lambda manifest, plan, log, run_args=(): calls.append(("components", plan)) or available.get(
                            "outcome"))
    monkeypatch.setattr(run, "_restart_clipem_to_update", lambda: calls.append("restart_clipem"))
    def restart_self(args):
        calls.append(("restart_self", args))
        raise Restarted

    monkeypatch.setattr(run, "_restart_self", restart_self)
    monkeypatch.setattr(run, "_show_error", lambda message: calls.append(("error", message)))

    def ask(changes):
        calls.append(("ask", changes))
        return answer["value"]

    monkeypatch.setattr(run, "_ask_about_update", ask)
    return run.updater.load_update_config, calls, lambda value: answer.update(value=value), available.update


def test_update_is_never_installed_without_asking(update_flow):
    config, calls, _answer, _available = update_flow
    run._offer_update()  # prompt closed without choosing
    assert calls == [("ask", ["ClipEm 1.0.0 -> 1.1.0"])]
    assert config()["install_on_next_launch"] is False and config()["skipped_version"] is None
    run._install_pending_update()
    assert len(calls) == 1


def test_update_and_restart_now_installs_at_the_relaunch(update_flow):
    config, calls, answer, _available = update_flow
    answer(run.UPDATE_NOW)
    run._offer_update()
    assert calls[1:] == ["restart_clipem"]  # nothing installed while OBS/the tray still run
    assert config()["install_on_next_launch"] is True


def test_update_during_next_restart_installs_before_obs_next_launch(update_flow, monkeypatch):
    config, calls, answer, available = update_flow
    answer(run.UPDATE_NEXT_LAUNCH)
    available(components={"obs": ("32.2.2", "32.3.0")})
    run._offer_update()
    assert calls[0] == ("ask", ["ClipEm 1.0.0 -> 1.1.0", "OBS 32.2.2 -> 32.3.0"])
    assert "apply" not in calls and config()["install_on_next_launch"] is True

    calls.clear()
    with pytest.raises(Restarted):
        run._install_pending_update()  # ClipEm itself first, then the new code does the rest
    assert calls == ["apply", ("restart_self", [run.POST_UPDATE_FLAG])]
    assert config()["install_on_next_launch"] is True

    calls.clear()
    monkeypatch.setattr(sys, "argv", ["run.py", run.POST_UPDATE_FLAG])
    run._install_pending_update()
    assert calls == [("components", {"obs": ("32.2.2", "32.3.0")})]
    assert config()["install_on_next_launch"] is False


def test_a_python_update_restarts_on_the_new_python(update_flow):
    config, calls, _answer, available = update_flow
    available(shared_update=False, components={"python": ("3.12.14", "3.12.15")}, outcome=run.components.RESTART)
    run.updater.change_update_config(install_on_next_launch=True)
    with pytest.raises(Restarted):
        run._install_pending_update()
    assert calls == [("components", {"python": ("3.12.14", "3.12.15")}), ("restart_self", [run.POST_UPDATE_FLAG])]

    calls.clear()
    available(outcome=run.components.HANDOFF)  # Windows: the swap script starts ClipEm again
    run.updater.change_update_config(install_on_next_launch=True)
    with pytest.raises(SystemExit):
        run._install_pending_update()


def test_pending_update_waits_while_offline(update_flow, monkeypatch):
    config, calls, _answer, _available = update_flow
    run.updater.change_update_config(install_on_next_launch=True)

    def offline():
        raise OSError("no network")

    monkeypatch.setattr(run.updater, "fetch_manifest", offline)
    run._install_pending_update()
    assert calls == [] and config()["install_on_next_launch"] is True


def test_pending_update_already_installed_elsewhere_does_nothing(update_flow):
    config, calls, _answer, available = update_flow
    available(shared_update=False)  # e.g. installed from Settings meanwhile
    run.updater.change_update_config(install_on_next_launch=True)
    run._install_pending_update()
    assert calls == [] and config()["install_on_next_launch"] is False


def test_failed_component_update_is_reported_and_offered_again(update_flow, monkeypatch):
    config, calls, _answer, available = update_flow
    available(shared_update=False, components={"obs": ("32.2.2", "32.3.0")})
    run.updater.change_update_config(install_on_next_launch=True)

    def fail(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(run.components, "install", fail)
    run._install_pending_update()
    assert calls[0][0] == "error" and "disk full" in calls[0][1]
    assert config()["install_on_next_launch"] is False


def test_do_not_update_stays_quiet_until_a_newer_version(update_flow):
    config, calls, answer, available = update_flow
    answer(run.UPDATE_SKIP)
    run._offer_update()
    assert config()["skipped_version"] == "1.1.0"

    calls.clear()
    run._offer_update()
    assert calls == []

    available(remote_version="1.2.0")
    run._offer_update()
    assert calls == [("ask", ["ClipEm 1.0.0 -> 1.2.0"])]


def test_checking_for_updates_can_be_turned_off(update_flow):
    _config, calls, _answer, _available = update_flow
    run.updater.change_update_config(check_for_updates=False)
    run._offer_update()
    assert calls == []


def test_resolve_tray_python_prefers_pin(tmp_path):
    pinned = tmp_path / "python3.12"
    pinned.write_text("")
    (tmp_path / run.TRAY_PYTHON_PIN_FILENAME).write_text(str(pinned))
    assert run._resolve_tray_python(str(tmp_path), "/usr/bin/python3") == str(pinned)
    assert run._resolve_tray_python(None, "/usr/bin/python3") == "/usr/bin/python3"


@pytest.fixture
def app(tmp_path, monkeypatch):
    """run_windows()/run_linux() against an empty App/ in tmp_path, with a
    system `obs` on PATH, nothing actually launched (Popen calls are
    recorded) and no dialogs."""
    app = tmp_path / "App"
    app.mkdir()
    system_bin = tmp_path / "usr-bin"
    system_bin.mkdir()
    (system_bin / "obs").write_text("#!/bin/sh\n")
    (system_bin / "obs").chmod(0o755)
    monkeypatch.setenv("PATH", str(system_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(run, "APP_DIR", str(app))
    monkeypatch.setattr(run.obs_install, "APP_DIR", str(app))
    monkeypatch.setattr(run.obs_install, "LOCAL_CHECKSUMS_PATH", str(app / "checksums_local.json"))
    monkeypatch.setattr(run.helper, "prepare", lambda *a, **k: None)
    monkeypatch.setattr(run, "_show_error", lambda message: None)
    monkeypatch.setattr(run, "_run_with_notice", lambda text, work: work())
    launched = []
    monkeypatch.setattr(run.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))
    return app, launched


NO_TRAY = {"autostart_obs": True, "autostart_tray": False}


def _fake_install(monkeypatch, installed):
    def install(system, log=print):
        exe = run.obs_install.obs_exe(system)
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        open(exe, "w").close()
        installed.append(system)
        return exe

    monkeypatch.setattr(run.obs_install, "install_obs", install)


def _offline(monkeypatch):
    def install(system, log=print):
        raise run.obs_install.ObsInstallError("no network")

    monkeypatch.setattr(run.obs_install, "install_obs", install)


@pytest.mark.parametrize("system, run_platform, flag", [("Linux", "run_linux", "-p"), ("Windows", "run_windows", "--portable")])
def test_missing_obs_is_downloaded_then_launched(app, monkeypatch, system, run_platform, flag):
    _, launched = app
    installed = []
    _fake_install(monkeypatch, installed)
    if system == "Windows":
        monkeypatch.setattr(run.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, raising=False)
    getattr(run, run_platform)(NO_TRAY)
    assert installed == [system]
    assert launched[0][:2] == [run.obs_install.obs_exe(system), flag]


@pytest.mark.parametrize("system, run_platform", [("Linux", "run_linux"), ("Windows", "run_windows")])
def test_obs_that_cant_be_downloaded_stops_with_an_error_and_never_uses_a_system_obs(
    app, monkeypatch, system, run_platform
):
    _, launched = app
    _offline(monkeypatch)
    shown = []
    monkeypatch.setattr(run, "_show_error", shown.append)
    with pytest.raises(SystemExit) as exc:
        getattr(run, run_platform)(NO_TRAY)
    assert "couldn't be downloaded (no network)" in str(exc.value)
    assert shown and launched == []


def test_present_obs_is_not_downloaded_again(app, monkeypatch):
    _, launched = app
    exe = run.obs_install.obs_exe("Linux")
    os.makedirs(os.path.dirname(exe))
    open(exe, "w").close()
    monkeypatch.setattr(run.obs_install, "install_obs", lambda *a, **k: pytest.fail("already installed"))
    run.run_linux(NO_TRAY)
    assert launched[0][0] == exe


def test_download_notice_falls_back_to_no_window(monkeypatch):
    """No display (or no tkinter): the work still runs, errors still surface."""
    import tkinter

    def no_display(*a, **k):
        raise tkinter.TclError("no display")

    monkeypatch.setattr(tkinter, "Tk", no_display)
    assert run._run_with_notice("x", lambda: 42) == 42
    with pytest.raises(ValueError):
        run._run_with_notice("x", lambda: (_ for _ in ()).throw(ValueError("boom")))


@pytest.fixture
def launch_gate(tmp_path, monkeypatch):
    """_claim_launch() with its locks in tmp_path, nothing running and the
    "already open" prompt answered with set_answer() (records each reason)."""
    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.setattr(run, "RUN_LOCK_PATH", str(tmp_path / ".run.lock"))
    monkeypatch.setattr(run, "TRAY_LOCK_PATH", str(tmp_path / ".tray.lock"))
    monkeypatch.setattr(run.obs_process, "obs_running", lambda: False)
    asked = []
    answer = {"proceed": False}

    def ask(reason):
        asked.append(reason)
        return answer["proceed"]

    monkeypatch.setattr(run, "_ask_already_open", ask)
    return tmp_path, asked, lambda proceed: answer.update(proceed=proceed)


def _other_process():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_a_lone_launch_starts_without_asking(launch_gate):
    tmp_path, asked, _answer = launch_gate
    assert run._claim_launch() is True
    assert asked == []
    assert (tmp_path / ".run.lock").read_text() == str(os.getpid())


def test_cancel_when_another_launch_is_running(launch_gate):
    tmp_path, asked, _answer = launch_gate
    other = _other_process()
    try:
        (tmp_path / ".run.lock").write_text(str(other.pid))
        with pytest.raises(SystemExit):
            run._claim_launch()
        assert asked == ["ClipEm is still starting up or installing an update."]
    finally:
        other.kill()
        other.wait()


def test_proceed_anyway_next_to_another_launch_leaves_updates_to_it(launch_gate):
    tmp_path, asked, answer = launch_gate
    answer(True)
    other = _other_process()
    try:
        (tmp_path / ".run.lock").write_text(str(other.pid))
        assert run._claim_launch() is False  # no second update install at once
    finally:
        other.kill()
        other.wait()


def test_running_obs_counts_as_already_open(launch_gate, monkeypatch):
    _tmp_path, asked, answer = launch_gate
    monkeypatch.setattr(run.obs_process, "obs_running", lambda: True)
    answer(True)
    assert run._claim_launch() is True
    assert asked == ["ClipEm's OBS is already running."]


def test_a_restart_waits_for_the_launch_it_replaces(launch_gate, monkeypatch):
    tmp_path, asked, _answer = launch_gate
    monkeypatch.setattr(sys, "argv", ["run.py", run.RESTARTED_FLAG])
    monkeypatch.setattr(run.obs_process, "obs_running", lambda: True)  # never asked about on a restart
    previous = _other_process()
    (tmp_path / ".run.lock").write_text(str(previous.pid))
    exits = threading.Timer(0.5, lambda: (previous.kill(), previous.wait()))
    exits.start()
    assert run._claim_launch() is True
    assert asked == []
    assert (tmp_path / ".run.lock").read_text() == str(os.getpid())
    exits.join()


def test_restarts_are_marked_as_restarts(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", "--boot"])
    monkeypatch.setattr(sys, "platform", "linux")
    seen = []
    monkeypatch.setattr(run.os, "execv", lambda exe, argv: seen.append(argv[2:]))
    run._restart_self([run.POST_UPDATE_FLAG])
    assert seen == [["--boot", run.POST_UPDATE_FLAG, run.RESTARTED_FLAG]]


def test_obs_skips_replay_buffer_while_clips_folder_is_missing(app, monkeypatch, tmp_path):
    _, launched = app
    exe = run.obs_install.obs_exe("Linux")
    os.makedirs(os.path.dirname(exe))
    open(exe, "w").close()
    clips = tmp_path / "pCloudDrive" / "Clips"
    monkeypatch.setattr(run.obs_config, "load_clip_folder", lambda: str(clips))
    trays = []
    monkeypatch.setattr(run, "_start_tray_widget", lambda python, extra_env=None, **k: trays.append(extra_env))
    with_tray = {"autostart_obs": True, "autostart_tray": True}

    run.run_linux(with_tray)
    assert "--startreplaybuffer" not in launched[-1]
    assert trays[-1][run.REPLAY_BUFFER_DEFERRED_ENV] == "1"

    clips.mkdir(parents=True)
    run.run_linux(with_tray)
    assert "--startreplaybuffer" in launched[-1]
    assert run.REPLAY_BUFFER_DEFERRED_ENV not in trays[-1]
