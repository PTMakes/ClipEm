"""What saved clips are called (Scripts/clip_naming.py)."""

import datetime
import json
import os
import sys

import pytest
from conftest import INSTALLER_DIR, SCRIPTS_DIR

sys.path.insert(0, SCRIPTS_DIR)
import clip_naming  # noqa: E402

WHEN = datetime.datetime(2026, 10, 25, 21, 50, 7)


def naming(**changes):
    return {**clip_naming.DEFAULTS, **changes}


def saved_clip(folder, name="Replay 2026-10-25 21-50-07.mp4"):
    path = folder / name
    path.write_bytes(b"x")
    return str(path)


def test_the_default_name_has_no_spaces_and_replaces_obss(tmp_path):
    assert clip_naming.stem(naming(), WHEN) == "Replay-2026-10-25-21-50-07"
    assert clip_naming.stem(naming(), WHEN, app="Counter-Strike 2") == "Replay-2026-10-25-21-50-07-Counter-Strike-2"
    renamed = clip_naming.rename_clip(saved_clip(tmp_path), naming(), now=WHEN)
    assert os.path.basename(renamed) == "Replay-2026-10-25-21-50-07.mp4"


@pytest.mark.parametrize("changes, expected", [
    ({"prefix": "Clip", "date_format": "dd.MM.yyyy"}, "Clip-25.10.2026"),
    ({"prefix_on": False, "date": False, "id": "number", "number_digits": 0}, "7"),
    ({"prefix": "PTM_Clip-Em", "separator": "_", "date_format": "yyyy-MM-dd_hh-mm-ss"},
     "PTM_Clip-Em_2026-10-25_21-50-07"),
    ({"prefix": "Clip", "date": False, "id": "number"}, "Clip-0007"),
    ({"prefix": "a/b", "date_format": "hh:mm"}, "a-b-21-50"),  # nothing a file name can't hold
    ({"prefix": "My Best Clip", "separator": "_"}, "My_Best_Clip_2026_10_25_21_50_07"),
    ({"date_format": "ddMMyyyyhhmmss", "separator": "."}, "Replay.25.10.2026.21.50.07"),
    ({"date_format": "%Y %m"}, "Replay-2026-10"),  # a format saved before date codes
    ({"order": ["date", "id", "app", "prefix"], "id": "number"}, "2026-10-25-21-50-07-0007-Game-Name-Replay"),
    ({"prefix_on": False, "app": False}, "2026-10-25-21-50-07"),
])
def test_names(changes, expected):
    app = "Game Name" if "order" in changes else None
    assert clip_naming.stem(naming(**changes), WHEN, number=7, app=app) == expected


def test_an_app_name_is_cut_to_a_sensible_length():
    name = clip_naming.stem(naming(date=False), WHEN, app="x" * 100 + ".")
    assert name == "Replay-" + "x" * clip_naming.MAX_APP_LENGTH


def test_an_apps_exe_isnt_part_of_its_name():
    assert clip_naming.stem(naming(date=False), WHEN, app="Deadlock.EXE") == "Replay-Deadlock"


def test_clips_are_named_after_the_active_app(tmp_path):
    clip_naming.publish_active_app("Deadlock")
    assert clip_naming.active_app() == "Deadlock"
    renamed = clip_naming.rename_clip(saved_clip(tmp_path), naming(), now=WHEN)
    assert os.path.basename(renamed) == "Replay-2026-10-25-21-50-07-Deadlock.mp4"
    other = clip_naming.rename_clip(saved_clip(tmp_path, "b.mp4"), naming(app=False), now=WHEN)
    assert os.path.basename(other) == "Replay-2026-10-25-21-50-07.mp4"
    clip_naming.publish_active_app(None)
    assert clip_naming.active_app() is None


def test_numbers_count_up_from_the_highest_in_the_folder(tmp_path):
    settings = naming(prefix="Clip", date=False, id="number", number_digits=3, number_start=5)
    assert clip_naming.next_number(tmp_path, settings) == 5  # empty folder: the starting number
    for name in ("Clip-041.mp4", "Clip-007.mp4", "Clip-041-tracks.mkv", "Other-900.mp4", "Clip-999.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert clip_naming.next_number(tmp_path, settings) == 42
    renamed = clip_naming.rename_clip(saved_clip(tmp_path), settings, now=WHEN)
    assert os.path.basename(renamed) == "Clip-042.mp4"


@pytest.mark.parametrize("order, existing", [
    (["prefix", "id", "date", "app"], "Clip-0041-2026-10-25-21-50-07-Spotify.mp4"),
    (["prefix", "app", "id", "date"], "Clip-Spotify-0041-2026-10-25-21-50-07.mp4"),
    (["date", "id", "prefix", "app"], "2026-10-25-21-50-07-0041-Clip-Spotify.mp4"),
    (["app", "date", "prefix", "id"], "Spotify-2026-10-25-21-50-07-Clip-0041.mp4"),
    (["prefix", "id", "date", "app"], "Clip 0041.mp4"),  # from when spaces were allowed
])
def test_numbers_are_found_wherever_they_are_in_the_name(tmp_path, order, existing):
    settings = naming(prefix="Clip", id="number", order=order, date="2026" in existing)
    (tmp_path / existing).write_bytes(b"x")
    assert clip_naming.next_number(tmp_path, settings) == 42


@pytest.mark.parametrize("changes", [{}, {"id": "guid"}])
def test_names_without_a_number_have_nothing_to_count(tmp_path, changes):
    settings = naming(number_start=5, **changes)
    (tmp_path / f"{clip_naming.stem(settings, WHEN, guid='g')}.mp4").write_bytes(b"x")
    assert clip_naming.next_number(tmp_path, settings) == 5


def test_a_date_is_never_taken_for_a_number(tmp_path):
    for name in ("Clip-20261025215007.mp4", "Clip-20261025215007-Deadlock.mp4", "Clip-2026-10-25-21-50-07.mp4",
                 "Clip-Deadlock-2026-10-25-21-50-07.mp4"):
        (tmp_path / name).write_bytes(b"x")
    settings = naming(prefix="Clip", id="number")
    assert clip_naming.next_number(tmp_path, settings) == 1
    assert clip_naming.next_number(tmp_path, naming(prefix="Clip", id="number", date=False)) == 1


def test_guids_are_unique(tmp_path):
    settings = naming(prefix_on=False, date=False, id="guid")
    first = clip_naming.rename_clip(saved_clip(tmp_path), settings, now=WHEN)
    second = clip_naming.rename_clip(saved_clip(tmp_path, "Replay b.mp4"), settings, now=WHEN)
    assert first != second and len(os.path.basename(first)) == 36 + 4


def test_a_name_already_taken_gets_a_number(tmp_path):
    settings = naming(prefix="Clip")
    (tmp_path / "Clip-2026-10-25-21-50-07.mp4").write_bytes(b"old")
    renamed = clip_naming.rename_clip(saved_clip(tmp_path), settings, now=WHEN)
    assert os.path.basename(renamed) == "Clip-2026-10-25-21-50-07-2.mp4"


def test_settings_saved_before_part_order_are_upgraded(tmp_path):
    config = tmp_path / "run_config.json"
    config.write_text(json.dumps({"clip_naming": {
        "prefix": "Replay", "separator": " ", "date": True, "date_format": "%Y-%m-%d %H-%M-%S", "id": "none",
        "number_start": 1, "number_digits": 4, "small_suffix": "-small", "tracks_suffix": "_all",
        "extra_suffix": " +after"}}))
    settings = clip_naming.load(str(config))
    assert settings == naming(tracks_suffix="all", extra_suffix="+after")
    assert clip_naming.upgrade({"prefix": "", "separator": "_", "date_format": "%d"}) == naming(
        prefix="", prefix_on=False, separator="_", date_format="%d")


@pytest.mark.parametrize("changes", [
    {"date_format": "no codes"},
    {"small_suffix": "x", "tracks_suffix": "x"},
    {"tracks_suffix": ""},
    {"small_suffix": "a/b"},
    {"prefix": "", "date": False},
    {"prefix_on": False, "date": False},
    {"number_start": -1},
    {"separator": ""},
    {"separator": " "},
    {"separator": "/"},
    {"order": ["prefix", "date"]},
])
def test_unusable_settings_are_refused_and_never_used(tmp_path, changes):
    assert clip_naming.validate(naming(**changes))
    config = tmp_path / "run_config.json"
    config.write_text(json.dumps({"clip_naming": naming(**changes)}))
    assert clip_naming.load(str(config)) == clip_naming.DEFAULTS


def test_copies_use_the_chosen_suffixes_and_old_ones_still_count():
    settings = naming(small_suffix="discord size", tracks_suffix="all-tracks", extra_suffix="after", separator="_")
    assert clip_naming.small_path("/c/Clip.mp4", settings) == "/c/Clip_discord_size.mp4"
    assert clip_naming.tracks_path("/c/Clip.mp4", settings) == "/c/Clip_all-tracks.mkv"
    assert clip_naming.extra_path("/c/Clip.mp4", settings) == "/c/Clip_after.mkv"
    assert clip_naming.copy_suffixes(settings) == {
        "-small": "small_file", "-tracks": "tracks_file", "-extra": "extra_file",
        "_discord_size": "small_file", "_all-tracks": "tracks_file", "_after": "extra_file"}


def test_a_suffix_typed_with_the_separator_doesnt_get_two():
    assert clip_naming.tracks_path("/c/Clip.mp4", naming(tracks_suffix="-all")) == "/c/Clip-all.mkv"


def test_the_copy_suffixes_must_differ():
    assert clip_naming.validate(naming(extra_suffix="tracks"))
    assert clip_naming.validate(naming(extra_suffix=""))


@pytest.fixture
def form():
    tk = pytest.importorskip("tkinter")
    sys.path.insert(0, INSTALLER_DIR)
    import naming_form

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("needs a display")
    root.withdraw()
    yield lambda initial=None: naming_form.NamingForm(root, initial)
    root.destroy()


def test_the_form_gives_back_what_it_was_given(form):
    settings = naming(order=["date", "app", "prefix", "id"], prefix_on=False, separator="_", id="guid", app=False)
    assert form(settings).get() == settings
    assert form().get() == clip_naming.DEFAULTS
    assert form({"prefix": "Clip", "separator": " ", "tracks_suffix": "-all"}).get() == naming(
        prefix="Clip", tracks_suffix="all")  # as saved by an older version


@pytest.mark.parametrize("date_format, expected", [
    ("yyyyMMddhhmmss", ["yyyy", "MM", "dd", "hh", "mm", "ss"]),
    ("ssmmhhddMMyyyy", ["ss", "mm", "hh", "dd", "MM", "yyyy"]),
    ("dd.MM.yyyy", ["dd", "MM", "yyyy", "hh", "mm", "ss"]),
    ("%H-%M yy", ["hh", "mm", "yyyy", "MM", "dd", "ss"]),
    ("yyyy-yyyy", ["yyyy", "MM", "dd", "hh", "mm", "ss"]),
])
def test_a_date_format_becomes_the_six_parts(date_format, expected):
    assert clip_naming.date_parts(date_format) == expected


def _picks(shown):
    return [var.get() for var in shown._date_vars]


def test_the_date_dropdowns_set_the_order(form):
    shown = form(naming(date_format="ddMMyyyyhhmmss"))
    assert _picks(shown) == ["Day", "Month", "Year", "Hour", "Minute", "Second"]
    assert shown.get()["date_format"] == "ddMMyyyyhhmmss"


def test_picking_a_date_part_in_use_swaps_the_two_dropdowns(form):
    shown = form()
    shown._date_vars[0].set("Second")
    assert _picks(shown) == ["Second", "Month", "Day", "Hour", "Minute", "Year"]
    assert shown.get()["date_format"] == "ssMMddhhmmyyyy"
    shown._date_vars[3].set("Month")
    assert _picks(shown) == ["Second", "Hour", "Day", "Month", "Minute", "Year"]
    shown._date_vars[3].set("Month")  # no change
    assert sorted(_picks(shown)) == sorted(naming_labels())
    assert clip_naming.stem(shown.get(), WHEN) == "Replay-07-21-25-10-50-2026"
    assert shown._preview.cget("text").startswith("Replay-")  # it follows along, with the time now


def naming_labels():
    return [label for _code, label in clip_naming.DATE_PARTS]


def test_a_date_part_swapped_back_again_is_where_it_began(form):
    shown = form()
    shown._date_vars[1].set("Day")
    shown._date_vars[1].set("Month")
    assert shown.get()["date_format"] == clip_naming.DEFAULTS["date_format"]


def test_the_form_turns_the_number_off_and_on_keeping_its_kind(form):
    shown = form(naming(id="guid"))
    shown.vars["number_on"].set(False)
    assert shown.get()["id"] == "none"
    shown.vars["number_on"].set(True)
    assert shown.get()["id"] == "guid"


def test_dragging_a_handle_reorders_the_parts(form):
    shown = form()
    shown.pack()
    shown.update()
    date_row = shown._rows["date"][2]
    prefix_row = shown._rows["prefix"][2]

    class Event:
        y_root = shown.winfo_rooty() + prefix_row.winfo_y() + prefix_row.winfo_height() // 2

    shown._drag("date", Event)
    assert shown.get()["order"] == ["date", "prefix", "id", "app"]
    shown.update()
    assert date_row.winfo_y() == 0 or date_row.grid_info()["row"] == 0
    assert "Replay" in shown._preview.cget("text")


def test_a_real_drag_on_a_handle_reorders_the_parts(form):
    shown = form()
    root = shown.winfo_toplevel()
    root.deiconify()
    shown.pack()
    root.update()
    handle = shown._rows["app"][0]
    target = shown._rows["prefix"][2]
    handle.event_generate("<ButtonPress-1>", x=2, y=2)
    handle.event_generate("<B1-Motion>", x=2, y=2, rooty=shown.winfo_rooty() + target.winfo_y() + 2)
    handle.event_generate("<ButtonRelease-1>", x=2, y=2)
    root.update()
    assert shown.get()["order"] == ["app", "prefix", "id", "date"]


def _resets(shown):
    """Every Reset button in the form, top to bottom."""
    found = []

    def walk(widget):
        for child in widget.winfo_children():
            if child.winfo_class() == "Button" and child.cget("text") == "Reset":
                found.append(child)
            walk(child)

    walk(shown)
    return found


def test_a_bad_separator_is_shown(form):
    shown = form(naming(separator="_"))
    shown.vars["separator"].set(" ")
    assert "Between Parts" in shown._preview.cget("text")


@pytest.mark.parametrize("changes", [{}, {"id": "guid"}, {"id": "number"}])
def test_the_form_opens_on_a_folder_of_clips_it_named(tmp_path, changes):
    # What Settings does first: the preview, on the real clip folder.
    tk = pytest.importorskip("tkinter")
    sys.path.insert(0, INSTALLER_DIR)
    import naming_form

    settings = naming(**changes)
    (tmp_path / f"{clip_naming.stem(settings, WHEN, number=3, guid='g', app='Deadlock')}.mp4").write_bytes(b"x")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("needs a display")
    root.withdraw()
    try:
        shown = naming_form.NamingForm(root, settings, folder=lambda: str(tmp_path))
        assert shown._preview.cget("text").startswith("Replay-")
    finally:
        root.destroy()


def test_every_item_has_a_reset(form):
    assert len(_resets(form())) == 6  # the four parts, the suffixes, the separator


def test_each_reset_puts_back_only_its_own_item(form):
    changed = naming(order=["app", "date", "id", "prefix"], prefix="Clip", prefix_on=False, separator="_",
                     date=False, date_format="ssmmhhddMMyyyy", id="guid", number_start=7, number_digits=2,
                     app=False, small_suffix="s", tracks_suffix="t", extra_suffix="e")
    groups = [("prefix", "prefix_on"), ("id", "number_start", "number_digits"), ("date", "date_format"), ("app",)]
    groups += [("small_suffix", "tracks_suffix", "extra_suffix"), ("separator",)]
    for number, keys in enumerate(groups):
        shown = form(changed)
        _resets(_FormOrder(shown))[number].invoke()
        expected = dict(changed, **{key: clip_naming.DEFAULTS[key] for key in keys})
        assert shown.get() == expected, keys


class _FormOrder:
    """A form's children with its parts in PARTS order, then the rest."""

    def __init__(self, shown):
        self.shown = shown

    def winfo_children(self):
        fields = [self.shown._rows[key][2] for key in clip_naming.PARTS]
        return fields + [child for child in self.shown.winfo_children() if child not in fields]
