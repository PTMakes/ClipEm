import json

import pytest
import release_notes

ENTRIES = [{"version": v, "date": "2026-10-30", "sections": {"Fixes": [v]}} for v in ("1.4.10", "1.4.9", "1.4.8", "1.4.7")]


@pytest.mark.parametrize("old, new, expected", [
    ("1.4.8", "1.4.10", ["1.4.10", "1.4.9"]),
    ("1.4.9", "1.4.10", ["1.4.10"]),
    ("1.4.9", "1.4.9", []),
    (None, "1.4.9", ["1.4.9"]),
    ("1.4.8", "1.5.0", ["1.4.10", "1.4.9"]),  # 1.5.0 has no notes of its own
    ("1.4.9-dev.202610142130", "1.4.9", ["1.4.9"]),  # a dev build sorts before its release
    ("1.4.8", "1.4.9-dev.202610142130", ["1.4.9"]),  # a dev build shows its coming version's
    (None, "1.4.9-dev.202610142130", ["1.4.9"]),
])
def test_since(old, new, expected):
    assert [e["version"] for e in release_notes.since(ENTRIES, old, new)] == expected


@pytest.mark.parametrize("content", ["{broken", "{}", '[{"version": "1.0.0"}, "x"]', '[{"version": "1.0.0", "sections": {}}]'])
def test_load_keeps_only_usable_entries(tmp_path, content):
    path = tmp_path / "release_notes.json"
    path.write_text(content)
    assert release_notes.load(str(path)) == []


def test_load_missing_file(tmp_path):
    assert release_notes.load(str(tmp_path / "missing.json")) == []


def test_load_reads_the_history(tmp_path):
    path = tmp_path / "release_notes.json"
    path.write_text(json.dumps(ENTRIES))
    assert release_notes.load(str(path)) == ENTRIES


def test_lines_lay_out_each_version():
    entry = {"version": "1.4.10", "date": "2026-10-30", "sections": {"New Features": ["A", "B"], "Fixes": ["C"]}}
    body = [("section", "New Features"), ("note", "\u2022 A"), ("note", "\u2022 B"), (None, ""),
            ("section", "Fixes"), ("note", "\u2022 C"), (None, "")]
    assert release_notes.lines([entry]) == [("version", "1.4.10  (2026-10-30)"), *body]
    assert release_notes.lines([entry], versions=False) == body
    assert release_notes.lines([]) == []


def test_new_settings_are_gathered_without_repeats():
    one = {"key": "post_clip", "name": "Post-clip"}
    two = {"key": "view_logs", "name": "View Logs"}
    entries = [{"version": "1.5.0", "new_settings": [one]}, {"version": "1.4.9", "new_settings": [one, two]},
               {"version": "1.4.8"}]
    assert release_notes.new_settings(entries) == [one, two]
    assert release_notes.new_settings([]) == []
