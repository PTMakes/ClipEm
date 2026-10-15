"""
Run before starting OBS on any platform. It is what makes this whole
distribution "portable" and lets Windows/macOS/Linux share one set of
configs and scripts:

  1. Links <platform>/config/obs-studio -> ../../Shared/OBS-Config (NTFS
     junction on Windows, symlink on macOS/Linux). The "obs-studio" leaf
     name is OBS's own hardcoded portable-mode convention (it always
     resolves its config as <config_dir>/obs-studio, the same "obs-studio"
     folder name normally seen under ~/.config or %APPDATA%) - not
     something this project chose, so it has to stay literally named that
     on the platform side even though the shared source directory itself
     is named OBS-Config.
  2. Links <platform>/Scripts -> ../Shared/Scripts
  3. Creates <root>/Clips if it doesn't exist yet, and fills in the
     {{CLIPS_DIR}} placeholder in the shared profile with its absolute
     path (only on the very first run - if a user has since picked a
     different output folder in OBS's own Settings UI, this leaves it
     alone).
  4. On Windows, points OBS's [Python] Path64bit (user.ini) at this
     distribution's own bundled Python - the interpreter OBS runs the
     scripts below with. OBS uses the value as-is (relative paths resolve
     against bin/64bit), so an absolute path to wherever python.exe
     actually is here can't go stale when the folder moves.
  5. Rewrites the "path" entries OBS stored for the two bundled scripts
     in the DefaultPTM scene collection to an absolute path resolved
     against this distribution's actual location. OBS persists whatever
     relative depth happened to be correct on the platform that last
     saved the file (Windows' bin/64bit is 2 levels below the portable
     root; macOS/Linux app layouts are nested differently), so a single
     shared JSON file can't use one relative string that resolves
     correctly on every platform. Rewriting to an absolute path each
     launch sidesteps that.

Every platform's launcher calls this with its own platform folder name
(Windows, macOS, or Linux) before starting OBS. Nothing here touches
anything outside this distribution's own folder.
"""

import json
import os
import re
import subprocess
import sys

import obs_config
from clipem_common import write_json_atomic, write_text_atomic

SCRIPT_NAMES = ("replay_buffer_save_sound.py", "stuck_output_check.py", "clip_reexport.py")

# Scripts added after the scene collection first shipped: registered in it on
# launch if missing, since OBS-Config is never overwritten by updates.
# Settings start out disabled - Setup's wizard turns them on.
ADDED_SCRIPTS = {
    "clip_reexport.py": {
        "split_tracks_enabled": False, "reexport_enabled": False, "target_mb": 10.0, "crop_seconds": 30,
    },
}


def _is_junction(path):
    # os.path.isjunction only exists on Python 3.12+ (what Windows ships);
    # the fallback covers Windows installs made with an older bundled Python.
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(path)
    if sys.platform == "win32" and os.path.isdir(path):
        import ctypes

        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        return attrs != -1 and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _link_dir(link_path, target_path):
    """Make link_path point at target_path (junction on Windows, symlink elsewhere)."""
    target_path = os.path.abspath(target_path)

    if os.path.islink(link_path) or _is_junction(link_path):
        current = os.path.realpath(link_path)
        if current == os.path.realpath(target_path):
            return
        os.unlink(link_path) if os.path.islink(link_path) else os.rmdir(link_path)
    elif os.path.isdir(link_path):
        # A real directory already sitting where the link should go (e.g. a
        # fresh unzip of an official OBS release). Leave it alone rather
        # than destroying whatever's in it.
        print(f"[portable-launch] {link_path} already exists as a real directory; "
              f"not linking it to {target_path}. Move/remove it manually if you "
              f"want the shared version.", file=sys.stderr)
        return

    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target_path],
            check=True,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,  # run.py may have no console (pythonw)
        )
    else:
        os.symlink(target_path, link_path, target_is_directory=True)


# Clips folder paths from the ClipEm developer's machine that shipped in
# releases up to 1.2.7 instead of the {{CLIPS_DIR}} placeholder. They don't
# exist anywhere else, so an install still carrying one (Setup's clips-folder
# step never overwrote it) gets the default folder as if it were the
# placeholder.
LEAKED_CLIPS_DIRS = ("/home/pt/Development/PTMClipEm/ClipEm/Clips",)


def _fill_clips_dir(shared_config_dir, clips_dir):
    ini_path = os.path.join(
        shared_config_dir, "basic", "profiles", "DefaultPTM", "basic.ini"
    )
    if not os.path.exists(ini_path):
        return

    with open(ini_path, "r", encoding="utf-8") as f:
        content = f.read()

    for leaked in LEAKED_CLIPS_DIRS:
        content = re.sub(rf"(?m)^(\w*FilePath)={re.escape(leaked)}[ \t]*$", r"\1={{CLIPS_DIR}}", content)
    if "{{CLIPS_DIR}}" not in content:
        return

    os.makedirs(clips_dir, exist_ok=True)
    content = content.replace("{{CLIPS_DIR}}", clips_dir.replace("\\", "\\\\"))
    write_text_atomic(ini_path, content)


def _find_python_root(python_dir):
    for dirpath, _dirnames, filenames in os.walk(python_dir):
        if "python.exe" in filenames:
            return dirpath
    return None


def _set_windows_python_path(shared_config_dir, platform_dir):
    user_ini = os.path.join(shared_config_dir, "user.ini")
    python_root = _find_python_root(os.path.join(platform_dir, "Python"))
    if not python_root or not os.path.exists(user_ini):
        return
    value = os.path.abspath(python_root).replace("\\", "/")
    if obs_config.read_ini_section("Python", user_ini).get("Path64bit") != value:
        obs_config.update_ini_section(user_ini, "Python", {"Path64bit": value})


def _fix_script_paths(shared_config_dir, shared_scripts_dir):
    scene_path = os.path.join(
        shared_config_dir, "basic", "scenes", "DefaultPTM.json"
    )
    if not os.path.exists(scene_path):
        return

    with open(scene_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data.setdefault("modules", {}).setdefault("scripts-tool", [])
    changed = False
    present = {os.path.basename(entry.get("path", "")) for entry in entries}
    for name, default_settings in ADDED_SCRIPTS.items():
        if name not in present:
            entries.append({"path": os.path.join(shared_scripts_dir, name), "settings": dict(default_settings)})
            changed = True
    for entry in entries:
        name = os.path.basename(entry.get("path", ""))
        if name in SCRIPT_NAMES:
            absolute = os.path.join(shared_scripts_dir, name)
            if entry["path"] != absolute:
                entry["path"] = absolute
                changed = True
            settings = entry.get("settings", {})
            if "audio_file" in settings:
                wav_name = os.path.basename(settings["audio_file"])
                absolute_wav = os.path.join(shared_scripts_dir, wav_name)
                if settings["audio_file"] != absolute_wav:
                    settings["audio_file"] = absolute_wav
                    changed = True

    if changed:
        write_json_atomic(scene_path, data)


def prepare(platform_dir, config_link_name="config"):
    """platform_dir: absolute path to the Windows/, macOS/, or Linux/ folder.

    config_link_name: name of the directory (inside platform_dir) that
    should contain the OBS config link. Windows/macOS use "config" directly
    beside the OBS binary/bundle, matching OBS's own portable-mode
    convention. Linux's launcher instead points $XDG_CONFIG_HOME at a
    folder here (see Linux/launch.sh for why - AppImages execute from a
    temporary FUSE mount, so "beside the executable" isn't stable there)
    and passes a different config_link_name for that layout. Either way,
    OBS itself appends "obs-studio" to whichever of these it resolves to -
    see the module docstring - so config_link_name is made a real
    directory here, with the actual link nested one level inside it.
    """
    root_dir = os.path.dirname(platform_dir)
    shared_dir = os.path.join(root_dir, "Shared")
    shared_config_dir = os.path.join(shared_dir, "OBS-Config")
    shared_scripts_dir = os.path.join(shared_dir, "Scripts")
    clips_dir = os.path.join(os.path.dirname(root_dir), "Clips")

    config_dir = os.path.join(platform_dir, config_link_name)
    if os.path.islink(config_dir) or _is_junction(config_dir):
        # Older layout: config_dir used to *be* the link straight to the
        # shared config. Clear it so it can become a real directory
        # holding the nested obs-studio link instead.
        os.unlink(config_dir) if os.path.islink(config_dir) else os.rmdir(config_dir)
    os.makedirs(config_dir, exist_ok=True)

    _link_dir(os.path.join(config_dir, "obs-studio"), shared_config_dir)
    _link_dir(os.path.join(platform_dir, "Scripts"), shared_scripts_dir)
    _fill_clips_dir(shared_config_dir, clips_dir)
    if os.path.basename(platform_dir) == "Windows":
        _set_windows_python_path(shared_config_dir, platform_dir)
    _fix_script_paths(shared_config_dir, shared_scripts_dir)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print("usage: portable_launch_helper.py <platform-dir> [config-link-name]", file=sys.stderr)
        sys.exit(1)
    link_name = sys.argv[2] if len(sys.argv) == 3 else "config"
    prepare(os.path.abspath(sys.argv[1]), link_name)
