"""Scripts/clip_claim.py: exactly one of the tray widget and the OBS script
finishes each saved clip."""

import os
import sys

from conftest import SCRIPTS_DIR

sys.path.insert(0, SCRIPTS_DIR)
import clip_claim  # noqa: E402


def test_only_the_first_claim_wins(tmp_path):
    clip = str(tmp_path / "Replay.mkv")
    assert clip_claim.claim(clip, "tray", directory=str(tmp_path / "claims"))
    assert not clip_claim.claim(clip, "obs", directory=str(tmp_path / "claims"))
    assert clip_claim.claimed_by(clip, directory=str(tmp_path / "claims")) == "tray"


def test_each_clip_is_claimed_on_its_own(tmp_path):
    directory = str(tmp_path / "claims")
    assert clip_claim.claim(str(tmp_path / "a.mkv"), "tray", directory=directory)
    assert clip_claim.claim(str(tmp_path / "b.mkv"), "obs", directory=directory)
    assert clip_claim.claimed_by(str(tmp_path / "c.mkv"), directory=directory) is None


def test_the_same_file_by_another_spelling_is_the_same_clip(tmp_path):
    directory = str(tmp_path / "claims")
    assert clip_claim.claim(str(tmp_path / "a.mkv"), "tray", directory=directory)
    assert not clip_claim.claim(os.path.join(str(tmp_path), ".", "a.mkv"), "obs", directory=directory)


def test_old_claims_are_cleared(tmp_path):
    directory = str(tmp_path / "claims")
    clip = str(tmp_path / "a.mkv")
    assert clip_claim.claim(clip, "tray", directory=directory)
    later = os.path.getmtime(os.path.join(directory, os.listdir(directory)[0])) + clip_claim.CLAIM_KEEP_SECONDS + 1
    assert clip_claim.claim(str(tmp_path / "b.mkv"), "obs", directory=directory, now=later)
    assert clip_claim.claimed_by(clip, directory=directory) is None


def test_a_claim_that_cant_be_written_still_finishes_the_clip(tmp_path):
    """Finishing a clip twice beats not finishing it."""
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    assert clip_claim.claim(str(tmp_path / "a.mkv"), "obs", directory=str(blocker / "claims"))
