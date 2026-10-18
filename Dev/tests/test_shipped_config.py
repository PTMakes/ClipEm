"""The OBS config in App/Shared/OBS-Config ships to every user as-is. Running
ClipEm from a checkout writes machine-specific values into those same files
(it's the live config there too), and that once got committed and released:
every install defaulted to the developer's clips folder."""

import os
import re

import pytest
from conftest import APP_SHARED_DIR

OBS_CONFIG_DIR = os.path.join(APP_SHARED_DIR, "OBS-Config")
MACHINE_SPECIFIC = re.compile(r"/home/[^/\s\"]+|/Users/[^/\s\"]+|[A-Za-z]:\\\\+Users|Development/PTMClipEm")


def _shipped_files():
    for dirpath, _dirnames, filenames in os.walk(OBS_CONFIG_DIR):
        if "logs" in dirpath.split(os.sep):
            continue  # OBS's own logs - gitignored, never shipped
        for name in filenames:
            yield os.path.join(dirpath, name)


@pytest.mark.parametrize("path", sorted(_shipped_files()), ids=lambda p: os.path.relpath(p, OBS_CONFIG_DIR))
def test_no_paths_from_a_developer_machine(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        found = sorted(set(MACHINE_SPECIFIC.findall(f.read())))
    assert not found, (
        f"{os.path.relpath(path, APP_SHARED_DIR)} contains machine-specific paths {found} - "
        "probably written by running ClipEm from this checkout. Restore it with git before committing."
    )


def test_clips_folder_is_the_placeholder():
    with open(os.path.join(OBS_CONFIG_DIR, "basic", "profiles", "DefaultPTM", "basic.ini")) as f:
        values = dict(line.strip().split("=", 1) for line in f if "FilePath=" in line)
    assert values == {"FilePath": "{{CLIPS_DIR}}", "RecFilePath": "{{CLIPS_DIR}}", "FFFilePath": "{{CLIPS_DIR}}"}


def test_new_installs_start_with_the_clip_saved_notification_off():
    import json

    with open(os.path.join(APP_SHARED_DIR, "run_config.json"), encoding="utf-8") as f:
        shipped = json.load(f)
    assert shipped["clip_saved_notification"] is False
    # Written per install on its first Windows launch - never shipped.
    assert "start_menu_registered" not in shipped
