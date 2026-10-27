"""What saved clips are called (Scripts/clip_naming.py)."""

import datetime
import json
import os
import sys

import pytest
from conftest import SCRIPTS_DIR

sys.path.insert(0, SCRIPTS_DIR)
import clip_naming  # noqa: E402

WHEN = datetime.datetime(2026, 10, 25, 21, 50, 7)


def naming(**changes):
    return {**clip_naming.DEFAULTS, **changes}


def saved_clip(folder, name="Replay 2026-10-25 21-50-07.mp4"):
    path = folder / name
    path.write_bytes(b"x")
    return str(path)


def test_the_defaults_leave_obs_names_alone(tmp_path):
    clip = saved_clip(tmp_path)
    assert clip_naming.rename_clip(clip, naming()) == clip
    assert clip_naming.stem(naming(), WHEN) == "Replay 2026-10-25 21-50-07"


@pytest.mark.parametrize("changes, expected", [
    ({"prefix": "Clip", "date_format": "%d.%m.%Y"}, "Clip 25.10.2026"),
    ({"prefix": "", "date": False, "id": "number", "number_digits": 0}, "7"),
    ({"prefix": "PTM_Clip-Em", "separator": "_", "date_format": "%Y-%m-%d_%H-%M-%S"},
     "PTM_Clip-Em_2026-10-25_21-50-07"),
    ({"prefix": "Clip", "date": False, "id": "number"}, "Clip 0007"),
    ({"prefix": "a/b", "date_format": "%H:%M"}, "a-b 21-50"),  # nothing a file name can't hold
])
def test_names(changes, expected):
    assert clip_naming.stem(naming(**changes), WHEN, number=7) == expected


def test_numbers_count_up_from_the_highest_in_the_folder(tmp_path):
    settings = naming(prefix="Clip", date=False, id="number", number_digits=3, number_start=5)
    assert clip_naming.next_number(tmp_path, settings) == 5  # empty folder: the starting number
    for name in ("Clip 041.mp4", "Clip 007.mp4", "Clip 041-tracks.mkv", "Other 900.mp4", "Clip 999.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert clip_naming.next_number(tmp_path, settings) == 42
    renamed = clip_naming.rename_clip(saved_clip(tmp_path), settings, now=WHEN)
    assert os.path.basename(renamed) == "Clip 042.mp4"


def test_guids_are_unique(tmp_path):
    settings = naming(prefix="", date=False, id="guid")
    first = clip_naming.rename_clip(saved_clip(tmp_path), settings, now=WHEN)
    second = clip_naming.rename_clip(saved_clip(tmp_path, "Replay b.mp4"), settings, now=WHEN)
    assert first != second and len(os.path.basename(first)) == 36 + 4


def test_a_name_already_taken_gets_a_number(tmp_path):
    settings = naming(prefix="Clip")
    (tmp_path / "Clip 2026-10-25 21-50-07.mp4").write_bytes(b"old")
    renamed = clip_naming.rename_clip(saved_clip(tmp_path), settings, now=WHEN)
    assert os.path.basename(renamed) == "Clip 2026-10-25 21-50-07 (2).mp4"


@pytest.mark.parametrize("changes", [
    {"date_format": "no codes"},
    {"small_suffix": "-x", "tracks_suffix": "-x"},
    {"tracks_suffix": ""},
    {"small_suffix": "a/b"},
    {"prefix": "", "date": False},
    {"number_start": -1},
])
def test_unusable_settings_are_refused_and_never_used(tmp_path, changes):
    assert clip_naming.validate(naming(**changes))
    config = tmp_path / "run_config.json"
    config.write_text(json.dumps({"clip_naming": naming(**changes)}))
    assert clip_naming.load(str(config)) == clip_naming.DEFAULTS


def test_copies_use_the_chosen_suffixes_and_old_ones_still_count():
    settings = naming(small_suffix=" (discord size)", tracks_suffix="_all-tracks", extra_suffix=" +after")
    assert clip_naming.small_path("/c/Clip.mp4", settings) == "/c/Clip (discord size).mp4"
    assert clip_naming.tracks_path("/c/Clip.mp4", settings) == "/c/Clip_all-tracks.mkv"
    assert clip_naming.extra_path("/c/Clip.mp4", settings) == "/c/Clip +after.mkv"
    assert clip_naming.copy_suffixes(settings) == {
        "-small": "small_file", "-tracks": "tracks_file", "-extra": "extra_file",
        " (discord size)": "small_file", "_all-tracks": "tracks_file", " +after": "extra_file"}


def test_the_copy_suffixes_must_differ():
    assert clip_naming.validate(naming(extra_suffix="-tracks"))
    assert clip_naming.validate(naming(extra_suffix=""))
