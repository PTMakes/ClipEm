"""Release tooling: signing round trip, install-manifest layout, and the
deterministic shared zip. Run with `python3 -m pytest tests` from Dev/
(release.sh does, in preflight)."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile

import ed25519_verify
import pytest
from conftest import APP_INSTALLER_DIR

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
    argv += ["--windows-obs-version", "32.2.2", "--windows-python-version", "3.12.10",
             "--windows-pip-version", "26.2.1", "--macos-obs-version", "32.2.2"]
    generate_install_manifest.main(argv)
    return out, files


def test_manifest_layout(manifest):
    out, files = manifest
    data = json.loads(out.read_text())
    assert data["version"] == "1.2.3"
    assert list(data) == ["version", "windows", "linux", "macos", "sources"]
    assert list(data["linux"])[:2] == ["obs", "python"]
    python = data["linux"]["python"]
    assert python["url"].endswith("/releases/download/v1.2.3/" + os.path.basename(files["--linux-python-tar"]))
    with open(files["--linux-python-tar"], "rb") as f:
        assert python["sha256"] == hashlib.sha256(f.read()).hexdigest()
    assert data["sources"]["ffmpeg"]["url"].endswith("ffmpeg-9.0.2-source.tar.gz")
    assert data["windows"]["obs"]["url"].endswith("OBS-Studio-32.2.2-Windows-x64.zip")


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
        "python": _zip_file(cache / "python-3.12.10-embed-amd64.zip", {
            "python.exe": "py", "python312._pth": "python312.zip\n.\n\n#import site\n",
        }),
        "pip_wheel": _zip_file(cache / "pip-26.2.1-py3-none-any.whl", {
            "pip/__init__.py": "", "pip-26.2.1.dist-info/METADATA": "Name: pip\nVersion: 26.2.1\n",
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
    tcltk = _zip_file(tmp_path / "tcltk.zip", {"_tkinter.pyd": "tk", "tcl/tcl8.6/init.tcl": "", "Lib/tkinter/__init__.py": ""})
    vcruntime = _zip_file(tmp_path / "vcruntime.zip", {"msvcp140.dll": "vc", "vcruntime140.dll": "vc"})
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "pystray-0.19.5-py2.py3-none-any.whl").write_bytes(b"wheel")
    setup_bat = tmp_path / "Setup_Windows.bat"
    setup_bat.write_text("@echo off")

    out = tmp_path / "clipem-windows-portable-1.4.0.zip"
    release_common.build_windows_offline_bundle(
        manifest, shared, ffmpeg, tcltk, vcruntime, setup_bat, wheelhouse, tmp_path / "stage", out, 1700000000,
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
        "ClipEm/App/Windows/Python/_tkinter.pyd",
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
    assert "import site" in pth.splitlines() and "#import site" not in pth and "Lib" in pth.splitlines()
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


def test_manifests_are_uploaded_last(tmp_path):
    names = ["install-manifest.json.sig", "clipem-shared-1.4.0.zip", "install-manifest.json", "build-info.json"]
    ordered = [f.name for f in release_common._ordered_uploads(tmp_path / n for n in names)]
    assert ordered[:2] == ["clipem-shared-1.4.0.zip", "build-info.json"]
    assert set(ordered[2:]) == {"install-manifest.json", "install-manifest.json.sig"}
