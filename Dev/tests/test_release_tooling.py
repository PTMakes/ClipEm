"""Release tooling: signing round trip, install-manifest layout, and the
deterministic shared zip. Run with `python3 -m pytest tests` from Dev/
(release.sh does, in preflight)."""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import zipfile

import ed25519_verify
import pytest
from conftest import APP_INSTALLER_DIR, DEV_DIR

import generate_install_manifest
import release_common

LINUX_INSTALLER = os.path.join(APP_INSTALLER_DIR, "Scripts", "ClipEmSetupLinux.sh")


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    key = tmp_path / "signing-key.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True)
    monkeypatch.setenv("CLIPEM_SIGNING_KEY", str(key))
    return key


def test_signature_made_at_release_verifies_in_the_client(tmp_path, signing_key):
    """release_common signs with openssl; clients verify with the pure-Python
    ed25519_verify (and openssl in the Linux installer) - they must agree."""
    manifest = tmp_path / "install-manifest.json"
    manifest.write_text('{"version": "1.2.3"}\n')
    sig_path = release_common.sign_file(manifest)

    public_key = ed25519_verify.decode_key_or_signature(release_common.public_key_from_private(signing_key))
    signature = ed25519_verify.decode_key_or_signature(sig_path.read_text())
    assert ed25519_verify.verify(public_key, manifest.read_bytes(), signature)
    assert not ed25519_verify.verify(public_key, b'{"version": "9.9.9"}\n', signature)


def test_signature_verifies_via_the_cli_bootstrap_uses(tmp_path, signing_key):
    manifest = tmp_path / "m.json"
    manifest.write_text("{}")
    sig_path = release_common.sign_file(manifest)
    key_file = tmp_path / "key.txt"
    key_file.write_text(release_common.public_key_from_private(signing_key))
    assert ed25519_verify.main(["x", str(key_file), str(manifest), str(sig_path)]) == 0
    manifest.write_text("{ }")
    assert ed25519_verify.main(["x", str(key_file), str(manifest), str(sig_path)]) == 1


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    """Runs generate_install_manifest.main() with local stand-ins for our
    own artifacts and no network (upstream pins are faked)."""
    files = {}
    for arg, name in (
        ("--shared-zip", "clipem-shared-1.2.3.zip"),
        ("--linux-obs-tar", "linux-obs-install-32.2.2.tar.gz"),
        ("--linux-python-tar", "cpython-3.12.14+20260924-x86_64-unknown-linux-gnu-install_only-xft.tar.gz"),
        ("--linux-ffmpeg", "ffmpeg-9.0.2-linux-x86_64.tar.gz"),
        ("--windows-ffmpeg", "ffmpeg-9.0.2-windows-x86_64.zip"),
        ("--ffmpeg-source", "ffmpeg-9.0.2-source.tar.gz"),
    ):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files[arg] = str(path)

    fake_pin = lambda url: {"url": url, "sha256": "0" * 64}  # noqa: E731
    monkeypatch.setattr(generate_install_manifest, "PINS_PATH", tmp_path / "pins.json")
    monkeypatch.setattr(generate_install_manifest, "pinned_upstream", lambda pins, url, published=None: fake_pin(url))
    monkeypatch.setattr(generate_install_manifest, "github_obs_asset", lambda pins, v, asset: fake_pin(asset))
    monkeypatch.setattr(generate_install_manifest, "github_release_asset_digest", lambda repo, tag, asset: None)
    monkeypatch.setattr(generate_install_manifest, "pypi_wheel", lambda pins, pkg, v: fake_pin(f"{pkg}-{v}.whl"))

    out = tmp_path / "install-manifest.json"
    argv = ["--version", "v1.2.3", "--out", str(out)]
    for arg, path in files.items():
        argv += [arg, path]
    argv += ["--windows-obs-version", "32.2.2", "--windows-python-version", "3.12.16",
             "--windows-python-pbs-tag", "20261026",
             "--windows-pip-version", "26.2.1", "--macos-obs-version", "32.2.2",
             "--electron-version", "44.5.1"]
    history = tmp_path / "release_notes.json"
    history.write_text(json.dumps([{"version": "1.2.3", "date": "2026-11-01", "sections": {"Fixes": ["A fix."]}}]))
    argv += ["--release-history", str(history)]
    generate_install_manifest.main(argv)
    return out, files


def test_manifest_layout(manifest):
    out, files = manifest
    data = json.loads(out.read_text())
    assert data["version"] == "1.2.3"
    # ClipEmSetupLinux.sh's plain-text scan needs the notes after "linux" (see main()).
    assert list(data) == ["version", "windows", "linux", "macos", "sources", "release_notes"]
    assert data["release_notes"] == "Fixes\n- A fix."
    assert list(data["linux"])[:2] == ["obs", "python"]
    python = data["linux"]["python"]
    assert python["url"].endswith("/releases/download/v1.2.3/" + os.path.basename(files["--linux-python-tar"]))
    with open(files["--linux-python-tar"], "rb") as f:
        assert python["sha256"] == hashlib.sha256(f.read()).hexdigest()
    assert data["sources"]["ffmpeg"]["url"].endswith("ffmpeg-9.0.2-source.tar.gz")
    assert data["windows"]["obs"]["url"].endswith("OBS-Studio-32.2.2-Windows-x64.zip")
    assert data["windows"]["python"]["url"] == (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
        "cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only.tar.gz"
    )
    assert data["windows"]["tcltk_msi"]["url"].endswith("/3.12.10/amd64/tcltk.msi")
    electron = "https://github.com/electron/electron/releases/download/v44.5.1/electron-v44.5.1-"
    assert data["linux"]["editor_window"]["url"] == electron + "linux-x64.zip"
    assert data["windows"]["editor_window"]["url"] == electron + "win32-x64.zip"
    assert data["macos"]["editor_window_arm64"]["url"] == electron + "darwin-arm64.zip"
    assert data["macos"]["editor_window_x86_64"]["url"] == electron + "darwin-x64.zip"


def test_windows_obs_asset_was_renamed_in_32():
    assert generate_install_manifest.windows_obs_asset_name("31.0.2") == "OBS-Studio-31.0.2-Windows.zip"
    assert generate_install_manifest.windows_obs_asset_name("32.2.2") == "OBS-Studio-32.2.2-Windows-x64.zip"


def test_linux_installer_awk_scan_finds_linux_python(manifest):
    """ClipEmSetupLinux.sh reads linux.python with awk (no JSON parser yet) -
    run its actual function against the generated manifest."""
    out, _ = manifest
    expected = json.loads(out.read_text())["linux"]["python"]
    for field in ("url", "sha256"):
        result = subprocess.run(
            ["bash", "-c",
             f'source <(sed -n "/^manifest_linux_python_field() {{/,/^}}/p" "$1"); '
             f'MANIFEST_JSON="$2" manifest_linux_python_field {field}',
             "_", LINUX_INSTALLER, str(out)],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == expected[field]


def _tree(root, files):
    for rel, (content, mode) in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)


@pytest.mark.skipif(os.name == "nt", reason="releases are built on Linux: Windows has no executable bit to keep")
def test_shared_zip_is_deterministic(tmp_path):
    files = {
        "Shared/VERSION": ("1.2.3", 0o644),
        "Shared/Installer/run.py": ("print()", 0o644),
        "Shared/Installer/Scripts/ClipEmSetupLinux.sh": ("#!/bin/bash", 0o755),
        "Shared/tray-widget/a.py": ("", 0o644),
    }
    first, second = tmp_path / "a", tmp_path / "b"
    _tree(first, files)
    _tree(second, dict(reversed(list(files.items()))))  # created in another order
    os.utime(second / "Shared" / "VERSION", (0, 0))  # and with other mtimes

    zip_a, zip_b = tmp_path / "a.zip", tmp_path / "b.zip"
    assert release_common.write_deterministic_zip(first, "Shared", zip_a, 1700000000) == 4
    release_common.write_deterministic_zip(second, "Shared", zip_b, 1700000000)
    assert zip_a.read_bytes() == zip_b.read_bytes()

    with zipfile.ZipFile(zip_a) as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert all(name.startswith("Shared/") for name in names)
        modes = {info.filename: info.external_attr >> 16 for info in zf.infolist()}
    assert modes["Shared/Installer/Scripts/ClipEmSetupLinux.sh"] & 0o111
    assert not modes["Shared/VERSION"] & 0o111


def test_shared_zip_passes_the_updaters_checks(tmp_path):
    import updater

    _tree(tmp_path, {"Shared/VERSION": ("1", 0o644), "Shared/Installer/x.py": ("", 0o644)})
    out = tmp_path / "shared.zip"
    release_common.write_deterministic_zip(tmp_path, "Shared", out, 1700000000)
    with zipfile.ZipFile(out) as zf:
        updater.validate_zip(zf)


@pytest.mark.skipif(os.name == "nt", reason="releases are built on Linux: Windows has no executable bit to keep")
def test_portable_bundle_layout_and_exec_bits(tmp_path):
    import io
    import tarfile

    shared = tmp_path / "shared.zip"
    _tree(tmp_path / "s", {"Shared/VERSION": ("1.2.3", 0o644),
                           "Shared/Installer/Scripts/UninstallAutostartLinux.sh": ("#!/bin/sh", 0o755)})
    release_common.write_deterministic_zip(tmp_path / "s", "Shared", shared, 1700000000)
    _tree(tmp_path, {"obs-install/bin/obs": ("obs", 0o755), "obs-install/lib/libobs.so.32": ("lib", 0o644)})
    (tmp_path / "obs-install/lib/libobs.so").symlink_to("libobs.so.32")

    def tar(path, files):
        with tarfile.open(path, "w:gz") as tf:
            for name, (data, mode) in files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                tf.addfile(info, io.BytesIO(data))

    tar(tmp_path / "python.tar.gz", {"python/bin/python3": (b"py", 0o755)})
    tar(tmp_path / "ffmpeg.tar.gz", {"ffmpeg/bin/ffmpeg": (b"ff", 0o755)})
    setup_script = tmp_path / "ClipEmSetupLinux.sh"
    setup_script.write_text("#!/bin/bash")

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "pillow-12.3.0-cp312-cp312-manylinux_2_28_x86_64.whl").write_bytes(b"wheel")

    out = tmp_path / "bundle.zip"
    release_common.build_portable_bundle(
        shared, tmp_path / "obs-install", tmp_path / "python.tar.gz", tmp_path / "ffmpeg.tar.gz",
        setup_script, tmp_path / "stage", out, 1700000000, wheelhouse=wheelhouse,
    )

    with zipfile.ZipFile(out) as zf:
        modes = {i.filename: i.external_attr >> 16 for i in zf.infolist()}
    executable = {name for name, mode in modes.items() if mode & 0o111}
    assert executable == {
        "ClipEm/ClipEmSetupLinux.sh",
        "ClipEm/App/Shared/Installer/Scripts/UninstallAutostartLinux.sh",
        "ClipEm/App/Linux/obs-install/bin/obs",
        "ClipEm/App/Linux/python/bin/python3",
        "ClipEm/App/Linux/ffmpeg/bin/ffmpeg",
    }
    assert "ClipEm/App/Shared/VERSION" in modes and "ClipEm/App/Linux/obs-install/lib/libobs.so" in modes
    assert "ClipEm/App/Linux/wheelhouse/pillow-12.3.0-cp312-cp312-manylinux_2_28_x86_64.whl" in modes
    assert (tmp_path / "stage" / "ClipEm" / "App" / "Linux" / "obs-install" / "lib" / "libobs.so").is_symlink()


def _tar_file(path, files):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data.encode())
            tf.addfile(info, io.BytesIO(data.encode()))
    return path


def _zip_file(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def test_windows_offline_bundle_is_a_ready_install(tmp_path):
    """Everything bootstrap.ps1 and Setup would otherwise download, already
    in place - including the setup steps that need the internet (pip)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    upstream = {
        "python": _tar_file(cache / "cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only.tar.gz", {
            "python/python.exe": "py", "python/python312.pdb": "symbols", "python/DLLs/_tkinter.pyd": "tk",
            "python/tcl/tcl8.6/init.tcl": "", "python/Lib/tkinter/__init__.py": "",
            "python/Lib/site-packages/pip/__init__.py": "",
        }),
        "obs": _zip_file(cache / "OBS-Studio-32.2.2-Windows-x64.zip", {
            "bin/64bit/obs64.exe": "obs", "bin/64bit/obs64.pdb": "symbols", "data/libobs/x.effect": "fx",
            "obs-plugins/64bit/obs-ffmpeg.dll": "plugin",
        }),
    }
    manifest = tmp_path / "install-manifest.json"
    manifest.write_text(json.dumps({"version": "1.4.0", "windows": {
        name: {"url": f"https://example.invalid/{path.name}", "sha256": release_common.sha256_of_file(path)}
        for name, path in upstream.items()
    }}))
    shared = _zip_file(tmp_path / "shared.zip", {"Shared/VERSION": "1.4.0"})
    ffmpeg = _zip_file(tmp_path / "ffmpeg.zip", {"ffmpeg/bin/ffmpeg.exe": "ff"})
    vcruntime = _zip_file(tmp_path / "vcruntime.zip", {"msvcp140.dll": "vc", "vcruntime140.dll": "vc"})
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "pystray-0.19.5-py2.py3-none-any.whl").write_bytes(b"wheel")
    setup_bat = tmp_path / "Setup_Windows.bat"
    setup_bat.write_text("@echo off")

    out = tmp_path / "clipem-windows-portable-1.4.0.zip"
    release_common.build_windows_offline_bundle(
        manifest, shared, ffmpeg, vcruntime, setup_bat, wheelhouse, tmp_path / "stage", out, 1700000000,
        cache_dir=cache,
    )

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        pth = zf.read("ClipEm/App/Windows/Python/python312._pth").decode()
        versions = json.loads(zf.read("ClipEm/App/installed_versions.json"))
    for expected in (
        "ClipEm/Setup_Windows.bat",
        "ClipEm/App/Shared/VERSION",
        "ClipEm/App/Windows/Python/python.exe",
        "ClipEm/App/Windows/Python/DLLs/_tkinter.pyd",
        "ClipEm/App/Windows/Python/Lib/tkinter/__init__.py",
        "ClipEm/App/Windows/Python/Lib/site-packages/pip/__init__.py",
        "ClipEm/App/Windows/bin/64bit/obs64.exe",
        "ClipEm/App/Windows/bin/64bit/msvcp140.dll",
        "ClipEm/App/Windows/obs-plugins/64bit/obs-ffmpeg.dll",
        "ClipEm/App/Windows/ffmpeg/bin/ffmpeg.exe",
        "ClipEm/App/Windows/wheelhouse/pystray-0.19.5-py2.py3-none-any.whl",
    ):
        assert expected in names, expected
    assert not any(name.endswith(".pdb") for name in names)
    assert pth.splitlines() == [".", "Lib", "DLLs", "import site"]
    assert versions == {"obs": "32.2.2"}


def test_tampered_cached_download_is_refetched_and_refused(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "python.zip").write_bytes(b"tampered")
    entry = {"url": "https://example.invalid/python.zip", "sha256": hashlib.sha256(b"real").hexdigest()}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(release_common.urllib.request, "urlopen", lambda *a, **k: Response(b"still wrong"))
    with pytest.raises(SystemExit, match="Checksum mismatch"):
        release_common.cached_download(entry, cache)
    monkeypatch.setattr(release_common.urllib.request, "urlopen", lambda *a, **k: Response(b"real"))
    assert release_common.cached_download(entry, cache).read_bytes() == b"real"


def test_wheelhouses_get_each_platforms_own_pins(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(release_common.subprocess, "run", lambda command, **kwargs: commands.append(command))
    tray = os.path.join(os.path.dirname(APP_INSTALLER_DIR), "tray-widget", "requirements.txt")
    mic = os.path.join(APP_INSTALLER_DIR, "mic-filter", "requirements.txt")
    windows = release_common.build_wheelhouse([tray, mic], "Windows", "3.12.10", tmp_path / "w")
    linux = release_common.build_wheelhouse([tray], "Linux", "3.12.14", tmp_path / "l")
    assert "pystray" in windows and "sounddevice" in windows and "dbus-next" not in windows
    assert "dbus-next" in linux and "pystray" not in linux
    assert "win_amd64" in commands[0] and "3.12" in commands[0]
    assert any(arg.startswith("manylinux") for arg in commands[1])


def test_offline_downloads_are_listed_in_the_manifest(tmp_path):
    manifest = tmp_path / "install-manifest.json"
    manifest.write_text(json.dumps({"version": "1.4.0", "linux": {}}))
    windows = tmp_path / "clipem-windows-portable-1.4.0.zip"
    windows.write_bytes(b"w" * 10)
    linux = tmp_path / "clipem-linux-portable-1.4.0.zip"
    linux.write_bytes(b"l" * 20)
    release_common.add_offline_downloads(manifest, "1.4.0", {"windows": windows, "linux": linux})
    offline = json.loads(manifest.read_text())["offline"]
    assert offline["windows"]["url"].endswith("/releases/download/v1.4.0/clipem-windows-portable-1.4.0.zip")
    assert offline["linux"]["size"] == 20 and len(offline["linux"]["sha256"]) == 64
    assert list(json.loads(manifest.read_text())) == ["version", "linux", "offline"]  # appended last


def test_progress_bar_is_silent_without_a_terminal():
    stream = io.StringIO()
    bar = release_common.ProgressBar(100, "x", stream=stream)
    bar.advance(50)
    bar.finish()
    assert stream.getvalue() == ""


def test_release_asset_urls_point_at_github_releases():
    assert release_common.release_asset_url("1.4.0", "clipem-shared-1.4.0.zip") == (
        f"https://github.com/{release_common.RELEASE_REPO}/releases/download/v1.4.0/clipem-shared-1.4.0.zip")
    # Dev builds all live on the rolling "dev" prerelease.
    assert release_common.release_tag("1.4.1-dev.202610142130") == "dev"
    assert "/releases/download/dev/" in release_common.release_asset_url("1.4.1-dev.202610142130", "x.zip")


HISTORY = [{"version": "1.4.9", "date": "2026-11-01", "sections": {"New Features": ["A.", "B."], "Fixes": ["C."]}},
           {"version": "1.4.8", "date": "2026-10-26", "sections": {"Fixes": ["Old."]}}]


@pytest.fixture
def history(tmp_path):
    path = tmp_path / "release_notes.json"
    path.write_text(json.dumps(HISTORY))
    return path


def test_release_notes_come_from_the_history(history):
    assert release_common.release_notes_text(history, "1.4.9") == "New Features\n- A.\n- B.\n\nFixes\n- C."
    assert release_common.release_notes_text(history, "1.4.9-dev.202610310049").startswith("New Features")
    assert release_common.release_notes_text(history, "1.5.0") == ""


def test_a_release_needs_notes_and_a_dev_build_doesnt(history):
    release_common.check_release_notes(history, "1.4.9")
    release_common.check_release_notes(history, "1.5.0-dev.202610310049")
    with pytest.raises(SystemExit):
        release_common.check_release_notes(history, "1.5.0")


def test_too_long_release_notes_stop_the_release(tmp_path):
    path = tmp_path / "release_notes.json"
    long_note = "x" * (release_common.MAX_RELEASE_NOTES_CHARS + 1)
    path.write_text(json.dumps([{"version": "1.4.9", "date": "2026-11-01", "sections": {"Fixes": [long_note]}}]))
    with pytest.raises(SystemExit):
        release_common.check_release_notes(path, "1.4.9")


def test_shipped_release_history_is_complete_and_newest_first():
    import updater

    with open(os.path.join(APP_INSTALLER_DIR, "..", "release_notes.json"), encoding="utf-8") as f:
        history = json.load(f)
    versions = [entry["version"] for entry in history]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d", entry["date"]) for entry in history)
    assert all(entry["sections"] and all(entry["sections"].values()) for entry in history)
    assert all(updater.is_newer(a, b) for a, b in zip(versions, versions[1:], strict=False))
    with open(os.path.join(APP_INSTALLER_DIR, "..", "VERSION"), encoding="utf-8") as f:
        assert f.read().strip() in versions  # the newest may be the coming version's


@pytest.mark.parametrize("version, refused", [("1.4.9", True), ("1.4.9-dev.202611012245", True),
                                              ("1.5.0-dev.202611012245", False), ("1.5.0", False), ("1.5.1", False)])
def test_a_release_older_than_the_notes_is_stopped(tmp_path, version, refused):
    history = tmp_path / "notes.json"
    history.write_text(json.dumps([{"version": "1.5.0", "date": "x", "sections": {"New": ["A."]}}]))
    if version == "1.5.1":
        version += "-dev.1"
    if refused:
        with pytest.raises(SystemExit, match="older than 1.5.0"):
            release_common.check_release_notes(history, version)
    else:
        release_common.check_release_notes(history, version)


def test_the_linux_obs_archive_doesnt_change_with_each_commit():
    # Its sha256 is how installs spot a new OBS build (components.check).
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "release.sh"),
              encoding="utf-8") as f:
        packaging = f.read().split('step "Linux OBS (built from source)"')[1].split('echo "Wrote')[0]
    assert '--mtime="@0"' in packaging and "SOURCE_DATE_EPOCH" not in packaging


def test_the_newest_notes_fit_the_update_prompts():
    path = os.path.join(APP_INSTALLER_DIR, "..", "release_notes.json")
    with open(path, encoding="utf-8") as f:
        newest = json.load(f)[0]["version"]
    release_common.check_release_notes(path, newest)  # exits if they're too long


@pytest.mark.parametrize("version, expected", [("1.4.9", "New Features\n- A.\n- B.\n\nFixes\n- C."), ("1.5.0", "ClipEm 1.5.0")])
def test_github_release_gets_the_notes(tmp_path, monkeypatch, history, version, expected):
    calls = []
    monkeypatch.setattr(release_common, "_gh_ok", lambda *args: calls.append(args))
    asset = tmp_path / f"clipem-shared-{version}.zip"
    asset.write_bytes(b"zip")
    release_common.upload_release(version, [asset], "abc123", history)
    args = list(calls[0])
    assert args[args.index("--notes") + 1] == expected


def test_obs_plugin_keeps_obs_running_when_it_hides_the_tray_icon():
    """On KDE, hiding OBS's tray icon releases Qt's last quit lock, and OBS exits."""
    with open(os.path.join(DEV_DIR, "obs-plugin", "clipem-obs.cpp"), encoding="utf-8") as f:
        assert "QCoreApplication::setQuitLockEnabled(false);" in f.read()


def test_obs_plugin_is_built_for_the_obs_windows_installs_get():
    with open(os.path.join(DEV_DIR, "build", "build-obs-plugin.sh"), encoding="utf-8") as f:
        script = f.read()
    with open(os.path.join(APP_INSTALLER_DIR, "setup.py"), encoding="utf-8") as f:
        setup_py = f.read()
    built_for = re.search(r'^OBS_WINDOWS_VERSION="([^"]+)"', script, re.M).group(1)
    assert built_for == re.search(r'^WINDOWS_OBS_VERSION = "([^"]+)"', setup_py, re.M).group(1)


def test_manifests_are_uploaded_last(tmp_path):
    names = ["install-manifest.json.sig", "clipem-shared-1.4.0.zip", "install-manifest.json", "build-info.json"]
    ordered = [f.name for f in release_common._ordered_uploads(tmp_path / n for n in names)]
    assert ordered[:2] == ["clipem-shared-1.4.0.zip", "build-info.json"]
    assert set(ordered[2:]) == {"install-manifest.json", "install-manifest.json.sig"}


# --- Upstream pins: what a release trusts ------------------------------------

def test_a_first_time_pin_is_the_publishers_digest(monkeypatch, capsys):
    monkeypatch.setattr(generate_install_manifest, "sha256_of_url", lambda url: pytest.fail("no need to download"))
    pins = {}
    entry = generate_install_manifest.pinned_upstream(pins, "https://x/a", "ABC123")
    assert entry == {"url": "https://x/a", "sha256": "abc123"} and pins == {"https://x/a": "abc123"}


def test_without_a_published_digest_the_download_is_pinned_with_a_warning(monkeypatch, capsys):
    monkeypatch.setattr(generate_install_manifest, "sha256_of_url", lambda url: "d1")
    pins = {}
    assert generate_install_manifest.pinned_upstream(pins, "https://x/a")["sha256"] == "d1"
    assert pins == {"https://x/a": "d1"} and "WARNING" in capsys.readouterr().out


def test_a_pin_is_kept_without_fetching_again(monkeypatch):
    monkeypatch.setattr(generate_install_manifest, "sha256_of_url", lambda url: pytest.fail("already pinned"))
    pins = {"https://x/a": "abc"}
    assert generate_install_manifest.pinned_upstream(pins, "https://x/a")["sha256"] == "abc"
    assert generate_install_manifest.pinned_upstream(pins, "https://x/a", "ABC")["sha256"] == "abc"


def test_a_pin_the_publisher_disagrees_with_stops_the_release():
    with pytest.raises(SystemExit, match="investigate before releasing"):
        generate_install_manifest.pinned_upstream({"https://x/a": "abc"}, "https://x/a", "def")


def test_a_broken_pins_file_stops_the_release_instead_of_dropping_every_pin(tmp_path, monkeypatch):
    pins = tmp_path / "pins.json"
    monkeypatch.setattr(generate_install_manifest, "PINS_PATH", pins)
    assert generate_install_manifest.load_pins() == {}
    pins.write_text('{"https://x/a": "abc"}')
    assert generate_install_manifest.load_pins() == {"https://x/a": "abc"}
    pins.write_text("{broken")
    with pytest.raises(SystemExit, match="fix or restore it"):
        generate_install_manifest.load_pins()


def test_the_shipped_pins_are_real_digests():
    pins = json.loads(generate_install_manifest.PINS_PATH.read_text())
    assert pins and all(url.startswith("https://") and re.fullmatch("[0-9a-f]{64}", sha) for url, sha in pins.items())


def test_a_new_github_asset_uses_its_published_digest(monkeypatch):
    asked = []
    monkeypatch.setattr(generate_install_manifest, "github_release_asset_digest",
                        lambda repo, tag, asset: asked.append((repo, tag, asset)) or "ABC")
    entry = generate_install_manifest.github_asset({}, "o/r", "v1", "a.zip")
    assert entry == {"url": "https://github.com/o/r/releases/download/v1/a.zip", "sha256": "abc"}
    assert asked == [("o/r", "v1", "a.zip")]


def test_the_obs_asset_comes_from_obs(monkeypatch):
    monkeypatch.setattr(generate_install_manifest, "github_release_asset_digest", lambda *a: "abc")
    url = generate_install_manifest.github_obs_asset({}, "32.2.2", "obs.zip")["url"]
    assert url == "https://github.com/obsproject/obs-studio/releases/download/32.2.2/obs.zip"


def test_a_wheel_is_taken_from_pypi_universal_only(monkeypatch):
    urls = [
        {"packagetype": "sdist", "filename": "pip-1.tar.gz", "url": "https://x/s", "digests": {"sha256": "s"}},
        {"packagetype": "bdist_wheel", "filename": "pip-1-cp312-win_amd64.whl", "url": "https://x/w",
         "digests": {"sha256": "w"}},
        {"packagetype": "bdist_wheel", "filename": "pip-1-py3-none-any.whl", "url": "https://x/any",
         "digests": {"sha256": "ABC"}},
    ]
    monkeypatch.setattr(generate_install_manifest, "fetch_json", lambda url: {"urls": urls})
    assert generate_install_manifest.pypi_wheel({}, "pip", "1") == {"url": "https://x/any", "sha256": "abc"}
    monkeypatch.setattr(generate_install_manifest, "fetch_json", lambda url: {"urls": urls[:2]})
    with pytest.raises(SystemExit, match="No py3-none-any wheel"):
        generate_install_manifest.pypi_wheel({}, "pip", "1")


def test_a_missing_artifact_stops_the_manifest(tmp_path):
    args = type("Args", (), {"version": "1.2.3"})
    with pytest.raises(SystemExit, match="File not found"):
        generate_install_manifest.own_asset(args, str(tmp_path / "gone.zip"))


def test_github_digests_come_from_the_api_or_the_release_notes(monkeypatch):
    sha = "ab" * 32
    releases = {
        "v1": {"assets": [{"name": "other.zip", "digest": "sha256:" + "00" * 32},
                          {"name": "a.zip", "digest": f"sha256:{sha}"}]},
        "v2": {"assets": [{"name": "a.zip", "digest": "md5:xyz"}], "body": f"Files:\n  a.zip: {sha.upper()}\n"},
        "v3": {"assets": [{"name": "a.zip"}], "body": None},
        "v4": {"assets": [], "body": f"a.zip.sig: {sha}\nnot-a.zip: {sha}\n"},
    }
    monkeypatch.setattr(release_common, "fetch_json", lambda url: releases[url.rpartition("/")[2]])
    assert release_common.github_release_asset_digest("o/r", "v1", "a.zip") == sha
    assert release_common.github_release_asset_digest("o/r", "v2", "a.zip") == sha
    assert release_common.github_release_asset_digest("o/r", "v3", "a.zip") is None
    assert release_common.github_release_asset_digest("o/r", "v4", "a.zip") is None


def test_a_failed_digest_lookup_is_no_digest(monkeypatch, capsys):
    def offline(_url):
        raise OSError("offline")

    monkeypatch.setattr(release_common, "fetch_json", offline)
    assert release_common.github_release_asset_digest("o/r", "v1", "a.zip") is None
    assert "could not query GitHub" in capsys.readouterr().out


# --- Keys ---------------------------------------------------------------------

def test_every_shipped_copy_of_the_public_key_is_the_same_valid_key():
    keys = release_common.shipped_public_keys()
    assert len(keys) == 2 and len(set(keys.values())) == 1
    assert len(ed25519_verify.decode_key_or_signature(next(iter(keys.values())))) == 32


def _shipped_key(tmp_path, monkeypatch, value):
    key_file, script = tmp_path / "signing_public_key.txt", tmp_path / "setup.sh"
    key_file.write_text(value + "\n")
    script.write_text(f'#!/bin/bash\n{release_common.LINUX_INSTALLER_KEY_PREFIX}{value}"\n')
    monkeypatch.setattr(release_common, "PUBLIC_KEY_FILE", key_file)
    monkeypatch.setattr(release_common, "LINUX_INSTALLER_SCRIPT", script)
    return key_file, script


def test_check_key_passes_when_everything_matches(tmp_path, monkeypatch, signing_key):
    public = release_common.public_key_from_private(signing_key)
    _shipped_key(tmp_path, monkeypatch, public)
    assert release_common.check_key() == public


def test_check_key_names_what_doesnt_match(tmp_path, monkeypatch, signing_key):
    public = release_common.public_key_from_private(signing_key)
    key_file, script = _shipped_key(tmp_path, monkeypatch, public)
    script.write_text("no key here\n")
    with pytest.raises(SystemExit) as exc:
        release_common.check_key()
    assert str(script) in str(exc.value) and str(key_file) not in str(exc.value)


def test_check_key_needs_a_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPEM_SIGNING_KEY", str(tmp_path / "gone.pem"))
    with pytest.raises(SystemExit, match="signing key not found"):
        release_common.check_key()


def test_a_missing_public_key_file_is_a_mismatch(tmp_path, monkeypatch):
    _key_file, script = _shipped_key(tmp_path, monkeypatch, "abc")
    monkeypatch.setattr(release_common, "PUBLIC_KEY_FILE", tmp_path / "gone")
    assert release_common.shipped_public_keys() == {str(tmp_path / "gone"): "", str(script): "abc"}


def test_a_key_that_isnt_ed25519_is_refused(tmp_path, monkeypatch):
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    key = tmp_path / "rsa.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "rsa", "-pkeyopt", "rsa_keygen_bits:1024", "-out", str(key)],
                   check=True, capture_output=True)
    with pytest.raises(SystemExit, match="not an Ed25519"):
        release_common.public_key_from_private(key)


def test_the_signing_key_defaults_to_the_users_config(monkeypatch):
    monkeypatch.delenv("CLIPEM_SIGNING_KEY", raising=False)
    assert str(release_common.signing_key_path()).endswith(os.path.join(".config", "clipem", "signing-key.pem"))


def test_signing_writes_one_line_of_base64_beside_the_file(tmp_path, signing_key):
    manifest = tmp_path / "m.json"
    manifest.write_text("{}")
    sig_path = release_common.sign_file(manifest)
    assert sig_path == tmp_path / "m.json.sig"
    assert len(ed25519_verify.decode_key_or_signature(sig_path.read_text())) == 64
    assert sig_path.read_text().count("\n") == 1


def test_signing_fails_loudly_without_its_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPEM_SIGNING_KEY", str(tmp_path / "gone.pem"))
    manifest = tmp_path / "m.json"
    manifest.write_text("{}")
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    with pytest.raises(subprocess.CalledProcessError):
        release_common.sign_file(manifest)
    assert not (tmp_path / "m.json.sig").exists()


# --- GitHub --------------------------------------------------------------------

class FakeGh:
    """gh's answers by the words in its command line; records every call."""

    def __init__(self, monkeypatch, answers=()):
        self.calls = []
        self.answers = dict(answers)
        monkeypatch.setattr(release_common.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(release_common.subprocess, "run", self.run)

    def run(self, cmd, **_kwargs):
        self.calls.append(cmd)
        for words, answer in self.answers.items():
            if all(word in cmd for word in words.split()):
                return subprocess.CompletedProcess(cmd, *answer)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def commands(self):
        return [" ".join(arg for arg in cmd[1:3]) for cmd in self.calls]


def test_no_gh_stops_a_release(monkeypatch):
    monkeypatch.setattr(release_common.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="GitHub CLI"):
        release_common.check_github()
    with pytest.raises(SystemExit, match="GitHub CLI"):
        release_common.release_exists("v1")


def test_gh_must_be_logged_in_and_reach_the_repo(monkeypatch):
    FakeGh(monkeypatch, {"auth status": (1, "", "no")})
    with pytest.raises(SystemExit, match="isn't logged in"):
        release_common.check_github()
    FakeGh(monkeypatch, {"repo view": (1, "", "no such repo")})
    with pytest.raises(SystemExit, match="no such repo"):
        release_common.check_github()
    FakeGh(monkeypatch)
    release_common.check_github()


def test_release_exists_tells_not_found_from_failure(monkeypatch):
    FakeGh(monkeypatch)
    assert release_common.release_exists("v1") is True
    FakeGh(monkeypatch, {"release view": (1, "", "release not found")})
    assert release_common.release_exists("v1") is False
    FakeGh(monkeypatch, {"release view": (1, "", "HTTP 502")})
    with pytest.raises(SystemExit, match="HTTP 502"):
        release_common.release_exists("v1")


def test_gh_failures_stop_the_release_with_its_message(monkeypatch):
    FakeGh(monkeypatch, {"release upload": (1, "", "upload failed")})
    with pytest.raises(SystemExit, match="upload failed"):
        release_common._gh_ok("release", "upload", "v1")


def _assets(tmp_path, *names):
    files = []
    for name in names:
        (tmp_path / name).write_bytes(b"x")
        files.append(tmp_path / name)
    return files


def test_a_release_is_a_draft_until_published(tmp_path, monkeypatch):
    gh = FakeGh(monkeypatch)
    release_common.upload_release("1.2.3", _assets(tmp_path, "a.zip"), "abc123")
    cmd = gh.calls[0]
    assert cmd[1:4] == ["release", "create", "v1.2.3"] and "--draft" in cmd
    assert cmd[cmd.index("--notes") + 1] == "ClipEm 1.2.3"


def test_publishing_tags_the_release_commit(monkeypatch):
    gh = FakeGh(monkeypatch)
    release_common.publish_release("1.2.3", "abc123")
    cmd = gh.calls[0]
    assert cmd[1:4] == ["release", "edit", "v1.2.3"]
    assert cmd[cmd.index("--target") + 1] == "abc123" and "--draft=false" in cmd and "--latest" in cmd


def test_the_first_dev_build_makes_the_prerelease(tmp_path, monkeypatch):
    gh = FakeGh(monkeypatch, {"release view": (1, "", "release not found")})
    release_common.upload_release("1.2.3-dev.5", _assets(tmp_path, "a.zip"), "abc123")
    create = gh.calls[-1]
    assert create[1:4] == ["release", "create", "dev"] and "--prerelease" in create and "--draft" not in create
    assert "abc123" in create[create.index("--notes") + 1]


def test_a_later_dev_build_replaces_the_assets_manifests_last_then_drops_stale_ones(tmp_path, monkeypatch):
    files = _assets(tmp_path, "install-manifest.json", "new.zip")
    assets = json.dumps({"assets": [{"name": "old.zip"}, {"name": "new.zip"}, {"name": "install-manifest.json"}]})
    gh = FakeGh(monkeypatch, {"release view --json assets": (0, assets, "")})
    release_common.upload_release("1.2.3-dev.5", files, "abc123")
    uploads = [cmd for cmd in gh.calls if cmd[1:3] == ["release", "upload"]]
    assert [[os.path.basename(arg) for arg in cmd if arg.startswith(str(tmp_path))] for cmd in uploads] == [
        ["new.zip"], ["install-manifest.json"]]
    assert all("--clobber" in cmd for cmd in uploads)
    deletes = [cmd for cmd in gh.calls if cmd[1:3] == ["release", "delete-asset"]]
    assert [cmd[4] for cmd in deletes] == ["old.zip"] and "--yes" in deletes[0]
    assert gh.calls.index(uploads[-1]) < gh.calls.index(deletes[0])


# --- The CLI release.sh runs ------------------------------------------------------

def test_the_cli_checks_the_key_before_signing(tmp_path, monkeypatch, capsys):
    order = []
    monkeypatch.setattr(release_common, "check_key", lambda: order.append("check") or "pub")
    monkeypatch.setattr(release_common, "sign_file", lambda f: order.append(f) or "sig")
    release_common.main(["sign", "a", "b"])
    assert order == ["check", "a", "b"]


def test_the_cli_reports_whether_a_release_exists(monkeypatch):
    monkeypatch.setattr(release_common, "release_exists", lambda tag: tag == "v1.2.3")
    assert release_common.main(["release-exists", "1.2.3"]) == 0
    assert release_common.main(["release-exists", "1.2.4"]) == 1


def test_the_cli_prints_asset_urls(capsys):
    release_common.main(["asset-url", "1.2.3", "a.zip"])
    assert capsys.readouterr().out.strip() == release_common.release_asset_url("1.2.3", "a.zip")


@pytest.mark.parametrize("command", [
    ["zip-dir", "base", "top", "out"],
    ["portable-bundle", "--shared-zip", "a", "--obs-install", "a", "--python-tar", "a", "--ffmpeg-tar", "a",
     "--setup-script", "a", "--stage-dir", "a", "--out", "a"],
    ["windows-offline-bundle", "--manifest", "a", "--shared-zip", "a", "--ffmpeg-zip", "a", "--vcruntime-zip", "a",
     "--setup-script", "a", "--wheelhouse", "a", "--stage-dir", "a", "--out", "a"],
], ids=lambda c: c[0])
def test_reproducible_builds_need_their_timestamp(monkeypatch, command):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    with pytest.raises(SystemExit, match="SOURCE_DATE_EPOCH"):
        release_common.main(command)


def test_the_atomic_json_helper_keeps_the_old_file_on_failure(tmp_path):
    path = tmp_path / "pins.json"
    path.write_text("old")
    with pytest.raises(TypeError):
        release_common.write_json_atomic(path, {"bad": object()})
    assert path.read_text() == "old" and os.listdir(tmp_path) == ["pins.json"]
