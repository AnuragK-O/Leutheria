const { app, BrowserWindow, Tray, nativeImage, ipcMain } = require("electron");
const { spawn, execSync } = require("child_process");
const path = require("path");
const WebSocket = require("ws");

const AGENT_DIR = path.join(__dirname, "..", "agent");
const AGENT_PYTHON = path.join(AGENT_DIR, ".venv", "bin", "python");
const AGENT_URL = "ws://127.0.0.1:8765";
const ASSETS_DIR = path.join(__dirname, "..", "assets");

let devWindow;
let tray;
let agentProcess;
let ws;
let pendingIsChat = false; // true only while waiting on a reply to a user-typed chat message

// Control requests (inventory reads, preference writes) are request/response,
// unlike the rest of the protocol which is a stream of events. Each one gets
// an id and parks its resolver here until the matching control_result lands.
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
    // Never leave the UI spinning forever if the agent dies mid-request.
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

// Tells the renderer its cached catalog is stale (a skill was saved, a
// preference changed) so the Library view refetches instead of drifting.
function invalidateInventory() {
  if (devWindow) {
    devWindow.webContents.send("inventory-changed");
  }
}

function confirmRequest(id, tool, args) {
  if (devWindow) {
    devWindow.webContents.send("confirm-request", { id, tool, args });
  }
}

function skillProposed(id, name, description, steps, uncertainParams) {
  if (devWindow) {
    devWindow.webContents.send("skill-proposed", {
      id,
      name,
      description,
      steps,
      uncertain_params: uncertainParams,
    });
  }
}

// The renderer draws the tool trace itself (one row per step, with a
// pass/fail marker), so pass it through structured rather than flattening
// everything into a single string here.
function sendAgentReply(payload) {
  if (payload.type === "response") {
    chat("assistant", payload.text, { trace: payload.trace || [] });
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

function createDevWindow() {
  devWindow = new BrowserWindow({
    width: 1180,
    height: 780,
    minWidth: 940,
    minHeight: 600,
    // Frameless-with-traffic-lights, so the sidebar can run the full height of
    // the window instead of sitting under a stock title bar.
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 18, y: 22 },
    backgroundColor: "#141119",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
    },
  });
  devWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  // Avoid the white flash between window creation and first paint.
  devWindow.once("ready-to-show", () => devWindow.show());
}

function createTray() {
  // Template image: macOS recolors it to match the menu bar (light or dark)
  // instead of showing a purple blob that fights the system appearance.
  const icon = nativeImage
    .createFromPath(path.join(ASSETS_DIR, "1.png"))
    .resize({ width: 18, height: 18 });
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Leutheria");
  tray.on("click", () => {
    if (devWindow) devWindow.isVisible() ? devWindow.focus() : devWindow.show();
  });
}

function killStaleAgent() {
  // If the app was force-quit or crashed last run, a previous "python -m agent"
  // can be left holding port 8765, so the new one fails to bind on startup.
  // pkill exits non-zero when nothing matches -- that's the expected common case.
  try {
    execSync("pkill -if '[p]ython -m agent'");
    log("[agent] killed a stale agent process from a previous run");
  } catch (_err) {
    // nothing to kill
  }
}

function startAgent() {
  killStaleAgent();
  // Give the OS a moment to release the port after the kill above.
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

    // Don't dump a base64 audio blob into the log verbatim -- it can be
    // over a megabyte of text for a single reply and drowns out everything
    // else. Log a summary instead; the audio itself still gets played.
    if (payload.data) {
      const { data: _omitted, ...summary } = payload;
      log(`[bridge] received: ${JSON.stringify(summary)} <audio ${payload.data.length} b64 chars>`);
    } else if (payload.type === "control_result" && payload.tools) {
      // An inventory snapshot is the whole capability catalog -- summarize it
      // for the same reason audio is summarized above.
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

    if (payload.type === "confirmation_required") {
      // Mid-flight pause -- don't clear pendingIsChat, the real final
      // reply is still coming once this is answered.
      confirmRequest(payload.id, payload.tool, payload.args);
      return;
    }

    if (payload.type === "confirmation_resolved") {
      // Answered out-of-band (by voice) -- settle the on-screen card.
      if (devWindow) devWindow.webContents.send("confirm-resolved", payload);
      return;
    }

    if (payload.type === "transcript") {
      // What Whisper heard -- shown as the user's own message. The real
      // final reply is still coming, so pendingIsChat stays true.
      chat("user", payload.text, { voice: true });
      return;
    }

    if (payload.type === "speech") {
      // Follows the "response" message, so pendingIsChat is already false
      // by the time this arrives -- doesn't affect that flag either way.
      if (devWindow) devWindow.webContents.send("speech", payload.data);
      return;
    }

    if (payload.type === "skill_proposed") {
      // Arrives after the real response -- purely additive, doesn't touch pendingIsChat.
      skillProposed(payload.id, payload.name, payload.description, payload.steps, payload.uncertain_params);
      return;
    }

    if (payload.type === "skill_saved") {
      chat("assistant", `✅ Saved "${payload.name}" as a new skill.`);
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

ipcMain.handle("get-inventory", () => sendControlRequest({ type: "inventory" }));

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

ipcMain.on("confirm-response", (_event, { id, approved, remember }) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const message = { type: "confirm", id, approved, remember };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  ws.send(JSON.stringify(message));
});

ipcMain.on("skill-response", (_event, { id, approved, resolutions }) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const message = { type: "skill_response", id, approved, resolutions };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  ws.send(JSON.stringify(message));
});

ipcMain.on("user-audio", (_event, { data, format }) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    chat("assistant", "⚠️ not connected to agent yet");
    return;
  }
  const message = { type: "audio", data, format };
  log(`[bridge] sending: {"type":"audio","format":"${format}","data":"<${data.length} b64 chars>"}`);
  pendingIsChat = true;
  ws.send(JSON.stringify(message));
});

ipcMain.on("user-command", (_event, text) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    chat("assistant", "⚠️ not connected to agent yet");
    return;
  }
  chat("user", text);
  const message = { type: "command", text };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  pendingIsChat = true;
  ws.send(JSON.stringify(message));
});

app.whenReady().then(() => {
  createTray();
  createDevWindow();
  startAgent();
});

app.on("window-all-closed", () => {
  if (agentProcess) agentProcess.kill();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (agentProcess) agentProcess.kill();
});
