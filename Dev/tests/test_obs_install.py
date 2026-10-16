"""obs_install.install_obs() for Windows and Linux with real archives -
only the verified download itself is faked."""

import io
import json
import os
import tarfile
import zipfile

import obs_install
import pytest
import setup


def _windows_zip(path, include_exe=True):
    with zipfile.ZipFile(path, "w") as zf:
        if include_exe:
            zf.writestr("bin/64bit/obs64.exe", "obs")
        zf.writestr("bin/64bit/obs.dll", "new dll")
        zf.writestr("bin/64bit/obs64.pdb", "debug symbols")
        zf.writestr("obs-plugins/64bit/obs-ffmpeg.PDB", "debug symbols")
        zf.writestr("data/libobs/default.effect", "fx")
        zf.writestr("obs-plugins/64bit/obs-ffmpeg.dll", "plugin")


def _linux_tar(path, include_exe=True):
    with tarfile.open(path, "w:gz") as tf:
        files = {"obs-install/lib/libobs.so": b"lib"}
        if include_exe:
            files["obs-install/bin/obs"] = b"obs"
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tf.addfile(info, io.BytesIO(data))


ARCHIVES = {"Windows": _windows_zip, "Linux": _linux_tar}


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = tmp_path / "App"
    app.mkdir()
    monkeypatch.setattr(obs_install, "APP_DIR", str(app))
    monkeypatch.setattr(obs_install, "LOCAL_CHECKSUMS_PATH", str(app / "checksums_local.json"))
    monkeypatch.setattr(obs_install, "INSTALLED_VERSIONS_PATH", str(app / "installed_versions.json"))
    fetched = []

    def serve(include_exe=True):
        def fetch(platform_key, artifact, dest, log=print):
            fetched.append((platform_key, artifact))
            system = "Windows" if platform_key == "windows" else "Linux"
            ARCHIVES[system](dest, include_exe)
            return {"url": f"https://example.invalid/obs-32.2.2-{platform_key}.zip", "sha256": "x"}

        monkeypatch.setattr(obs_install, "fetch_verified", fetch)

    serve()
    return app, fetched, serve


@pytest.mark.parametrize("system, key", [("Windows", "windows"), ("Linux", "linux")])
def test_installs_verified_obs_and_records_checksums(app, system, key):
    app_dir, fetched, _ = app
    exe = obs_install.install_obs(system, log=lambda m: None)

    assert exe == obs_install.obs_exe(system) and os.path.isfile(exe)
    assert fetched == [(key, "obs")]
    platform = app_dir / system
    assert not (platform / obs_install.STAGING_DIR_NAME).exists()
    assert not any(name.startswith("obs-download") for name in os.listdir(platform))
    recorded = json.loads((app_dir / "checksums_local.json").read_text())
    assert any(path.startswith(f"{system}/") for path in recorded)
    if system == "Linux":
        assert os.access(exe, os.X_OK)


def test_already_installed_is_not_fetched_again(app):
    _, fetched, _ = app
    obs_install.install_obs("Linux", log=lambda m: None)
    obs_install.install_obs("Linux", log=lambda m: None)
    assert fetched == [("linux", "obs")]


def test_windows_keeps_files_the_installer_put_there(app):
    """ClipEmSetupWindows.exe puts the VC++ runtime next to obs64.exe; the
    embeddable Python lives beside bin/. Neither is in OBS's zip."""
    app_dir, _, _ = app
    bin_dir = app_dir / "Windows" / "bin" / "64bit"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vcruntime140.dll").write_text("runtime")
    (bin_dir / "obs.dll").write_text("old dll")
    (app_dir / "Windows" / "Python").mkdir()
    (app_dir / "Windows" / "Python" / "python.exe").write_text("")

    obs_install.install_obs("Windows", log=lambda m: None)

    assert (bin_dir / "vcruntime140.dll").read_text() == "runtime"
    assert (bin_dir / "obs.dll").read_text() == "new dll"
    assert (app_dir / "Windows" / "Python" / "python.exe").exists()


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_package_without_obs_leaves_nothing_behind(app, system):
    app_dir, _, serve = app
    serve(include_exe=False)
    with pytest.raises(obs_install.ObsInstallError, match="has no"):
        obs_install.install_obs(system, log=lambda m: None)
    assert os.listdir(app_dir / system) == []


def test_failed_download_raises_install_error(app, monkeypatch):
    app_dir, _, _ = app

    def offline(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(obs_install, "fetch_verified", offline)
    with pytest.raises(obs_install.ObsInstallError, match="no network"):
        obs_install.install_obs("Linux", log=lambda m: None)
    assert not os.path.exists(obs_install.obs_exe("Linux"))


def test_exe_is_moved_last(tmp_path):
    """A move interrupted part way must not leave obs64.exe in place (its
    presence is what means "installed")."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    for rel in ("bin/64bit/obs64.exe", "bin/64bit/a.dll", "data/z.txt"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(rel)
    order = []
    real_replace = os.replace
    obs_install.os.replace = lambda a, b: (order.append(os.path.relpath(a, src)), real_replace(a, b))
    try:
        obs_install._merge_move(str(src), str(dest), last=os.path.join("bin", "64bit", "obs64.exe"))
    finally:
        obs_install.os.replace = real_replace
    assert order[-1] == os.path.join("bin", "64bit", "obs64.exe")
    assert len(order) == 3


def test_setup_linux_uses_the_bundled_obs_even_with_a_system_obs(app, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/obs")
    assert setup.setup_linux()
    assert os.path.isfile(obs_install.obs_exe("Linux"))


def test_setup_linux_stops_when_obs_cant_be_downloaded(app, monkeypatch):
    def offline(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(obs_install, "fetch_verified", offline)
    assert not setup.setup_linux()


def test_windows_debug_symbols_are_not_extracted(app):
    app_dir, _, _ = app
    obs_install.install_obs("Windows", log=lambda m: None)
    windows = app_dir / "Windows"
    assert not list(windows.rglob("*.pdb")) and not list(windows.rglob("*.PDB"))
    assert (windows / "obs-plugins" / "64bit" / "obs-ffmpeg.dll").exists()
    recorded = json.loads((app_dir / "checksums_local.json").read_text())
    assert not [path for path in recorded if path.lower().endswith(".pdb")]


def test_debug_symbols_from_older_installs_are_removed(app):
    app_dir, _, _ = app
    windows = app_dir / "Windows"
    for rel in ("bin/64bit/obs64.exe", "bin/64bit/obs64.pdb", "data/x.pdb", "obs-plugins/64bit/p.dll"):
        (windows / rel).parent.mkdir(parents=True, exist_ok=True)
        (windows / rel).write_text(rel)
    obs_install.record_local_checksums([str(windows / "bin"), str(windows / "data"), str(windows / "obs-plugins")],
                                       "Windows OBS", log=lambda m: None)

    assert obs_install.remove_debug_files(str(windows), log=lambda m: None) == 2

    assert not list(windows.rglob("*.pdb"))
    assert (windows / "bin" / "64bit" / "obs64.exe").exists()
    recorded = json.loads((app_dir / "checksums_local.json").read_text())
    assert sorted(recorded) == ["Windows/bin/64bit/obs64.exe", "Windows/obs-plugins/64bit/p.dll"]
    assert obs_install.remove_debug_files(str(windows), log=lambda m: None) == 0
