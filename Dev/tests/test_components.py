"""components.py: checking and installing OBS, the bundled Python and the
tray's packages, with real archives in a temporary App/ - only downloads
and pip are faked."""

import io
import json
import os
import shutil
import tarfile
import types
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
    windows = components.requirement_pins(tray, "Windows", "3.12.16")
    linux = components.requirement_pins(tray, "Linux", "3.12.16")
    assert "pystray" in windows and "dbus-next" not in windows
    assert "dbus-next" in linux and "pystray" not in linux
    assert not any(name.startswith("pyobjc") for name in set(windows) | set(linux))
    assert {"pillow", "websocket-client"} <= set(windows) & set(linux)
    assert components.requirement_pins(mic, "Windows", "3.11.9")["numpy"].startswith("2.4.")
    assert components.requirement_pins(mic, "Windows", "3.12.16")["numpy"] != (
        components.requirement_pins(mic, "Windows", "3.11.9")["numpy"]
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
    exe.write_text("")
    answer = types.SimpleNamespace(stdout="OBS Studio - 32.2.2-modified\n")
    monkeypatch.setattr(components.subprocess, "run", lambda *_a, **_k: answer)
    assert components.installed_obs_version("Linux") == "32.2.2"
    answer.stdout = ""
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


# --- Editor window ------------------------------------------------------------

ELECTRON_URL = "https://x.invalid/electron-v44.5.1-linux-x64.zip"


def _electron_zip(dest, version="new"):
    with zipfile.ZipFile(dest, "w") as zf:
        info = zipfile.ZipInfo("electron")
        info.external_attr = 0o755 << 16
        zf.writestr(info, version)
        zf.writestr("resources/default_app.asar", "asar")
        zf.writestr("LICENSE", "MIT")


def test_editor_window_is_installed_when_missing_and_updated_when_its_hash_changes(app, monkeypatch):
    app_dir, _archives, _downloads = app
    monkeypatch.setattr(components.editor_window, "APP_DIR", str(app_dir))
    monkeypatch.setattr(components, "installed_obs_version", lambda system: None)
    monkeypatch.setattr(components, "installed_python_version", lambda system: None)
    monkeypatch.setattr(components, "outdated_package_sets", lambda system: [])
    manifest = _manifest(linux_editor_window=ELECTRON_URL)
    assert components.check(manifest, "Linux") == {"editor_window": (None, "44.5.1")}
    exe = app_dir / "Linux" / "editor-window" / "clipem-editor"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    obs_install.record_installed_sha256("editor_window", {"sha256": "old"})
    assert components.check(manifest, "Linux") == {"editor_window": ("old", "44.5.1")}
    obs_install.record_installed_sha256("editor_window", {"sha256": "X"})
    assert components.check(manifest, "Linux") == {}
    assert components.describe({"editor_window": (None, "44.5.1")}) == ["Clip editor window (new)"]


def test_editor_window_update_replaces_the_runtime(app, monkeypatch):
    app_dir, archives, _downloads = app
    monkeypatch.setattr(components.editor_window, "APP_DIR", str(app_dir))
    old = app_dir / "Linux" / "editor-window" / "clipem-editor"
    old.parent.mkdir(parents=True)
    old.write_text("old")
    archives["electron-v44.5.1-linux-x64.zip"] = _electron_zip
    components.install_editor_window(_manifest(linux_editor_window=ELECTRON_URL), "Linux", log=lambda m: None)
    assert old.read_text() == "new" and os.access(old, os.X_OK)
    assert not (old.parent / "electron").exists() and (old.parent / "resources" / "default_app.asar").exists()
    assert sorted(p.name for p in (app_dir / "Linux").iterdir()) == ["editor-window"]
    assert obs_install.installed_sha256("editor_window") == "x"


def test_failed_editor_window_download_keeps_the_installed_one(app, monkeypatch):
    app_dir, _archives, _downloads = app
    monkeypatch.setattr(components.editor_window, "APP_DIR", str(app_dir))
    old = app_dir / "Linux" / "editor-window" / "clipem-editor"
    old.parent.mkdir(parents=True)
    old.write_text("old")
    with pytest.raises(updater.UpdateError):
        components.install_editor_window(_manifest(linux_editor_window=ELECTRON_URL), "Linux", log=lambda m: None)
    assert old.read_text() == "old"
    assert sorted(p.name for p in (app_dir / "Linux").iterdir()) == ["editor-window"]


def test_editor_window_package_with_unsafe_paths_is_refused(tmp_path):
    archive = tmp_path / "e.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil", "x")
    with pytest.raises(components.editor_window.ExtractError):
        components.editor_window.extract(str(archive), str(tmp_path / "out"), "Linux")
    assert not (tmp_path / "evil").exists()


def test_editor_window_manifest_keys():
    key = components.editor_window.manifest_key
    assert key("Linux") == ("linux", "editor_window")
    assert key("Windows") == ("windows", "editor_window")
    assert key("Darwin", "arm64") == ("macos", "editor_window_arm64")
    assert key("Darwin", "x86_64") == ("macos", "editor_window_x86_64")


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


PBS_WINDOWS = "cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only.tar.gz"


def test_windows_python_update_stages_an_isolated_pbs_python(app, monkeypatch):
    """An embeddable 3.10 install's Python is replaced by PBS's: python\\'s
    contents become the staged folder (no .pdb), it gets a ._pth, and every
    package set the old one had is installed into it."""
    app_dir, archives, _downloads = app
    old = app_dir / "Windows" / "Python"
    old.mkdir(parents=True)
    (old / "python.exe").write_text("old python")
    (old / "python310._pth").write_text("python310.zip\n.\nimport site\n")
    _install_pins(str(old / "Lib" / "site-packages"), components.TRAY_REQUIREMENTS, "Windows", "3.10.11")
    pip_calls = []
    monkeypatch.setattr(components, "_pip_install", lambda python, requirements, log: pip_calls.append(
        (python, os.path.basename(os.path.dirname(requirements)))))
    archives[PBS_WINDOWS] = lambda dest: _tar(dest, {
        "python/python.exe": b"new python", "python/python312.pdb": b"symbols",
        "python/DLLs/_tkinter.pyd": b"tk", "python/tcl/tcl8.6/init.tcl": b"",
    })
    staging = components.stage_windows_python(_manifest(windows_python=f"https://x.invalid/{PBS_WINDOWS}"),
                                               log=lambda message: None)

    assert os.path.basename(staging) == ".python" + components.STAGING_SUFFIX
    assert sorted(os.listdir(staging)) == ["DLLs", "python.exe", "python312._pth", "tcl"]
    with open(os.path.join(staging, "python312._pth"), encoding="utf-8") as f:
        assert f.read().splitlines() == [".", "Lib", "DLLs", "import site"]
    assert pip_calls == [(os.path.join(staging, "python.exe"), "tray-widget")]
    assert sorted(p.name for p in (app_dir / "Windows").iterdir()) == [".python.update-staging", "Python"]


def test_windows_python_update_refuses_an_unexpected_layout(app, monkeypatch):
    app_dir, archives, _downloads = app
    (app_dir / "Windows").mkdir()
    archives[PBS_WINDOWS] = lambda dest: _tar(dest, {"python.exe": b"not in python/"})
    with pytest.raises(updater.UpdateError):
        components.stage_windows_python(_manifest(windows_python=f"https://x.invalid/{PBS_WINDOWS}"),
                                        log=lambda message: None)
    assert list((app_dir / "Windows").iterdir()) == []


def test_tar_extraction_without_filters_still_refuses_escapes(tmp_path, monkeypatch):
    """Python 3.10.11 (old Windows installs) has no tarfile.data_filter."""
    monkeypatch.setattr(obs_install, "tarfile", types.SimpleNamespace(open=tarfile.open))
    good = tmp_path / "good.tar.gz"
    _tar(good, {"python/python.exe": b"py", "python/python.pdb": b"symbols"})
    obs_install._extract(str(good), str(tmp_path / "out"))
    assert sorted(os.listdir(tmp_path / "out" / "python")) == ["python.exe"]
    bad = tmp_path / "bad.tar.gz"
    _tar(bad, {"../escape.txt": b"x"})
    with pytest.raises(updater.UpdateError):
        obs_install._extract(str(bad), str(tmp_path / "out2"))
    assert not (tmp_path / "escape.txt").exists()


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


@pytest.mark.parametrize("platform, system", [("win32", "Windows"), ("linux", "Linux"), ("darwin", None)])
def test_only_windows_and_linux_are_managed(monkeypatch, platform, system):
    monkeypatch.setattr(components.sys, "platform", platform)
    assert components._system() == system


def test_macos_has_nothing_to_check_or_install(monkeypatch):
    monkeypatch.setattr(components, "_system", lambda: None)
    assert components.check({"linux": {"obs": {}}}) == {}
    assert components.install({}, {"obs": ("1", "2")}, print) == components.CONTINUE


def test_no_plan_installs_nothing(monkeypatch):
    monkeypatch.setattr(components, "_system", lambda: "Linux")
    monkeypatch.setattr(components, "install_obs", lambda *a: pytest.fail("nothing planned"))
    assert components.install({}, {}, print) == components.CONTINUE


def test_python_exe_needs_the_python_to_be_there(app):
    app_dir, _, _ = app
    assert components.python_exe("Linux") is None and components.python_exe("Windows") is None
    assert components.installed_python_version("Linux") is None
    python = app_dir / "Linux" / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("")
    assert components.python_exe("Linux") == str(python)
    nested = app_dir / "Windows" / "Python" / "python-3.10" / "python.exe"
    nested.parent.mkdir(parents=True)
    nested.write_text("")
    assert components.python_exe("Windows") == str(nested)


def test_this_python_answers_for_itself(monkeypatch):
    monkeypatch.setattr(components.subprocess, "run", lambda *a, **k: pytest.fail("no need to start it"))
    assert components._query_python_version(components.sys.executable) == components.platform.python_version()


@pytest.mark.parametrize("result, version", [
    (types.SimpleNamespace(returncode=0, stdout="3.12.14\n"), "3.12.14"),
    (types.SimpleNamespace(returncode=1, stdout="garbage"), None),
])
def test_another_python_is_asked(monkeypatch, tmp_path, result, version):
    monkeypatch.setattr(components.subprocess, "run", lambda *a, **k: result)
    assert components._query_python_version(str(tmp_path / "python")) == version


def test_a_python_that_wont_run_has_no_version(monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise OSError("not executable")

    monkeypatch.setattr(components.subprocess, "run", fail)
    assert components._query_python_version(str(tmp_path / "python")) is None


def _obs_exe(system="Linux"):
    exe = obs_install.obs_exe(system)
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "w") as f:
        f.write("")
    return exe


def test_no_obs_has_no_version(app):
    assert components.installed_obs_version("Linux") is None


@pytest.mark.parametrize("stdout, version", [("OBS Studio - 32.2.2-modified\n", "32.2.2"), ("no version here", None)])
def test_linux_asks_obs_its_version(app, monkeypatch, stdout, version):
    _obs_exe()
    monkeypatch.setattr(components.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=stdout))
    assert components.installed_obs_version("Linux") == version


def test_an_obs_that_wont_answer_uses_the_recorded_version(app, monkeypatch):
    app_dir, _, _ = app
    _obs_exe()

    def fail(*_args, **_kwargs):
        raise components.subprocess.TimeoutExpired("obs", 30)

    (app_dir / "installed_versions.json").write_text('{"obs": "31.0.0"}')
    monkeypatch.setattr(components.subprocess, "run", fail)
    assert components.installed_obs_version("Linux") == "31.0.0"


def test_the_tray_python_is_the_pinned_one_while_it_exists(app, monkeypatch, tmp_path):
    deps = tmp_path / "tray-deps"
    deps.mkdir()
    monkeypatch.setattr(components, "_linux_tray_deps_dir", lambda: str(deps))
    assert components._linux_tray_python() == components.sys.executable  # no pin, no bundled Python
    pinned = tmp_path / "python3"
    pinned.write_text("")
    (deps / components.TRAY_PYTHON_PIN_FILENAME).write_text(f"{pinned}\n")
    assert components._linux_tray_python() == str(pinned)
    pinned.unlink()
    assert components._linux_tray_python() == components.sys.executable


def test_a_rebuilt_obs_is_never_a_downgrade(app, monkeypatch):
    app_dir, _, _ = app
    _obs_exe()
    (app_dir / "installed_versions.json").write_text('{"obs_sha256": "old"}')
    monkeypatch.setattr(components, "installed_obs_version", lambda system: "33.0.0")
    monkeypatch.setattr(components, "installed_python_version", lambda system: None)
    monkeypatch.setattr(components, "outdated_package_sets", lambda system: [])
    manifest = _manifest(linux_obs="https://x/linux-obs-install-32.2.2.tar.gz")
    assert "obs" not in components.check(manifest, "Linux")


def test_a_package_that_says_it_is_ffmpeg_but_has_no_binary_is_refused(app):
    app_dir, archives, _ = app
    archives["ffmpeg.tar.gz"] = lambda dest: _tar(dest, {"ffmpeg/readme": b"x"})
    with pytest.raises(updater.UpdateError, match="has no"):
        components.install_ffmpeg(_manifest(linux_ffmpeg="https://x/ffmpeg.tar.gz"), "Linux", lambda m: None)
    assert sorted(n for n in os.listdir(app_dir / "Linux") if n.startswith(".")) == []


def test_a_ffmpeg_package_with_anything_extra_is_refused(app):
    _app_dir, archives, _ = app
    archives["ffmpeg.tar.gz"] = lambda dest: _tar(dest, {"ffmpeg/bin/ffmpeg": b"x", "evil/file": b"x"})
    with pytest.raises(updater.UpdateError, match="unexpected entries"):
        components.install_ffmpeg(_manifest(linux_ffmpeg="https://x/ffmpeg.tar.gz"), "Linux", lambda m: None)


def test_windows_ffmpeg_is_a_zip_checked_like_an_update(app):
    app_dir, archives, _ = app
    (app_dir / "Windows").mkdir()
    archives["ffmpeg.zip"] = lambda dest: _zip(dest, {"elsewhere/ffmpeg.exe": "x"})
    with pytest.raises(updater.UpdateError, match="unsafe path"):
        components.install_ffmpeg(_manifest(windows_ffmpeg="https://x/ffmpeg.zip"), "Windows", lambda m: None)
    archives["ffmpeg.zip"] = lambda dest: _zip(dest, {"ffmpeg/bin/ffmpeg.exe": "new"})
    components.install_ffmpeg(_manifest(windows_ffmpeg="https://x/ffmpeg.zip"), "Windows", lambda m: None)
    assert (app_dir / "Windows" / "ffmpeg" / "bin" / "ffmpeg.exe").read_text() == "new"


def test_an_obs_package_without_obs_keeps_the_installed_one(app):
    app_dir, archives, _ = app
    exe = _obs_exe()
    with open(exe, "w") as f:
        f.write("installed")
    archives["obs.tar.gz"] = lambda dest: _tar(dest, {"obs-install/readme": b"x"})
    with pytest.raises(updater.UpdateError, match="has no"):
        components.install_obs(_manifest(linux_obs="https://x/obs.tar.gz"), "Linux", lambda m: None)
    with open(exe) as f:
        assert f.read() == "installed"
    assert not [n for n in os.listdir(app_dir / "Linux") if n.startswith(".obs")]


def test_an_obs_package_with_anything_extra_is_refused(app):
    _app_dir, archives, _ = app
    archives["obs.tar.gz"] = lambda dest: _tar(dest, {"obs-install/bin/obs": b"x", "evil/file": b"x"})
    with pytest.raises(updater.UpdateError, match="unexpected folders: evil"):
        components.install_obs(_manifest(linux_obs="https://x/obs.tar.gz"), "Linux", lambda m: None)


@pytest.mark.usefixtures("links")
def test_deleting_a_folder_never_follows_its_links(tmp_path):
    keep = tmp_path / "OBS-Config"
    keep.mkdir()
    (keep / "user.ini").write_text("mine")
    folder = tmp_path / "obs-install"
    folder.mkdir()
    (folder / "config").symlink_to(keep)
    components._clean(str(folder))
    assert not folder.exists() and (keep / "user.ini").read_text() == "mine"


def test_pip_is_called_how_it_needs_to_be(monkeypatch):
    calls = []
    monkeypatch.setattr(components, "install_requirements", lambda args, requirements: calls.append((args, requirements)))
    requirements = os.path.join(components.SHARED_DIR, "r.txt")
    components._pip_install("/python", requirements, lambda m: None, target="/deps", force=True)
    args, _requirements = calls[0]
    assert args[:4] == ["/python", "-m", "pip", "install"]
    assert "--force-reinstall" in args and args[args.index("--target") + 1] == "/deps"
    components._pip_install("/python", requirements, lambda m: None)
    assert "--force-reinstall" not in calls[1][0] and "--target" not in calls[1][0]


def test_windows_python_is_handed_to_a_script_once_clipem_exits(app, monkeypatch):
    app_dir, _, _ = app
    (app_dir / "Windows").mkdir()
    started = []
    monkeypatch.setattr(components, "LOG_DIR", str(app_dir / "logs"))
    monkeypatch.setattr(components.subprocess, "CREATE_NO_WINDOW", 1, raising=False)
    monkeypatch.setattr(components.subprocess, "CREATE_NEW_PROCESS_GROUP", 2, raising=False)
    monkeypatch.setattr(components.subprocess, "Popen", lambda args, **kwargs: started.append((args, kwargs)))
    components.start_windows_python_swap(["--boot", "--post-update"])
    args, kwargs = started[0]
    script = app_dir / "Windows" / ".python-update.ps1"
    assert script.read_bytes().startswith(b"\xef\xbb\xbf")  # PowerShell needs the BOM to read UTF-8
    assert args[args.index("-File") + 1] == str(script)
    assert args[args.index("-WaitPid") + 1] == str(os.getpid())
    assert args[args.index("-RunArgs") + 1].split("|")[1:] == ["--boot", "--post-update"]
    assert kwargs["creationflags"] == 3
