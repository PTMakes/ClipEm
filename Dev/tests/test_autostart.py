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


def test_linux_desktop_entry():
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


def test_linux_menu_entry_is_listed_in_menus(menu_dir):
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
