"""
Clean reinstall, chosen in the tray's Settings: downloads this update
channel's ClipEm (App/Shared), OBS and the tray's packages afresh and puts
them in place of the installed ones, optionally keeping the OBS settings
(OBS-Config) and the ClipEm settings (run_config.json, update_config.json,
the clip sound).

Like an update, it can only replace OBS and the packages while nothing
ClipEm runs is open, so Settings only records the request (REQUEST_PATH)
and restarts ClipEm; run.py carries it out at launch, before OBS starts:
App/Shared first, then - re-run, so the freshly installed code does the
rest - OBS and the packages. The bundled Python itself is left as it is
(it's the interpreter doing the reinstall). Without the OBS settings, run.py
then opens Setup instead of OBS, since the capture display, audio devices
and clip folder all need choosing again.
"""

import os

import components
import updater
from clipem_common import APP_DIR, load_json, write_json_atomic

# In App/, which an update or reinstall of App/Shared never replaces.
REQUEST_PATH = os.path.join(APP_DIR, "reinstall_request.json")
STAGE_SHARED, STAGE_COMPONENTS = "shared", "components"


def request_reinstall(keep_obs_settings, keep_clipem_settings):
    write_json_atomic(REQUEST_PATH, {
        "keep_obs_settings": bool(keep_obs_settings),
        "keep_clipem_settings": bool(keep_clipem_settings),
        "stage": STAGE_SHARED,
    })


def load_request():
    """The pending request, or None."""
    request = load_json(REQUEST_PATH)
    if not isinstance(request, dict) or request.get("stage") not in (STAGE_SHARED, STAGE_COMPONENTS):
        return None
    return request


def clear_request():
    try:
        os.remove(REQUEST_PATH)
    except FileNotFoundError:
        pass


def preserved_paths(request):
    """What apply_update keeps of App/Shared for this request."""
    keep = set(updater.ALWAYS_PRESERVED_PATHS)
    if request.get("keep_obs_settings"):
        keep |= updater.OBS_SETTINGS_PATHS
    if request.get("keep_clipem_settings"):
        keep |= updater.CLIPEM_SETTINGS_PATHS
    return keep


def reinstall_shared(request, manifest):
    """Replaces App/Shared with the manifest's copy. On success the request
    moves on to its components stage. Returns (ok, message)."""
    ok, message = updater.apply_update(manifest, preserved=preserved_paths(request))
    if ok:
        write_json_atomic(REQUEST_PATH, dict(request, stage=STAGE_COMPONENTS))
        message = f"Reinstalled ClipEm {manifest['version']}."
    return ok, message


def component_plan(manifest, system=None):
    """A components.install() plan that puts back OBS and every installed
    package set whatever their versions - empty where the manifest doesn't
    manage them (macOS)."""
    system = system or components._system()
    if system is None:
        return {}
    plan = {}
    entry = manifest.get(components._platform_key(system), {}).get("obs")
    if entry:
        plan["obs"] = (components.installed_obs_version(system) or "none", updater.artifact_version(entry))
    packages = components.installed_package_sets(system)
    if packages:
        plan["packages"] = packages
    return plan


def describe_plan(plan):
    lines = []
    if "obs" in plan:
        lines.append(f"OBS {plan['obs'][1]}")
    if "packages" in plan:
        lines.append("ClipEm's Python packages")
    return lines


def reinstall_components(manifest, plan, log):
    """Reinstalls what component_plan() listed. Raises on failure."""
    system = components._system()
    if "obs" in plan:
        components.install_obs(manifest, system, log)
    if "packages" in plan:
        components.install_packages(system, plan["packages"], log, force=True)
