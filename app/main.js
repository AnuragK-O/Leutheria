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

function log(line) {
  console.log(line);
  if (devWindow) {
    devWindow.webContents.send("log", line);
  }
}

function createDevWindow() {
  devWindow = new BrowserWindow({
    width: 640,
    height: 480,
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
    log(`[bridge] received: ${data.toString()}`);
  });

  ws.on("error", (err) => {
    log(`[bridge:err] ${err.message}`);
  });
}

ipcMain.on("user-command", (_event, text) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    log("[bridge:err] not connected to agent yet");
    return;
  }
  const message = { type: "command", text };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
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
