const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("leutheria", {
  onLog: (callback) => ipcRenderer.on("log", (_event, line) => callback(line)),
  sendCommand: (text) => ipcRenderer.send("user-command", text),
});
