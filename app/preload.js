const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("leutheria", {
  onLog: (callback) => ipcRenderer.on("log", (_event, line) => callback(line)),
  onChat: (callback) => ipcRenderer.on("chat", (_event, message) => callback(message)),
  onConfirmRequest: (callback) => ipcRenderer.on("confirm-request", (_event, request) => callback(request)),
  onSpeech: (callback) => ipcRenderer.on("speech", (_event, base64Wav) => callback(base64Wav)),
  sendCommand: (text) => ipcRenderer.send("user-command", text),
  sendConfirmResponse: (id, approved) => ipcRenderer.send("confirm-response", { id, approved }),
  sendAudio: (base64Data, format) => ipcRenderer.send("user-audio", { data: base64Data, format }),
});
