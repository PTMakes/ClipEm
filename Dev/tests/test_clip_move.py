"""Moving or copying clips when the clip folder changes (Installer/clip_move.py)."""

import json
import os
import sys

from conftest import INSTALLER_DIR, TRAY_WIDGET_DIR

sys.path.insert(0, INSTALLER_DIR)
sys.path.insert(0, TRAY_WIDGET_DIR)
import clip_browser  # noqa: E402
import clip_move  # noqa: E402


def clip(folder, name="a.mp4", data=b"video", when=1_000_000):
    path = folder / name
    path.write_bytes(data)
    os.utime(path, (when, when))
    return path


def folders(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    return old, new


def test_only_clips_are_listed(tmp_path):
    old, _new = folders(tmp_path)
    for name in ("a.mp4", "b.MKV", ".hidden.mp4", "notes.txt", "c.mov.partial"):
        clip(old, name)
    (old / "sub").mkdir()
    clip(old / "sub", "deep.mp4")
    assert [os.path.basename(p) for p in clip_move.list_clips(old)] == ["a.mp4", "b.MKV"]


def test_a_missing_folder_has_no_clips(tmp_path):
    assert clip_move.list_clips(tmp_path / "gone") == []


def test_moving_takes_them_all_keeping_their_dates(tmp_path):
    old, new = folders(tmp_path)
    clip(old, "a.mp4", b"one")
    clip(old, "a-tracks.mkv", b"two")
    (old / "keep.txt").write_text("not a clip")
    seen = []
    result = clip_move.transfer(old, new, move=True, progress=lambda done, total: seen.append((done, total)))
    assert result == {"done": 2, "skipped": [], "failed": []}
    assert sorted(os.listdir(old)) == ["keep.txt"]
    assert (new / "a.mp4").read_bytes() == b"one" and (new / "a-tracks.mkv").read_bytes() == b"two"
    assert os.path.getmtime(new / "a.mp4") == 1_000_000
    assert seen[-1] == (6, 6)


def test_copying_leaves_them_too(tmp_path):
    old, new = folders(tmp_path)
    clip(old)
    assert clip_move.transfer(old, new, move=False)["done"] == 1
    assert (old / "a.mp4").exists() and (new / "a.mp4").read_bytes() == b"video"
    assert os.path.getmtime(new / "a.mp4") == 1_000_000
    assert [n for n in os.listdir(new) if n.startswith(".")] == []


def test_nothing_is_overwritten(tmp_path):
    old, new = folders(tmp_path)
    clip(old, "a.mp4", b"old one")
    new.mkdir()
    clip(new, "a.mp4", b"new one")
    assert clip_move.transfer(old, new, move=True) == {"done": 0, "skipped": ["a.mp4"], "failed": []}
    assert (old / "a.mp4").read_bytes() == b"old one" and (new / "a.mp4").read_bytes() == b"new one"


def test_a_clip_that_wont_move_is_reported_and_the_rest_go_on(tmp_path, monkeypatch):
    old, new = folders(tmp_path)
    clip(old, "a.mp4")
    clip(old, "b.mp4")
    real_rename = os.rename

    def rename(path, target):
        if str(path).endswith("a.mp4"):
            raise OSError("cross-device")
        real_rename(path, target)

    def copy(path, target, copied):
        raise PermissionError("in use")

    monkeypatch.setattr(clip_move.os, "rename", rename)
    monkeypatch.setattr(clip_move, "_copy", copy)
    result = clip_move.transfer(old, new, move=True)
    assert result["done"] == 1 and result["failed"][0][0] == "a.mp4"
    assert (old / "a.mp4").exists() and (new / "b.mp4").exists()


def test_a_move_across_drives_copies_then_removes(tmp_path, monkeypatch):
    old, new = folders(tmp_path)
    clip(old)

    def rename(path, target):
        raise OSError("cross-device")

    monkeypatch.setattr(clip_move.os, "rename", rename)
    assert clip_move.transfer(old, new, move=True)["done"] == 1
    assert not (old / "a.mp4").exists() and (new / "a.mp4").read_bytes() == b"video"


def test_the_key_is_the_editors(tmp_path):
    old, _new = folders(tmp_path)
    path = clip(old)
    assert clip_move.edit_key(path) == clip_browser.thumb_key(str(path), os.path.getmtime(path))


def test_a_clips_edit_goes_with_it(tmp_path):
    old, new = folders(tmp_path)
    path = clip(old)
    edits = tmp_path / "edits"
    edits.mkdir()
    key = clip_move.edit_key(path)
    (edits / f"{key}.json").write_text(json.dumps({"clip": str(path), "layers": []}))
    clip_move.transfer(old, new, move=True, edits_dir=str(edits))
    assert not (edits / f"{key}.json").exists()
    moved = json.loads((edits / f"{clip_move.edit_key(new / 'a.mp4')}.json").read_text())
    assert moved["clip"] == os.path.realpath(new / "a.mp4")


def test_a_copy_keeps_the_edit_for_both(tmp_path):
    old, new = folders(tmp_path)
    path = clip(old)
    edits = tmp_path / "edits"
    edits.mkdir()
    (edits / f"{clip_move.edit_key(path)}.json").write_text("{}")
    clip_move.transfer(old, new, move=False, edits_dir=str(edits))
    assert len(os.listdir(edits)) == 2


def test_exports_follow_a_move(tmp_path):
    old, new = folders(tmp_path)
    source, export = clip(old, "a.mp4"), clip(old, "a-edit.mp4")
    record = tmp_path / "exports.json"
    source_path, export_path = os.path.realpath(source), os.path.realpath(export)
    record.write_text(json.dumps({source_path: {"file": export_path, "time": 5},
                                  "/other/x.mp4": {"file": "/other/y.mp4", "time": 1}}))
    clip_move.transfer(old, new, move=True, exports_path=str(record))
    assert json.loads(record.read_text()) == {
        os.path.realpath(new / "a.mp4"): {"file": os.path.realpath(new / "a-edit.mp4"), "time": 5},
        "/other/x.mp4": {"file": "/other/y.mp4", "time": 1}}


def test_too_little_room_is_said_before_starting(tmp_path, monkeypatch):
    old, new = folders(tmp_path)
    new.mkdir()
    path = clip(old, data=b"x" * 2000)

    class Usage:
        free = 1000

    monkeypatch.setattr(clip_move.shutil, "disk_usage", lambda folder: Usage)
    assert "free" in clip_move.room_problem([str(path)], new, move=False)
    assert clip_move.room_problem([str(path)], new, move=True) is None  # one drive: nothing to write
