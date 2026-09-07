const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("leutheria", {
  // --- event stream (agent -> UI) ---
  onLog: (callback) => ipcRenderer.on("log", (_event, line) => callback(line)),
  onChat: (callback) => ipcRenderer.on("chat", (_event, message) => callback(message)),
  onTimelineEvent: (callback) =>
    ipcRenderer.on("timeline-event", (_event, eventData) => callback(eventData)),
  onGhostTarget: (callback) =>
    ipcRenderer.on("ghost-target", (_event, target) => callback(target)),
  onConfirmRequest: (callback) =>
    ipcRenderer.on("confirm-request", (_event, request) => callback(request)),
  onConfirmResolved: (callback) =>
    ipcRenderer.on("confirm-resolved", (_event, payload) => callback(payload)),
  onSpeech: (callback) => ipcRenderer.on("speech", (_event, base64Wav) => callback(base64Wav)),
  onSkillProposed: (callback) =>
    ipcRenderer.on("skill-proposed", (_event, proposal) => callback(proposal)),
  onStatus: (callback) => ipcRenderer.on("status", (_event, status) => callback(status)),
  onInventoryChanged: (callback) => ipcRenderer.on("inventory-changed", () => callback()),

  // --- commands (UI -> agent) ---
  sendCommand: (text, mode = "copilot") =>
    ipcRenderer.send("user-command", { text, mode }),
  sendConfirmResponse: (id, approved, remember, correction) =>
    ipcRenderer.send("confirm-response", { id, approved, remember, correction }),
  sendAudio: (base64Data, format, mode = "copilot") =>
    ipcRenderer.send("user-audio", { data: base64Data, format, mode }),
  sendSkillResponse: (id, approved, resolutions) =>
    ipcRenderer.send("skill-response", { id, approved, resolutions }),

  // --- management (request/response) ---
  getInventory: () => ipcRenderer.invoke("get-inventory"),
  getMetrics: () => ipcRenderer.invoke("get-metrics"),
  exportSkill: (name) => ipcRenderer.invoke("export-skill", name),
  importSkill: (packageStr) => ipcRenderer.invoke("import-skill", packageStr),
  getTrace: (taskId) => ipcRenderer.invoke("get-trace", taskId),
  setPreference: (name, changes) => ipcRenderer.invoke("set-preference", { name, ...changes }),
  deleteSkill: (name) => ipcRenderer.invoke("delete-skill", name),
  revokeTrust: (key) => ipcRenderer.invoke("revoke-trust", key),
});
