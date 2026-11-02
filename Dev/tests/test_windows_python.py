"""windows_python.py: what's done to the bundled Windows Python, without
needing Windows - its ._pth file, pip from the signed manifest, and Tcl/Tk
out of an MSI (msiexec faked)."""

import os
import subprocess

import pytest
import updater
import windows_python

VERSION = (3, 12)


def _pth(root, text):
    path = root / "python312._pth"
    path.write_text(text)
    return path


def test_site_is_turned_on(tmp_path):
    path = _pth(tmp_path, "python312.zip\n.\n#import site\n")
    windows_python.enable_site(str(tmp_path), VERSION)
    assert path.read_text() == "python312.zip\n.\nimport site\n"


def test_lib_is_added_once(tmp_path):
    path = _pth(tmp_path, ".\n")
    windows_python.add_lib_to_pth(str(tmp_path), VERSION)
    windows_python.add_lib_to_pth(str(tmp_path), VERSION)
    assert path.read_text() == ".\nLib\n"


def test_no_pth_file_is_nothing_to_edit(tmp_path):
    windows_python.enable_site(str(tmp_path), VERSION)
    windows_python.add_lib_to_pth(str(tmp_path), VERSION)
    assert os.listdir(tmp_path) == []


def test_only_a_standalone_python_is_isolated(tmp_path):
    windows_python.isolate(str(tmp_path), VERSION)  # no DLLs\: not a Python
    assert os.listdir(tmp_path) == []
    (tmp_path / "DLLs").mkdir()
    windows_python.isolate(str(tmp_path), VERSION)
    assert (tmp_path / "python312._pth").read_text() == ".\nLib\nDLLs\nimport site\n"
    (tmp_path / "python312._pth").write_text("mine\n")
    windows_python.isolate(str(tmp_path), VERSION)
    assert (tmp_path / "python312._pth").read_text() == "mine\n"


@pytest.mark.parametrize("folder", ["", "DLLs"])
def test_tcltk_is_found_wherever_the_python_keeps_it(tmp_path, folder):
    assert not windows_python.tcltk_installed(str(tmp_path))
    (tmp_path / folder).mkdir(exist_ok=True)
    (tmp_path / folder / "_tkinter.pyd").write_text("")
    assert not windows_python.tcltk_installed(str(tmp_path))  # no script library
    marker = tmp_path / windows_python.TCLTK_TCL_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    assert windows_python.tcltk_installed(str(tmp_path))


def test_msiexec_values_are_quoted_for_its_own_parser():
    assert windows_python._msiexec_quote("C:\\Users\\First Last") == '"C:\\Users\\First Last"'


@pytest.fixture
def manifest(monkeypatch):
    downloads = []

    def download(url, path, sha256, timeout):
        downloads.append((url, sha256))
        with open(path, "wb") as f:
            f.write(b"data")

    monkeypatch.setattr(updater, "fetch_manifest", lambda: {"windows": {
        "pip_wheel": {"url": "https://x/pip-26.2.1-py3-none-any.whl", "sha256": "pip-hash"},
        "tcltk_msi": {"url": "https://x/tcltk.msi", "sha256": "tcl-hash"}}})
    monkeypatch.setattr(updater, "download_file", download)
    return downloads


def test_pip_comes_from_the_signed_manifest_and_its_wheel_is_cleaned_up(tmp_path, manifest, monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: ran.append(cmd))
    python = tmp_path / "python.exe"
    windows_python.bootstrap_pip(str(python), log=lambda m: None)
    assert manifest == [("https://x/pip-26.2.1-py3-none-any.whl", "pip-hash")]
    assert ran[0][0] == str(python) and ran[0][-1] == str(tmp_path / "pip-26.2.1-py3-none-any.whl")
    assert os.listdir(tmp_path) == []


def test_a_failed_pip_install_quotes_pip_and_cleans_up(tmp_path, manifest, monkeypatch):
    def fail(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd, output="out\n", stderr="ERROR: no space")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="no space"):
        windows_python.bootstrap_pip(str(tmp_path / "python.exe"), log=lambda m: None)
    assert os.listdir(tmp_path) == []


def _fake_msiexec(monkeypatch, code=0, skip=()):
    """Unpacks what tcltk.msi has into TARGETDIR, leaving out `skip`."""
    commands = []

    def run(command):
        commands.append(command)
        extract = command.split('TARGETDIR="')[1].split('"')[0]
        names = [os.path.join("DLLs", name) for name in windows_python.TCLTK_DLLS]
        names += [windows_python.TCLTK_TCL_MARKER, os.path.join(windows_python.TCLTK_TKINTER_PKG, "__init__.py")]
        for name in names:
            if name not in skip and code == 0:
                path = os.path.join(extract, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(name)
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(subprocess, "run", run)
    return commands


def test_tcltk_is_copied_out_of_the_msi(tmp_path, manifest, monkeypatch):
    commands = _fake_msiexec(monkeypatch)
    assert windows_python.install_tcltk(str(tmp_path)) is True
    assert manifest == [("https://x/tcltk.msi", "tcl-hash")]
    assert "/a" in commands[0] and "/qn" in commands[0]
    for name in windows_python.TCLTK_DLLS:
        assert (tmp_path / name).exists()
    assert windows_python.tcltk_installed(str(tmp_path))
    assert (tmp_path / "Lib" / "tkinter" / "__init__.py").exists()
    assert not (tmp_path / "_tcltk_bootstrap").exists()


def test_a_failed_msiexec_keeps_its_log_for_troubleshooting(tmp_path, manifest, monkeypatch, capsys):
    _fake_msiexec(monkeypatch, code=1603)
    log = tmp_path / "_tcltk_bootstrap" / "msiexec.log"
    assert windows_python.install_tcltk(str(tmp_path)) is False
    assert (tmp_path / "_tcltk_bootstrap").is_dir() and not (tmp_path / "tcl").exists()
    assert "no msiexec log" in capsys.readouterr().out
    assert not log.exists()


def test_files_missing_from_the_msi_are_named(tmp_path, manifest, monkeypatch, capsys):
    missing = os.path.join("DLLs", "zlib1.dll")
    _fake_msiexec(monkeypatch, skip={missing})
    assert windows_python.install_tcltk(str(tmp_path)) is False
    assert missing in capsys.readouterr().out and not (tmp_path / "tcl86t.dll").exists()


def test_the_msiexec_log_is_read_as_utf_16(tmp_path, capsys):
    log = tmp_path / "msiexec.log"
    log.write_bytes("\ufeff" .encode("utf-16-le") + "\n".join(f"line {n}" for n in range(50)).encode("utf-16-le"))
    windows_python._print_msiexec_log_tail(str(log), lines=2)
    out = capsys.readouterr().out
    assert "line 49" in out and "line 48" in out and "line 47" not in out


def test_a_plain_msiexec_log_is_read_too(tmp_path, capsys):
    log = tmp_path / "msiexec.log"
    log.write_text("Return value 3\n")
    windows_python._print_msiexec_log_tail(str(log))
    assert "Return value 3" in capsys.readouterr().out


def test_the_tree_listing_caps_long_folders(tmp_path, capsys):
    for n in range(22):
        (tmp_path / f"file{n:02}").write_text("")
    windows_python._list_tree(str(tmp_path))
    assert "... and 2 more files" in capsys.readouterr().out
    windows_python._list_tree(str(tmp_path / "gone"))
    assert "does not exist" in capsys.readouterr().out


def test_tkinter_is_tried_in_the_environment_the_gui_gets(tmp_path, monkeypatch):
    envs = []

    def run(cmd, env, **_kwargs):
        envs.append(env)
        return subprocess.CompletedProcess(cmd, 0, stdout="8.6\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert windows_python.verify_tkinter("python.exe", str(tmp_path)) is True
    assert envs[0]["TCL_LIBRARY"] == str(tmp_path / "tcl" / "tcl8.6")
    assert envs[0]["TK_LIBRARY"] == str(tmp_path / "tcl" / "tk8.6")


def test_a_tkinter_that_wont_start_shows_why(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **_kwargs: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr="ImportError: DLL load failed\n"))
    assert windows_python.verify_tkinter("python.exe", str(tmp_path)) is False
    assert "DLL load failed" in capsys.readouterr().out
