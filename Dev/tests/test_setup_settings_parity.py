"""Every option in Settings can be chosen in Setup, and every option in Setup
changed in Settings - so a feature can't quietly reach only one of them. New
options fail here until they're in both, or listed below with a reason."""

import os
import re

import pytest
from conftest import APP_INSTALLER_DIR, TRAY_WIDGET_DIR

with open(os.path.join(TRAY_WIDGET_DIR, "settings_dialog.py"), encoding="utf-8") as f:
    SETTINGS = f.read()
with open(os.path.join(APP_INSTALLER_DIR, "gui.py"), encoding="utf-8") as f:
    SETUP = f.read()

# What Settings keeps in initial_obs_settings -> what Setup saves it with.
OBS_SETTINGS = {
    "websocket": "save_obs_websocket_settings",
    "clip_hotkey": "save_clip_hotkey",
    "toggle_hotkeys": "save_toggle_hotkey",
    "clip_folder": "set_clip_folder",
    "clip_sound": "save_clip_sound_enabled",
    "split_tracks": "save_split_tracks",
    "post_clip": "save_post_clip",
    "clip_reexport": "save_clip_reexport",
    "audio_script": "save_audio_script_settings",
    "audio_devices": "save_audio_devices",
    "mic_filter": "MicFilterWizard",
    "video": "save_video_settings",
    "capture_monitor": "save_capture_monitor",
    "recording_encoder": "save_recording_encoder",
    "recording_bitrate": "save_recording_bitrate",
    "replay_buffer_seconds": "save_replay_buffer_seconds",
}
# Only in Settings: name -> why.
SETTINGS_ONLY = {
    "dark_mode": "Appearance: nothing to choose before ClipEm has been used",
    "custom_colors": "Appearance: nothing to choose before ClipEm has been used",
}
# Only in Setup's calls, not an option: name -> why.
SETUP_ONLY = {
    "save_run_config": "how Setup stores the options checked in test_run_config_options",
    "save_clip_naming": "Settings stores it as run_config's clip_naming, checked with the others",
    "save_clip_sound_volume": "half of clip_sound, with save_clip_sound_enabled",
}


def _quoted_keys(source, target):
    return set(re.findall(rf'{target}\["(\w+)"\]', source))


def _settings_obs_keys():
    block = SETTINGS[SETTINGS.index("initial_obs_settings = {"):]
    block = block[:block.index("\n    }\n")]
    return set(re.findall(r'^\s+"(\w+)":', block, re.M)) | _quoted_keys(SETTINGS, "obs_settings")


def _setup_saves():
    return {name for name in _quoted_keys(SETUP, "controller") if name.startswith(("save_", "set_"))}


def test_every_obs_setting_in_settings_is_mapped_to_setup():
    unmapped = _settings_obs_keys() - set(OBS_SETTINGS) - set(SETTINGS_ONLY)
    assert not unmapped, f"Settings options with no Setup equivalent decided: {sorted(unmapped)}"


@pytest.mark.parametrize("setting, saver", sorted(OBS_SETTINGS.items()))
def test_setup_saves_each_setting(setting, saver):
    assert saver in SETUP, f"Setup has no way to choose {setting} (no {saver})"


def test_everything_setup_saves_is_an_option_in_settings():
    unknown = _setup_saves() - set(OBS_SETTINGS.values()) - set(SETUP_ONLY)
    assert not unknown, f"Setup options with no Settings equivalent decided: {sorted(unknown)}"


def _settings_run_config():
    block = SETTINGS[SETTINGS.rindex("run_config = load_run_config()"):SETTINGS.rindex("save_run_config(run_config)")]
    return _quoted_keys(block, "run_config") - set(SETTINGS_ONLY)


def _setup_run_config():
    block = SETUP[SETUP.index("run_config.update({"):]
    return set(re.findall(r'"(\w+)":', block[:block.index("})")]))


def test_run_config_options_match():
    in_settings = _settings_run_config() - {"clip_naming"}  # save_clip_naming, in Setup
    assert in_settings - _setup_run_config() == set(), "in Settings but not Setup"
    # Autostart is saved separately in Settings; the rest must be there too.
    assert _setup_run_config() - in_settings - {"autostart_obs", "autostart_tray"} == set(), "in Setup but not Settings"
    assert 'run_config["clip_naming"]' in SETTINGS and '"save_clip_naming"' in SETUP


@pytest.mark.parametrize("key", ["autostart_obs", "autostart_tray"])
def test_autostart_is_in_both(key):
    assert key in SETTINGS and f'"{key}"' in SETUP


def test_tray_menu_items_match():
    setup = set(re.findall(r'"(show_\w+_item)"', SETUP))
    settings = set(re.findall(r'"(show_\w+_item)"', SETTINGS))
    assert setup and setup == settings


@pytest.mark.parametrize("key", ["check_for_updates", "source_repo", "update_channel"])
def test_update_settings_are_in_both(key):
    assert key in SETTINGS and key in SETUP


def test_this_test_would_notice_a_missing_option(monkeypatch):
    """Guards the guard: an option only Settings has has to fail it."""
    import test_setup_settings_parity as parity

    monkeypatch.setattr(parity, "SETTINGS", SETTINGS.replace('"post_clip":', '"brand_new_option": 1, "post_clip":', 1))
    assert "brand_new_option" in parity._settings_obs_keys() - set(OBS_SETTINGS) - set(SETTINGS_ONLY)
    monkeypatch.setattr(parity, "SETUP", SETUP.replace('self.controller["save_post_clip"]', '""'))
    with pytest.raises(AssertionError):
        parity.test_setup_saves_each_setting("post_clip", "save_post_clip")


def test_the_text_installer_offers_the_clip_add_ons():
    with open(os.path.join(APP_INSTALLER_DIR, "setup.py"), encoding="utf-8") as f:
        wizard = f.read().split("def main_cli(system):")[1].split("def main():")[0]
    for step in ("configure_split_tracks()", "configure_clip_reexport()", "configure_post_clip()"):
        assert step in wizard
