"""replace_file.py: replacing or renaming a clip something still has open -
which Windows refuses until it's let go."""

import os
import sys
import threading

import pytest
from conftest import SCRIPTS_DIR

sys.path.insert(0, SCRIPTS_DIR)
import replace_file  # noqa: E402


def test_a_refused_replace_is_tried_again_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(replace_file.sys, "platform", "win32")
    tries, waits = [], []

    def busy_then_free(src, dst):
        tries.append(dst)
        if len(tries) < 3:
            raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(replace_file.os, "replace", busy_then_free)
    replace_file.replace("a.partial", "a.mp4", sleep=waits.append)
    assert len(tries) == 3 and waits == [replace_file.POLL_SECONDS] * 2


def test_it_gives_up_once_the_wait_is_over(monkeypatch):
    monkeypatch.setattr(replace_file.sys, "platform", "win32")
    monkeypatch.setattr(replace_file.os, "rename", lambda src, dst: (_ for _ in ()).throw(PermissionError("in use")))
    with pytest.raises(PermissionError):
        replace_file.rename("a.mp4", "b.mp4", wait_seconds=0, sleep=lambda s: None)


def test_elsewhere_a_refusal_is_final(monkeypatch):
    monkeypatch.setattr(replace_file.sys, "platform", "linux")
    calls = []

    def refuse(src, dst):
        calls.append(src)
        raise PermissionError("read-only folder")

    monkeypatch.setattr(replace_file.os, "replace", refuse)
    with pytest.raises(PermissionError):
        replace_file.replace("a.partial", "a.mp4", sleep=lambda s: pytest.fail("waited"))
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to replace an open file")
def test_a_clip_being_read_is_replaced_once_its_let_go(tmp_path):
    clip, partial = tmp_path / "clip.mp4", tmp_path / "clip.mp4.partial"
    clip.write_bytes(b"old")
    partial.write_bytes(b"new")
    reader = open(clip, "rb")  # what ffmpeg making a waveform does
    threading.Timer(1.0, reader.close).start()
    replace_file.replace(str(partial), str(clip))
    assert clip.read_bytes() == b"new" and not os.path.exists(partial)
