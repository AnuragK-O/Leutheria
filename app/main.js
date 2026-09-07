const { app, BrowserWindow, Tray, nativeImage, ipcMain, screen } = require("electron");
const { spawn, execSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const WebSocket = require("ws");

const AGENT_DIR = path.join(__dirname, "..", "agent");
const isWin = process.platform === "win32";

function getPythonPath() {
  if (isWin) {
    const venvPython = path.join(AGENT_DIR, ".venv", "Scripts", "python.exe");
    if (fs.existsSync(venvPython)) return venvPython;
    return process.env.PYTHON_PATH || "python";
  }
  const venvPython = path.join(AGENT_DIR, ".venv", "bin", "python");
  if (fs.existsSync(venvPython)) return venvPython;
  return process.env.PYTHON_PATH || "python3";
}

const AGENT_PYTHON = getPythonPath();
const AGENT_URL = "ws://127.0.0.1:8765";
const ASSETS_DIR = path.join(__dirname, "..", "assets");

let devWindow;
let overlayWindow;
let tray;
let agentProcess;
let ws;
let pendingIsChat = false;

const controlRequests = new Map();
let nextControlId = 1;

function sendControlRequest(payload) {
  return new Promise((resolve) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      resolve({ ok: false, error: "not connected to agent" });
      return;
    }
    const requestId = String(nextControlId++);
    controlRequests.set(requestId, resolve);
    ws.send(JSON.stringify({ ...payload, request_id: requestId }));
    setTimeout(() => {
      if (controlRequests.delete(requestId)) resolve({ ok: false, error: "agent timed out" });
    }, 10000);
  });
}

function log(line) {
  console.log(line);
  if (devWindow) {
    devWindow.webContents.send("log", line);
  }
}

function chat(role, text, extra) {
  if (devWindow) {
    devWindow.webContents.send("chat", { role, text, ...extra });
  }
}

function setStatus(status) {
  if (devWindow) {
    devWindow.webContents.send("status", status);
  }
}

function invalidateInventory() {
  if (devWindow) {
    devWindow.webContents.send("inventory-changed");
  }
}

function confirmRequest(req) {
  if (devWindow) {
    devWindow.webContents.send("confirm-request", req);
  }
}

function skillProposed(proposal) {
  if (devWindow) {
    devWindow.webContents.send("skill-proposed", proposal);
  }
}

function sendAgentReply(payload) {
  if (payload.type === "response") {
    chat("assistant", payload.text, {
      trace: payload.trace || [],
      skill_used: payload.skill_used,
      task_id: payload.task_id,
      mode: payload.mode,
    });
    return;
  }
  if (payload.type === "tool_result") {
    chat("assistant", "", {
      trace: [{ tool: payload.tool, args: {}, result: payload.result }],
    });
    return;
  }
  chat("assistant", payload.text || JSON.stringify(payload), { error: true });
}

app.disableHardwareAcceleration();

function createDevWindow() {
  devWindow = new BrowserWindow({
    title: "Leutheria - Self-Improving Desktop Agent",
    width: 1240,
    height: 820,
    minWidth: 940,
    minHeight: 620,
    center: true,
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    trafficLightPosition: { x: 18, y: 22 },
    backgroundColor: "#141119",
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
    },
  });
  devWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  devWindow.webContents.on("did-finish-load", () => {
    log("[electron] devWindow loaded index.html successfully");
    if (devWindow) {
      devWindow.show();
      devWindow.focus();
    }
  });
  devWindow.once("ready-to-show", () => {
    if (devWindow) {
      devWindow.show();
      devWindow.focus();
    }
  });
}

function createOverlayWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { width, height } = primaryDisplay.workAreaSize;

  overlayWindow = new BrowserWindow({
    title: "Leutheria Ghost Overlay",
    width,
    height,
    x: 0,
    y: 0,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    focusable: false,
    show: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });
  overlayWindow.setIgnoreMouseEvents(true, { forward: true });
  overlayWindow.loadFile(path.join(__dirname, "renderer", "overlay.html"));
}

function createTray() {
  const iconPath = path.join(ASSETS_DIR, "1.png");
  if (!fs.existsSync(iconPath)) return;
  const icon = nativeImage.createFromPath(iconPath).resize({ width: 18, height: 18 });
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Leutheria");
  tray.on("click", () => {
    if (devWindow) devWindow.isVisible() ? devWindow.focus() : devWindow.show();
  });
}

function killStaleAgent() {
  try {
    if (!isWin) {
      execSync("pkill -if '[p]ython -m agent'");
      log("[agent] killed a stale agent process from a previous run");
    }
  } catch (_err) {
    // nothing to kill
  }
}

function startAgent() {
  killStaleAgent();
  setTimeout(spawnAgent, 300);
}

function spawnAgent() {
  agentProcess = spawn(AGENT_PYTHON, ["-m", "agent"], { cwd: AGENT_DIR });

  agentProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    log(`[agent] ${text}`);
    if (text.startsWith("AGENT_READY") && !ws) {
      connectToAgent();
    }
  });

  agentProcess.stderr.on("data", (data) => {
    log(`[agent:err] ${data.toString().trim()}`);
  });

  agentProcess.on("exit", (code) => {
    log(`[agent] exited with code ${code}`);
  });
}

function connectToAgent() {
  ws = new WebSocket(AGENT_URL);

  ws.on("open", () => {
    log("[bridge] connected to agent");
    setStatus("connected");
  });

  ws.on("message", (data) => {
    let payload;
    try {
      payload = JSON.parse(data.toString());
    } catch (_err) {
      log(`[bridge] received: ${data.toString()}`);
      if (pendingIsChat) {
        pendingIsChat = false;
        chat("assistant", data.toString());
      }
      return;
    }

    if (payload.data) {
      const { data: _omitted, ...summary } = payload;
      log(`[bridge] received: ${JSON.stringify(summary)} <audio ${payload.data.length} b64 chars>`);
    } else if (payload.type === "control_result" && payload.tools) {
      log(
        `[bridge] received: {"type":"control_result","request_id":"${payload.request_id}"} ` +
          `<inventory: ${payload.tools.length} tools, ${payload.skills.length} skills>`
      );
    } else {
      log(`[bridge] received: ${JSON.stringify(payload)}`);
    }

    if (payload.type === "control_result") {
      const resolve = controlRequests.get(payload.request_id);
      if (resolve) {
        controlRequests.delete(payload.request_id);
        resolve(payload);
      }
      return;
    }

    if (payload.type === "timeline_event") {
      if (devWindow) devWindow.webContents.send("timeline-event", payload);
      return;
    }

    if (payload.type === "ghost_guidance" || payload.tool === "ghost_guidance") {
      const guidance = payload.guidance || payload.args || {};
      if (overlayWindow) {
        if (guidance.hide) {
          overlayWindow.hide();
        } else {
          overlayWindow.showInactive();
        }
        overlayWindow.webContents.send("ghost-update", guidance);
      }
      if (devWindow) devWindow.webContents.send("ghost-target", guidance);
      return;
    }

    if (payload.type === "confirmation_required") {
      confirmRequest(payload);
      return;
    }

    if (payload.type === "confirmation_resolved") {
      if (devWindow) devWindow.webContents.send("confirm-resolved", payload);
      return;
    }

    if (payload.type === "transcript") {
      chat("user", payload.text, { voice: true });
      return;
    }

    if (payload.type === "speech") {
      if (devWindow) devWindow.webContents.send("speech", payload.data);
      return;
    }

    if (payload.type === "skill_proposed") {
      skillProposed(payload);
      return;
    }

    if (payload.type === "skill_saved") {
      chat("assistant", `✅ Saved "${payload.name}" as a reusable skill.`);
      invalidateInventory();
      return;
    }

    if (pendingIsChat) {
      pendingIsChat = false;
      sendAgentReply(payload);
    }
  });

  ws.on("error", (err) => {
    log(`[bridge:err] ${err.message}`);
    setStatus("error");
  });

  ws.on("close", () => {
    log("[bridge] disconnected from agent");
    setStatus("disconnected");
  });
}

// IPC Handlers
ipcMain.handle("get-inventory", () => sendControlRequest({ type: "inventory" }));
ipcMain.handle("get-metrics", () => sendControlRequest({ type: "get_metrics" }));
ipcMain.handle("get-trace", (_event, taskId) => sendControlRequest({ type: "get_trace", task_id: taskId }));
ipcMain.handle("export-skill", (_event, name) => sendControlRequest({ type: "export_skill", name }));
ipcMain.handle("import-skill", (_event, packageStr) => sendControlRequest({ type: "import_skill", package: packageStr }));

ipcMain.handle("set-preference", async (_event, { name, enabled, requiresConfirmation, reset }) => {
  const result = await sendControlRequest({
    type: "set_preference",
    name,
    enabled,
    requires_confirmation: requiresConfirmation,
    reset,
  });
  if (result.ok) invalidateInventory();
  return result;
});

ipcMain.handle("delete-skill", async (_event, name) => {
  const result = await sendControlRequest({ type: "delete_skill", name });
  if (result.ok) invalidateInventory();
  return result;
});

ipcMain.handle("revoke-trust", async (_event, key) => {
  const result = await sendControlRequest({ type: "revoke_trust", key });
  if (result.ok) invalidateInventory();
  return result;
});

ipcMain.on("confirm-response", (_event, { id, approved, remember, correction }) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const message = { type: "confirm", id, approved, remember, correction };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  ws.send(JSON.stringify(message));
});

ipcMain.on("skill-response", (_event, { id, approved, resolutions }) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const message = { type: "skill_response", id, approved, resolutions };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  ws.send(JSON.stringify(message));
});

ipcMain.on("user-audio", (_event, { data, format, mode }) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    chat("assistant", "⚠️ not connected to agent yet");
    return;
  }
  const message = { type: "audio", data, format, mode: mode || "copilot" };
  log(`[bridge] sending: {"type":"audio","format":"${format}","mode":"${message.mode}"}`);
  pendingIsChat = true;
  ws.send(JSON.stringify(message));
});

ipcMain.on("user-command", (_event, payload) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    chat("assistant", "⚠️ not connected to agent yet");
    return;
  }
  const text = typeof payload === "string" ? payload : payload.text;
  const mode = (typeof payload === "object" && payload.mode) ? payload.mode : "copilot";

  chat("user", text, { mode });
  const message = { type: "command", text, mode };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  pendingIsChat = true;
  ws.send(JSON.stringify(message));
});

app.whenReady().then(() => {
  createTray();
  createDevWindow();
  createOverlayWindow();
  startAgent();
});

app.on("window-all-closed", () => {
  if (agentProcess) agentProcess.kill();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (agentProcess) agentProcess.kill();
});
