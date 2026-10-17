"use strict";

// ClipEm's editor window: run by clip_browser.py with ClipEm's own Electron
// (Installer/editor_window.py) as
//   <electron> <this folder> --clipem-url=<editor page> --clipem-profile=<dir> --clipem-icon=<png>
// It shows that one page - ClipEm's local editor on 127.0.0.1 - in a window
// with ClipEm's name and icon, and nothing else. A second launch with the
// same profile just brings the open window to the front.
//
// package.json's desktopName sets the window's app id on Linux (Wayland),
// matching the clipem-editor.desktop entry clip_browser.py writes, so the
// taskbar shows ClipEm's icon.

const { app, BrowserWindow, Menu } = require("electron");

function arg(name) {
  const prefix = `--clipem-${name}=`;
  const found = process.argv.find((a) => a.startsWith(prefix));
  return found ? found.slice(prefix.length) : null;
}

const url = arg("url");
const profile = arg("profile");
const icon = arg("icon");

if (!url || !/^http:\/\/127\.0\.0\.1:\d+\//.test(url)) {
  console.error("ClipEm editor window: --clipem-url must be ClipEm's local editor page.");
  app.exit(2);
} else {
  start();
}

function start() {
  const origin = new URL(url).origin;
  if (profile) app.setPath("userData", profile);
  app.setName("ClipEm Editor");
  // Hover-to-play has sound from the first hover, no click needed.
  app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
  if (process.platform === "win32") app.setAppUserModelId("ClipEm.Editor");
  // macOS keeps its default menu - copy/paste shortcuts live there.
  if (process.platform !== "darwin") Menu.setApplicationMenu(null);

  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }

  let win = null;
  app.on("second-instance", () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });

  app.whenReady().then(() => {
    if (process.platform === "darwin" && icon && app.dock) app.dock.setIcon(icon);
    win = new BrowserWindow({
      width: 1440,
      height: 900,
      title: "ClipEm Editor",
      icon: icon || undefined,
      autoHideMenuBar: true,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: false },
    });
    // Only ClipEm's own page: no pop-ups, no navigating anywhere else.
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    win.webContents.on("will-navigate", (event, target) => {
      if (new URL(target).origin !== origin) event.preventDefault();
    });
    win.on("closed", () => { win = null; });
    win.loadURL(url);
  });

  app.on("window-all-closed", () => app.quit());
}
