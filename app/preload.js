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

  // --- shared skills (the public registry) ---
  registryList: (refresh) => ipcRenderer.invoke("registry-list", refresh),
  registryPreview: (name, contentHash) => ipcRenderer.invoke("registry-preview", { name, contentHash }),
  // Only from the install card's button, after every step has been shown.
  registryInstall: (name, contentHash) => ipcRenderer.invoke("registry-install", { name, contentHash }),
  registrySharePreview: (skill) => ipcRenderer.invoke("registry-share-preview", skill),
  // main.js opens the GitHub page itself (https://github.com/ only); no URL
  // crosses this bridge in either direction.
  registryShare: (skill, acknowledged) => ipcRenderer.invoke("registry-share", { skill, acknowledged }),

  // --- voice session ---
  // action: "start" | "end" | "toggle"
  voiceSession: (action) => ipcRenderer.invoke("voice-session", action),
  getVoiceState: () => ipcRenderer.invoke("get-voice-state"),
  getVoiceSettings: () => ipcRenderer.invoke("get-voice-settings"),
  setVoiceSettings: (settings) => ipcRenderer.invoke("set-voice-settings", settings),
  // Enumeration only -- neither opens the mic nor loads a model.
  listInputDevices: (refresh) => ipcRenderer.invoke("list-input-devices", refresh),
  // paths: custom .onnx entries to check for existence on the agent's disk
  listWakeModels: (paths) => ipcRenderer.invoke("list-wake-models", paths),

  // --- overlay window only ---
  getOverlayManifest: () => ipcRenderer.invoke("overlay-manifest"),
  // true while the overlay shows something clickable (a confirmation card)
  setOverlayInteractive: (interactive) => ipcRenderer.send("overlay-interactive", interactive),

  // --- appearance (every window: js/appearance.js) ---
  // Read once, synchronously, before the page paints; then kept current.
  initialAppearance: ipcRenderer.sendSync("get-appearance-sync"),
  onAppearance: (callback) => ipcRenderer.on("appearance", (_event, appearance) => callback(appearance)),

  // --- panels and their settings ---
  getOverlaySettings: () => ipcRenderer.invoke("get-overlay-settings"),
  // patch: a partial settings object; refused whole if any of it is invalid
  setOverlaySettings: (patch) => ipcRenderer.invoke("set-overlay-settings", patch),
  checkAccent: (hex) => ipcRenderer.invoke("check-accent", hex),
  resetPanelLayout: (id) => ipcRenderer.invoke("reset-panel-layout", id),
  onOverlaySettings: (callback) => ipcRenderer.on("overlay-settings", (_event, settings) => callback(settings)),
  showPanel: (id, query) => ipcRenderer.invoke("show-panel", id, query),
  hidePanel: (id) => ipcRenderer.invoke("hide-panel", id),
  listTimers: () => ipcRenderer.invoke("list-timers"),
  openMain: (view, name) => ipcRenderer.send("open-main", { view, name }),
  onNavigate: (callback) => ipcRenderer.on("navigate", (_event, target) => callback(target)),
  onSkillResolved: (callback) => ipcRenderer.on("skill-resolved", (_event, payload) => callback(payload)),

  // --- a panel window's own chrome (renderer/panels/panel.js) ---
  // op: ready | move-start | move | move-end | resize-start | resize |
  // resize-end | dock | orientation | opacity | pin | auto-hide | close
  panelOp: (op, data) => ipcRenderer.invoke("panel-op", op, data),
  onPanelConfig: (callback) => ipcRenderer.on("panel-config", (_event, config) => callback(config)),
  onPanelQuery: (callback) => ipcRenderer.on("panel-query", (_event, query) => callback(query)),
  getChatHistory: () => ipcRenderer.invoke("chat-history"),
});
