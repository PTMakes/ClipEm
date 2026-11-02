import base64
import hashlib
import io
import json
import os
import urllib.request
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
        "last_version": None, "source_repo": "PTMakes/ClipEm", "update_channel": "releases",
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


def test_repo_from_url_accepts_github_links():
    for text in ("https://github.com/me/fork", "https://www.github.com/me/fork/", " https://github.com/me/fork.git ",
                 "me/fork"):
        assert updater.repo_from_url(text) == "me/fork"
    for bad in ("", "http://github.com/me/fork", "https://gitlab.com/me/fork", "https://github.com/me",
                "https://github.com/me/fork/releases", "https://github.com/../x", "a/b/c"):
        assert updater.repo_from_url(bad) is None
    assert updater.repo_url("me/fork") == "https://github.com/me/fork"


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


@pytest.mark.parametrize("local, manifest, expected", [
    ("1.3.5", {"version": "1.3.6", "release_notes": "New things."}, "New things."),
    ("1.3.5", {"version": "1.3.6"}, ""),                                # a release from before notes
    ("1.3.6", {"version": "1.3.6", "release_notes": "New things."}, ""),  # e.g. only OBS is out of date
])
def test_check_for_update_release_notes(tmp_path, monkeypatch, local, manifest, expected):
    import components

    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(tmp_path / "update_config.json"))
    monkeypatch.setattr(updater, "get_local_version", lambda: local)
    monkeypatch.setattr(updater, "fetch_manifest", lambda config=None: manifest)
    monkeypatch.setattr(components, "check", lambda manifest: {})
    assert updater.check_for_update()["release_notes"] == expected


def test_swap_in_resets_entries_not_preserved(tmp_path):
    live, staged, backup = (str(tmp_path / n) for n in ("live", "staged", "backup"))
    _tree(live, {"VERSION": "1", "run_config.json": "mine", "app_tracks.json": "mine", "OBS-Config/a": "mine"})
    _tree(staged, {"VERSION": "2", "run_config.json": "shipped", "OBS-Config/a": "shipped"})
    keep = updater.PRESERVED_RELATIVE_PATHS - {"run_config.json", "app_tracks.json"}
    updater.swap_in(staged, live, backup, preserved=keep, reset={"run_config.json", "app_tracks.json"})
    assert _read(live, "run_config.json") == "shipped"
    assert not os.path.exists(os.path.join(live, "app_tracks.json"))  # nothing shipped - removed
    assert _read(live, "OBS-Config/a") == "mine"


def test_a_validly_signed_manifest_is_parsed(signing_key):
    body = b'{"version": "1.2.3"}'
    manifest = updater.parse_signed_manifest(body, signing_key.sign(body), signing_key.public_raw)
    assert manifest == {"version": "1.2.3"}


@pytest.mark.parametrize("body", [b"[]", b'"1.2.3"', b'{"name": "no version"}'])
def test_a_signed_manifest_needs_a_version(signing_key, body):
    with pytest.raises(updater.UpdateError, match="missing its version"):
        updater.parse_signed_manifest(body, signing_key.sign(body), signing_key.public_raw)


def test_the_installed_public_key_is_read(tmp_path, monkeypatch):
    key = tmp_path / "key.txt"
    monkeypatch.setattr(updater, "SIGNING_PUBLIC_KEY_PATH", str(key))
    key.write_text(base64.b64encode(PUBLIC_KEY).decode() + "\n")
    assert updater.load_public_key() == PUBLIC_KEY


@pytest.mark.parametrize("bad", [base64.b64encode(b"short").decode(), base64.b64encode(bytes(33)).decode(), "!!!"])
def test_a_public_key_that_isnt_ed25519_fails_closed(tmp_path, monkeypatch, bad):
    key = tmp_path / "key.txt"
    key.write_text(bad)
    monkeypatch.setattr(updater, "SIGNING_PUBLIC_KEY_PATH", str(key))
    with pytest.raises(updater.UpdateError):
        updater.load_public_key()


def test_no_public_key_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "SIGNING_PUBLIC_KEY_PATH", str(tmp_path / "nope.txt"))
    with pytest.raises(updater.UpdateError):
        updater.load_public_key()


def test_the_manifest_comes_with_its_signature(monkeypatch, signing_key):
    body = b'{"version": "1.2.3"}'
    served = {updater.manifest_url({}): body, updater.manifest_url({}) + ".sig": signing_key.sign(body).encode()}
    limits = {}

    def read_url(url, timeout, max_bytes):
        limits[url.rpartition(".")[2]] = max_bytes
        return served[url]

    monkeypatch.setattr(updater, "_read_url", read_url)
    monkeypatch.setattr(updater, "load_public_key", lambda: signing_key.public_raw)
    assert updater.fetch_manifest({})["version"] == "1.2.3"
    assert limits == {"json": updater.MAX_MANIFEST_BYTES, "sig": 1024}


@pytest.mark.parametrize("size, fits", [(9, True), (10, True), (11, False)])
def test_read_url_refuses_more_than_it_expects(monkeypatch, size, fits):
    monkeypatch.setattr(updater, "_open_url", lambda url, timeout: io.BytesIO(b"x" * size))
    if fits:
        assert updater._read_url("https://x/y", 5, 10) == b"x" * size
    else:
        with pytest.raises(updater.UpdateError, match="larger than expected"):
            updater._read_url("https://x/y", 5, 10)


def test_requests_name_clipem_and_stay_on_https(monkeypatch):
    opened = []
    monkeypatch.setattr(updater._opener, "open", lambda request, timeout: opened.append((request, timeout)) or "res")
    assert updater._open_url("https://x/y", 7) == "res"
    assert opened[0][0].get_header("User-agent") == "ClipEm" and opened[0][1] == 7
    with pytest.raises(updater.UpdateError):
        updater._open_url("http://x/y", 7)
    assert len(opened) == 1


def test_a_redirect_to_https_is_followed():
    request = urllib.request.Request("https://github.com/x")
    followed = updater._HttpsOnlyRedirectHandler().redirect_request(request, None, 302, "Found", {}, "https://cdn/y")
    assert followed.full_url == "https://cdn/y"


def test_download_checks_the_hash_whatever_its_case(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_open_url", lambda url, timeout: io.BytesIO(b"payload"))
    dest = tmp_path / "file"
    good = hashlib.sha256(b"payload").hexdigest()
    updater.download_file("https://x/y", str(dest), good.upper())
    assert dest.read_bytes() == b"payload"


def test_a_bad_download_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_open_url", lambda url, timeout: io.BytesIO(b"tampered"))
    dest = tmp_path / "file"
    with pytest.raises(updater.UpdateError, match="Checksum mismatch"):
        updater.download_file("https://x/y", str(dest), "0" * 64)
    assert not dest.exists()


@pytest.mark.parametrize("url, version", [
    ("https://x/OBS-Studio-32.2.2-Windows-x64.zip", "32.2.2"),
    ("https://x/linux-obs-install-32.2.2.tar.gz", "32.2.2"),
    ("https://x/cpython-3.12.14+20260924-x86_64.tar.gz", "3.12.14"),
    ("https://x/1.2.3/no-version-in-the-name.zip", None),  # only the file name counts
])
def test_artifact_version_comes_from_the_file_name(url, version):
    assert updater.artifact_version({"url": url}) == version


def test_an_entry_without_a_url_has_no_version():
    assert updater.artifact_version({}) is None


@pytest.mark.parametrize("entry", [None, {}, {"url": "u"}, {"sha256": "s"}])
def test_a_manifest_entry_needs_its_url_and_hash(entry):
    with pytest.raises(updater.UpdateError):
        updater.manifest_entry({"linux": {"obs": entry}}, "linux", "obs")


def test_a_manifest_entry_is_found_and_a_missing_platform_isnt():
    entry = {"url": "u", "sha256": "s"}
    assert updater.manifest_entry({"linux": {"obs": entry}}, "linux", "obs") == entry
    with pytest.raises(updater.UpdateError):
        updater.manifest_entry({}, "linux", "obs")


def test_remote_version_reports_failure_as_a_value(monkeypatch):
    monkeypatch.setattr(updater, "fetch_manifest", lambda: {"version": "1.2.3"})
    assert updater.get_remote_version() == ("1.2.3", None)

    def offline():
        raise OSError("offline")

    monkeypatch.setattr(updater, "fetch_manifest", offline)
    assert updater.get_remote_version() == (None, "offline")


def test_a_failed_check_is_no_update(tmp_path, monkeypatch):
    def offline(config=None):
        raise OSError("offline")

    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(tmp_path / "update_config.json"))
    monkeypatch.setattr(updater, "get_local_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "fetch_manifest", offline)
    result = updater.check_for_update()
    assert result["error"] == "offline"
    assert result["update_available"] is False and result["shared_update"] is False
    assert result["local_version"] == "1.0.0" and result["remote_version"] is None


def test_outdated_components_alone_are_an_update(tmp_path, monkeypatch):
    import components

    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(tmp_path / "update_config.json"))
    monkeypatch.setattr(updater, "get_local_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "fetch_manifest", lambda config=None: {"version": "1.0.0"})
    monkeypatch.setattr(components, "check", lambda manifest: {"obs": ("1", "2")})
    result = updater.check_for_update()
    assert result["shared_update"] is False and result["update_available"] is True
    assert result["components"] == {"obs": ("1", "2")}


def test_an_update_is_described_part_by_part(monkeypatch):
    import components

    monkeypatch.setattr(components, "describe", lambda plan: [f"OBS {plan['obs']}"] if plan else [])
    result = {"shared_update": True, "local_version": "1.0.0", "remote_version": "1.1.0", "components": {"obs": "2"}}
    assert updater.describe_update(result) == ["ClipEm 1.0.0 -> 1.1.0", "OBS 2"]
    assert updater.describe_update(dict(result, shared_update=False, components={})) == []


@pytest.mark.parametrize("platform, key", [("win32", "windows"), ("darwin", "macos"), ("linux", "linux")])
def test_platform_key(monkeypatch, platform, key):
    monkeypatch.setattr(updater.sys, "platform", platform)
    assert updater._platform_key() == key


def test_equal_versions_are_never_newer_however_theyre_written():
    assert updater.is_newer("v1.0.0", "1.0.0") is False
    assert updater.is_newer("1.0", "1.0.0") is False
    assert updater.is_newer(None, "1.0.0") is False
    assert updater.is_newer("1.0.1", "") is False
    assert updater.is_newer("garbage", "1.0.0") is False
    assert updater._parse_version("1.2.10") == (1, 2, 10, 1, 0)
    assert updater._parse_version("1.2.10-dev.5") == (1, 2, 10, 0, 5)
    assert updater.is_dev_version("") is False and updater.is_dev_version(None) is False


def test_a_setting_saved_since_beats_the_old_auto_update_one(tmp_path, monkeypatch):
    path = tmp_path / "update_config.json"
    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(path))
    path.write_text('{"auto_update": false, "check_for_updates": true}')
    assert updater.load_update_config()["check_for_updates"] is True


def test_a_broken_update_config_is_the_defaults(tmp_path, monkeypatch):
    path = tmp_path / "update_config.json"
    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(path))
    for broken in ("{broken", "[1, 2]", "null"):
        path.write_text(broken)
        assert updater.load_update_config() == updater.DEFAULT_UPDATE_CONFIG


def test_changing_the_update_config_returns_the_result(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "UPDATE_CONFIG_PATH", str(tmp_path / "update_config.json"))
    assert updater.change_update_config(skipped_version="1.2.3")["skipped_version"] == "1.2.3"


def test_the_local_version_is_the_installed_one(monkeypatch):
    monkeypatch.setattr(updater, "read_version", lambda: "1.2.3")
    assert updater.get_local_version() == "1.2.3"


def test_too_many_zip_entries_are_refused(monkeypatch):
    monkeypatch.setattr(updater, "MAX_ZIP_ENTRIES", 2)
    updater.validate_zip(_zip({"Shared/a": "", "Shared/b": ""}))
    with pytest.raises(updater.UpdateError, match="too many"):
        updater.validate_zip(_zip({"Shared/a": "", "Shared/b": "", "Shared/c": ""}))


def test_a_zip_bomb_is_refused_counting_every_entry(monkeypatch):
    monkeypatch.setattr(updater, "MAX_ZIP_UNCOMPRESSED_BYTES", 10)
    updater.validate_zip(_zip({"Shared/a": "x" * 5, "Shared/b": "x" * 5}))
    with pytest.raises(updater.UpdateError, match="larger than expected"):
        updater.validate_zip(_zip({"Shared/a": "x" * 6, "Shared/b": "x" * 5}))


@pytest.mark.parametrize("name", ["Shared\\..\\..\\evil", "\\abs", "Shared/a/../../evil", "SharedX/file"])
def test_validate_zip_rejects_escapes_however_theyre_written(name):
    with pytest.raises(updater.UpdateError, match="unsafe path"):
        updater.validate_zip(_zip({name: "x"}))


def test_carrying_over_skips_what_the_update_doesnt_have(tmp_path):
    live, staged = str(tmp_path / "live"), str(tmp_path / "staged")
    _tree(live, {"Scripts/sound.wav": "mine", "tray-widget/.tray.lock": "1", "run_config.json": "mine"})
    _tree(staged, {"Scripts/new.py": "new"})
    updater.carry_over_preserved(live, staged, {"Scripts/sound.wav", "tray-widget/.tray.lock", "Scripts/gone.wav",
                                                "run_config.json"})
    assert _read(staged, "Scripts/sound.wav") == "mine"
    assert not os.path.exists(os.path.join(staged, "tray-widget"))  # not part of this update
    assert not os.path.exists(os.path.join(staged, "Scripts", "gone.wav"))
    assert not os.path.exists(os.path.join(staged, "run_config.json"))  # top-level: never swapped anyway


def test_carrying_over_defaults_to_every_preserved_path(tmp_path):
    live, staged = str(tmp_path / "live"), str(tmp_path / "staged")
    _tree(live, {"tray-widget/.settings.lock": "9"})
    _tree(staged, {"tray-widget/tray_widget.py": "new"})
    updater.carry_over_preserved(live, staged)
    assert _read(staged, "tray-widget/.settings.lock") == "9"


def _failing_on(monkeypatch, failing_src):
    real_replace = os.replace

    def replace(src, dst):
        if src == failing_src:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(updater.os, "replace", replace)
    return real_replace


def test_rolling_back_removes_what_the_update_added(tmp_path, monkeypatch):
    live, staged, backup = (str(tmp_path / n) for n in ("live", "staged", "backup"))
    _tree(live, {"VERSION": "1"})
    _tree(staged, {"Added/file": "new", "VERSION": "2"})
    _failing_on(monkeypatch, os.path.join(staged, "VERSION"))
    with pytest.raises(OSError):
        updater.swap_in(staged, live, backup)
    assert sorted(os.listdir(live)) == ["VERSION"] and _read(live, "VERSION") == "1"


def test_rolling_back_restores_a_reset_entry(tmp_path, monkeypatch):
    live, staged, backup = (str(tmp_path / n) for n in ("live", "staged", "backup"))
    _tree(live, {"VERSION": "1", "pending_obs_settings.json": "mine"})
    _tree(staged, {"VERSION": "2"})
    # Sorted, the reset entry goes after VERSION: fail on the one after it.
    _tree(staged, {"run.py": "new"})
    _failing_on(monkeypatch, os.path.join(staged, "run.py"))
    keep = updater.PRESERVED_RELATIVE_PATHS - {"pending_obs_settings.json"}
    with pytest.raises(OSError):
        updater.swap_in(staged, live, backup, preserved=keep, reset={"pending_obs_settings.json"})
    assert _read(live, "VERSION") == "1" and _read(live, "pending_obs_settings.json") == "mine"


def test_a_failure_moving_the_first_entry_aside_changes_nothing(tmp_path, monkeypatch):
    live, staged, backup = (str(tmp_path / n) for n in ("live", "staged", "backup"))
    _tree(live, {"VERSION": "1"})
    _tree(staged, {"VERSION": "2"})
    _failing_on(monkeypatch, os.path.join(live, "VERSION"))
    with pytest.raises(OSError):
        updater.swap_in(staged, live, backup)
    assert _read(live, "VERSION") == "1"


def test_swapping_in_reuses_a_backup_folder_already_there(tmp_path):
    live, staged, backup = (str(tmp_path / n) for n in ("live", "staged", "backup"))
    _tree(live, {"VERSION": "1"})
    _tree(staged, {"VERSION": "2"})
    os.makedirs(backup)
    updater.swap_in(staged, live, backup)
    assert _read(live, "VERSION") == "2" and _read(backup, "VERSION") == "1"


@pytest.mark.usefixtures("links")
def test_a_link_is_removed_not_what_it_points_at(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target)
    updater._remove_path(str(link))
    assert not link.exists() and (target / "keep").exists()
    file = tmp_path / "file"
    file.write_text("x")
    updater._remove_path(str(file))
    assert not file.exists()
    updater._remove_path(str(target))
    assert not target.exists()
