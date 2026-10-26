"""The hotkey key picker's key mapping (Installer/key_picker.py)."""

import key_picker
import obs_config
import pytest

ALT = key_picker.ALT


@pytest.mark.parametrize("keysym, state, expected", [
    ("a", key_picker.CONTROL, (True, False, False, "A")),
    ("F9", key_picker.SHIFT | ALT, (False, True, True, "F9")),
    ("exclam", key_picker.SHIFT, (False, False, True, "1")),  # Shift+1 on a US layout
    ("grave", ALT, (False, True, False, "~")),
    ("KP_Add", 0, (False, False, False, "Num +")),
    ("Prior", key_picker.CONTROL, (True, False, False, "Page Up")),
    ("Return", ALT, (False, True, False, "Enter")),
])
def test_key_presses_become_hotkeys(keysym, state, expected):
    assert key_picker.hotkey_from_event(keysym, state) == expected


@pytest.mark.parametrize("keysym", ["Shift_L", "Control_R", "Caps_Lock", "XF86AudioPlay", "eacute"])
def test_modifiers_alone_and_unknown_keys_are_ignored(keysym):
    assert key_picker.hotkey_from_event(keysym, 0) is None


def test_every_key_in_the_list_round_trips_through_obs_names():
    assert len(set(obs_config.HOTKEY_KEY_CHOICES)) == len(obs_config.HOTKEY_KEY_CHOICES)
    for label in obs_config.HOTKEY_KEY_CHOICES:
        assert obs_config.obs_key_to_label(obs_config.hotkey_label_to_obs_key(label)) == label
    for label in key_picker.KEYSYM_LABELS.values():
        assert label in obs_config.HOTKEY_KEY_CHOICES
