"use strict";

// ClipEm's editor window: run by clip_browser.py with ClipEm's own Electron
// (Installer/editor_window.py) as
//   <electron> <this folder> --clipem-url=<editor page> --clipem-profile=<dir> --clipem-icon=<png>
//              [--clipem-parent-pid=<pid>]
// It shows that one page - ClipEm's local editor on 127.0.0.1 - in a
// maximized window with ClipEm's name and icon, and nothing else. A second
// launch with the same profile just brings the open window to the front. It
// closes if clip_browser.py (the parent pid, which serves the page) is gone,
// so a window whose server has died never stays around to be brought back.
//
// package.json's desktopName sets the window's app id on Linux (Wayland),
// matching the clipem-editor.desktop entry clip_browser.py writes, so the
// taskbar shows ClipEm's icon.

const path = require("path");
const { app, BrowserWindow, Menu, dialog, ipcMain } = require("electron");

function arg(name) {
  const prefix = `--clipem-${name}=`;
  const found = process.argv.find((a) => a.startsWith(prefix));
  return found ? found.slice(prefix.length) : null;
}

const url = arg("url");
const profile = arg("profile");
const icon = arg("icon");
const parentPid = Number(arg("parent-pid")) || null;
const PARENT_POLL_MS = 2000;
const FOCUS_CHECK_MS = 200;

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
    if (win) bringToFront(win);
  });

  if (parentPid) {
    setInterval(() => {
      try {
        process.kill(parentPid, 0);
      } catch (err) {
        if (err.code === "ESRCH") app.quit();
      }
    }, PARENT_POLL_MS);
  }

  app.whenReady().then(() => {
    if (process.platform === "darwin" && icon && app.dock) app.dock.setIcon(icon);
    win = new BrowserWindow({
      width: 1440,
      height: 900,
      title: "ClipEm Editor",
      icon: icon || undefined,
      autoHideMenuBar: true,
      show: false, // shown maximized, below
      webPreferences: {
        contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: false,
        preload: path.join(__dirname, "preload.js"),
      },
    });
    // Only ClipEm's own page: no pop-ups, no navigating anywhere else.
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    win.webContents.on("will-navigate", (event, target) => {
      if (new URL(target).origin !== origin) event.preventDefault();
    });
    win.on("closed", () => { win = null; });
    win.maximize();
    win.show();
    win.loadURL(url);
  });

  // "Export As": the page asks for a save path (preload.js); only ClipEm's
  // own page, and only one dialog at a time on this window.
  ipcMain.handle("clipem:save-as", async (event, options) => {
    if (!win || new URL(event.senderFrame.url).origin !== origin) return null;
    const result = await dialog.showSaveDialog(win, {
      title: "Export As",
      defaultPath: (options && options.defaultPath) || undefined,
      filters: [{ name: "MP4 video", extensions: ["mp4"] }],
      properties: ["showOverwriteConfirmation", "createDirectory"],
    });
    return result.canceled || !result.filePath ? null : result.filePath;
  });

  // The clip list's folder button: pick another folder to view clips from.
  ipcMain.handle("clipem:choose-folder", async (event, options) => {
    if (!win || new URL(event.senderFrame.url).origin !== origin) return null;
    const result = await dialog.showOpenDialog(win, {
      title: "Choose a clips folder",
      defaultPath: (options && options.defaultPath) || undefined,
      properties: ["openDirectory"],
    });
    return result.canceled || !result.filePaths.length ? null : result.filePaths[0];
  });

  app.on("window-all-closed", () => app.quit());
}

// Wayland compositors (KWin, Mutter) ignore focus() from an app that has no
// activation token - and a launch from the tray menu brings none - so the
// window would only flash in the taskbar. They do activate a newly mapped
// window, so if focus() didn't take, hide it and show it again. The page
// itself is untouched.
function bringToFront(win) {
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  setTimeout(() => {
    if (win.isDestroyed() || win.isFocused()) return;
    const maximized = win.isMaximized();
    win.hide();
    if (maximized) win.maximize();
    win.show();
    win.focus();
  }, FOCUS_CHECK_MS);
}
