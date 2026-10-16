"""apply_update() end to end: signed manifest -> download -> sha256 ->
version check -> swap, against a local "server" instead of clipem.app."""

import hashlib
import io
import json
import os
import zipfile

import pytest
import updater

OLD_VERSION = "1.0.0"
NEW_VERSION = "1.0.1"
SHARED_URL = "https://downloads.example/releases/v1.0.1/clipem-shared.zip"


def _write(root, rel, data):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def _read(root, rel):
    with open(os.path.join(root, *rel.split("/")), "r") as f:
        return f.read()


def _shared_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, data in files.items():
            zf.writestr(f"Shared/{rel}", data)
    return buf.getvalue()


@pytest.fixture
def install(tmp_path, monkeypatch, signing_key):
    """A live App/Shared plus a fake network serving a signed manifest and
    the new shared zip. Returns (shared_dir, publish) - publish(zip_bytes,
    version=..., sha256=...) sets what the "server" returns."""
    shared = str(tmp_path / "Shared")
    live = {
        "VERSION": OLD_VERSION,
        "Installer/run.py": "old run.py",
        "Installer/launch_obs.command": "#!/bin/bash\nuser's launcher",
        "Scripts/replay_buffer_save_sound.wav": "user's custom sound",
        "Scripts/clip_reexport.py": "old script",
        "tray-widget/tray_widget.py": "old tray",
        "tray-widget/.tray.lock": "4242",
        "OBS-Config/basic.ini": "user's OBS settings",
        "run_config.json": "user's run config",
        "pending_obs_settings.json": "user's queued settings",
    }
    for rel, data in live.items():
        _write(shared, rel, data)
    monkeypatch.setattr(updater, "SHARED_DIR", shared)
    monkeypatch.setattr(updater, "load_public_key", lambda: signing_key.public_raw)
    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(tmp_path / "update_config.json"))

    served = {}

    def publish(zip_bytes, version=NEW_VERSION, sha256=None):
        entry = {"url": SHARED_URL, "sha256": sha256 or hashlib.sha256(zip_bytes).hexdigest()}
        manifest = json.dumps({"version": version, "windows": {"shared": entry}, "linux": {"shared": entry}}).encode()
        served[updater.manifest_url()] = manifest
        served[updater.manifest_url() + ".sig"] = signing_key.sign(manifest).encode()
        served[SHARED_URL] = zip_bytes

    def fake_open_url(url, timeout):
        updater.require_https(url)
        return io.BytesIO(served[url])

    monkeypatch.setattr(updater, "_open_url", fake_open_url)
    return shared, publish


NEW_FILES = {
    "VERSION": NEW_VERSION,
    "Installer/run.py": "new run.py",
    "Scripts/replay_buffer_save_sound.wav": "shipped sound",
    "Scripts/clip_reexport.py": "new script",
    "tray-widget/tray_widget.py": "new tray",
    "OBS-Config/basic.ini": "shipped OBS settings",
    "run_config.json": "shipped run config",
}


def test_update_replaces_code_and_keeps_user_files(install):
    shared, publish = install
    publish(_shared_zip(NEW_FILES))

    ok, message = updater.apply_update()

    assert ok, message
    assert _read(shared, "VERSION") == NEW_VERSION
    assert _read(shared, "Installer/run.py") == "new run.py"
    assert _read(shared, "Scripts/clip_reexport.py") == "new script"
    assert _read(shared, "tray-widget/tray_widget.py") == "new tray"
    # Everything in PRESERVED_RELATIVE_PATHS survives, nested ones included.
    assert _read(shared, "Scripts/replay_buffer_save_sound.wav") == "user's custom sound"
    assert _read(shared, "tray-widget/.tray.lock") == "4242"
    assert _read(shared, "Installer/launch_obs.command").endswith("user's launcher")
    assert _read(shared, "OBS-Config/basic.ini") == "user's OBS settings"
    assert _read(shared, "run_config.json") == "user's run config"
    assert _read(shared, "pending_obs_settings.json") == "user's queued settings"
    assert not os.path.exists(os.path.join(shared, updater.STAGING_DIR_NAME))
    assert not os.path.exists(os.path.join(shared, updater.BACKUP_DIR_NAME))


def test_reinstall_without_settings_resets_them(install):
    import reinstall

    shared, publish = install
    publish(_shared_zip(dict(NEW_FILES, VERSION=OLD_VERSION)), version=OLD_VERSION)
    request = {"keep_obs_settings": False, "keep_clipem_settings": False}

    ok, message = updater.apply_update(preserved=reinstall.preserved_paths(request))

    assert ok, message
    assert _read(shared, "Installer/run.py") == "new run.py"
    assert _read(shared, "OBS-Config/basic.ini") == "shipped OBS settings"
    assert _read(shared, "run_config.json") == "shipped run config"
    assert _read(shared, "Scripts/replay_buffer_save_sound.wav") == "shipped sound"
    assert not os.path.exists(os.path.join(shared, "pending_obs_settings.json"))
    # Locks and the macOS launcher are kept regardless.
    assert _read(shared, "tray-widget/.tray.lock") == "4242"
    assert _read(shared, "Installer/launch_obs.command").endswith("user's launcher")


def test_reinstall_keeping_obs_settings_only(install):
    import reinstall

    shared, publish = install
    publish(_shared_zip(NEW_FILES))
    request = {"keep_obs_settings": True, "keep_clipem_settings": False}

    assert updater.apply_update(preserved=reinstall.preserved_paths(request))[0]
    assert _read(shared, "OBS-Config/basic.ini") == "user's OBS settings"
    assert _read(shared, "pending_obs_settings.json") == "user's queued settings"
    assert _read(shared, "run_config.json") == "shipped run config"


def test_shipped_file_is_used_when_there_is_no_user_copy(install):
    shared, publish = install
    os.remove(os.path.join(shared, "Scripts", "replay_buffer_save_sound.wav"))
    publish(_shared_zip(NEW_FILES))
    assert updater.apply_update()[0]
    assert _read(shared, "Scripts/replay_buffer_save_sound.wav") == "shipped sound"


def test_version_mismatch_aborts_without_touching_anything(install):
    shared, publish = install
    publish(_shared_zip(NEW_FILES), version="1.0.2")

    ok, message = updater.apply_update()

    assert not ok
    assert "doesn't match" in message
    assert _read(shared, "VERSION") == OLD_VERSION
    assert _read(shared, "Installer/run.py") == "old run.py"
    assert not os.path.exists(os.path.join(shared, updater.STAGING_DIR_NAME))


def test_checksum_mismatch_aborts(install):
    shared, publish = install
    publish(_shared_zip(NEW_FILES), sha256="0" * 64)

    ok, message = updater.apply_update()

    assert not ok
    assert "Checksum mismatch" in message
    assert _read(shared, "Installer/run.py") == "old run.py"


def test_tampered_manifest_is_refused(install, monkeypatch):
    shared, publish = install
    publish(_shared_zip(NEW_FILES))
    real_open = updater._open_url

    def tampered(url, timeout):
        res = real_open(url, timeout)
        if url == updater.manifest_url():
            return io.BytesIO(res.read().replace(b"1.0.1", b"9.9.9"))
        return res

    monkeypatch.setattr(updater, "_open_url", tampered)
    ok, message = updater.apply_update()
    assert not ok
    assert "INVALID" in message
    assert _read(shared, "VERSION") == OLD_VERSION


def test_leftover_staging_from_a_crashed_update_is_cleared(install):
    shared, publish = install
    _write(shared, f"{updater.STAGING_DIR_NAME}/junk", "from a crash")
    publish(_shared_zip(NEW_FILES))
    assert updater.apply_update()[0]
    assert not os.path.exists(os.path.join(shared, updater.STAGING_DIR_NAME))
