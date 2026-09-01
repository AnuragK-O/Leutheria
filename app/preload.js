const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("leutheria", {
  onLog: (callback) => ipcRenderer.on("log", (_event, line) => callback(line)),
  onChat: (callback) => ipcRenderer.on("chat", (_event, message) => callback(message)),
  sendCommand: (text) => ipcRenderer.send("user-command", text),
});
