import os

import autostart
import pytest
import uninstall_autostart


def test_ps_quote_escapes_single_quotes():
    assert autostart.ps_quote("C:\\Users\\O'Brien") == "'C:\\Users\\O''Brien'"
    assert autostart.ps_quote("a\u2019b") == "'a\u2019\u2019b'"


def test_windows_shortcut_script_quotes_every_path():
    script = autostart.windows_shortcut_script(
        "C:\\Users\\O'Brien\\ClipEm.lnk", "C:\\Py\\pythonw.exe",
        autostart.windows_command_line_arg("C:\\Users\\O'Brien\\run.py") + " --boot", "C:\\x y",
    )
    assert "$WshShell.CreateShortcut('C:\\Users\\O''Brien\\ClipEm.lnk')" in script
    assert "$Shortcut.Arguments = 'C:\\Users\\O''Brien\\run.py --boot'" in script
    assert "$Shortcut.WorkingDirectory = 'C:\\x y'" in script


def test_windows_command_line_arg_quotes_spaces():
    assert autostart.windows_command_line_arg("C:\\John Doe\\run.py") == '"C:\\John Doe\\run.py"'


def test_desktop_exec_arg_escaping():
    assert autostart.desktop_exec_arg("/home/a b/run.py") == '"/home/a b/run.py"'
    assert autostart.desktop_exec_arg('/x/"q"$`%') == '"/x/\\\\"q\\\\"\\\\$\\\\`%%"'


def test_linux_desktop_entry(monkeypatch):
    monkeypatch.setattr(autostart, "RUN_PY", "/x/run.py")
    entry = autostart.linux_desktop_entry("/usr/bin/python3", ["--boot"], autostart=True)
    assert entry.startswith("[Desktop Entry]\n")
    assert f'Exec="/usr/bin/python3" "{autostart.RUN_PY}" "--boot"\n' in entry
    assert "X-GNOME-Autostart-enabled=true\n" in entry


def test_plist_escapes_xml():
    plist = autostart.macos_launch_agent_plist("/Users/a&b/<py>")
    assert "<string>/Users/a&amp;b/&lt;py&gt;</string>" in plist


# --- Finding and clearing autostart entries from other ClipEm copies ------

@pytest.fixture
def autostart_dir(tmp_path, monkeypatch):
    """A Linux ~/.config/autostart in tmp_path; this install's run.py at a
    path that needs escaping in Exec= lines."""
    folder = tmp_path / "autostart"
    folder.mkdir()
    monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
    monkeypatch.setattr(autostart, "autostart_entry_path", lambda: str(folder / "clipem.desktop"))
    monkeypatch.setattr(autostart, "RUN_PY", "/home/pt/My $Clips & Co/ClipEm/App/Shared/Installer/run.py")
    return folder


def _write(folder, name, text):
    (folder / name).write_text(text)
    return str(folder / name)


def test_finds_this_and_other_clipem_entries_only(autostart_dir):
    ours = _write(autostart_dir, "clipem.desktop", autostart.linux_desktop_entry("/usr/bin/python3", ["--boot"]))
    other = _write(autostart_dir, "clipem-copy.desktop",
                   '[Desktop Entry]\nExec="/usr/bin/python3" "/home/pt/ClipEm-old/App/Shared/Installer/run.py" --boot\n')
    legacy = _write(autostart_dir, "obs-clips.desktop", "[Desktop Entry]\nExec=/home/pt/Old/RunLinux.sh\n")
    _write(autostart_dir, "steam.desktop", "[Desktop Entry]\nExec=/usr/bin/steam -silent\n")
    _write(autostart_dir, "notes.txt", "ClipEm")  # not an autostart entry at all

    entries = {e["path"]: e["this_install"] for e in autostart.find_autostart_entries()}

    assert entries == {ours: True, other: False, legacy: False}
    assert [e["path"] for e in autostart.find_other_autostart_entries()] == sorted([other, legacy])


def test_describes_which_folder_an_entry_starts(autostart_dir):
    path = _write(autostart_dir, "a.desktop",
                  '[Desktop Entry]\nExec="/usr/bin/python3" "/home/pt/ClipEm-old/App/Shared/Installer/run.py" --boot\n')
    entry = {"path": path, "command": open(path).read()}
    assert autostart.describe_autostart_entry(entry) == "a.desktop (starts /home/pt/ClipEm-old)"
    assert autostart.describe_autostart_entry({"path": "/x/b.desktop", "command": "Exec=/y/RunLinux.sh"}) \
        == "b.desktop (starts /y)"
    windows = {"path": r"C:\x\ClipEm.lnk", "command": r'C:\Games\ClipEm\App\Windows\Python\pythonw.exe "C:\Games\ClipEm\App\Shared\Installer\run.py" --boot'}
    assert autostart.describe_autostart_entry(windows).endswith(r"(starts C:\Games\ClipEm)")


def test_disabling_autostart_leaves_another_copys_entry_alone(autostart_dir):
    other = _write(autostart_dir, "clipem.desktop",
                   '[Desktop Entry]\nExec="/usr/bin/python3" "/elsewhere/App/Shared/Installer/run.py" --boot\n')
    autostart.apply_boot_autostart(False)
    assert (autostart_dir / "clipem.desktop").exists()

    autostart.remove_autostart_entries(autostart.find_other_autostart_entries())
    assert not os.path.exists(other)


def test_disabling_autostart_removes_our_own_entry(autostart_dir):
    _write(autostart_dir, "clipem.desktop", autostart.linux_desktop_entry("/usr/bin/python3", ["--boot"]))
    autostart.apply_boot_autostart(False)
    assert not (autostart_dir / "clipem.desktop").exists()


def test_uninstall_script_only_removes_this_copy(autostart_dir, capsys):
    _write(autostart_dir, "clipem.desktop", autostart.linux_desktop_entry("/usr/bin/python3", ["--boot"]))
    other = _write(autostart_dir, "old.desktop", "[Desktop Entry]\nExec=/home/pt/Old/RunLinux.sh\n")
    uninstall_autostart.main()
    assert not (autostart_dir / "clipem.desktop").exists()
    assert os.path.exists(other)
    assert "Left in place" in capsys.readouterr().out


def test_macos_plist_with_escaped_path_is_recognised(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    plist = agents / f"{autostart.MACOS_LAUNCH_AGENT_LABEL}.plist"
    monkeypatch.setattr(autostart.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(autostart, "autostart_entry_path", lambda: str(plist))
    monkeypatch.setattr(autostart, "RUN_PY", "/Users/pt/Clips & <Stuff>/ClipEm/App/Shared/Installer/run.py")
    plist.write_text(autostart.macos_launch_agent_plist("/usr/bin/python3"))
    (agents / "com.example.other.plist").write_text("<plist><string>/usr/bin/other</string></plist>")

    assert [(os.path.basename(e["path"]), e["this_install"]) for e in autostart.find_autostart_entries()] == [
        (plist.name, True)
    ]


# --- Linux application menu entry ------------------------------------------

@pytest.fixture
def menu_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "_refresh_linux_menus", lambda _dir: None)
    return tmp_path / "applications"


def test_linux_menu_entry_is_listed_in_menus(menu_dir, monkeypatch):
    monkeypatch.setattr(autostart, "RUN_PY", "/x/run.py")
    autostart.create_linux_menu_entry("/usr/bin/python3", "/x/ClipEm.png")
    entry = (menu_dir / "clipem.desktop").read_text()
    assert f'Exec="/usr/bin/python3" "{autostart.RUN_PY}"\n' in entry
    assert "Categories=" in entry
    assert "StartupWMClass=ClipEm\n" in entry
    assert "NoDisplay" not in entry


def test_remove_linux_menu_entry_only_removes_this_install(menu_dir):
    autostart.create_linux_menu_entry("/usr/bin/python3", None)
    autostart.remove_start_menu_shortcut()
    assert not (menu_dir / "clipem.desktop").exists()

    other = _write(menu_dir, "clipem.desktop", '[Desktop Entry]\nExec="/py" "/elsewhere/run.py"\n')
    autostart.remove_start_menu_shortcut()
    assert os.path.exists(other)


def test_start_menu_entry_is_added_once_for_updated_installs(monkeypatch):
    config = {}
    made = []
    monkeypatch.setattr(autostart.platform, "system", lambda: "Windows")
    monkeypatch.setattr(autostart, "load_run_config", lambda: dict(config))

    def create():
        made.append(True)
        config["start_menu_registered"] = autostart.START_MENU_REGISTRATION
        return "C:\\Start Menu\\ClipEm.lnk"

    monkeypatch.setattr(autostart, "create_start_menu_shortcut", create)
    assert autostart.ensure_start_menu_entry() == "C:\\Start Menu\\ClipEm.lnk"  # updated from before it existed
    assert autostart.ensure_start_menu_entry() is None and made == [True]  # done - not again
    config["start_menu_registered"] = "junk"
    assert autostart.ensure_start_menu_entry() and len(made) == 2
    monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
    config.clear()
    assert autostart.ensure_start_menu_entry() is None and len(made) == 2


def test_start_menu_entry_gives_up_after_a_few_failed_launches(monkeypatch):
    config = {}
    tries = []
    monkeypatch.setattr(autostart.platform, "system", lambda: "Windows")
    monkeypatch.setattr(autostart, "load_run_config", lambda: dict(config))
    monkeypatch.setattr(autostart, "save_run_config", lambda new: (config.clear(), config.update(new)))

    def fail():
        tries.append(True)
        raise OSError("PowerShell is blocked")

    monkeypatch.setattr(autostart, "create_start_menu_shortcut", fail)
    for _ in range(autostart.START_MENU_MAX_ATTEMPTS):
        with pytest.raises(OSError):
            autostart.ensure_start_menu_entry()
    assert autostart.ensure_start_menu_entry() is None
    assert len(tries) == autostart.START_MENU_MAX_ATTEMPTS


@pytest.fixture
def shortcuts(menu_dir, tmp_path, monkeypatch):
    """Linux, with the menu and desktop in tmp_path and run_config in memory."""
    config = {}
    desktop = tmp_path / "Desktop"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # no user-dirs.dirs: ~/Desktop
    monkeypatch.setattr(autostart, "_linux_desktop_dir", lambda: str(desktop))
    monkeypatch.setattr(autostart, "find_linux_python_bin", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autostart.shutil, "which", lambda _name: None)  # no gio
    monkeypatch.setattr(autostart, "load_run_config", lambda: dict(config))
    monkeypatch.setattr(autostart, "save_run_config", lambda new: (config.clear(), config.update(new)))
    return config, menu_dir, desktop


@pytest.mark.parametrize("kind", [autostart.SHORTCUT_START_MENU, autostart.SHORTCUT_DESKTOP])
def test_shortcuts_install_and_uninstall(shortcuts, kind):
    config, _menu, _desktop = shortcuts
    assert autostart.shortcut_state(kind) == autostart.SHORTCUT_MISSING
    path = autostart.install_shortcut(kind)
    assert autostart.shortcut_state(kind) == autostart.SHORTCUT_OURS
    assert "StartupWMClass=ClipEm\n" in open(path).read()
    assert config[f"{kind}_shortcut"] is True
    assert autostart.uninstall_shortcut(kind) is True
    assert not os.path.exists(path) and config[f"{kind}_shortcut"] is False


def test_shortcut_from_a_deleted_install_is_broken_and_one_from_a_live_one_is_left_alone(shortcuts, tmp_path):
    _config, menu, _desktop = shortcuts
    menu.mkdir()
    gone = _write(menu, "clipem.desktop", '[Desktop Entry]\nExec="/py" "/deleted/ClipEm/App/Shared/Installer/run.py"\n')
    assert autostart.shortcut_state(autostart.SHORTCUT_START_MENU) == autostart.SHORTCUT_BROKEN
    assert autostart.uninstall_shortcut(autostart.SHORTCUT_START_MENU) is True
    assert not os.path.exists(gone)

    other_run_py = tmp_path / "Other" / "run.py"
    other_run_py.parent.mkdir()
    other_run_py.write_text("")
    other = _write(menu, "clipem.desktop", f'[Desktop Entry]\nExec="/py" "{other_run_py}"\n')
    assert autostart.shortcut_state(autostart.SHORTCUT_START_MENU) == autostart.SHORTCUT_OTHER
    assert autostart.uninstall_shortcut(autostart.SHORTCUT_START_MENU) is False
    assert os.path.exists(other)


def test_launch_repairs_a_broken_menu_entry_unless_uninstalled(shortcuts):
    config, menu, _desktop = shortcuts
    menu.mkdir()
    _write(menu, "clipem.desktop", '[Desktop Entry]\nExec="/py" "/deleted/run.py"\n')
    config["start_menu_shortcut"] = False
    assert autostart.repair_linux_menu_entry() is None  # the user removed it in Settings
    del config["start_menu_shortcut"]
    assert autostart.repair_linux_menu_entry() == str(menu / "clipem.desktop")
    assert autostart.shortcut_state(autostart.SHORTCUT_START_MENU) == autostart.SHORTCUT_OURS
    assert autostart.repair_linux_menu_entry() is None  # working now: left alone


@pytest.mark.skipif(os.name == "nt", reason="Linux only: it removes the entry it has open, which Windows refuses")
def test_uninstall_removes_this_installs_window_entries_only(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
    monkeypatch.setattr(autostart, "SHARED_DIR", "/home/x/ClipEm/App/Shared")
    ours, theirs = tmp_path / "clipem-editor.desktop", tmp_path / "clipem-settings.desktop"
    ours.write_text(f"Icon={autostart.SHARED_DIR}/tray-widget/ClipEmEditor.png\n")
    theirs.write_text("Icon=/other/ClipEm/App/Shared/tray-widget/ClipEmSettings.png\n")
    autostart.remove_window_desktop_entries(str(tmp_path))
    assert not ours.exists() and theirs.exists()



# --- Registering the boot autostart entry -------------------------------------

@pytest.fixture
def linux_boot(tmp_path, monkeypatch):
    path = tmp_path / "autostart" / "clipem.desktop"
    monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
    monkeypatch.setattr(autostart, "autostart_entry_path", lambda: str(path))
    monkeypatch.setattr(autostart, "find_linux_python_bin", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autostart, "RUN_PY", "/home/x/ClipEm/App/Shared/Installer/run.py")
    return path


def test_boot_autostart_writes_a_runnable_entry_that_passes_boot(linux_boot):
    autostart.apply_boot_autostart(True)
    entry = linux_boot.read_text()
    assert '"--boot"' in entry and f'"{autostart.RUN_PY}"' in entry and "X-GNOME-Autostart-enabled=true" in entry
    assert os.access(linux_boot, os.X_OK)


def test_turning_boot_autostart_off_removes_this_installs_entry(linux_boot):
    autostart.apply_boot_autostart(True)
    autostart.apply_boot_autostart(False)
    assert not linux_boot.exists()


def test_turning_boot_autostart_off_twice_is_fine(linux_boot):
    autostart.apply_boot_autostart(False)
    assert not linux_boot.exists()


def test_another_installs_entry_is_left_alone(linux_boot):
    linux_boot.parent.mkdir()
    entry = "[Desktop Entry]\nExec=/usr/bin/python3 /other/ClipEm/App/Shared/Installer/run.py --boot\n"
    linux_boot.write_text(entry)
    autostart.apply_boot_autostart(False)
    assert linux_boot.read_text() == entry


def test_a_platform_without_autostart_is_told_so(monkeypatch, capsys):
    monkeypatch.setattr(autostart, "autostart_entry_path", lambda: None)
    monkeypatch.setattr(autostart.platform, "system", lambda: "Plan9")
    autostart.apply_boot_autostart(True)
    assert "isn't supported on Plan9" in capsys.readouterr().err


def test_an_entry_that_cant_be_written_is_skipped_not_fatal(linux_boot, monkeypatch, capsys):
    def fail():
        raise OSError("no python")

    monkeypatch.setattr(autostart, "find_linux_python_bin", fail)
    autostart.apply_boot_autostart(True)
    captured = capsys.readouterr()
    assert "skipping it" in captured.err and "enabled" not in captured.out


def test_macos_writes_a_launch_agent(tmp_path, monkeypatch):
    path = tmp_path / "LaunchAgents" / "clipem.plist"
    monkeypatch.setattr(autostart.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(autostart, "autostart_entry_path", lambda: str(path))
    autostart.apply_boot_autostart(True)
    assert "<string>--boot</string>" in path.read_text()


def test_windows_makes_a_shortcut_to_pythonw_passing_boot(tmp_path, monkeypatch):
    path = tmp_path / "Startup" / "ClipEm.lnk"
    scripts = []
    monkeypatch.setattr(autostart.platform, "system", lambda: "Windows")
    monkeypatch.setattr(autostart, "autostart_entry_path", lambda: str(path))
    monkeypatch.setattr(autostart, "find_windows_python_root", lambda: "C:\\Py")
    monkeypatch.setattr(autostart, "run_powershell", lambda script, check: scripts.append(script))
    autostart.apply_boot_autostart(True)
    assert os.path.join("C:\\Py", "pythonw.exe") in scripts[0] and "--boot" in scripts[0]


def test_removing_entries_counts_what_really_went(tmp_path, monkeypatch, capsys):
    real = tmp_path / "real.desktop"
    real.write_text("x")
    busy = tmp_path / "busy.desktop"
    busy.write_text("x")
    real_remove = os.remove

    def remove(path):
        if path == str(busy):
            raise PermissionError("in use")
        real_remove(path)

    monkeypatch.setattr(autostart.os, "remove", remove)
    entries = [{"path": str(real)}, {"path": str(tmp_path / "gone.desktop")}, {"path": str(busy)}]
    assert autostart.remove_autostart_entries(entries) == 1
    assert not real.exists() and busy.exists()
    assert f"Could not remove the autostart entry {busy}" in capsys.readouterr().err


def test_the_desktop_shortcut_is_made_runnable_and_trusted(tmp_path, monkeypatch):
    path = tmp_path / "Desktop" / "ClipEm.desktop"
    ran, remembered = [], []
    monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
    monkeypatch.setattr(autostart, "shortcut_path", lambda kind: str(path))
    monkeypatch.setattr(autostart, "find_linux_python_bin", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autostart, "_remember_shortcut", lambda kind, on: remembered.append((kind, on)))
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/gio")
    monkeypatch.setattr(autostart.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    assert autostart.install_shortcut("desktop") == str(path)
    assert os.access(path, os.X_OK) and "StartupWMClass=ClipEm" in path.read_text()
    assert ran == [["gio", "set", str(path), "metadata::trusted", "true"]]
    assert remembered == [("desktop", True)]


def test_a_shortcut_this_platform_doesnt_have_is_an_error(monkeypatch):
    monkeypatch.setattr(autostart, "shortcut_path", lambda kind: None)
    with pytest.raises(RuntimeError, match="doesn't make this shortcut"):
        autostart.install_shortcut("desktop")


def test_the_folders_launcher_is_kept_and_a_menu_failure_isnt_fatal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(autostart, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(autostart, "find_linux_python_bin", lambda: "/usr/bin/python3")

    def no_menu(_python, _icon):
        raise OSError("read-only")

    monkeypatch.setattr(autostart, "create_linux_menu_entry", no_menu)
    autostart.create_shortcut_linux()
    launcher = tmp_path / "ClipEm.desktop"
    assert os.access(launcher, os.X_OK) and "skipping it" in capsys.readouterr().err
    launcher.write_text("edited")
    autostart.create_shortcut_linux()
    assert launcher.read_text() == "edited"


@pytest.mark.parametrize("system, name", [
    ("Linux", "create_shortcut_linux"), ("Windows", "create_shortcut_windows"), ("Darwin", "create_shortcut_macos"),
])
def test_each_platform_gets_its_own_shortcut(monkeypatch, system, name):
    made = []
    monkeypatch.setattr(autostart.platform, "system", lambda: system)
    for other in ("create_shortcut_linux", "create_shortcut_windows", "create_shortcut_macos"):
        monkeypatch.setattr(autostart, other, lambda who=other: made.append(who))
    autostart.create_shortcut()
    assert made == [name]
