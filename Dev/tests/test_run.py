import json
import os
import subprocess
import sys
import threading

import pytest
import run
from conftest import has_display


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

    def ask(changes, notes=""):
        calls.append(("ask", changes))
        return answer["value"]

    monkeypatch.setattr(run, "_ask_about_update", ask)
    return run.updater.load_update_config, calls, lambda value: answer.update(value=value), available.update


def test_update_prompt_gets_the_release_notes(update_flow, monkeypatch):
    _config, _calls, _answer, available = update_flow
    available({"release_notes": "New: post-clip."})
    asked = []
    monkeypatch.setattr(run, "_ask_about_update", lambda changes, notes="": asked.append(notes))
    run._offer_update()
    assert asked == ["New: post-clip."]


def _prompt_text(monkeypatch, show):
    """The real window's text, top to bottom, from its labels and notes box."""
    import tkinter

    texts = []

    def read(widget):
        if isinstance(widget, tkinter.Label):
            texts.append(widget.cget("text"))
        elif isinstance(widget, tkinter.Text):
            assert widget.cget("state") == "disabled"
            texts.append(widget.get("1.0", "end-1c"))
        for child in widget.winfo_children():
            read(child)

    def read_and_close(root, *_args):
        read(root)
        root.destroy()

    monkeypatch.setattr(tkinter.Misc, "mainloop", read_and_close)
    show()
    return texts


def _update_prompt_text(monkeypatch, notes):
    return _prompt_text(monkeypatch, lambda: run._ask_about_update(["ClipEm 1.0.0 -> 1.1.0"], notes))


@pytest.mark.skipif(not has_display(), reason="needs a display")
def test_update_prompt_shows_the_notes_above_the_warning(monkeypatch):
    labels = _update_prompt_text(monkeypatch, "New: post-clip.")
    assert labels[1:3] == ["Release notes", "New: post-clip."]
    assert labels[3].startswith("Updating keeps")


@pytest.mark.skipif(not has_display(), reason="needs a display")
def test_update_prompt_without_notes_has_no_section(monkeypatch):
    assert "Release notes" not in _update_prompt_text(monkeypatch, "")


@pytest.fixture
def started(tmp_path, monkeypatch):
    """_note_version_started() on 1.5.0, returning what it showed: (old, new) or None."""
    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.setattr(run.updater, "UPDATE_CONFIG_PATH", str(tmp_path / "update_config.json"))
    monkeypatch.setattr(run.updater, "get_local_version", lambda: "1.5.0")
    shown = []
    monkeypatch.setattr(run, "_show_update_finished", lambda old, new: shown.append((old, new)))

    def start(last):
        shown.clear()
        run.updater.change_update_config(last_version=last)
        run._note_version_started()
        return shown[0] if shown else None

    return start


def test_a_newer_version_says_what_changed_once(started):
    assert started("1.4.9") == ("1.4.9", "1.5.0")
    run._note_version_started()
    assert run.updater.load_update_config()["last_version"] == "1.5.0"
    assert started("1.5.0") is None


def test_a_first_launch_says_nothing_and_remembers(started):
    assert started(None) is None
    assert run.updater.load_update_config()["last_version"] == "1.5.0"


def test_an_update_from_a_version_that_kept_no_record_has_no_from(started, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", run.POST_UPDATE_FLAG])
    assert started(None) == (None, "1.5.0")


def test_going_back_a_version_says_nothing(started):
    assert started("1.6.0-dev.202610142130") is None


def test_a_window_that_fails_doesnt_stop_the_launch_or_return(started, monkeypatch):
    def fail(old, new):
        raise RuntimeError("no display")

    monkeypatch.setattr(run, "_show_update_finished", fail)
    assert started("1.4.9") is None
    assert run.updater.load_update_config()["last_version"] == "1.5.0"


@pytest.mark.skipif(not has_display(), reason="needs a display")
@pytest.mark.parametrize("old, heading, shown", [
    ("1.4.0", "ClipEm Has Updated From 1.4.0 to 1.4.2", ["Two.", "One."]),
    ("1.4.1", "ClipEm Has Updated From 1.4.1 to 1.4.2", ["Two."]),
    (None, "ClipEm Has Updated To 1.4.2", ["Two."]),
])
def test_update_finished_window(monkeypatch, tmp_path, old, heading, shown):
    history = tmp_path / "release_notes.json"
    history.write_text(json.dumps([{"version": "1.4.2", "date": "2026-11-01", "sections": {"Fixes": ["Two."]}},
                                   {"version": "1.4.1", "date": "2026-10-31", "sections": {"Fixes": ["One."]}},
                                   {"version": "1.4.0", "date": "2026-10-30", "sections": {"Fixes": ["Old."]}}]))
    monkeypatch.setattr(run.release_notes, "RELEASE_NOTES_PATH", str(history))
    texts = _prompt_text(monkeypatch, lambda: run._show_update_finished(old, "1.4.2"))
    assert texts[0] == heading
    assert all(f"\u2022 {note}" in texts[1] for note in shown) and "Old." not in texts[1]
    assert "Fixes" in texts[1]
    assert ("1.4.2  (2026-11-01)" in texts[1]) == (len(shown) > 1)  # one version: the window's heading says it


@pytest.mark.skipif(not has_display(), reason="needs a display")
def test_update_finished_window_without_notes(monkeypatch, tmp_path):
    monkeypatch.setattr(run.release_notes, "RELEASE_NOTES_PATH", str(tmp_path / "missing.json"))
    texts = _prompt_text(monkeypatch, lambda: run._show_update_finished("1.4.0", "1.4.2"))
    assert texts[1] == run.release_notes.NO_NOTES


@pytest.mark.skipif(not has_display(), reason="needs a display")
@pytest.mark.parametrize("new_settings", [[{"key": "post_clip", "name": "Post-clip"}], []])
def test_update_finished_window_offers_the_new_settings(monkeypatch, tmp_path, new_settings):
    import tkinter

    history = tmp_path / "release_notes.json"
    history.write_text(json.dumps([{"version": "1.4.9", "date": "2026-11-01", "sections": {"Fixes": ["One."]},
                                    "new_settings": new_settings}]))
    request = tmp_path / ".settings.request"
    monkeypatch.setattr(run.release_notes, "RELEASE_NOTES_PATH", str(history))
    monkeypatch.setattr(run, "SETTINGS_REQUEST_PATH", str(request))
    monkeypatch.setattr(run, "_tray_widget_running", lambda: True)
    found = {}

    def press(root, *_args):
        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tkinter.Button):
                    found[child.cget("text")] = child
                elif isinstance(child, tkinter.Label):
                    found.setdefault("labels", []).append(child.cget("text"))
                walk(child)

        walk(root)
        found.get("Show New Settings", found["OK"]).invoke()

    monkeypatch.setattr(tkinter.Misc, "mainloop", press)
    run._show_update_finished("1.4.8", "1.4.9")
    assert ("Show New Settings" in found) == bool(new_settings)
    if new_settings:
        assert request.read_text() == "post_clip"
        assert "New in Settings: Post-clip." in found["labels"]
    else:
        assert not request.exists()


@pytest.mark.skipif(not has_display(), reason="needs a display")
def test_show_new_settings_says_so_when_the_tray_is_gone(monkeypatch, tmp_path):
    import tkinter

    history = tmp_path / "release_notes.json"
    history.write_text(json.dumps([{"version": "1.4.9", "date": "2026-11-01", "sections": {"Fixes": ["One."]},
                                    "new_settings": [{"key": "post_clip", "name": "Post-clip"}]}]))
    request = tmp_path / ".settings.request"
    errors = []
    monkeypatch.setattr(run.release_notes, "RELEASE_NOTES_PATH", str(history))
    monkeypatch.setattr(run, "SETTINGS_REQUEST_PATH", str(request))
    monkeypatch.setattr(run, "_tray_widget_running", lambda: False)
    monkeypatch.setattr(run, "_show_error", errors.append)

    def press(root, *_args):
        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tkinter.Button) and child.cget("text") == "Show New Settings":
                    child.invoke()
                    return
                walk(child)

        walk(root)

    monkeypatch.setattr(tkinter.Misc, "mainloop", press)
    run._show_update_finished("1.4.8", "1.4.9")
    assert not request.exists() and len(errors) == 1 and "tray icon" in errors[0]


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
    answer = {"value": None}

    def ask(reason, can_kill=True):
        asked.append(reason)
        answer["can_kill"] = can_kill
        return answer["value"]

    monkeypatch.setattr(run, "_ask_already_open", ask)
    monkeypatch.setattr(run, "_run_with_notice", lambda _text, work: work())

    def set_answer(value):
        answer["value"] = value
        return answer

    return tmp_path, asked, set_answer


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
    answer(run.ALREADY_OPEN_PROCEED)
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
    answer(run.ALREADY_OPEN_PROCEED)
    assert run._claim_launch() is True
    assert asked == ["ClipEm's OBS is already running."]


def _existing_clipem(monkeypatch, closes=True):
    """OBS "running" until closed (politely if `closes`, else only killed); returns the calls."""
    calls, state = [], {"obs": True}

    def close(log, timeout_seconds=15):
        calls.append("close")
        state["obs"] = not closes
        return closes

    def kill(log):
        calls.append("kill")
        state["obs"] = False
        return True

    monkeypatch.setattr(run.obs_process, "obs_running", lambda: state["obs"])
    monkeypatch.setattr(run.obs_process, "close_obs_stopping_outputs", close)
    monkeypatch.setattr(run.obs_process, "kill_obs", kill)
    monkeypatch.setattr(run, "stop_clip_editor", lambda log: calls.append("editor"))
    monkeypatch.setattr(run, "stop_tray_widget", lambda log: calls.append("tray"))
    return calls


def test_kill_existing_closes_everything_and_carries_on(launch_gate, monkeypatch):
    _tmp_path, _asked, answer = launch_gate
    calls = _existing_clipem(monkeypatch)
    state = answer(run.ALREADY_OPEN_KILL)
    assert run._claim_launch() is True  # this launch goes ahead as the only copy
    assert calls == ["close", "editor", "tray"] and state["can_kill"] is True


def test_kill_existing_forces_an_obs_that_wont_close(launch_gate, monkeypatch):
    _tmp_path, _asked, answer = launch_gate
    calls = _existing_clipem(monkeypatch, closes=False)
    answer(run.ALREADY_OPEN_KILL)
    assert run._claim_launch() is True
    assert calls == ["close", "tray", "kill", "editor", "tray"]  # or the tray takes it for a crash


def test_kill_existing_stops_if_something_survives(launch_gate, monkeypatch):
    _tmp_path, _asked, answer = launch_gate
    _existing_clipem(monkeypatch)
    monkeypatch.setattr(run, "_tray_widget_running", lambda: True)
    monkeypatch.setattr(run, "_show_error", lambda message: None)
    answer(run.ALREADY_OPEN_KILL)
    with pytest.raises(SystemExit):
        run._claim_launch()


def test_another_launch_cant_be_killed(launch_gate):
    tmp_path, _asked, answer = launch_gate
    state = answer(None)
    other = _other_process()
    try:
        (tmp_path / ".run.lock").write_text(str(other.pid))
        with pytest.raises(SystemExit):
            run._claim_launch()
        assert state["can_kill"] is False  # it may be installing an update
    finally:
        other.kill()
        other.wait()


@pytest.mark.skipif(not has_display(), reason="needs a display")
@pytest.mark.parametrize("can_kill, shown", [(True, True), (False, False)])
def test_already_open_dialog_offers_the_kill_button(monkeypatch, can_kill, shown):
    import tkinter

    labels = []

    def read_and_close(root, *_args):
        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tkinter.Button):
                    labels.append(child)
                walk(child)

        walk(root)
        kill = [b for b in labels if b.cget("text") == "Kill Existing ClipEm and OBS"]
        assert bool(kill) is shown
        (kill or [b for b in labels if b.cget("text") == "Cancel"])[0].invoke()

    monkeypatch.setattr(tkinter.Misc, "mainloop", read_and_close)
    assert run._ask_already_open("Reason.", can_kill=can_kill) == (run.ALREADY_OPEN_KILL if shown else None)


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


def test_a_running_tray_counts_as_already_open_except_for_an_obs_restart(launch_gate, monkeypatch):
    tmp_path, asked, answer = launch_gate
    answer(run.ALREADY_OPEN_PROCEED)
    tray = _other_process()
    monkeypatch.setattr(run, "is_tray_widget_process", lambda pid: pid == tray.pid)
    try:
        (tmp_path / ".tray.lock").write_text(str(tray.pid))
        run._claim_launch()
        assert asked == ["ClipEm's tray icon is already running."]
        # Settings' "Restart OBS" keeps the tray widget running on purpose.
        (tmp_path / ".run.lock").unlink()
        monkeypatch.setattr(sys, "argv", ["run.py", run.OBS_RESTART_FLAG])
        assert run._claim_launch() is True
        assert len(asked) == 1  # not asked again
        assert run._tray_widget_running()  # so run.py doesn't start a second one
        # ...but an OBS that's still running is still a second copy.
        (tmp_path / ".run.lock").unlink()
        monkeypatch.setattr(run.obs_process, "obs_running", lambda: True)
        run._claim_launch()
        assert asked[-1] == "ClipEm's OBS is already running."
    finally:
        tray.kill()
        tray.wait()


def test_resolve_tray_python_falls_back(tmp_path):
    assert run._resolve_tray_python(None, "/fallback") == "/fallback"
    assert run._resolve_tray_python(str(tmp_path), "/fallback") == "/fallback"  # no pin
    (tmp_path / run.TRAY_PYTHON_PIN_FILENAME).write_text("/gone/python\n")
    assert run._resolve_tray_python(str(tmp_path), "/fallback") == "/fallback"  # uninstalled since
    (tmp_path / run.TRAY_PYTHON_PIN_FILENAME).write_text("  \n")
    assert run._resolve_tray_python(str(tmp_path), "/fallback") == "/fallback"


def test_a_crash_sentinel_is_cleared_so_obs_doesnt_ask_about_safe_mode(tmp_path):
    (tmp_path / ".sentinel").mkdir()
    (tmp_path / ".sentinel" / "run_abc").write_text("")
    run._clear_sentinel(str(tmp_path))
    assert os.listdir(tmp_path / ".sentinel") == []
    run._clear_sentinel(str(tmp_path / "gone"))


def test_a_sentinel_that_cant_be_removed_doesnt_stop_a_launch(tmp_path, monkeypatch):
    def in_use(_path):
        raise PermissionError("in use")

    (tmp_path / ".sentinel").mkdir()
    (tmp_path / ".sentinel" / "run_abc").write_text("")
    monkeypatch.setattr(run.os, "remove", in_use)
    run._clear_sentinel(str(tmp_path))


class Tray:
    """_start_tray_widget with its prerequisite check's exit code `check`;
    `started`: what was launched."""

    def __init__(self, monkeypatch, tmp_path, check=0, stderr=""):
        self.started = []
        self.checks = []
        self.logs = []

        def run_check(cmd, **kwargs):
            self.checks.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, check, stderr=stderr)

        monkeypatch.setattr(run, "_tray_widget_running", lambda: False)
        monkeypatch.setattr(run.subprocess, "run", run_check)
        monkeypatch.setattr(run.subprocess, "Popen", lambda cmd, **kwargs: self.started.append((cmd, kwargs)))
        monkeypatch.setattr(run.log, "info", self.logs.append)


def test_the_tray_starts_with_its_packages_on_the_path(monkeypatch, tmp_path):
    tray = Tray(monkeypatch, tmp_path)
    monkeypatch.setenv("PYTHONPATH", "/existing")
    run._start_tray_widget("/python", extra_env={"EXTRA": "1"}, tray_deps_dir="/deps")
    (cmd, kwargs), = tray.started
    assert cmd == ["/python", run.TRAY_WIDGET_SCRIPT]
    assert kwargs["env"]["PYTHONPATH"] == os.pathsep.join(["/deps", "/existing"]) and kwargs["env"]["EXTRA"] == "1"
    assert tray.checks[0][1]["env"] == kwargs["env"]


def test_the_tray_path_has_no_trailing_separator_when_nothing_was_set(monkeypatch, tmp_path):
    tray = Tray(monkeypatch, tmp_path)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    run._start_tray_widget("/python", tray_deps_dir="/deps")
    assert tray.started[0][1]["env"]["PYTHONPATH"] == "/deps"


def test_the_tray_check_needs_pillows_compiled_part(monkeypatch, tmp_path):
    """`import PIL` alone passes with a Pillow built for another Python."""
    tray = Tray(monkeypatch, tmp_path)
    monkeypatch.setattr(run.sys, "platform", "linux")
    run._start_tray_widget("/python")
    code = tray.checks[0][0][-1]
    assert "from PIL import Image, ImageDraw" in code and "dbus_next" in code and "websocket" in code
    monkeypatch.setattr(run.sys, "platform", "darwin")
    run._start_tray_widget("/python")
    assert "pystray" in tray.checks[1][0][-1]


def test_a_tray_that_couldnt_run_isnt_started(monkeypatch, tmp_path):
    tray = Tray(monkeypatch, tmp_path, check=1, stderr="Traceback\nImportError: _imaging\n")
    run._start_tray_widget("/python")
    assert tray.started == [] and tray.logs[-1] == "ImportError: _imaging"


def test_a_running_tray_isnt_started_again(monkeypatch, tmp_path):
    tray = Tray(monkeypatch, tmp_path)
    monkeypatch.setattr(run, "_tray_widget_running", lambda: True)
    run._start_tray_widget("/python")
    assert tray.started == [] and tray.checks == []


def test_windows_starts_the_tray_windowless_and_detached(monkeypatch, tmp_path):
    tray = Tray(monkeypatch, tmp_path)
    monkeypatch.setattr(run.sys, "platform", "win32")
    monkeypatch.setattr(run, "_NO_CONSOLE_FLAGS", 0x08000000, raising=False)
    monkeypatch.setattr(run, "_DETACHED_FLAGS", 520, raising=False)
    (tmp_path / "pythonw.exe").write_text("")
    python = str(tmp_path / "python.exe")
    run._start_tray_widget(python)
    (cmd, kwargs), = tray.started
    assert cmd[0] == str(tmp_path / "pythonw.exe") and kwargs["creationflags"] == 520
    assert tray.checks[0][0][0] == python


def test_a_python_without_pythonw_is_used_as_it_is(monkeypatch, tmp_path):
    monkeypatch.setattr(run.sys, "platform", "win32")
    assert run._windowless_python(str(tmp_path / "python.exe")) == str(tmp_path / "python.exe")


@pytest.mark.parametrize("config, obs_started, tray_started", [
    ({"autostart_obs": True, "autostart_tray": True}, True, True),
    ({"autostart_obs": False, "autostart_tray": True}, False, True),
    ({"autostart_obs": True, "autostart_tray": False}, True, False),
    ({"autostart_obs": False, "autostart_tray": False}, False, False),
])
def test_each_platform_follows_the_boot_config(tmp_path, monkeypatch, config, obs_started, tray_started):
    exe = tmp_path / "Linux" / "obs-install" / "bin" / "obs"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    started, tray = [], []
    monkeypatch.setattr(run.obs_install, "platform_dir", lambda system: str(tmp_path / "Linux"))
    monkeypatch.setattr(run, "_ensure_obs", lambda system: str(exe))
    monkeypatch.setattr(run.helper, "prepare", lambda *a, **k: None)
    monkeypatch.setattr(run, "_obs_launch_flags", lambda: (["--startreplaybuffer"], {"A": "1"}))
    monkeypatch.setattr(run.subprocess, "Popen", lambda cmd, **kwargs: started.append(cmd))
    monkeypatch.setattr(run, "_start_tray_widget", lambda python, extra_env, tray_deps_dir: tray.append(extra_env))
    run.run_linux(config)
    assert bool(started) is obs_started and bool(tray) is tray_started
    if started:
        assert started[0] == [str(exe), "-p", "--startreplaybuffer"]
    if tray:
        assert tray[0] == {"A": "1", "PYSTRAY_BACKEND": "appindicator"}


def test_obs_is_told_where_clipems_plugin_is(tmp_path, monkeypatch):
    (tmp_path / "linux").mkdir()
    monkeypatch.setattr(run, "OBS_PLUGIN_DIR", str(tmp_path))
    env = run._obs_env("linux")
    assert env["OBS_PLUGINS_PATH"] == env["OBS_PLUGINS_DATA_PATH"] == str(tmp_path / "linux")


def test_without_the_plugin_obs_still_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "OBS_PLUGIN_DIR", str(tmp_path))
    monkeypatch.delenv("OBS_PLUGINS_PATH", raising=False)
    assert "OBS_PLUGINS_PATH" not in run._obs_env("linux")


def test_macos_stops_without_obs(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "APP_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="OBS.app not found"):
        run.run_macos({"autostart_obs": True, "autostart_tray": True})


def test_macos_starts_obs_and_the_tray(tmp_path, monkeypatch):
    exe = tmp_path / "macOS" / "OBS.app" / "Contents" / "MacOS" / "OBS"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    started, tray = [], []
    monkeypatch.setattr(run, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(run.helper, "prepare", lambda *a, **k: None)
    monkeypatch.setattr(run, "_obs_launch_flags", lambda: (["--flag"], {}))
    monkeypatch.setattr(run.subprocess, "Popen", lambda cmd, **kwargs: started.append(cmd))
    monkeypatch.setattr(run, "_start_tray_widget", lambda python, extra_env, tray_deps_dir: tray.append(tray_deps_dir))
    run.run_macos({"autostart_obs": True, "autostart_tray": True})
    assert started == [[str(exe), "--portable", "--flag"]]
    assert tray == [str(tmp_path / "macOS" / "tray-deps")]


def test_tcl_is_only_pointed_at_on_windows_and_never_over_a_setting(tmp_path, monkeypatch):
    (tmp_path / "tcl" / "tcl8.6").mkdir(parents=True)
    monkeypatch.setattr(run.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(run.sys, "platform", "linux")
    monkeypatch.delenv("TCL_LIBRARY", raising=False)
    monkeypatch.delenv("TK_LIBRARY", raising=False)
    run._enable_windows_tk()
    assert "TCL_LIBRARY" not in os.environ
    monkeypatch.setattr(run.sys, "platform", "win32")
    run._enable_windows_tk()
    assert os.environ["TCL_LIBRARY"] == str(tmp_path / "tcl" / "tcl8.6") and "TK_LIBRARY" not in os.environ
    monkeypatch.setenv("TCL_LIBRARY", "/mine")
    run._enable_windows_tk()
    assert os.environ["TCL_LIBRARY"] == "/mine"


def test_an_error_dialog_that_cant_open_is_only_skipped(monkeypatch):
    def no_display(*_args, **_kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr("tkinter.Tk", no_display)
    run._show_error("message")


def test_a_fatal_error_is_logged_shown_and_exits(monkeypatch):
    shown, logged = [], []
    monkeypatch.setattr(run, "_show_error", shown.append)
    monkeypatch.setattr(run.log, "error", logged.append)
    with pytest.raises(SystemExit) as exc:
        run._fatal("broken")
    assert exc.value.code == "broken" and shown == ["broken"] and logged == ["broken"]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_restarting_replaces_this_launch(monkeypatch, platform):
    ran = []
    monkeypatch.setattr(sys, "argv", ["run.py", "--boot", run.POST_UPDATE_FLAG])
    monkeypatch.setattr(run.sys, "platform", platform)
    monkeypatch.setattr(run, "_DETACHED_FLAGS", 520, raising=False)
    monkeypatch.setattr(run.os, "execv", lambda exe, argv: ran.append(argv))
    monkeypatch.setattr(run.subprocess, "Popen", lambda argv, creationflags: ran.append(argv))
    if platform == "win32":
        with pytest.raises(SystemExit) as exc:
            run._restart_self([run.POST_UPDATE_FLAG])
        assert exc.value.code == 0
    else:
        run._restart_self([run.POST_UPDATE_FLAG])
    assert ran[0][2:] == ["--boot", run.POST_UPDATE_FLAG, run.RESTARTED_FLAG]  # the flag isn't doubled


class Launch:
    """run.main() with every step faked; `steps` is what happened, in order."""

    def __init__(self, monkeypatch, tmp_path, argv=(), may_update=True, obs_running=False, pending=None):
        self.steps = []
        self.obs_running = obs_running
        step = self.steps.append
        monkeypatch.setattr(sys, "argv", ["run.py", *argv])
        monkeypatch.setattr(run, "_claim_launch", lambda: may_update)
        monkeypatch.setattr(run.platform, "system", lambda: "Linux")
        monkeypatch.setattr(run, "_reinstall_if_requested", lambda: step("reinstall"))
        monkeypatch.setattr(run, "_install_pending_update", lambda: step("update"))
        monkeypatch.setattr(run, "_note_version_started", lambda: step("note_version"))
        monkeypatch.setattr(run, "_offer_update", lambda: step("offer"))
        monkeypatch.setattr(run, "run_linux", lambda config: step(("linux", config)))
        monkeypatch.setattr(run, "load_run_config", lambda: {"from": "run_config.json"})
        monkeypatch.setattr(run.obs_process, "obs_running", lambda: self.obs_running)
        config = run.obs_config
        monkeypatch.setattr(config, "ensure_obs_websocket_password", lambda: step("password"))
        monkeypatch.setattr(config, "load_pending_obs_settings", lambda: pending)
        monkeypatch.setattr(config, "apply_pending_obs_settings", lambda: step("apply_pending") or ["x"])
        monkeypatch.setattr(config, "migrate_discord_target_size", lambda: step("migrate_discord"))
        monkeypatch.setattr(config, "migrate_split_tracks_layout", lambda: step("migrate_tracks"))
        monkeypatch.setattr(config, "migrate_audio_bitrate", lambda: step("migrate_audio"))
        monkeypatch.setattr(config, "fit_replay_buffer_size", lambda: step("fit_buffer") or 512)
        monkeypatch.setattr(config, "load_split_tracks", lambda: False)
        monkeypatch.setattr(run.autostart, "ensure_start_menu_entry", lambda: step("start_menu"))
        monkeypatch.setattr(run.autostart, "repair_linux_menu_entry", lambda: step("repair_menu"))
        import setup

        monkeypatch.setattr(setup, "ensure_recording_encoder_available", lambda: step("encoder"))


def test_a_launch_updates_first_and_offers_updates_last(monkeypatch, tmp_path):
    launch = Launch(monkeypatch, tmp_path)
    run.main()
    assert launch.steps[:3] == ["reinstall", "update", "password"]
    assert launch.steps[-3:] == [("linux", dict(run.DEFAULT_RUN_CONFIG)), "note_version", "offer"]


def test_settings_and_migrations_all_happen_before_obs_starts(monkeypatch, tmp_path):
    launch = Launch(monkeypatch, tmp_path, pending={"video": 1})
    run.main()
    started = launch.steps.index(("linux", dict(run.DEFAULT_RUN_CONFIG)))
    for step in ("password", "apply_pending", "migrate_discord", "migrate_tracks", "migrate_audio", "fit_buffer",
                 "encoder", "start_menu"):
        assert launch.steps.index(step) < started


def test_a_launch_beside_another_leaves_updates_to_it(monkeypatch, tmp_path):
    launch = Launch(monkeypatch, tmp_path, may_update=False)
    run.main()
    assert not {"reinstall", "update", "note_version", "offer"} & set(map(str, launch.steps))
    assert "password" in launch.steps


def test_only_a_boot_launch_follows_the_saved_config(monkeypatch, tmp_path):
    launch = Launch(monkeypatch, tmp_path, argv=["--boot"])
    run.main()
    assert ("linux", {"from": "run_config.json"}) in launch.steps


def test_nothing_changes_obss_config_while_it_runs(monkeypatch, tmp_path):
    """It'd overwrite the scene collection on exit."""
    launch = Launch(monkeypatch, tmp_path, obs_running=True, pending={"video": 1})
    run.main()
    for step in ("apply_pending", "migrate_discord", "migrate_tracks", "migrate_audio", "fit_buffer", "encoder"):
        assert step not in launch.steps


def test_no_queued_settings_means_nothing_to_apply(monkeypatch, tmp_path):
    launch = Launch(monkeypatch, tmp_path)
    run.main()
    assert "apply_pending" not in launch.steps


@pytest.mark.parametrize("module, name", [
    ("obs_config", "ensure_obs_websocket_password"),
    ("obs_config", "apply_pending_obs_settings"),
    ("obs_config", "migrate_discord_target_size"),
    ("obs_config", "migrate_split_tracks_layout"),
    ("obs_config", "migrate_audio_bitrate"),
    ("obs_config", "fit_replay_buffer_size"),
    ("obs_config", "load_split_tracks"),
    ("autostart", "ensure_start_menu_entry"),
    ("autostart", "repair_linux_menu_entry"),
])
def test_no_single_step_stops_obs_starting(monkeypatch, tmp_path, module, name):
    launch = Launch(monkeypatch, tmp_path, pending={"video": 1})

    def fail():
        raise OSError("step failed")

    monkeypatch.setattr(getattr(run, module), name, fail)
    monkeypatch.setattr(run.log, "warning", lambda _message: None)
    run.main()
    assert ("linux", dict(run.DEFAULT_RUN_CONFIG)) in launch.steps


def test_a_failed_encoder_check_doesnt_stop_obs_starting(monkeypatch, tmp_path):
    import setup

    launch = Launch(monkeypatch, tmp_path)

    def fail():
        raise OSError("no encoders")

    monkeypatch.setattr(setup, "ensure_recording_encoder_available", fail)
    run.main()
    assert ("linux", dict(run.DEFAULT_RUN_CONFIG)) in launch.steps


def test_a_changed_encoder_is_reported(monkeypatch, tmp_path):
    import setup

    Launch(monkeypatch, tmp_path)
    warnings = []
    monkeypatch.setattr(setup, "ensure_recording_encoder_available", lambda: "Moved to x264.")
    monkeypatch.setattr(run.log, "warning", warnings.append)
    run.main()
    assert warnings == ["Moved to x264."]


def test_a_missing_app_capture_helper_is_reported_but_not_fatal(monkeypatch, tmp_path):
    launch = Launch(monkeypatch, tmp_path)
    warnings = []
    monkeypatch.setattr(run.obs_config, "load_split_tracks", lambda: True)
    monkeypatch.setattr(run.obs_config, "app_capture_helper_missing", lambda: True)
    monkeypatch.setattr(run.log, "warning", warnings.append)
    run.main()
    assert "apps won't get their own tracks" in warnings[0]
    assert ("linux", dict(run.DEFAULT_RUN_CONFIG)) in launch.steps


@pytest.mark.parametrize("system, runner", [("Windows", "run_windows"), ("Darwin", "run_macos")])
def test_each_platform_runs_its_own_launch(monkeypatch, tmp_path, system, runner):
    launch = Launch(monkeypatch, tmp_path)
    monkeypatch.setattr(run.platform, "system", lambda: system)
    monkeypatch.setattr(run, runner, lambda config: launch.steps.append(runner))
    run.main()
    assert runner in launch.steps


def test_an_unknown_platform_stops(monkeypatch, tmp_path):
    Launch(monkeypatch, tmp_path)
    monkeypatch.setattr(run.platform, "system", lambda: "Plan9")
    with pytest.raises(SystemExit, match="Unsupported platform: Plan9"):
        run.main()
