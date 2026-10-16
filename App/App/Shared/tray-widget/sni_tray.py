"""Pure-Python Linux tray backend: talks StatusNotifierItem + dbusmenu
directly over D-Bus instead of going through pystray's AppIndicator/GTK
backend, which needs PyGObject + libayatana-appindicator - system packages
with a different name on every distro that pip has no way to vendor (see
setup.py's old check_tray_prereqs).

libappindicator itself is just a convenience wrapper around these same two
D-Bus interfaces, so this doesn't change what a user needs on their desktop
to see a tray icon (a StatusNotifierWatcher still has to be running - GNOME
Shell needs its "AppIndicator and KStatusNotifierItem Support" extension for
that either way) - it only removes the GTK/GObject-Introspection dependency
from our side.

Exposes just the slice of pystray's API tray_widget.py actually uses, so
that module can import this in place of pystray with no other changes:
Icon(name, icon=PIL_image, title=str, menu=Menu), Menu(*items),
MenuItem(text_or_fn, action, enabled=fn_or_None, visible=fn_or_None).

Specs implemented (minimally - one level of submenus, no checkboxes/icons
per item):
  https://freedesktop.org/wiki/Specifications/StatusNotifierItem/
  https://github.com/AyatanaIndicators/libdbusmenu (com.canonical.dbusmenu)
"""

import asyncio
import os
import sys
import threading

from dbus_next import Message, MessageType, PropertyAccess, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, dbus_property, method, signal

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
# Where helpers report back to the tray widget over this same connection -
# obs_window.py's KWin script telling it whether OBS's window is shown.
from obs_window import CLIPEM_INTERFACE, CLIPEM_PATH  # noqa: E402

WATCHER_BUS_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"


def _image_to_pixmap(image):
    """PIL image -> the (width, height, ARGB32-network-byte-order bytes)
    triple the IconPixmap property wants."""
    image = image.convert("RGBA")
    w, h = image.size
    src = image.tobytes("raw", "RGBA")
    argb = bytearray(len(src))
    for i in range(0, len(src), 4):
        r, g, b, a = src[i], src[i + 1], src[i + 2], src[i + 3]
        argb[i] = a
        argb[i + 1] = r
        argb[i + 2] = g
        argb[i + 3] = b
    return w, h, bytes(argb)



def _run_action(item, icon):
    """Runs a menu item's action on its own thread, as pystray's other
    backends do. These handlers run on the D-Bus event loop, and actions can
    take a while (closing OBS waits for it to exit) or talk back to the loop
    (Icon.notify waits for it) - run inline, they'd freeze the icon and
    deadlock notifications."""
    threading.Thread(target=item.action, args=(icon, item), daemon=True, name="tray-menu-action").start()

class MenuItem:
    def __init__(self, text, action, enabled=None, visible=None):
        self._text = text
        self._enabled = enabled
        self._visible = visible
        # Matches pystray's own convention: passing a Menu as the action
        # makes this item a submenu container (hover to expand) instead of
        # a directly-clickable entry - see Quit's "close OBS too?" submenu
        # in tray_widget.py.
        if isinstance(action, Menu):
            self.submenu = action
            self.action = None
        else:
            self.submenu = None
            self.action = action

    def label(self):
        return self._text(self) if callable(self._text) else self._text

    def is_enabled(self):
        return self._enabled(self) if self._enabled is not None else True

    def is_visible(self):
        return self._visible(self) if self._visible is not None else True


class Menu:
    def __init__(self, *items):
        self.items = list(items)


class _ClipEmInterface(ServiceInterface):
    def __init__(self, icon):
        super().__init__(CLIPEM_INTERFACE)
        self._icon = icon

    @method()
    def ObsWindowVisible(self, visible: "b"):
        callback = self._icon.obs_window_callback
        if callback is not None:
            # Off the D-Bus loop, like menu actions (see _run_action).
            threading.Thread(target=callback, args=(bool(visible),), daemon=True).start()


class _StatusNotifierItemInterface(ServiceInterface):
    def __init__(self, icon):
        super().__init__("org.kde.StatusNotifierItem")
        self._icon = icon

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return self._icon.name

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return self._icon.title

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        image = self._icon.icon
        if image is None:
            return []
        w, h, data = _image_to_pixmap(image)
        return [[w, h, data]]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return ["", [], self._icon.title, ""]

    @method()
    def Activate(self, x: "i", y: "i"):
        pass

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        pass

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @signal()
    def NewIcon(self):
        pass

    @signal()
    def NewTitle(self):
        pass

    @signal()
    def NewToolTip(self):
        pass

    def announce_new_icon(self):
        self.NewIcon()
        self.emit_properties_changed({"IconPixmap": self.IconPixmap})

    def announce_new_title(self):
        self.NewTitle()
        self.emit_properties_changed({"Title": self.Title})


def _item_properties(item):
    props = {
        "label": Variant("s", item.label()),
        "enabled": Variant("b", item.is_enabled()),
        "visible": Variant("b", item.is_visible()),
        "type": Variant("s", "standard"),
    }
    if item.submenu is not None:
        props["children-display"] = Variant("s", "submenu")
    return props


class _DBusMenuInterface(ServiceInterface):
    def __init__(self, icon):
        super().__init__("com.canonical.dbusmenu")
        self._icon = icon
        self._revision = 1
        # dbusmenu item ids start at 1; 0 is reserved for the (implicit)
        # root. Submenu items are flattened into this same id space so
        # Event/GetProperty/etc. don't need to know how deep an item is.
        self._items_by_id = {}
        self._next_id = 1
        self._assign_ids(icon.menu.items)

    def _assign_ids(self, items):
        for item in items:
            self._items_by_id[self._next_id] = item
            item._dbus_id = self._next_id
            self._next_id += 1
            if item.submenu is not None:
                self._assign_ids(item.submenu.items)

    def _children_variants(self, items):
        return [
            Variant(
                "(ia{sv}av)",
                [
                    item._dbus_id,
                    _item_properties(item),
                    self._children_variants(item.submenu.items) if item.submenu else [],
                ],
            )
            for item in items
        ]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: "as") -> "u(ia{sv}av)":
        if parent_id == 0:
            children = self._children_variants(self._icon.menu.items)
            root = [0, {"children-display": Variant("s", "submenu")}, children]
        else:
            item = self._items_by_id.get(parent_id)
            if item is None:
                root = [parent_id, {}, []]
            else:
                children = self._children_variants(item.submenu.items) if item.submenu else []
                root = [parent_id, _item_properties(item), children]
        return [self._revision, root]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        result = []
        for item_id in ids:
            item = self._items_by_id.get(item_id)
            if item is not None:
                result.append([item_id, _item_properties(item)])
        return result

    @method()
    def GetProperty(self, id: "i", name: "s") -> "v":
        item = self._items_by_id.get(id)
        props = _item_properties(item) if item else {}
        return props.get(name, Variant("s", ""))

    @method()
    def Event(self, id: "i", event_id: "s", data: "v", timestamp: "u"):
        if event_id != "clicked":
            return
        item = self._items_by_id.get(id)
        if item is not None and item.action is not None and item.is_enabled():
            _run_action(item, self._icon)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        missing = []
        for item_id, event_id, _data, _timestamp in events:
            item = self._items_by_id.get(item_id)
            if item is None:
                missing.append(item_id)
            elif event_id == "clicked" and item.action is not None and item.is_enabled():
                _run_action(item, self._icon)
        return missing

    @method()
    def AboutToShow(self, id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "(aiai)":
        return [[], []]

    @signal()
    def LayoutUpdated(self, revision, parent_id) -> "ui":
        return [revision, parent_id]

    @signal()
    def ItemsPropertiesUpdated(self, updated, removed) -> "a(ia{sv})a(ias)":
        return [updated, removed]

    def announce_layout_changed(self):
        self._revision += 1
        self.LayoutUpdated(self._revision, 0)


class Icon:
    """Mimics the slice of pystray.Icon's API tray_widget.py relies on, but
    backed by our own StatusNotifierItem/dbusmenu D-Bus service instead of
    pystray's GTK/AppIndicator or legacy-XEmbed backends."""

    def __init__(self, name, icon=None, title="", menu=None):
        self.name = name
        self._icon_image = icon
        self._title = title
        self.menu = menu

        self._loop = None
        self._bus = None
        # Our well-known D-Bus name once running, and a callable taking a
        # bool for ObsWindowVisible reports (see _ClipEmInterface).
        self.bus_name = None
        self.obs_window_callback = None
        self._sni_iface = None
        self._menu_iface = None
        self._stop_event = None

    @property
    def icon(self):
        return self._icon_image

    @icon.setter
    def icon(self, image):
        self._icon_image = image
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._sni_iface.announce_new_icon)

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._sni_iface.announce_new_title)

    def update_menu(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._menu_iface.announce_layout_changed)

    def notify(self, message, title):
        if self._loop is None:
            raise RuntimeError("tray icon is not running yet")
        future = asyncio.run_coroutine_threadsafe(self._send_notification(message, title), self._loop)
        future.result(timeout=5)

    async def _send_notification(self, message, title):
        # A plain method call rather than an introspected proxy: dbus-next's
        # introspection parser is strict, and a notification server whose
        # XML it rejects would make every notification fail.
        reply = await self._bus.call(Message(
            destination="org.freedesktop.Notifications",
            path="/org/freedesktop/Notifications",
            interface="org.freedesktop.Notifications",
            member="Notify",
            signature="susssasa{sv}i",
            body=[self.name, 0, "", title, message, [], {}, 5000],
        ))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")

    def stop(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def run(self):
        asyncio.run(self._main())

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._bus = await MessageBus().connect()

        self._sni_iface = _StatusNotifierItemInterface(self)
        self._menu_iface = _DBusMenuInterface(self)
        self._bus.export(SNI_PATH, self._sni_iface)
        self._bus.export(MENU_PATH, self._menu_iface)
        self._bus.export(CLIPEM_PATH, _ClipEmInterface(self))

        bus_name = f"org.freedesktop.StatusNotifierItem-{os.getpid()}-1"
        await self._bus.request_name(bus_name)
        self.bus_name = bus_name

        # If nothing implements StatusNotifierWatcher at all (e.g. vanilla
        # GNOME without the AppIndicator extension), this raises - swallow
        # it rather than crashing the whole widget. tray_widget.py's own
        # tray_host_available() check (a plain dbus-send probe, unaffected
        # by any of this) still catches that case and shuts down cleanly a
        # little later so a headless-looking process doesn't run forever.
        try:
            watcher_intr = await self._bus.introspect(WATCHER_BUS_NAME, WATCHER_PATH)
            watcher_proxy = self._bus.get_proxy_object(WATCHER_BUS_NAME, WATCHER_PATH, watcher_intr)
            watcher_iface = watcher_proxy.get_interface(WATCHER_BUS_NAME)
            await watcher_iface.call_register_status_notifier_item(bus_name)
        except Exception as exc:
            print(f"Could not register with a StatusNotifierWatcher: {exc}", file=sys.stderr)

        try:
            await self._stop_event.wait()
        finally:
            self._bus.disconnect()
