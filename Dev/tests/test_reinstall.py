"""A clean reinstall (Installer/reinstall.py), and run.py carrying one out.
It throws away what's installed, so a failure must be reported once, change
nothing it didn't say it would, and never come back on every launch."""

import json
import sys

import components
import pytest
import reinstall
import run
import updater


@pytest.fixture
def request_file(tmp_path, monkeypatch):
    path = tmp_path / "reinstall_request.json"
    monkeypatch.setattr(reinstall, "REQUEST_PATH", str(path))
    return path


@pytest.mark.parametrize("obs, clipem", [(True, False), (False, True), (1, 0)])
def test_a_request_is_recorded_for_the_next_launch(request_file, obs, clipem):
    reinstall.request_reinstall(obs, clipem)
    assert reinstall.load_request() == {"keep_obs_settings": bool(obs), "keep_clipem_settings": bool(clipem),
                                        "stage": reinstall.STAGE_SHARED}


@pytest.mark.parametrize("content", ["{broken", "[1]", "{}", '{"stage": "other"}'])
def test_anything_else_in_the_file_is_no_request(request_file, content):
    request_file.write_text(content)
    assert reinstall.load_request() is None


def test_no_file_is_no_request(request_file):
    assert reinstall.load_request() is None


def test_clearing_twice_is_fine(request_file):
    reinstall.request_reinstall(True, True)
    reinstall.clear_request()
    reinstall.clear_request()
    assert reinstall.load_request() is None


@pytest.mark.parametrize("request_, kept, reset", [
    ({"keep_obs_settings": True, "keep_clipem_settings": True}, {"OBS-Config", "run_config.json"}, set()),
    ({"keep_obs_settings": True}, {"OBS-Config", "pending_obs_settings.json"}, {"run_config.json"}),
    ({"keep_clipem_settings": True}, {"run_config.json", "Scripts/replay_buffer_save_sound.wav"}, {"OBS-Config"}),
    ({}, set(), {"OBS-Config", "run_config.json", "update_config.json"}),
])
def test_what_each_choice_keeps(request_, kept, reset):
    preserved = reinstall.preserved_paths(request_)
    assert kept <= preserved and not reset & preserved
    assert updater.ALWAYS_PRESERVED_PATHS <= preserved


def test_a_reinstalled_shared_moves_on_to_the_components(request_file, monkeypatch):
    reinstall.request_reinstall(True, False)
    seen = []
    monkeypatch.setattr(updater, "apply_update", lambda manifest, preserved: seen.append((manifest, preserved)) or (True, ""))
    request = reinstall.load_request()
    manifest = {"version": "1.2.3"}
    assert reinstall.reinstall_shared(request, manifest) == (True, "Reinstalled ClipEm 1.2.3.")
    assert seen == [(manifest, reinstall.preserved_paths(request))]
    assert reinstall.load_request()["stage"] == reinstall.STAGE_COMPONENTS
    assert reinstall.load_request()["keep_obs_settings"] is True


def test_a_failed_shared_stays_where_it_was(request_file, monkeypatch):
    reinstall.request_reinstall(True, False)
    monkeypatch.setattr(updater, "apply_update", lambda manifest, preserved: (False, "no space"))
    assert reinstall.reinstall_shared(reinstall.load_request(), {"version": "1"}) == (False, "no space")
    assert reinstall.load_request()["stage"] == reinstall.STAGE_SHARED


MANIFEST = {"linux": {"obs": {"url": "https://x/linux-obs-install-32.2.2.tar.gz", "sha256": "s"}}}


def test_the_plan_puts_back_obs_and_the_packages(monkeypatch):
    monkeypatch.setattr(components, "installed_obs_version", lambda system: "30.0.0")
    monkeypatch.setattr(components, "installed_package_sets", lambda system: ["tray"])
    plan = reinstall.component_plan(MANIFEST, "Linux")
    assert plan == {"obs": ("30.0.0", "32.2.2"), "packages": ["tray"]}
    assert reinstall.describe_plan(plan) == ["OBS 32.2.2", "ClipEm's Python packages"]


def test_the_plan_copes_with_an_obs_that_isnt_there(monkeypatch):
    monkeypatch.setattr(components, "installed_obs_version", lambda system: None)
    monkeypatch.setattr(components, "installed_package_sets", lambda system: [])
    assert reinstall.component_plan(MANIFEST, "Linux") == {"obs": ("none", "32.2.2")}


def test_no_manifest_entry_and_no_packages_is_no_plan(monkeypatch):
    monkeypatch.setattr(components, "installed_package_sets", lambda system: [])
    assert reinstall.component_plan({}, "Linux") == {}
    assert reinstall.describe_plan({}) == []


def test_a_platform_the_manifest_doesnt_manage_has_no_plan(monkeypatch):
    monkeypatch.setattr(components, "_system", lambda: None)
    assert reinstall.component_plan(MANIFEST) == {}


def test_every_part_of_the_plan_is_reinstalled_forced(monkeypatch):
    calls = []
    monkeypatch.setattr(components, "_system", lambda: "Linux")
    monkeypatch.setattr(components, "install_obs", lambda manifest, system, log: calls.append(("obs", system)))
    monkeypatch.setattr(components, "install_packages",
                        lambda system, packages, log, force: calls.append(("packages", packages, force)))
    reinstall.reinstall_components(MANIFEST, {"obs": ("1", "2"), "packages": ["tray"]}, print)
    assert calls == [("obs", "Linux"), ("packages", ["tray"], True)]
    calls.clear()
    reinstall.reinstall_components(MANIFEST, {}, print)
    assert calls == []


class Restarted(BaseException):
    """What the faked _restart_self raises: a real one never returns."""


@pytest.fixture
def launch(request_file, monkeypatch):
    """run._reinstall_if_requested() with nothing really reinstalled. Returns
    what happened, in `calls`."""
    calls = []
    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.setattr(run, "_run_with_notice", lambda _text, work: work())
    monkeypatch.setattr(run, "_show_error", lambda message: calls.append(("error", message)))
    monkeypatch.setattr(run.updater, "fetch_manifest", lambda: {"version": "1.2.3"})

    def restart_self(args):
        calls.append(("restart_self", args))
        raise Restarted

    monkeypatch.setattr(run, "_restart_self", restart_self)
    monkeypatch.setattr(run, "_start_setup", lambda: calls.append("setup"))
    monkeypatch.setattr(reinstall, "component_plan", lambda manifest: {"obs": ("1", "2")})
    monkeypatch.setattr(reinstall, "reinstall_components", lambda manifest, plan, log: calls.append(("components", plan)))
    monkeypatch.setattr(reinstall, "reinstall_shared", lambda request, manifest: calls.append("shared") or (True, "ok"))
    return calls


def _stage(request_file, stage, keep_obs=True):
    request_file.write_text(json.dumps({"keep_obs_settings": keep_obs, "keep_clipem_settings": True, "stage": stage}))


def test_no_request_does_nothing(launch):
    run._reinstall_if_requested()
    assert launch == []


def test_the_first_stage_reinstalls_shared_then_restarts_keeping_the_request(launch, request_file):
    _stage(request_file, reinstall.STAGE_SHARED)
    with pytest.raises(Restarted):
        run._reinstall_if_requested()
    assert launch == ["shared", ("restart_self", [])]
    assert request_file.exists()  # the new run.py still has the components to do


def test_being_offline_changes_nothing_and_isnt_asked_again(launch, request_file, monkeypatch):
    def offline():
        raise OSError("no network")

    monkeypatch.setattr(run.updater, "fetch_manifest", offline)
    _stage(request_file, reinstall.STAGE_SHARED)
    run._reinstall_if_requested()
    assert len(launch) == 1 and "Nothing was changed" in launch[0][1]
    assert not request_file.exists()


def test_a_failed_shared_is_reported_once(launch, request_file, monkeypatch):
    monkeypatch.setattr(reinstall, "reinstall_shared", lambda request, manifest: (False, "no space"))
    _stage(request_file, reinstall.STAGE_SHARED)
    run._reinstall_if_requested()
    assert launch == [("error", "no space")] and not request_file.exists()


def test_the_second_stage_reinstalls_the_components(launch, request_file):
    _stage(request_file, reinstall.STAGE_COMPONENTS)
    run._reinstall_if_requested()
    assert launch == [("components", {"obs": ("1", "2")})] and not request_file.exists()


def test_a_failure_in_the_second_stage_is_reported_once_and_setup_isnt_opened(launch, request_file, monkeypatch):
    def fail(manifest, plan, log):
        raise OSError("disk full")

    monkeypatch.setattr(reinstall, "reinstall_components", fail)
    _stage(request_file, reinstall.STAGE_COMPONENTS, keep_obs=False)
    run._reinstall_if_requested()
    assert len(launch) == 1 and "disk full" in launch[0][1] and not request_file.exists()


def test_without_the_obs_settings_setup_opens_after_the_reinstall(launch, request_file):
    _stage(request_file, reinstall.STAGE_COMPONENTS, keep_obs=False)
    run._reinstall_if_requested()
    assert launch == [("components", {"obs": ("1", "2")}), "setup"]


def test_nothing_to_reinstall_still_finishes(launch, request_file, monkeypatch):
    monkeypatch.setattr(reinstall, "component_plan", lambda manifest: {})
    _stage(request_file, reinstall.STAGE_COMPONENTS)
    run._reinstall_if_requested()
    assert launch == [] and not request_file.exists()


def _popen(monkeypatch, platform):
    started = []
    monkeypatch.setattr(run.sys, "platform", platform)
    monkeypatch.setattr(run, "_DETACHED_FLAGS", 520, raising=False)
    monkeypatch.setattr(run.subprocess, "Popen", lambda args, **kwargs: started.append((args, kwargs)))
    return started


@pytest.mark.parametrize("platform, detail", [("linux", {"start_new_session": True}), ("win32", {"creationflags": 520})])
def test_setup_takes_over_and_this_launch_ends(monkeypatch, platform, detail):
    started = _popen(monkeypatch, platform)
    with pytest.raises(SystemExit) as exc:
        run._start_setup()
    assert exc.value.code == 0
    args, kwargs = started[0]
    assert args[1].endswith("setup.py") and kwargs == {"cwd": run.ROOT_DIR, **detail}
