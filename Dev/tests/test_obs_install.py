"""obs_install.install_obs() for Windows and Linux with real archives -
only the verified download itself is faked."""

import io
import json
import os
import tarfile
import types
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
    exe = obs_install.install_obs("Linux", log=lambda m: None)
    assert obs_install.install_obs("Linux", log=lambda m: None) == exe
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
    assert list(recorded) == ["Windows/bin/64bit/obs64.exe", "Windows/obs-plugins/64bit/p.dll"]
    assert obs_install.remove_debug_files(str(windows), log=lambda m: None) == 0


def test_a_download_is_only_trusted_through_the_signed_manifest(monkeypatch, tmp_path):
    manifest = {"version": "1", "linux": {"obs": {"url": "https://x/obs.tar.gz", "sha256": "abc"}}}
    downloads = []
    monkeypatch.setattr(obs_install.updater, "fetch_manifest", lambda: manifest)
    monkeypatch.setattr(obs_install.updater, "download_file",
                        lambda url, dest, sha256, timeout: downloads.append((url, dest, sha256, timeout)))
    entry = obs_install.fetch_verified("linux", "obs", "dest", log=lambda m: None)
    assert entry == manifest["linux"]["obs"]
    assert downloads == [("https://x/obs.tar.gz", "dest", "abc", 300)]  # OBS is big: not the short default


def test_an_artifact_missing_from_the_manifest_is_never_downloaded(monkeypatch):
    monkeypatch.setattr(obs_install.updater, "fetch_manifest", lambda: {"version": "1"})
    monkeypatch.setattr(obs_install.updater, "download_file", lambda *a, **k: pytest.fail("nothing to fetch"))
    with pytest.raises(obs_install.updater.UpdateError):
        obs_install.fetch_verified("linux", "obs", "dest", log=lambda m: None)


def test_unsupported_platforms_are_refused(app):
    assert not obs_install.supported("Darwin")
    with pytest.raises(obs_install.ObsInstallError, match="can't install OBS on Darwin"):
        obs_install.install_obs("Darwin", log=lambda m: None)


def test_an_installed_obs_is_recorded_for_the_update_check(app):
    app_dir, _, _ = app
    (app_dir / "installed_versions.json").write_text('{"ffmpeg": "9"}')
    obs_install.install_obs("Linux", log=lambda m: None)
    versions = json.loads((app_dir / "installed_versions.json").read_text())
    assert versions == {"ffmpeg": "9", "obs": "32.2.2", "obs_sha256": "x"}
    assert obs_install.installed_sha256("obs") == "x"


def test_the_linux_exe_is_made_runnable(app, monkeypatch):
    modes = []
    monkeypatch.setattr(obs_install.os, "chmod", lambda path, mode: modes.append((path, mode)))
    obs_install.install_obs("Linux", log=lambda m: None)
    assert (obs_install.obs_exe("Linux"), 0o755) in modes


def test_a_leftover_staging_folder_doesnt_end_up_installed(app):
    app_dir, _, _ = app
    staging = app_dir / "Linux" / obs_install.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    (staging / "from-a-crash").write_text("old")
    obs_install.install_obs("Linux", log=lambda m: None)
    assert not (app_dir / "Linux" / "from-a-crash").exists() and not staging.exists()


def test_windows_warns_about_a_missing_vc_runtime_after_installing(app, monkeypatch):
    monkeypatch.setenv("SystemRoot", "/nonexistent")
    logs = []
    obs_install.install_obs("Windows", log=logs.append)
    assert any("msvcp140.dll not found" in line for line in logs)


@pytest.mark.parametrize("beside_obs, in_system32, present", [
    (("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"), (), True),
    ((), ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"), True),
    (("msvcp140.dll",), ("vcruntime140.dll", "vcruntime140_1.dll"), True),
    (("msvcp140.dll", "vcruntime140.dll"), (), False),
])
def test_the_vc_runtime_counts_beside_obs_or_in_system32(tmp_path, monkeypatch, beside_obs, in_system32, present):
    bin_dir = tmp_path / "bin" / "64bit"
    system32 = tmp_path / "windows" / "System32"
    bin_dir.mkdir(parents=True)
    system32.mkdir(parents=True)
    for folder, names in ((bin_dir, beside_obs), (system32, in_system32)):
        for name in names:
            (folder / name).write_text("")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "windows"))
    logs = []
    assert obs_install.warn_if_windows_vc_runtime_missing(str(tmp_path), log=logs.append) is present
    assert bool(logs) is not present


def test_a_missing_vc_runtime_goes_to_stderr_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    obs_install.warn_if_windows_vc_runtime_missing(str(tmp_path))
    assert "not found" in capsys.readouterr().err


def _tar(path, members):
    """members: (name, "file" or "link", its data or link target)."""
    with tarfile.open(path, "w:gz") as tf:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            if kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, data
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return str(path)


def _without_extraction_filters(monkeypatch):
    monkeypatch.setattr(obs_install, "tarfile", types.SimpleNamespace(open=tarfile.open))


@pytest.mark.parametrize("filtered", [True, False], ids=["with extraction filters", "python 3.10.11"])
def test_a_plain_archive_is_extracted(tmp_path, monkeypatch, filtered):
    if not filtered:
        _without_extraction_filters(monkeypatch)
    archive = _tar(tmp_path / "a.tar.gz", [("dir/file", "file", b"data"), ("dir/obs.pdb", "file", b"debug")])
    obs_install._extract(archive, str(tmp_path / "out"))
    assert (tmp_path / "out" / "dir" / "file").read_bytes() == b"data"
    assert not (tmp_path / "out" / "dir" / "obs.pdb").exists()


@pytest.mark.parametrize("filtered", [True, False], ids=["with extraction filters", "python 3.10.11"])
@pytest.mark.parametrize("members", [
    [("../evil", "file", b"evil")],
    [("a/../../evil", "file", b"evil")],
    [("out-link", "link", "/etc/passwd")],
], ids=["parent folder", "nested parent folder", "absolute link"])
def test_an_archive_cant_write_outside_its_folder(tmp_path, monkeypatch, filtered, members):
    if not filtered:
        _without_extraction_filters(monkeypatch)
    archive = _tar(tmp_path / "a.tar.gz", members)
    # Without filters only ClipEm's own check stands in the way.
    with pytest.raises((obs_install.updater.UpdateError, tarfile.TarError) if filtered
                       else obs_install.updater.UpdateError):
        obs_install._extract(archive, str(tmp_path / "out"))
    assert not (tmp_path / "evil").exists()


def test_extraction_filters_are_used_where_python_has_them(tmp_path, monkeypatch):
    filters = []
    real_extractall = tarfile.TarFile.extractall

    def extractall(self, path, members=None, *, filter=None, **kwargs):
        filters.append(filter)
        return real_extractall(self, path, members=members, filter=filter, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)
    obs_install._extract(_tar(tmp_path / "a.tar.gz", [("file", "file", b"data")]), str(tmp_path / "out"))
    assert filters == ["data"]


def test_a_refused_archive_stops_the_install_cleanly(app, monkeypatch):
    app_dir, _, _ = app

    def fetch(platform_key, artifact, dest, log=print):
        _tar(dest, [("../../evil", "file", b"evil")])
        return {"url": "https://x/obs.tar.gz", "sha256": "x"}

    monkeypatch.setattr(obs_install, "fetch_verified", fetch)
    with pytest.raises(obs_install.ObsInstallError):
        obs_install.install_obs("Linux", log=lambda m: None)
    assert os.listdir(app_dir / "Linux") == []
    assert not (app_dir / "evil").exists()


@pytest.mark.usefixtures("links")
def test_local_checksums_are_merged_and_skip_links_and_missing_folders(app):
    app_dir, _, _ = app
    (app_dir / "checksums_local.json").write_text('{"other/file": "abc"}')
    folder = app_dir / "Linux" / "ffmpeg"
    folder.mkdir(parents=True)
    (folder / "real").write_text("x")
    (folder / "link").symlink_to(folder / "real")
    logs = []
    obs_install.record_local_checksums([str(folder), str(app_dir / "gone")], "ffmpeg", log=logs.append)
    recorded = json.loads((app_dir / "checksums_local.json").read_text())
    assert sorted(recorded) == ["Linux/ffmpeg/real", "other/file"]
    assert list(recorded) == sorted(recorded)
    assert recorded["other/file"] == "abc" and logs == ["Recorded checksums for 1 ffmpeg files (local baseline)."]


def test_recording_a_version_without_one_in_the_name_changes_nothing(app):
    app_dir, _, _ = app
    obs_install.record_installed_version("obs", {"url": "https://x/obs.zip"})
    assert not (app_dir / "installed_versions.json").exists()


def test_a_sha256_is_recorded_in_lower_case_and_a_broken_file_is_replaced(app):
    app_dir, _, _ = app
    (app_dir / "installed_versions.json").write_text("{broken")
    obs_install.record_installed_sha256("ffmpeg", {"sha256": "ABC"})
    assert obs_install.installed_sha256("ffmpeg") == "abc"
    assert obs_install.installed_sha256("obs") is None
    (app_dir / "installed_versions.json").write_text("null")
    assert obs_install.installed_sha256("ffmpeg") is None
    obs_install.record_installed_version("obs", {"url": "https://x/obs-1.2.3.zip"})
    assert json.loads((app_dir / "installed_versions.json").read_text()) == {"obs": "1.2.3"}


def test_a_debug_file_in_use_is_skipped(app, monkeypatch):
    app_dir, _, _ = app
    (app_dir / "Windows" / "bin").mkdir(parents=True)
    (app_dir / "Windows" / "bin" / "obs.pdb").write_text("x")

    def in_use(_path):
        raise PermissionError("in use")

    monkeypatch.setattr(obs_install.os, "remove", in_use)
    assert obs_install.remove_debug_files(str(app_dir / "Windows"), log=lambda m: pytest.fail("nothing went")) == 0
