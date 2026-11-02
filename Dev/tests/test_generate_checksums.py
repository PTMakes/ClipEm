"""checksums.json - what Setup checks an install against. A wrong list here
fails every install, or misses a corrupted one."""

import json
import os
import subprocess
import sys

import pytest
import setup
import updater

import generate_checksums


def _tree(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _listed(root):
    manifest = root / "checksums.json"
    return sorted(os.path.relpath(p, root).replace(os.sep, "/")
                  for p in generate_checksums.iter_files(str(root), str(manifest)))


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(generate_checksums, "git_listed_files", lambda root: None)


FILES = {
    "VERSION": "1",
    "Installer/run.py": "",
    "Installer/__pycache__/run.pyc": "",
    "Installer/stale.pyc": "",
    ".DS_Store": "",
    "OBS-Config/basic.ini": "",
    "run_config.json": "",
    "update_config.json": "",
    "checksums.json": "{}",
}


def test_outside_git_the_walk_skips_caches_and_per_install_files(tmp_path, no_git):
    _tree(tmp_path, FILES)
    assert _listed(tmp_path) == ["Installer/run.py", "VERSION"]


def test_a_nested_folder_named_like_a_per_install_one_is_kept(tmp_path, no_git):
    """Only App/Shared's own OBS-Config is rewritten per install."""
    _tree(tmp_path, {"Installer/OBS-Config/x": "", "Installer/run_config.json": ""})
    assert _listed(tmp_path) == ["Installer/OBS-Config/x", "Installer/run_config.json"]


@pytest.mark.usefixtures("links")
def test_links_are_never_followed(tmp_path, no_git):
    _tree(tmp_path / "root", {"VERSION": "1"})
    _tree(tmp_path / "elsewhere", {"secret": "x"})
    (tmp_path / "root" / "linked-dir").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "root" / "linked-file").symlink_to(tmp_path / "elsewhere" / "secret")
    assert _listed(tmp_path / "root") == ["VERSION"]


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    _tree(root / "Shared", {"VERSION": "1", "Installer/run.py": "", "tracked_gone.py": "", "new_untracked.py": "",
                            "ignored.log": "", "OBS-Config/basic.ini": "", "Tools/keep.txt": ""})
    (root / ".gitignore").write_text("*.log\n*.exe\n*.so\n")
    _git(root, "init", "-q")
    _git(root, "add", ".gitignore", "Shared/VERSION", "Shared/Installer/run.py", "Shared/tracked_gone.py",
         "Shared/OBS-Config/basic.ini", "Shared/Tools/keep.txt")
    _git(root, "commit", "-qm", "x")
    (root / "Shared" / "tracked_gone.py").unlink()
    return root / "Shared"


def test_in_git_the_list_is_what_release_sh_zips(repo):
    """Tracked or new, not ignored, not deleted."""
    assert _listed(repo) == ["Installer/run.py", "Tools/keep.txt", "VERSION", "new_untracked.py"]


def test_built_files_are_listed_though_git_ignores_them(repo):
    _tree(repo, {"Tools/clipem-app-capture.exe": "built", "OBS-Plugin/linux/clipem-obs.so": "built"})
    assert "Tools/clipem-app-capture.exe" in _listed(repo)
    assert "OBS-Plugin/linux/clipem-obs.so" in _listed(repo)
    assert "OBS-Plugin/windows/clipem-obs.dll" not in _listed(repo)


def test_a_built_file_already_tracked_is_listed_once(repo):
    _tree(repo, {"Tools/clipem-app-capture.exe": "built"})
    _git(repo, "add", "-f", "Tools/clipem-app-capture.exe")
    assert generate_checksums.git_listed_files(str(repo)).count(str(repo / "Tools" / "clipem-app-capture.exe")) == 1


def test_outside_a_checkout_git_lists_nothing(tmp_path):
    assert generate_checksums.git_listed_files(str(tmp_path)) is None


def test_without_git_installed_nothing_is_listed(tmp_path, monkeypatch):
    def no_git_here(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git_here)
    assert generate_checksums.git_listed_files(str(tmp_path)) is None


def test_the_manifest_never_lists_itself_and_setup_accepts_it(tmp_path, monkeypatch, no_git):
    shared = tmp_path / "App" / "Shared"
    _tree(shared, {"VERSION": "1", "Installer/run.py": "print()", "OBS-Config/basic.ini": "mine"})
    (shared / "checksums.json").write_text("{}")
    generate_checksums.write_checksums(str(shared))
    manifest = json.loads((shared / "checksums.json").read_text())
    assert sorted(manifest) == ["Installer/run.py", "VERSION"] and list(manifest) == sorted(manifest)

    monkeypatch.setattr(setup, "APP_DIR", str(shared.parent))
    monkeypatch.setattr(setup, "SHARED_DIR", str(shared))
    monkeypatch.setattr(setup, "CHECKSUMS_MANIFEST_PATH", str(shared / "checksums.json"))
    monkeypatch.setattr(setup, "LOCAL_CHECKSUMS_PATH", str(shared.parent / "checksums_local.json"))
    assert setup.verify_checksums()[0] is True


def test_every_excluded_file_is_one_the_updater_preserves():
    """What's left out of the manifest must not be replaced by updates either."""
    assert generate_checksums.EXCLUDE_TOP_LEVEL <= updater.PRESERVED_RELATIVE_PATHS


def test_the_version_manifest_describes_the_zip(tmp_path):
    from clipem_common import sha256_of_file

    release = tmp_path / "shared.zip"
    release.write_bytes(b"zip")
    out = tmp_path / "clipem-latest.json"
    generate_checksums.write_version_manifest(str(release), "1.2.3", "https://x/shared.zip", str(out))
    assert json.loads(out.read_text()) == {"version": "1.2.3", "url": "https://x/shared.zip",
                                           "sha256": sha256_of_file(release)}


def _main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["generate_checksums.py", *args])
    generate_checksums.main()


def test_the_cli_writes_checksums_by_default(tmp_path, monkeypatch, no_git):
    _tree(tmp_path, {"VERSION": "1"})
    _main(monkeypatch, str(tmp_path))
    assert "VERSION" in json.loads((tmp_path / "checksums.json").read_text())


def test_the_cli_can_leave_checksums_alone(tmp_path, monkeypatch, no_git):
    _main(monkeypatch, str(tmp_path), "--skip-checksums")
    assert not (tmp_path / "checksums.json").exists()


def test_the_version_manifest_needs_all_its_arguments(tmp_path, monkeypatch, no_git):
    with pytest.raises(SystemExit, match="must be given together"):
        _main(monkeypatch, str(tmp_path), "--skip-checksums", "--version", "1.2.3")


def test_the_version_manifest_defaults_to_the_github_asset(tmp_path, monkeypatch, no_git):
    release = tmp_path / "shared.zip"
    release.write_bytes(b"zip")
    out = tmp_path / "latest.json"
    _main(monkeypatch, str(tmp_path), "--skip-checksums", "--zip", str(release), "--version", "1.2.3",
          "--manifest-out", str(out))
    assert json.loads(out.read_text())["url"] == generate_checksums.release_asset_url("1.2.3", "shared.zip")
    _main(monkeypatch, str(tmp_path), "--skip-checksums", "--zip", str(release), "--version", "1.2.3",
          "--manifest-out", str(out), "--url", "https://mirror/x.zip")
    assert json.loads(out.read_text())["url"] == "https://mirror/x.zip"
