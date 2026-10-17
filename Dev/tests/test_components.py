"""components.py: checking and installing OBS, the bundled Python and the
tray's packages, with real archives in a temporary App/ - only downloads
and pip are faked."""

import io
import json
import os
import shutil
import tarfile
import zipfile

import components
import obs_install
import pytest
import updater

REQUIREMENTS_DIR = os.path.dirname(components.TRAY_REQUIREMENTS)


def _tar(path, files):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def _dist_info(folder, name, version):
    info = os.path.join(folder, f"{name.replace('-', '_')}-{version}.dist-info")
    os.makedirs(info, exist_ok=True)
    with open(os.path.join(info, "METADATA"), "w", encoding="utf-8") as f:
        f.write(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")


def _install_pins(folder, requirements, system, python_version, overrides=None):
    for name, version in components.requirement_pins(requirements, system, python_version).items():
        _dist_info(folder, name, (overrides or {}).get(name, version))


@pytest.fixture
def app(tmp_path, monkeypatch):
    """An empty App/ in tmp_path; downloads are served from `archives`
    ({file name: builder(path)}) by URL file name."""
    app = tmp_path / "App"
    (app / "Shared" / "OBS-Config").mkdir(parents=True)
    (app / "Shared" / "OBS-Config" / "global.ini").write_text("user settings")
    for module in (components, obs_install):
        monkeypatch.setattr(module, "APP_DIR", str(app))
    monkeypatch.setattr(obs_install, "LOCAL_CHECKSUMS_PATH", str(app / "checksums_local.json"))
    monkeypatch.setattr(obs_install, "INSTALLED_VERSIONS_PATH", str(app / "installed_versions.json"))
    archives = {}
    downloads = []

    def download_file(url, dest, sha256, timeout=None):
        name = url.rsplit("/", 1)[1]
        downloads.append(name)
        if name not in archives:
            raise updater.UpdateError(f"offline: {url}")
        archives[name](dest)

    monkeypatch.setattr(updater, "download_file", download_file)
    return app, archives, downloads


def _manifest(**entries):
    manifest = {"version": "9.9.9", "windows": {}, "linux": {}}
    for key, url in entries.items():
        platform_key, artifact = key.split("_", 1)
        manifest[platform_key][artifact] = {"url": url, "sha256": "x"}
    return manifest


# --- Versions and pins ------------------------------------------------------

def test_artifact_versions_come_from_the_manifest_file_names():
    for url, version in (
        ("https://github.com/obsproject/obs-studio/releases/download/32.2.2/OBS-Studio-32.2.2-Windows-x64.zip",
         "32.2.2"),
        ("https://downloads.clipem.app/releases/v1.3.3/linux-obs-install-32.2.2.tar.gz", "32.2.2"),
        ("https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip", "3.12.10"),
        ("https://downloads.clipem.app/releases/v1.3.3/"
         "cpython-3.12.14+20260924-x86_64-unknown-linux-gnu-install_only-xft.tar.gz", "3.12.14"),
    ):
        assert updater.artifact_version({"url": url}) == version


def test_shipped_requirements_resolve_for_every_platform():
    """Every marker the shipped requirements files use must be one
    components can evaluate, or package updates would fail."""
    tray, mic = components.TRAY_REQUIREMENTS, components.MIC_FILTER_REQUIREMENTS
    windows = components.requirement_pins(tray, "Windows", "3.12.10")
    linux = components.requirement_pins(tray, "Linux", "3.12.14")
    assert "pystray" in windows and "dbus-next" not in windows
    assert "dbus-next" in linux and "pystray" not in linux
    assert {"pillow", "websocket-client"} <= set(windows) & set(linux)
    assert components.requirement_pins(mic, "Windows", "3.10.11")["numpy"].startswith("2.2.")
    assert components.requirement_pins(mic, "Windows", "3.12.10")["numpy"] != (
        components.requirement_pins(mic, "Windows", "3.10.11")["numpy"]
    )


def test_unsupported_markers_are_refused(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text('foo==1.0; platform_machine == "x86_64"\n')
    with pytest.raises(ValueError):
        components.requirement_pins(str(requirements), "Linux", "3.12.0")


def test_linux_packages_are_outdated_only_when_a_pin_differs(app, monkeypatch):
    app_dir, _archives, _downloads = app
    monkeypatch.setattr(components, "_query_python_version", lambda exe: "3.12.14")
    assert components.outdated_package_sets("Linux") == []  # tray widget never set up

    deps = app_dir / "Linux" / "tray-deps"
    _install_pins(str(deps), components.TRAY_REQUIREMENTS, "Linux", "3.12.14")
    assert components.outdated_package_sets("Linux") == []

    shutil.rmtree(deps)
    _install_pins(str(deps), components.TRAY_REQUIREMENTS, "Linux", "3.12.14", {"pillow": "1.0.0"})
    assert components.outdated_package_sets("Linux") == [components.TRAY_REQUIREMENTS]


def test_windows_checks_the_mic_filter_packages_only_if_installed(app, monkeypatch):
    app_dir, _archives, _downloads = app
    python = app_dir / "Windows" / "Python"
    python.mkdir(parents=True)
    (python / "python.exe").write_text("")
    site = python / "Lib" / "site-packages"
    monkeypatch.setattr(components, "installed_python_version", lambda system: "3.12.10")

    _install_pins(str(site), components.TRAY_REQUIREMENTS, "Windows", "3.12.10")
    assert components.installed_package_sets("Windows") == [components.TRAY_REQUIREMENTS]
    assert components.outdated_package_sets("Windows") == []

    _dist_info(str(site), "sounddevice", "0.0.1")  # mic filter set up with older pins
    assert components.outdated_package_sets("Windows") == [components.MIC_FILTER_REQUIREMENTS]


def test_check_reports_only_newer_versions(app, monkeypatch):
    monkeypatch.setattr(components, "installed_obs_version", lambda system: "32.2.2")
    monkeypatch.setattr(components, "installed_python_version", lambda system: "3.12.14")
    monkeypatch.setattr(components, "outdated_package_sets", lambda system: [])
    manifest = _manifest(
        linux_obs="https://x.invalid/linux-obs-install-32.3.0.tar.gz",
        linux_python="https://x.invalid/cpython-3.12.14+20270101-x86_64-install_only.tar.gz",
    )
    assert components.check(manifest, "Linux") == {"obs": ("32.2.2", "32.3.0")}
    manifest["linux"]["obs"]["url"] = "https://x.invalid/linux-obs-install-32.0.0.tar.gz"
    assert components.check(manifest, "Linux") == {}  # never a downgrade
    assert components.describe({"obs": ("32.2.2", "32.3.0"), "packages": [components.TRAY_REQUIREMENTS]}) == [
        "OBS 32.2.2 -> 32.3.0", "Updated tray widget packages",
    ]


def test_obs_version_falls_back_to_the_recorded_one(app, monkeypatch):
    app_dir, _archives, _downloads = app
    exe = app_dir / "Linux" / "obs-install" / "bin" / "obs"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\necho 'OBS Studio - 32.2.2-modified'\n")
    exe.chmod(0o755)
    assert components.installed_obs_version("Linux") == "32.2.2"
    exe.write_text("#!/bin/sh\nexit 1\n")
    (app_dir / "installed_versions.json").write_text(json.dumps({"obs": "31.0.0"}))
    assert components.installed_obs_version("Linux") == "31.0.0"


def test_linux_obs_is_outdated_when_its_build_hash_changes(app, monkeypatch):
    app_dir, _archives, _downloads = app
    monkeypatch.setattr(components, "installed_python_version", lambda system: None)
    monkeypatch.setattr(components, "outdated_package_sets", lambda system: [])
    monkeypatch.setattr(components, "installed_obs_version", lambda system: "32.2.2")
    manifest = _manifest(linux_obs="https://x.invalid/linux-obs-install-32.2.2.tar.gz")
    assert components.check(manifest, "Linux") == {}  # not installed - nothing to update
    exe = app_dir / "Linux" / "obs-install" / "bin" / "obs"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    # Installed before obs_sha256 was recorded: gets the current build once.
    assert components.check(manifest, "Linux") == {"obs": ("32.2.2", "32.2.2")}
    obs_install.record_installed_sha256("obs", {"sha256": "X"})
    assert components.check(manifest, "Linux") == {}
    manifest["linux"]["obs"]["sha256"] = "y"
    assert components.check(manifest, "Linux") == {"obs": ("32.2.2", "32.2.2")}
    manifest["linux"]["obs"]["url"] = "https://x.invalid/linux-obs-install-32.0.0.tar.gz"
    assert components.check(manifest, "Linux") == {}  # never a downgrade
    assert components.describe({"obs": ("32.2.2", "32.2.2")}) == ["OBS 32.2.2 (new build)"]


def test_windows_obs_ignores_the_build_hash(app, monkeypatch):
    monkeypatch.setattr(components, "installed_python_version", lambda system: None)
    monkeypatch.setattr(components, "outdated_package_sets", lambda system: [])
    monkeypatch.setattr(components, "installed_obs_version", lambda system: "32.2.2")
    monkeypatch.setattr(obs_install, "obs_exe", lambda system: __file__)
    manifest = _manifest(windows_obs="https://x.invalid/OBS-Studio-32.2.2-Windows-x64.zip")
    assert components.check(manifest, "Windows") == {}


# --- Installing OBS ---------------------------------------------------------

def test_linux_obs_update_replaces_obs_and_never_touches_obs_config(app):
    app_dir, archives, _downloads = app
    linux = app_dir / "Linux"
    old = linux / "obs-install"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "obs").write_text("old obs")
    (old / "lib").mkdir()
    (old / "lib" / "old-plugin.so").write_text("gone in the new OBS")
    (old / "config").mkdir()
    (old / "config" / "obs-studio").symlink_to(app_dir / "Shared" / "OBS-Config")
    archives["linux-obs-install-32.3.0.tar.gz"] = lambda dest: _tar(dest, {
        "obs-install/bin/obs": b"new obs", "obs-install/lib/libobs.so": b"lib",
    })
    manifest = _manifest(linux_obs="https://x.invalid/linux-obs-install-32.3.0.tar.gz")

    components.install_obs(manifest, "Linux", log=lambda message: None)

    assert (old / "bin" / "obs").read_text() == "new obs"
    assert os.access(old / "bin" / "obs", os.X_OK)
    assert not (old / "lib" / "old-plugin.so").exists()
    assert (app_dir / "Shared" / "OBS-Config" / "global.ini").read_text() == "user settings"
    assert sorted(p.name for p in linux.iterdir()) == ["obs-install"]  # no staging/backup left
    assert json.loads((app_dir / "installed_versions.json").read_text()) == {"obs": "32.3.0", "obs_sha256": "x"}


def test_windows_obs_update_keeps_the_vc_runtime_and_links(app):
    app_dir, archives, _downloads = app
    windows = app_dir / "Windows"
    (windows / "bin" / "64bit").mkdir(parents=True)
    (windows / "bin" / "64bit" / "obs64.exe").write_text("old obs")
    (windows / "bin" / "64bit" / "msvcp140.dll").write_text("vc runtime")
    (windows / "obs-plugins" / "64bit").mkdir(parents=True)
    (windows / "obs-plugins" / "64bit" / "old-plugin.dll").write_text("gone in the new OBS")
    (windows / "config").mkdir()
    (windows / "config" / "obs-studio").symlink_to(app_dir / "Shared" / "OBS-Config")
    (windows / "Python").mkdir()
    archives["OBS-Studio-32.3.0-Windows-x64.zip"] = lambda dest: _zip(dest, {
        "bin/64bit/obs64.exe": "new obs", "bin/64bit/obs64.pdb": "debug symbols",
        "data/libobs/default.effect": "fx", "obs-plugins/64bit/obs-ffmpeg.dll": "plugin",
    })
    manifest = _manifest(windows_obs="https://x.invalid/OBS-Studio-32.3.0-Windows-x64.zip")

    components.install_obs(manifest, "Windows", log=lambda message: None)

    assert (windows / "bin" / "64bit" / "obs64.exe").read_text() == "new obs"
    assert (windows / "bin" / "64bit" / "msvcp140.dll").read_text() == "vc runtime"
    assert not (windows / "bin" / "64bit" / "obs64.pdb").exists()
    assert not (windows / "obs-plugins" / "64bit" / "old-plugin.dll").exists()
    assert (windows / "config" / "obs-studio" / "global.ini").read_text() == "user settings"
    assert (windows / "Python").is_dir()
    assert sorted(p.name for p in windows.iterdir()) == ["Python", "bin", "config", "data", "obs-plugins"]


def test_failed_obs_download_keeps_the_installed_obs(app):
    app_dir, _archives, _downloads = app
    exe = app_dir / "Linux" / "obs-install" / "bin" / "obs"
    exe.parent.mkdir(parents=True)
    exe.write_text("old obs")
    manifest = _manifest(linux_obs="https://x.invalid/linux-obs-install-32.3.0.tar.gz")
    with pytest.raises(updater.UpdateError):
        components.install_obs(manifest, "Linux", log=lambda message: None)
    assert exe.read_text() == "old obs"
    assert sorted(p.name for p in (app_dir / "Linux").iterdir()) == ["obs-install"]


# --- ffmpeg ------------------------------------------------------------------

def test_ffmpeg_is_outdated_when_its_build_hash_changes(app, monkeypatch):
    app_dir, _archives, _downloads = app
    monkeypatch.setattr(components, "installed_obs_version", lambda system: None)
    monkeypatch.setattr(components, "installed_python_version", lambda system: None)
    monkeypatch.setattr(components, "outdated_package_sets", lambda system: [])
    manifest = _manifest(linux_ffmpeg="https://x.invalid/ffmpeg-9.0.2-r2-linux-x86_64.tar.gz")
    assert components.check(manifest, "Linux") == {}  # not installed - nothing to update
    exe = app_dir / "Linux" / "ffmpeg" / "bin" / "ffmpeg"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert components.check(manifest, "Linux") == {"ffmpeg": (None, "ffmpeg-9.0.2-r2-linux-x86_64")}
    obs_install.record_installed_sha256("ffmpeg", {"sha256": "X"})
    assert components.check(manifest, "Linux") == {}
    assert components.describe({"ffmpeg": (None, "ffmpeg-9.0.2-r2-linux-x86_64")}) == [
        "ffmpeg (new build: ffmpeg-9.0.2-r2-linux-x86_64)",
    ]


def test_ffmpeg_update_replaces_the_build(app):
    app_dir, archives, _downloads = app
    exe = app_dir / "Linux" / "ffmpeg" / "bin" / "ffmpeg"
    exe.parent.mkdir(parents=True)
    exe.write_text("old ffmpeg")
    (app_dir / "Linux" / "obs-install").mkdir()
    archives["ffmpeg-9.0.2-r2-linux-x86_64.tar.gz"] = lambda dest: _tar(dest, {
        "ffmpeg/bin/ffmpeg": b"new ffmpeg", "ffmpeg/LICENSE.txt": b"GPL",
    })
    manifest = _manifest(linux_ffmpeg="https://x.invalid/ffmpeg-9.0.2-r2-linux-x86_64.tar.gz")
    components.install_ffmpeg(manifest, "Linux", log=lambda message: None)
    assert exe.read_text() == "new ffmpeg" and os.access(exe, os.X_OK)
    assert sorted(p.name for p in (app_dir / "Linux").iterdir()) == ["ffmpeg", "obs-install"]
    assert obs_install.installed_sha256("ffmpeg") == "x"


def test_failed_ffmpeg_download_keeps_the_installed_ffmpeg(app):
    app_dir, _archives, _downloads = app
    exe = app_dir / "Linux" / "ffmpeg" / "bin" / "ffmpeg"
    exe.parent.mkdir(parents=True)
    exe.write_text("old ffmpeg")
    manifest = _manifest(linux_ffmpeg="https://x.invalid/ffmpeg-9.0.2-r2-linux-x86_64.tar.gz")
    with pytest.raises(updater.UpdateError):
        components.install_ffmpeg(manifest, "Linux", log=lambda message: None)
    assert exe.read_text() == "old ffmpeg"
    assert sorted(p.name for p in (app_dir / "Linux").iterdir()) == ["ffmpeg"]


# --- Installing Python and packages ----------------------------------------

def _fake_pip(installed):
    """Stands in for pip: "installs" the pinned versions as dist-info."""
    def pip_install(python, requirements, log, target=None):
        installed.append((python, os.path.basename(os.path.dirname(requirements)), target))
        if target:
            _install_pins(target, requirements, "Linux", "3.12.14")
    return pip_install


def test_linux_python_update_rebuilds_the_tray_packages_for_it(app, monkeypatch):
    app_dir, archives, _downloads = app
    linux = app_dir / "Linux"
    (linux / "python" / "bin").mkdir(parents=True)
    (linux / "python" / "bin" / "python3").write_text("old python")
    deps = linux / "tray-deps"
    _install_pins(str(deps), components.TRAY_REQUIREMENTS, "Linux", "3.12.14", {"pillow": "1.0.0"})
    pip_calls = []
    monkeypatch.setattr(components, "_pip_install", _fake_pip(pip_calls))
    archives["cpython-3.12.15+20270101-install_only.tar.gz"] = lambda dest: _tar(dest, {
        "python/bin/python3": b"new python",
    })
    manifest = _manifest(linux_python="https://x.invalid/cpython-3.12.15+20270101-install_only.tar.gz")

    components.install_linux_python(manifest, log=lambda message: None)

    assert (linux / "python" / "bin" / "python3").read_text() == "new python"
    assert pip_calls[0][0].endswith(os.path.join(".python.update-staging", "python", "bin", "python3"))
    assert not list(deps.glob("pillow-1.0.0*"))
    pin = (deps / components.TRAY_PYTHON_PIN_FILENAME).read_text()
    assert pin == os.path.realpath(linux / "python" / "bin" / "python3")
    assert sorted(p.name for p in linux.iterdir()) == ["python", "tray-deps"]


def test_failed_linux_package_update_keeps_the_old_packages(app, monkeypatch):
    app_dir, _archives, _downloads = app
    deps = app_dir / "Linux" / "tray-deps"
    _install_pins(str(deps), components.TRAY_REQUIREMENTS, "Linux", "3.12.14", {"pillow": "1.0.0"})
    before = sorted(p.name for p in deps.iterdir())

    def pip_fails(*args, **kwargs):
        raise RuntimeError("pip install failed (no network)")

    monkeypatch.setattr(components, "_pip_install", pip_fails)
    with pytest.raises(RuntimeError):
        components.install_packages("Linux", [components.TRAY_REQUIREMENTS], log=lambda message: None)
    assert sorted(p.name for p in deps.iterdir()) == before
    assert sorted(p.name for p in (app_dir / "Linux").iterdir()) == ["tray-deps"]

    monkeypatch.setattr(components, "_pip_install", _fake_pip([]))
    components.install_packages("Linux", [components.TRAY_REQUIREMENTS], log=lambda message: None)
    assert not list(deps.glob("pillow-1.0.0*")) and list(deps.glob("pillow-*"))


def test_install_orders_and_reports_what_the_caller_must_do(monkeypatch):
    calls = []
    monkeypatch.setattr(components, "_system", lambda: "Linux")
    monkeypatch.setattr(components, "install_obs", lambda m, s, log: calls.append("obs"))
    monkeypatch.setattr(components, "install_ffmpeg", lambda m, s, log: calls.append("ffmpeg"))
    monkeypatch.setattr(components, "install_linux_python", lambda m, log: calls.append("python"))
    monkeypatch.setattr(components, "install_packages", lambda s, sets, log: calls.append("packages"))
    plan = {"obs": ("1.0.0", "2.0.0"), "ffmpeg": (None, "f"), "python": ("3.12.0", "3.12.1"), "packages": ["x"]}
    assert components.install({}, plan, log=print) == components.RESTART
    assert calls == ["obs", "ffmpeg", "python"]  # the Python update reinstalls the packages itself
    calls.clear()
    assert components.install({}, {"packages": ["x"]}, log=print) == components.CONTINUE
    assert calls == ["packages"]
