const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  nativeImage,
  ipcMain,
  globalShortcut,
  screen,
} = require("electron");
const { spawn, execSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const WebSocket = require("ws");

const AGENT_DIR = path.join(__dirname, "..", "agent");
const AGENT_PYTHON = path.join(AGENT_DIR, ".venv", "bin", "python");
// Testing overrides: LEUTHERIA_AGENT_URL points the bridge at a different
// agent (e.g. scripts/mock-agent.js); LEUTHERIA_NO_SPAWN=1 skips spawning --
// and, just as importantly, skips pkill-ing -- the real Python sidecar.
const AGENT_URL = process.env.LEUTHERIA_AGENT_URL || "ws://127.0.0.1:8765";
const NO_SPAWN = process.env.LEUTHERIA_NO_SPAWN === "1";
const ASSETS_DIR = path.join(__dirname, "..", "assets");
const OVERLAY_DIR = path.join(__dirname, "renderer", "overlay");
const OVERLAY_ASSETS_DIR = path.join(OVERLAY_DIR, "assets");

// Global shortcut that toggles a manual voice session (configurable later).
const VOICE_SHORTCUT = "Alt+Space";

// The overlay sits top-center on whichever display the cursor is on when a
// session starts. positionOverlay() is the one place that decides this.
const OVERLAY_SIZE = { width: 460, height: 300 };
const OVERLAY_TOP_MARGIN = 12;
// How long the overlay stays mapped after a session ends, so its CSS fade-out
// (overlay.css, body.leaving) can finish before the window disappears.
const OVERLAY_LINGER_MS = 650;

const OVERLAY_STATES = ["listening", "hearing", "transcribing", "thinking", "speaking", "awaiting_confirmation"];
const ASSET_TYPES = ["css", "image", "video"];

// Debug only: when set, the overlay (and main window) are screenshotted into
// this directory on every voice-state change. See captureWindows(). Never set
// in normal use.
const CAPTURE_DIR = process.env.LEUTHERIA_CAPTURE_DIR || null;
const CAPTURE_THEME = process.env.LEUTHERIA_CAPTURE_THEME || null;
if (CAPTURE_DIR) {
  fs.mkdirSync(CAPTURE_DIR, { recursive: true });
  // The main window is usually behind other apps during a capture run, and an
  // occluded window stops painting -- capturePage would return a stale frame.
  app.commandLine.appendSwitch("disable-backgrounding-occluded-windows");
  // A debug instance must not share localStorage/LevelDB with a real one.
  app.setPath("userData", path.join(CAPTURE_DIR, "userdata"));
}

let devWindow;
let overlayWindow;
let overlayHideTimer = null;
let overlayInteractive = false;
let tray;
let agentProcess;
let ws;
let pendingIsChat = false; // true only while waiting on a reply to a user-typed chat message
// Set when the agent reports a confirmation answered by voice: the push-to-talk
// clip that answered it gets an empty {"type":"response"} ack, which must not
// be mistaken for the reply the original command is still working on.
let expectVoiceConfirmAck = false;

// Last voice_state the agent reported. "off" until told otherwise, and reset
// whenever the bridge drops -- a stale "listening" would be a lie.
let voice = { state: "off", session: false, error: null };

// Control requests (inventory reads, preference writes) are request/response,
// unlike the rest of the protocol which is a stream of events. Each one gets
// an id and parks its resolver here until the matching control_result lands.
const controlRequests = new Map();
let nextControlId = 1;

function isConnected() {
  return Boolean(ws && ws.readyState === WebSocket.OPEN);
}

function sendControlRequest(payload) {
  return new Promise((resolve) => {
    if (!isConnected()) {
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

// --- window messaging -----------------------------------------------------

// The main window can be closed while the app (and a voice session) keeps
// running, so every send checks it's still alive rather than trusting the
// variable.
function alive(win) {
  return Boolean(win && !win.isDestroyed());
}

function sendToMain(channel, payload) {
  if (alive(devWindow)) devWindow.webContents.send(channel, payload);
}

// Voice-session events go to both windows: the overlay draws them, the main
// window's header indicator mirrors them.
function broadcast(channel, payload) {
  for (const win of [devWindow, overlayWindow]) {
    if (alive(win)) win.webContents.send(channel, payload);
  }
}

function log(line) {
  console.log(line);
  sendToMain("log", line);
}

function chat(role, text, extra) {
  sendToMain("chat", { role, text, ...extra });
}

// Remembered so a window that loads after the event (or is reopened) can ask
// for it -- BUGS.md #18.
let bridgeStatus = "connecting";

function setStatus(status) {
  bridgeStatus = status;
  sendToMain("status", status);
}

// Tells the renderer its cached catalog is stale (a skill was saved, a
// preference changed) so the Library view refetches instead of drifting.
function invalidateInventory() {
  sendToMain("inventory-changed");
}

function confirmRequest(id, tool, args) {
  // Both windows get it: the main window draws the full four-way card, the
  // overlay a compact Approve/Decline one (only while a session is live).
  broadcast("confirm-request", { id, tool, args });
}

function skillProposed(id, name, description, steps, uncertainParams) {
  sendToMain("skill-proposed", {
    id,
    name,
    description,
    steps,
    uncertain_params: uncertainParams,
  });
}

function isEmptyResponse(payload) {
  return payload.type === "response" && !payload.text && !(payload.trace && payload.trace.length);
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

// --- main window ----------------------------------------------------------

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
  devWindow.on("closed", () => {
    devWindow = null;
  });
}

function showMainWindow() {
  if (!alive(devWindow)) {
    createDevWindow();
    return;
  }
  if (!devWindow.isVisible()) devWindow.show();
  devWindow.focus();
}

// --- overlay window -------------------------------------------------------

function createOverlayWindow() {
  overlayWindow = new BrowserWindow({
    ...OVERLAY_SIZE,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    // NSPanel: can float over other apps' full-screen spaces and doesn't
    // activate Leutheria when clicked.
    type: "panel",
    // Never takes keyboard focus -- except while a confirmation card is up,
    // see setOverlayInteractive().
    focusable: false,
    // A click on the confirmation card should press the button, not merely
    // bring the window forward first.
    acceptFirstMouse: true,
    webPreferences: {
      // Same single bridge as the main window (CLAUDE.md: preload.js is the only one).
      preload: path.join(__dirname, "preload.js"),
      // The overlay spends most of its life hidden; it must still react at
      // once when a session starts.
      backgroundThrottling: false,
    },
  });
  overlayWindow.setAlwaysOnTop(true, "screen-saver");
  // skipTransformProcessType: without it Electron briefly turns the app into
  // a UIElement to get full-screen visibility, which makes the Dock icon flicker.
  overlayWindow.setVisibleOnAllWorkspaces(true, {
    visibleOnFullScreen: true,
    skipTransformProcessType: true,
  });
  setOverlayInteractive(false);
  const query = CAPTURE_THEME ? { theme: CAPTURE_THEME } : {};
  overlayWindow.loadFile(path.join(OVERLAY_DIR, "overlay.html"), { query });
  overlayWindow.on("closed", () => {
    overlayWindow = null;
  });
}

function positionOverlay() {
  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const area = display.workArea;
  overlayWindow.setBounds({
    x: Math.round(area.x + (area.width - OVERLAY_SIZE.width) / 2),
    y: area.y + OVERLAY_TOP_MARGIN,
    ...OVERLAY_SIZE,
  });
}

function showOverlay() {
  if (!alive(overlayWindow)) return;
  clearTimeout(overlayHideTimer);
  overlayHideTimer = null;
  if (overlayWindow.isVisible()) return; // don't jump displays mid-session
  positionOverlay();
  // showInactive: the user is talking to Leutheria *while* using another app,
  // so appearing must never steal their focus.
  overlayWindow.showInactive();
  log("[overlay] shown");
}

function hideOverlaySoon() {
  if (!alive(overlayWindow) || !overlayWindow.isVisible() || overlayHideTimer) return;
  overlayHideTimer = setTimeout(() => {
    overlayHideTimer = null;
    if (voice.session || !alive(overlayWindow)) return;
    setOverlayInteractive(false);
    overlayWindow.hide();
    log("[overlay] hidden");
  }, OVERLAY_LINGER_MS);
}

// Click-through unless the overlay is asking for something. forward:true keeps
// mouse-move events flowing to the page so hover styles still work.
function setOverlayInteractive(interactive) {
  if (!alive(overlayWindow)) return;
  overlayInteractive = interactive;
  if (interactive) {
    overlayWindow.setIgnoreMouseEvents(false);
    log("[overlay] setIgnoreMouseEvents(false) -- confirmation card up, clickable");
  } else {
    overlayWindow.setIgnoreMouseEvents(true, { forward: true });
    log("[overlay] setIgnoreMouseEvents(true, {forward: true}) -- click-through");
  }
  overlayWindow.setFocusable(interactive);
}

/* Reads assets/manifest.json fresh on every overlay load, so dropping in new
   assets needs a reload at most, never a code change. Anything missing or
   malformed falls back to the built-in CSS placeholder, with a log line saying
   why -- a typo in the manifest shouldn't blank the overlay. */
function loadOverlayManifest() {
  const fallback = Object.fromEntries(OVERLAY_STATES.map((state) => [state, { type: "css" }]));
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(path.join(OVERLAY_ASSETS_DIR, "manifest.json"), "utf8"));
  } catch (err) {
    log(`[overlay] manifest unreadable, using CSS placeholders: ${err.message}`);
    return { states: fallback };
  }

  const states = {};
  for (const state of OVERLAY_STATES) {
    const entry = (raw.states || {})[state] || { type: "css" };
    const problem = !ASSET_TYPES.includes(entry.type)
      ? `unknown type "${entry.type}"`
      : entry.type !== "css" && !entry.src
        ? "no src"
        : null;
    if (problem) {
      log(`[overlay] manifest: ${state}: ${problem} -- using CSS placeholder`);
      states[state] = { type: "css" };
      continue;
    }
    if (entry.type !== "css") {
      const file = path.resolve(OVERLAY_ASSETS_DIR, entry.src);
      if (!file.startsWith(OVERLAY_ASSETS_DIR + path.sep) || !fs.existsSync(file)) {
        log(`[overlay] manifest: ${state}: ${entry.src} not found in assets/ -- using CSS placeholder`);
        states[state] = { type: "css" };
        continue;
      }
    }
    states[state] = entry;
  }
  return { states, size: raw.size || null };
}

// --- debug capture (LEUTHERIA_CAPTURE_DIR only) ---------------------------

let captureSeq = 0;

function captureWindows(label) {
  if (!CAPTURE_DIR) return;
  const seq = String(++captureSeq).padStart(2, "0");
  // Let the state's CSS transition settle before grabbing the frame.
  setTimeout(async () => {
    for (const [name, win] of [
      ["overlay", overlayWindow],
      ["main", devWindow],
    ]) {
      if (!alive(win) || !win.isVisible()) continue;
      try {
        const image = await win.webContents.capturePage();
        const file = path.join(CAPTURE_DIR, `${seq}-${label}-${name}.png`);
        fs.writeFileSync(file, image.toPNG());
        console.log(`[capture] ${file}`);
      } catch (err) {
        console.log(`[capture] failed for ${name}: ${err.message}`);
      }
    }
  }, 500);
}

// --- voice session ----------------------------------------------------------

async function setVoiceSession(action) {
  const result = await sendControlRequest({ type: "voice_session", action });
  if (!result.ok) log(`[voice] couldn't ${action} a session: ${result.error || "unknown error"}`);
  return result;
}

function toggleVoiceSession() {
  return setVoiceSession(voice.session ? "end" : "start");
}

function publishVoiceState() {
  broadcast("voice-event", { type: "voice_state", ...voice, connected: isConnected() });
  updateTrayMenu();
}

function handleVoiceEvent(payload) {
  if (payload.type === "voice_state") {
    voice = { state: payload.state, session: Boolean(payload.session), error: payload.error || null };
    publishVoiceState();
    if (voice.session) showOverlay();
    else hideOverlaySoon();
    captureWindows(payload.state);
    return;
  }
  broadcast("voice-event", payload);
  if (payload.type === "session_started") showOverlay();
  if (payload.type === "session_ended") hideOverlaySoon();
  if (payload.type === "speaking" && payload.state === "started") captureWindows("speaking-text");
}

// --- tray / shortcut --------------------------------------------------------

function createTray() {
  // Template image: macOS recolors it to match the menu bar (light or dark)
  // instead of showing a purple blob that fights the system appearance.
  const icon = nativeImage
    .createFromPath(path.join(ASSETS_DIR, "1.png"))
    .resize({ width: 18, height: 18 });
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip("Leutheria");
  updateTrayMenu();
}

// Rebuilt on every voice-state change so only the action that makes sense
// right now is enabled.
function updateTrayMenu() {
  if (!tray) return;
  const connected = isConnected();
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Show Leutheria", click: showMainWindow },
      { type: "separator" },
      {
        label: "Start listening",
        accelerator: VOICE_SHORTCUT,
        registerAccelerator: false, // the global shortcut owns it
        enabled: connected && !voice.session,
        click: () => setVoiceSession("start"),
      },
      {
        label: "Stop listening",
        accelerator: VOICE_SHORTCUT,
        registerAccelerator: false,
        enabled: connected && voice.session,
        click: () => setVoiceSession("end"),
      },
      { type: "separator" },
      { label: "Quit Leutheria", role: "quit" },
    ])
  );
}

function registerShortcut() {
  const ok = globalShortcut.register(VOICE_SHORTCUT, () => {
    log(`[voice] ${VOICE_SHORTCUT} pressed -- ${voice.session ? "ending" : "starting"} session`);
    toggleVoiceSession();
  });
  if (!ok) log(`[voice] couldn't register ${VOICE_SHORTCUT} -- another app already owns it`);
}

// --- agent process ----------------------------------------------------------

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
  if (NO_SPAWN) {
    log(`[agent] LEUTHERIA_NO_SPAWN=1 -- not starting Python, connecting to ${AGENT_URL}`);
    connectToAgent();
    return;
  }
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

function logReceived(payload) {
  if (payload.data) {
    // Don't dump a base64 blob into the log verbatim -- it can be over a
    // megabyte of text and drowns out everything else.
    const { data: _omitted, ...summary } = payload;
    log(`[bridge] received: ${JSON.stringify(summary)} <${payload.data.length} b64 chars>`);
  } else if (payload.type === "control_result" && payload.tools) {
    // An inventory snapshot is the whole capability catalog -- summarize it
    // for the same reason blobs are summarized above.
    log(
      `[bridge] received: {"type":"control_result","request_id":"${payload.request_id}"} ` +
        `<inventory: ${payload.tools.length} tools, ${payload.skills.length} skills>`
    );
  } else {
    log(`[bridge] received: ${JSON.stringify(payload)}`);
  }
}

function connectToAgent() {
  ws = new WebSocket(AGENT_URL);

  ws.on("open", () => {
    log(`[bridge] connected to agent at ${AGENT_URL}`);
    setStatus("connected");
    updateTrayMenu();
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

    // ~15 Hz while listening: relay, but never log -- it would bury everything.
    if (payload.type === "audio_level") {
      broadcast("voice-event", payload);
      return;
    }

    logReceived(payload);

    if (payload.type === "control_result") {
      const resolve = controlRequests.get(payload.request_id);
      if (resolve) {
        controlRequests.delete(payload.request_id);
        resolve(payload);
      }
      return;
    }

    if (["voice_state", "session_started", "session_ended", "speaking"].includes(payload.type)) {
      handleVoiceEvent(payload);
      return;
    }

    if (payload.type === "speech") {
      // Pre-week-4 agents sent TTS audio for the renderer to play. The sidecar
      // plays it itself now; an old agent's clip is dropped, not played twice.
      return;
    }

    if (payload.type === "confirmation_required") {
      // Mid-flight pause -- don't clear pendingIsChat, the real final
      // reply is still coming once this is answered.
      confirmRequest(payload.id, payload.tool, payload.args);
      captureWindows("confirm-card");
      return;
    }

    if (payload.type === "confirmation_resolved") {
      // Answered out-of-band (by voice) -- settle the on-screen cards.
      if (payload.by === "voice") expectVoiceConfirmAck = true;
      broadcast("confirm-resolved", payload);
      return;
    }

    if (payload.type === "transcript") {
      // What Whisper heard -- shown as the user's own message. For
      // push-to-talk the real final reply is still coming, so pendingIsChat
      // stays true; a voice-session transcript has no pending request at all.
      chat("user", payload.text, { voice: true });
      broadcast("voice-event", payload);
      return;
    }

    if (payload.origin === "voice_session") {
      // The renderer never asked for this -- the live session did -- so it
      // must not depend on (or consume) pendingIsChat. An empty response is a
      // voice-confirmation ack, not a reply worth a "(no reply)" bubble.
      broadcast("voice-event", payload);
      if (isEmptyResponse(payload)) expectVoiceConfirmAck = false;
      else sendAgentReply(payload);
      captureWindows("voice-reply");
      return;
    }

    if (isEmptyResponse(payload) && expectVoiceConfirmAck) {
      // BUGS.md #17: the push-to-talk clip that answered a confirmation gets
      // an empty ack. Consuming pendingIsChat on it dropped the real reply.
      expectVoiceConfirmAck = false;
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
    voice = { state: "off", session: false, error: null };
    publishVoiceState();
    hideOverlaySoon();
    // A spawned agent that dies is reported, not retried (unchanged). With
    // NO_SPAWN the agent is someone else's process -- keep trying, so the app
    // can be started before the mock/standalone agent.
    if (NO_SPAWN) setTimeout(connectToAgent, 2000);
  });
}

// --- IPC ---------------------------------------------------------------------

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

ipcMain.handle("revoke-grant", async (_event, app) => {
  const result = await sendControlRequest({ type: "revoke_grant", app });
  if (result.ok) invalidateInventory();
  return result;
});

ipcMain.handle("voice-session", (_event, action) =>
  action === "toggle" ? toggleVoiceSession() : setVoiceSession(action)
);
ipcMain.handle("get-status", () => bridgeStatus);
ipcMain.handle("get-voice-state", () => ({ ...voice, connected: isConnected() }));
ipcMain.handle("get-voice-settings", () => sendControlRequest({ type: "get_voice_settings" }));
ipcMain.handle("set-voice-settings", (_event, settings) =>
  sendControlRequest({ type: "set_voice_settings", settings })
);
ipcMain.handle("list-input-devices", () => sendControlRequest({ type: "list_input_devices" }));
ipcMain.handle("list-wake-models", (_event, paths) =>
  sendControlRequest({ type: "list_wake_models", paths: paths || [] })
);

ipcMain.handle("overlay-manifest", () => loadOverlayManifest());
ipcMain.on("overlay-interactive", (_event, interactive) => {
  if (Boolean(interactive) !== overlayInteractive) setOverlayInteractive(Boolean(interactive));
});

// Either window can answer a confirmation. The other one's card is settled
// here rather than waiting on the agent, which only reports voice answers.
ipcMain.on("confirm-response", (_event, { id, approved, remember }) => {
  if (!isConnected()) return;
  const message = { type: "confirm", id, approved, remember };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  ws.send(JSON.stringify(message));
  broadcast("confirm-resolved", { id, approved, by: "ui" });
});

ipcMain.on("skill-response", (_event, { id, approved, resolutions }) => {
  if (!isConnected()) return;
  const message = { type: "skill_response", id, approved, resolutions };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  ws.send(JSON.stringify(message));
});

ipcMain.on("user-audio", (_event, { data, format }) => {
  if (!isConnected()) {
    chat("assistant", "⚠️ not connected to agent yet");
    return;
  }
  const message = { type: "audio", data, format };
  log(`[bridge] sending: {"type":"audio","format":"${format}","data":"<${data.length} b64 chars>"}`);
  pendingIsChat = true;
  ws.send(JSON.stringify(message));
});

ipcMain.on("user-command", (_event, text) => {
  if (!isConnected()) {
    chat("assistant", "⚠️ not connected to agent yet");
    return;
  }
  chat("user", text);
  const message = { type: "command", text };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  pendingIsChat = true;
  ws.send(JSON.stringify(message));
});

// --- lifecycle ---------------------------------------------------------------

app.whenReady().then(() => {
  createTray();
  createDevWindow();
  createOverlayWindow();
  registerShortcut();
  startAgent();
});

// Dock icon click with the main window closed.
app.on("activate", showMainWindow);

// The overlay window always exists, so this only fires at quit. Closing the
// main window leaves the agent (and any voice session) running -- reopen it
// from the tray.
app.on("window-all-closed", () => {
  if (agentProcess) agentProcess.kill();
  if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", () => globalShortcut.unregisterAll());

app.on("before-quit", () => {
  if (agentProcess) agentProcess.kill();
});
