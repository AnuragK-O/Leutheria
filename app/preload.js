const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("leutheria", {
  onLog: (callback) => ipcRenderer.on("log", (_event, line) => callback(line)),
});
