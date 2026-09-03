const { app, BrowserWindow, Tray, nativeImage, ipcMain } = require("electron");
const { spawn, execSync } = require("child_process");
const path = require("path");
const WebSocket = require("ws");

const AGENT_DIR = path.join(__dirname, "..", "agent");
const AGENT_PYTHON = path.join(AGENT_DIR, ".venv", "bin", "python");
const AGENT_URL = "ws://127.0.0.1:8765";

// 1x1 transparent PNG so Tray doesn't need an external asset yet.
const TRAY_ICON = nativeImage.createFromDataURL(
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
);

let devWindow;
let tray;
let agentProcess;
let ws;
let pendingIsChat = false; // true only while waiting on a reply to a user-typed chat message

function log(line) {
  console.log(line);
  if (devWindow) {
    devWindow.webContents.send("log", line);
  }
}

function chat(role, text) {
  if (devWindow) {
    devWindow.webContents.send("chat", { role, text });
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

function formatAgentReply(payload) {
  if (payload.type === "tool_result") {
    return `🔧 ${payload.tool}(${JSON.stringify(payload.result)})`;
  }
  if (payload.type === "response") {
    const traceLines = (payload.trace || []).map(
      (step) => `🔧 ${step.tool}(${JSON.stringify(step.args)}) → ${JSON.stringify(step.result)}`
    );
    return [...traceLines, payload.text].filter(Boolean).join("\n");
  }
  return `⚠️ ${payload.text || JSON.stringify(payload)}`;
}

function createDevWindow() {
  devWindow = new BrowserWindow({
    width: 480,
    height: 640,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
    },
  });
  devWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
}

function createTray() {
  tray = new Tray(TRAY_ICON);
  tray.setToolTip("Leutheria");
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
    const testMessage = {
      type: "command",
      tool: "create_folder",
      args: { path: "~/Desktop/leutheria-test" },
    };
    log(`[bridge] sending: ${JSON.stringify(testMessage)}`);
    ws.send(JSON.stringify(testMessage));
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
    } else {
      log(`[bridge] received: ${JSON.stringify(payload)}`);
    }

    if (payload.type === "confirmation_required") {
      // Mid-flight pause -- don't clear pendingIsChat, the real final
      // reply is still coming once this is answered.
      confirmRequest(payload.id, payload.tool, payload.args);
      return;
    }

    if (payload.type === "transcript") {
      // What Whisper heard -- shown as the user's own message. The real
      // final reply is still coming, so pendingIsChat stays true.
      chat("user", `🎤 ${payload.text}`);
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
      return;
    }

    if (pendingIsChat) {
      pendingIsChat = false;
      chat("assistant", formatAgentReply(payload));
    }
  });

  ws.on("error", (err) => {
    log(`[bridge:err] ${err.message}`);
  });
}

ipcMain.on("confirm-response", (_event, { id, approved }) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const message = { type: "confirm", id, approved };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  chat("user", approved ? "✅ approved" : "❌ declined");
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
