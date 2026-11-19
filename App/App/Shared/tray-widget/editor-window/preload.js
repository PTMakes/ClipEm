"use strict";

// What ClipEm's editor page may ask of its window (main.js): a native
// "Export As" save dialog, a folder picker and copying an export to the clipboard. Nothing else of Electron or Node reaches the page.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("clipem", {
  copyFile: (filePath) => ipcRenderer.invoke("clipem:copy-file", String(filePath || "")),
  chooseFolder: (defaultPath) => ipcRenderer.invoke("clipem:choose-folder", { defaultPath: String(defaultPath || "") }),
  saveAs: (defaultPath) => ipcRenderer.invoke("clipem:save-as", { defaultPath: String(defaultPath || "") }),
});
