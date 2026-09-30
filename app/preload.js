const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("leutheria", {
  // --- event stream (agent -> UI) ---
  onLog: (callback) => ipcRenderer.on("log", (_event, line) => callback(line)),
  onChat: (callback) => ipcRenderer.on("chat", (_event, message) => callback(message)),
  onConfirmRequest: (callback) => ipcRenderer.on("confirm-request", (_event, request) => callback(request)),
  onConfirmResolved: (callback) =>
    ipcRenderer.on("confirm-resolved", (_event, payload) => callback(payload)),
  onSkillProposed: (callback) => ipcRenderer.on("skill-proposed", (_event, proposal) => callback(proposal)),
  onStatus: (callback) => ipcRenderer.on("status", (_event, status) => callback(status)),
  onInventoryChanged: (callback) => ipcRenderer.on("inventory-changed", () => callback()),
  onVoiceEvent: (callback) => ipcRenderer.on("voice-event", (_event, payload) => callback(payload)),

  // --- commands (UI -> agent) ---
  sendCommand: (text) => ipcRenderer.send("user-command", text),
  sendConfirmResponse: (id, approved, remember) =>
    ipcRenderer.send("confirm-response", { id, approved, remember }),
  sendAudio: (base64Data, format) => ipcRenderer.send("user-audio", { data: base64Data, format }),
  sendSkillResponse: (id, approved, resolutions) =>
    ipcRenderer.send("skill-response", { id, approved, resolutions }),

  // --- management (request/response) ---
  getInventory: () => ipcRenderer.invoke("get-inventory"),
  getStatus: () => ipcRenderer.invoke("get-status"),
  setPreference: (name, changes) => ipcRenderer.invoke("set-preference", { name, ...changes }),
  deleteSkill: (name) => ipcRenderer.invoke("delete-skill", name),
  revokeTrust: (key) => ipcRenderer.invoke("revoke-trust", key),
  revokeGrant: (app) => ipcRenderer.invoke("revoke-grant", app),

  // --- voice session ---
  // action: "start" | "end" | "toggle"
  voiceSession: (action) => ipcRenderer.invoke("voice-session", action),
  getVoiceState: () => ipcRenderer.invoke("get-voice-state"),
  getVoiceSettings: () => ipcRenderer.invoke("get-voice-settings"),
  setVoiceSettings: (settings) => ipcRenderer.invoke("set-voice-settings", settings),
  // Enumeration only -- neither opens the mic nor loads a model.
  listInputDevices: () => ipcRenderer.invoke("list-input-devices"),
  // paths: custom .onnx entries to check for existence on the agent's disk
  listWakeModels: (paths) => ipcRenderer.invoke("list-wake-models", paths),

  // --- overlay window only ---
  getOverlayManifest: () => ipcRenderer.invoke("overlay-manifest"),
  // true while the overlay shows something clickable (a confirmation card)
  setOverlayInteractive: (interactive) => ipcRenderer.send("overlay-interactive", interactive),
});
