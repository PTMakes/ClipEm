import base64
import io
import json
import os
import zipfile

import pytest
import updater

# RFC 8032 TEST 2 key pair is reused as a stand-in release key: we only
# need *a* valid (key, message, signature) triple, so sign-by-hand isn't
# needed - the manifest bytes are the RFC message.
PUBLIC_KEY = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
SIGNATURE_OF_0x72 = bytes.fromhex(
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)


def test_parse_version_and_is_newer():
    assert updater._parse_version("v1.2.10")[:3] == (1, 2, 10)
    assert updater._parse_version("1.x.3")[:3] == (1, 0, 3)
    assert updater._parse_version("1.2") == updater._parse_version("1.2.0")
    assert updater.is_newer("1.0.10", "1.0.9")
    assert not updater.is_newer("1.0.9", "1.0.10")
    assert not updater.is_newer("1.0.9", "1.0.9")
    assert not updater.is_newer(None, "1.0.0")


def test_require_https():
    assert updater.require_https("https://clipem.app/x") == "https://clipem.app/x"
    for url in ("http://clipem.app/x", "file:///etc/passwd", "ftp://x/y"):
        with pytest.raises(updater.UpdateError):
            updater.require_https(url)


def test_signed_manifest_rejects_bad_signature():
    sig_text = base64.b64encode(SIGNATURE_OF_0x72).decode()
    with pytest.raises(updater.UpdateError, match="INVALID"):
        updater.parse_signed_manifest(b'{"version": "9.9.9"}', sig_text, PUBLIC_KEY)
    with pytest.raises(updater.UpdateError, match="Malformed"):
        updater.parse_signed_manifest(b"{}", "not base64!!", PUBLIC_KEY)


def test_signed_manifest_accepts_valid_signature(monkeypatch):
    # The RFC message 0x72 isn't JSON, so check the signature step alone by
    # verifying json parsing is what fails after it.
    sig_text = base64.b64encode(SIGNATURE_OF_0x72).decode()
    with pytest.raises(json.JSONDecodeError):
        updater.parse_signed_manifest(b"\x72", sig_text, PUBLIC_KEY)


def test_missing_public_key_fails_closed(tmp_path, monkeypatch):
    empty = tmp_path / "key.txt"
    empty.write_text("")
    monkeypatch.setattr(updater, "SIGNING_PUBLIC_KEY_PATH", str(empty))
    with pytest.raises(updater.UpdateError):
        updater.load_public_key()


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_validate_zip_accepts_shared_layout():
    updater.validate_zip(_zip({"Shared/VERSION": "1", "Shared/Installer/run.py": ""}))


@pytest.mark.parametrize("name", ["../evil", "Shared/../../evil", "/abs", "Other/file", "C:/x"])
def test_validate_zip_rejects_unsafe_paths(name):
    with pytest.raises(updater.UpdateError):
        updater.validate_zip(_zip({name: "x"}))


def _tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


def _read(root, rel):
    with open(os.path.join(root, rel)) as f:
        return f.read()


def test_swap_in_replaces_and_preserves(tmp_path):
    live, staged, backup = (str(tmp_path / n) for n in ("live", "staged", "backup"))
    _tree(live, {"Installer/run.py": "old", "VERSION": "1", "run_config.json": "mine", "OBS-Config/a": "mine"})
    _tree(staged, {"Installer/run.py": "new", "VERSION": "2", "run_config.json": "shipped", "OBS-Config/a": "shipped"})
    updater.swap_in(staged, live, backup)
    assert _read(live, "Installer/run.py") == "new"
    assert _read(live, "VERSION") == "2"
    assert _read(live, "run_config.json") == "mine"
    assert _read(live, "OBS-Config/a") == "mine"


def test_swap_in_rolls_back_on_failure(tmp_path, monkeypatch):
    live, staged, backup = (str(tmp_path / n) for n in ("live", "staged", "backup"))
    _tree(live, {"Installer/run.py": "old", "VERSION": "1"})
    _tree(staged, {"Installer/run.py": "new", "VERSION": "2"})
    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append(src)
        # Fail when moving the second staged entry into place.
        if src == os.path.join(staged, "VERSION"):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(updater.os, "replace", flaky_replace)
    with pytest.raises(OSError):
        updater.swap_in(staged, live, backup)
    monkeypatch.setattr(updater.os, "replace", real_replace)
    assert _read(live, "Installer/run.py") == "old"
    assert _read(live, "VERSION") == "1"


def test_update_config_migrates_the_old_auto_update_setting(tmp_path, monkeypatch):
    path = tmp_path / "update_config.json"
    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(path))
    assert updater.load_update_config()["check_for_updates"] is True  # no file yet
    path.write_text('{"auto_update": false}')
    config = updater.load_update_config()
    assert config["check_for_updates"] is False and "auto_update" not in config
    updater.change_update_config(skipped_version="1.2.3")
    assert updater.load_update_config() == {
        "check_for_updates": False, "install_on_next_launch": False, "skipped_version": "1.2.3",
        "source_repo": "PTMakes/ClipEm", "update_channel": "releases",
    }


def test_dev_versions_sort_between_releases():
    assert updater.is_newer("1.3.6-dev.202610142130", "1.3.5")
    assert updater.is_newer("1.3.6", "1.3.6-dev.202610142130")
    assert updater.is_newer("1.3.6-dev.202610150900", "1.3.6-dev.202610142130")
    assert not updater.is_newer("1.3.6-dev.202610142130", "1.3.6")
    assert updater.is_dev_version("1.3.6-dev.1") and not updater.is_dev_version("1.3.6")


def test_manifest_url_follows_repo_and_channel():
    assert updater.manifest_url({"source_repo": "PTMakes/ClipEm", "update_channel": "releases"}) == (
        "https://github.com/PTMakes/ClipEm/releases/latest/download/install-manifest.json")
    assert updater.manifest_url({"source_repo": "me/fork", "update_channel": "latest"}) == (
        "https://github.com/me/fork/releases/download/dev/install-manifest.json")
    for bad in ("nope", "a/b/c", "../x", "a/b?x=1", "https://github.com/a/b"):
        assert not updater.is_valid_repo(bad)
        with pytest.raises(updater.UpdateError):
            updater.manifest_url({"source_repo": bad})


def test_redirects_must_stay_on_https():
    handler = updater._HttpsOnlyRedirectHandler()
    with pytest.raises(updater.UpdateError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://example.com/x")


@pytest.mark.parametrize("channel, local, remote, expected", [
    ("releases", "1.3.6-dev.202610142130", "1.3.5", True),   # leaving the dev channel
    ("releases", "1.3.5", "1.3.5", False),
    ("latest", "1.3.6-dev.202610142130", "1.3.5", False),    # no stale-dev downgrade
    ("latest", "1.3.5", "1.3.6-dev.202610142130", True),
])
def test_check_for_update_channel_switch(tmp_path, monkeypatch, channel, local, remote, expected):
    import components

    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(tmp_path / "update_config.json"))
    updater.change_update_config(update_channel=channel)
    monkeypatch.setattr(updater, "get_local_version", lambda: local)
    monkeypatch.setattr(updater, "fetch_manifest", lambda config=None: {"version": remote})
    monkeypatch.setattr(components, "check", lambda manifest: {})
    result = updater.check_for_update()
    assert result["shared_update"] is expected
    assert result["update_available"] is expected


def test_swap_in_resets_entries_not_preserved(tmp_path):
    live, staged, backup = (str(tmp_path / n) for n in ("live", "staged", "backup"))
    _tree(live, {"VERSION": "1", "run_config.json": "mine", "app_tracks.json": "mine", "OBS-Config/a": "mine"})
    _tree(staged, {"VERSION": "2", "run_config.json": "shipped", "OBS-Config/a": "shipped"})
    keep = updater.PRESERVED_RELATIVE_PATHS - {"run_config.json", "app_tracks.json"}
    updater.swap_in(staged, live, backup, preserved=keep, reset={"run_config.json", "app_tracks.json"})
    assert _read(live, "run_config.json") == "shipped"
    assert not os.path.exists(os.path.join(live, "app_tracks.json"))  # nothing shipped - removed
    assert _read(live, "OBS-Config/a") == "mine"
