import sys

import pytest
from conftest import TRAY_WIDGET_DIR

pytest.importorskip("dbus_next")
sys.path.insert(0, TRAY_WIDGET_DIR)
import sni_tray  # noqa: E402


class FakeIcon:
    def __init__(self, menu):
        self.menu = menu


def test_layout_is_only_announced_when_the_menu_changes(monkeypatch):
    """The tray widget asks for a menu update on every poll; telling the
    tray host each time makes it rebuild the open menu and drop clicks."""
    state = {"recording": False}
    menu = sni_tray.Menu(
        sni_tray.MenuItem(lambda item: "Stop Recording" if state["recording"] else "Start Recording", lambda *a: None),
        sni_tray.MenuItem("Quit", sni_tray.Menu(sni_tray.MenuItem("Quit Tray", lambda *a: None))),
    )
    iface = sni_tray._DBusMenuInterface(FakeIcon(menu))
    sent = []
    monkeypatch.setattr(iface, "LayoutUpdated", lambda revision, parent: sent.append(revision))
    for _ in range(5):
        iface.announce_layout_changed()
    assert sent == []  # nothing changed - no rebuilds
    state["recording"] = True
    iface.announce_layout_changed()
    iface.announce_layout_changed()
    assert sent == [2]  # one announcement for the one change
