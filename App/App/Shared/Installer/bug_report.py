"""
Bug report zip, made from the tray's Settings: ClipEm's own logs, OBS's
most recent logs and crash reports, and a summary of the install (versions,
platform, settings) - everything usually needed to look into a problem,
in one file the user can attach to an issue or a message.

The obs-websocket password is the one secret anywhere in these, so its
config file is never included and the password is masked wherever it
appears in the collected text.
"""

import datetime
import json
import os
import platform
import shutil
import sys
import zipfile

import obs_config
from clipem_common import APP_DIR, LOG_DIR, ROOT_DIR, load_json, load_run_config, read_version

# OBS writes a new log (and crash report) per run - the latest few cover the problem.
OBS_LOG_COUNT = 10
OBS_CRASH_COUNT = 5
OBS_CRASH_DIR = os.path.join(obs_config.OBS_CONFIG_DIR, "crashes")
INSTALLED_VERSIONS_PATH = os.path.join(APP_DIR, "installed_versions.json")
REDACTED = "<redacted>"


def default_file_name(now=None):
    now = now or datetime.datetime.now()
    return f"ClipEm-bug-report-{now:%Y%m%d-%H%M%S}.zip"


def default_save_dir():
    """The user's Downloads folder, or their home folder without one."""
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    return downloads if os.path.isdir(downloads) else os.path.expanduser("~")


def _newest_files(folder, count):
    try:
        paths = [os.path.join(folder, name) for name in os.listdir(folder)]
    except OSError:
        return []
    files = [path for path in paths if os.path.isfile(path)]
    return sorted(files, key=os.path.getmtime, reverse=True)[:count]


def _secrets():
    try:
        password = obs_config.load_obs_websocket_settings()["password"]
    except Exception:
        return []
    return [password] if password else []


def _redact(text, secrets):
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def system_info():
    """A readable summary of this install, for the top of the report."""
    lines = [
        f"ClipEm version: {read_version()}",
        f"Report made: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"Platform: {platform.platform()} ({platform.machine()})",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Install folder: {ROOT_DIR}",
    ]
    try:
        usage = shutil.disk_usage(ROOT_DIR)
        lines.append(f"Free disk space (install): {usage.free / 1e9:.1f} GB of {usage.total / 1e9:.1f} GB")
    except OSError:
        pass
    try:
        clip_folder = obs_config.load_clip_folder()
    except Exception as exc:
        lines.append(f"Clip folder: unknown ({exc})")
    else:
        try:
            usage = shutil.disk_usage(clip_folder)
            lines.append(f"Clip folder: {clip_folder} ({usage.free / 1e9:.1f} GB free)")
        except OSError as exc:
            lines.append(f"Clip folder: {clip_folder} (not accessible: {exc.strerror})")
    try:
        import obs_process

        lines.append(f"OBS running: {obs_process.obs_running()}")
    except Exception as exc:
        lines.append(f"OBS running: unknown ({exc})")
    try:
        import autostart

        entries = autostart.find_autostart_entries()
        lines.append("Autostart entries: " + (
            ", ".join(f"{autostart.describe_autostart_entry(e)}{'' if e['this_install'] else ' [other install]'}"
                      for e in entries) or "none"
        ))
    except Exception as exc:
        lines.append(f"Autostart entries: unknown ({exc})")
    sections = [
        ("Installed components", load_json(INSTALLED_VERSIONS_PATH)),
        ("run_config.json", load_run_config()),
    ]
    try:
        import updater

        sections.append(("update_config.json", updater.load_update_config()))
    except Exception:
        pass
    for title, data in sections:
        lines += ["", f"--- {title} ---", json.dumps(data, indent=2, sort_keys=True, default=str)]
    return "\n".join(lines) + "\n"


def collected_files():
    """[(path in the zip, file on disk)] of every log the report includes."""
    files = []
    for path in sorted(_newest_files(LOG_DIR, 1000)):
        files.append((f"clipem-logs/{os.path.basename(path)}", path))
    for path in _newest_files(obs_config.OBS_LOG_DIR, OBS_LOG_COUNT):
        files.append((f"obs-logs/{os.path.basename(path)}", path))
    for path in _newest_files(OBS_CRASH_DIR, OBS_CRASH_COUNT):
        files.append((f"obs-crashes/{os.path.basename(path)}", path))
    return files


def create_bug_report(dest_path):
    """Writes the report zip to dest_path. Returns how many log files it
    holds. A partly written zip is removed if anything fails."""
    secrets = _secrets()
    files = collected_files()
    try:
        with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("system-info.txt", _redact(system_info(), secrets))
            for arcname, path in files:
                try:
                    zf.writestr(arcname, _redact(_read_text(path), secrets))
                except OSError as exc:
                    zf.writestr(arcname + ".unreadable.txt", f"Couldn't read {path}: {exc}\n")
    except BaseException:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise
    return len(files)
