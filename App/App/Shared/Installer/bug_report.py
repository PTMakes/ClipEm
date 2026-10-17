"""
Bug report zip, made from the tray's Settings: ClipEm's own logs, OBS's
most recent logs and crash reports, and a summary of the install (versions,
platform, settings) - everything usually needed to look into a problem,
in one file the user can attach to an issue or a message.

send_bug_report() uploads it instead, to ClipEm's bug report relay
(BugReportWorker/), which files a GitHub issue with the user's description
and keeps the zip private - no GitHub account needed.

The obs-websocket password is the one secret anywhere in these, so its
config file is never included and the password is masked wherever it
appears in the collected text.
"""

import datetime
import json
import os
import platform
import secrets
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import obs_config
from clipem_common import APP_DIR, LOG_DIR, ROOT_DIR, load_json, load_run_config, read_version

# OBS writes a new log (and crash report) per run - the latest few cover the problem.
OBS_LOG_COUNT = 10
OBS_CRASH_COUNT = 5
OBS_CRASH_DIR = os.path.join(obs_config.OBS_CONFIG_DIR, "crashes")
INSTALLED_VERSIONS_PATH = os.path.join(APP_DIR, "installed_versions.json")
REDACTED = "<redacted>"

# CLIPEM_BUG_REPORT_URL points at a local `wrangler dev` for testing.
BUG_REPORT_URL = os.environ.get("CLIPEM_BUG_REPORT_URL", "https://bugs.clipem.app/report")
# Matches the relay's limit.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
SEND_TIMEOUT_SECONDS = 60


class BugReportError(Exception):
    """A send that failed, with a message fit to show the user."""


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


def _check_url(url):
    # Plain http only for a local test relay - the report holds logs.
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")):
        return url
    raise BugReportError(f"Refusing non-HTTPS URL: {url}")


def _multipart(fields, file_field, file_name, file_data):
    """(body, Content-Type) of a multipart/form-data upload."""
    boundary = secrets.token_hex(16)
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        + value.encode("utf-8") + b"\r\n"
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        "Content-Type: application/zip\r\n\r\n".encode() + file_data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _post(url, body, content_type):
    # Some CDNs answer urllib's default User-Agent with 403, as updater.py notes.
    request = urllib.request.Request(
        _check_url(url), data=body, method="POST",
        headers={"User-Agent": "ClipEm", "Content-Type": content_type},
    )
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as res:
        return json.loads(res.read(64 * 1024))


def _http_error_message(exc):
    try:
        detail = json.loads(exc.read(64 * 1024))
    except Exception:
        detail = {}
    if exc.code == 429:
        return "Too many reports were sent from this network recently - try again in a minute."
    if exc.code == 413:
        return "The report is too large to send. Save it to a file instead."
    if detail.get("id"):
        return (f"The logs were uploaded (report ID {detail['id']}), but the GitHub issue couldn't "
                "be created. Mention that ID if you report the problem another way.")
    return f"The report server answered {exc.code}: {detail.get('error') or exc.reason}"


def send_bug_report(description, url=None):
    """Makes the report zip and uploads it with the user's description.
    Returns (report id, GitHub issue URL). Raises BugReportError."""
    description = description.strip()
    if not description:
        raise BugReportError("Describe what happened first.")
    fd, zip_path = tempfile.mkstemp(prefix="ClipEm-bug-report-", suffix=".zip")
    os.close(fd)
    try:
        create_bug_report(zip_path)
        size = os.path.getsize(zip_path)
        if size > MAX_UPLOAD_BYTES:
            raise BugReportError(f"The logs are too large to send ({size / 1e6:.0f} MB). "
                                 "Save the report to a file instead.")
        with open(zip_path, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    fields = {
        "description": description,
        "version": read_version(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
    }
    body, content_type = _multipart(fields, "logs", default_file_name(), data)
    try:
        result = _post(url or BUG_REPORT_URL, body, content_type)
        return result["id"], result.get("issue_url")
    except urllib.error.HTTPError as exc:
        raise BugReportError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise BugReportError(f"Couldn't reach the bug report server: {exc.reason}") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BugReportError(f"Couldn't send the bug report: {exc}") from exc
