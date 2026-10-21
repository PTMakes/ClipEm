"use strict";

// What ClipEm's editor page may ask of its window (main.js): a native
// "Export As" save dialog and a folder picker. Nothing else of Electron or Node reaches the page.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("clipem", {
  chooseFolder: (defaultPath) => ipcRenderer.invoke("clipem:choose-folder", { defaultPath: String(defaultPath || "") }),
  saveAs: (defaultPath) => ipcRenderer.invoke("clipem:save-as", { defaultPath: String(defaultPath || "") }),
});
