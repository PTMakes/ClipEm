"""The pinned packages still work on the Pythons ClipEm ships."""

import os
import re

from conftest import TRAY_WIDGET_DIR

DEV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _release_constant(name):
    with open(os.path.join(DEV_DIR, "release.sh"), encoding="utf-8") as f:
        match = re.search(rf'^{name}="([^"]+)"', f.read(), re.MULTILINE)
    assert match, f"release.sh has no {name}"
    return match.group(1)


def _pin(package):
    with open(os.path.join(TRAY_WIDGET_DIR, "requirements.txt"), encoding="utf-8") as f:
        match = re.search(rf"^{re.escape(package)}==([^;\s]+)", f.read(), re.MULTILINE)
    return match.group(1) if match else None


def test_the_linux_tray_python_still_has_what_dbus_next_needs():
    """dbus-next (the Linux tray's D-Bus, sni_tray.py) is unmaintained, and its
    0.2.3 decorates every D-Bus method with typing.no_type_check_decorator -
    deprecated in Python 3.13 and removed in 3.15, where importing sni_tray
    fails and Linux gets no tray icon. The tray runs on ClipEm's portable
    Python (release.sh's LINUX_PYTHON_VERSION), so that's safe until it's
    bumped to 3.15: move to dbus-fast (a maintained fork, same API) first.
    The tests' warnings filter (Dev/pytest.ini) relies on this check."""
    if _pin("dbus-next") is None:
        return  # moved off it
    version = tuple(int(part) for part in _release_constant("LINUX_PYTHON_VERSION").split(".")[:2])
    assert version < (3, 15), (
        f"The Linux Python is {version[0]}.{version[1]}, but dbus-next {_pin('dbus-next')} needs "
        "typing.no_type_check_decorator, which Python 3.15 removed - switch sni_tray.py to dbus-fast first."
    )


def test_both_platforms_ship_the_same_python():
    """release.sh checks this as it runs - here it's caught before a release."""
    import setup

    assert _release_constant("LINUX_PYTHON_VERSION") == setup.WINDOWS_PYTHON_VERSION
    assert _release_constant("LINUX_PYTHON_PBS_TAG") == setup.WINDOWS_PYTHON_PBS_TAG
