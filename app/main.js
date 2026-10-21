const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  nativeImage,
  ipcMain,
  globalShortcut,
  screen,
  shell,
  clipboard,
} = require("electron");
const { spawn, execSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const WebSocket = require("ws");
const { TOKEN_ENV, createAuth, childEnv, connectOptions, explainRejection } = require("./agent-auth");
const overlaySettings = require("./overlay-settings");
const { createPanelManager } = require("./panels");

const AGENT_DIR = path.join(__dirname, "..", "agent");
const AGENT_PYTHON = path.join(AGENT_DIR, ".venv", "bin", "python");
// Testing overrides: LEUTHERIA_AGENT_URL points the bridge at a different
// agent (e.g. scripts/mock-agent.js); LEUTHERIA_NO_SPAWN=1 skips spawning --
// and, just as importantly, skips pkill-ing -- the real Python sidecar.
const AGENT_URL = process.env.LEUTHERIA_AGENT_URL || "ws://127.0.0.1:8765";
const NO_SPAWN = process.env.LEUTHERIA_NO_SPAWN === "1";
// The agent's socket is authenticated (agent-auth.js, core/ws_auth.py). When
// this app spawns the agent it makes a fresh token and hands it over in the
// child's environment; with LEUTHERIA_NO_SPAWN=1 it uses LEUTHERIA_TOKEN from
// its own environment (or a standalone agent's token file). Taken out of
// process.env here so nothing but the two children that need it inherits it.
const agentAuth = createAuth({ noSpawn: NO_SPAWN, agentUrl: AGENT_URL });
delete process.env[TOKEN_ENV];
const ASSETS_DIR = path.join(__dirname, "..", "assets");
const OVERLAY_DIR = path.join(__dirname, "renderer", "overlay");
const OVERLAY_ASSETS_DIR = path.join(OVERLAY_DIR, "assets");

// Which activation overlay to use. Default: the Electron one below.
// LEUTHERIA_OVERLAY=qt spawns the native C++/Qt overlay (overlay-qt/) instead;
// it's a second client of the agent's WebSocket (it says hello as role
// "overlay") and draws the same session. If its binary isn't built, this
// falls back to the Electron overlay with a log line, so the app never ends
// up with no overlay at all. LEUTHERIA_QT_OVERLAY_BIN overrides the path
// (a debug build elsewhere, or a stand-in when testing this wiring).
const OVERLAY_MODE = process.env.LEUTHERIA_OVERLAY === "qt" ? "qt" : "electron";
const QT_OVERLAY_BIN =
  process.env.LEUTHERIA_QT_OVERLAY_BIN ||
  path.join(__dirname, "..", "overlay-qt", "build", "LeutheriaOverlay.app", "Contents", "MacOS", "LeutheriaOverlay");

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

// Test only: LEUTHERIA_TEST_HOOKS=1 (set by scripts/ui-capture.js, never in
// normal use) defines globalThis.__leutheriaTest, which the capture script
// reaches through the main process's inspector port. It can stand in for the
// cursor position and the mouse-button state (the hot edge reads both) and
// report window focus. Without the variable none of it exists.
const TEST_HOOKS = process.env.LEUTHERIA_TEST_HOOKS === "1" ? { cursor: null, mouseDown: null } : null;

let devWindow;
let overlayWindow;
let overlayHideTimer = null;
let overlayInteractive = false;
let tray;
let agentProcess;
let qtOverlayProcess = null; // only ever the child this app spawned
let quitting = false;
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

function sendControlRequest(payload, timeoutMs = 10000) {
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
    }, timeoutMs);
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

// The panel windows (panels.js). Created once the app is ready.
let panels = null;
const panelWindows = () => (panels ? panels.windows() : []);

// What the main window shows that a panel shows too (the chat panel's
// transcript, the info panels' catalog and connection state).
function sendToUi(channel, payload) {
  for (const win of [devWindow, ...panelWindows()]) {
    if (alive(win)) win.webContents.send(channel, payload);
  }
}

// Voice-session events go to every window: the overlay draws them, the main
// window's header indicator and the voice panel mirror them.
function broadcast(channel, payload) {
  for (const win of [devWindow, overlayWindow, ...panelWindows()]) {
    if (alive(win)) win.webContents.send(channel, payload);
  }
}

// Lines logged before the main window has loaded (overlay-layout.json's
// problems are found at startup, before the window exists) are held and
// handed over once it has, rather than reaching only stdout.
let earlyLog = [];

function log(line) {
  console.log(line);
  if (earlyLog) earlyLog.push(line);
  else sendToMain("log", line);
}

function flushEarlyLog() {
  if (!earlyLog) return;
  const lines = earlyLog;
  earlyLog = null;
  for (const line of lines) sendToMain("log", line);
}

/* The transcript's events, in order, so a chat panel opened mid-session (or
   reloaded) starts with the conversation instead of an empty log. Replayed
   through the same handlers the live events use. Capped: it's a view of the
   recent session, not a history store. */
const TRANSCRIPT_EVENTS = ["chat", "confirm-request", "confirm-resolved", "skill-proposed", "skill-resolved"];
const TRANSCRIPT_CAP = 400;
const transcript = [];
let transcriptSeq = 0;

// Stamps the payload with _seq (chat.js uses it to skip an event it has
// already shown) and keeps it.
function record(channel, payload) {
  if (!TRANSCRIPT_EVENTS.includes(channel)) return;
  payload._seq = ++transcriptSeq;
  transcript.push({ channel, payload });
  if (transcript.length > TRANSCRIPT_CAP) transcript.splice(0, transcript.length - TRANSCRIPT_CAP);
}

function chat(role, text, extra) {
  const payload = { role, text, ...extra };
  record("chat", payload);
  sendToUi("chat", payload);
}

// Remembered so a window that loads after the event (or is reopened) can ask
// for it -- BUGS.md #18.
let bridgeStatus = "connecting";

function setStatus(status) {
  bridgeStatus = status;
  sendToUi("status", status);
}

// Tells the renderer its cached catalog is stale (a skill was saved, a
// preference changed) so the Library view refetches instead of drifting.
function invalidateInventory() {
  sendToUi("inventory-changed");
}

function confirmRequest(id, tool, args, reason, voiceApprovable) {
  // Every window gets it: the main window and the chat panel draw the full
  // four-way card, the overlay a compact Approve/Decline one (only while a
  // session is live). reason: why this one asks (optional).
  // voice_approvable: false means a spoken yes won't approve it -- it needs
  // a click. Older agents send neither; the cards treat that as no reason
  // and voice-approvable.
  const payload = { id, tool, args, reason: reason || null, voice_approvable: voiceApprovable !== false };
  record("confirm-request", payload);
  broadcast("confirm-request", payload);
}

// Settled out-of-band (voice, the Qt overlay, a timeout).
function confirmResolved(payload) {
  record("confirm-resolved", payload);
  broadcast("confirm-resolved", payload);
}

function skillProposed(id, name, description, steps, uncertainParams) {
  const payload = { id, name, description, steps, uncertain_params: uncertainParams };
  record("skill-proposed", payload);
  sendToUi("skill-proposed", payload);
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
    trafficLightPosition: { x: 16, y: 20 },
    backgroundColor: "#0a0810", // --base in renderer/styles.css
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
    },
  });
  devWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  devWindow.webContents.once("did-finish-load", flushEarlyLog);
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

// --- Qt overlay (LEUTHERIA_OVERLAY=qt) --------------------------------------

function startOverlay() {
  if (OVERLAY_MODE !== "qt") {
    createOverlayWindow();
    return;
  }
  let usable = false;
  try {
    fs.accessSync(QT_OVERLAY_BIN, fs.constants.X_OK);
    usable = true;
  } catch (_err) {
    // reported below
  }
  if (!usable) {
    log(
      `[overlay] LEUTHERIA_OVERLAY=qt but the Qt overlay isn't built (${QT_OVERLAY_BIN} missing or not executable) ` +
        "-- falling back to the Electron overlay. Build it with: cmake -S overlay-qt -B overlay-qt/build -G Ninja " +
        "-DCMAKE_PREFIX_PATH=/opt/homebrew/opt/qt && cmake --build overlay-qt/build"
    );
    createOverlayWindow();
    return;
  }
  spawnQtOverlay();
}

function spawnQtOverlay() {
  // No overlay window of our own in this mode: every overlayWindow use goes
  // through alive(), so the show/hide/interactive calls are simply no-ops,
  // and the Qt overlay reacts to the agent's messages itself. The tray, the
  // shortcut and the main window's voice pill don't depend on either overlay.
  const args = ["--url", AGENT_URL, "--assets", OVERLAY_ASSETS_DIR];
  log(`[overlay] LEUTHERIA_OVERLAY=qt -- spawning ${QT_OVERLAY_BIN} ${args.join(" ")}`);
  // The token goes in the environment, never in args: argv is visible to
  // every user through `ps`, and is logged on the line above.
  const child = spawn(QT_OVERLAY_BIN, args, {
    stdio: ["ignore", "pipe", "pipe"],
    env: childEnv(agentAuth.current()),
  });
  qtOverlayProcess = child;
  child.stdout.on("data", (data) => log(`[overlay-qt] ${data.toString().trim()}`));
  // Qt's logging (qInfo included) all goes to stderr, so stderr isn't an
  // error channel here; the lines carry their own category and level.
  child.stderr.on("data", (data) => log(`[overlay-qt] ${data.toString().trim()}`));
  // spawn() reports a failure to start (EACCES, a bad bundle) as "error",
  // possibly without an "exit" -- both paths land in overlayGone().
  child.on("error", (err) => overlayGone(child, `failed to start: ${err.message}`));
  child.on("exit", (code, signal) => overlayGone(child, `exited (code ${code}, signal ${signal})`));
}

function overlayGone(child, why) {
  if (qtOverlayProcess !== child) return; // already handled (error then exit)
  qtOverlayProcess = null;
  if (quitting) return;
  // A crashed overlay would otherwise leave sessions with nothing on screen.
  log(`[overlay] Qt overlay ${why} -- falling back to the Electron overlay`);
  if (!alive(overlayWindow)) createOverlayWindow();
}

function stopQtOverlay() {
  if (qtOverlayProcess) qtOverlayProcess.kill();
}

/* Where the overlay sits: alerts.anchor (top-center by default) on
   alerts.display (the cursor's display by default), read from the settings
   each time a session starts. overlay.css flips the stage for bottom
   anchors (body[data-anchor], set by appearance.js) so the plate stays on
   the screen edge and the card opens toward the middle. */
function positionOverlay() {
  const { anchor, display: which } = settings.alerts;
  const display = which === "primary" ? screen.getPrimaryDisplay() : screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const area = display.workArea;
  const { width, height } = OVERLAY_SIZE;
  const [row, column] = anchor === "center" ? ["center", "center"] : anchor.split("-");
  const x =
    column === "left"
      ? area.x + OVERLAY_TOP_MARGIN
      : column === "right"
        ? area.x + area.width - width - OVERLAY_TOP_MARGIN
        : area.x + (area.width - width) / 2;
  const y =
    row === "top"
      ? area.y + OVERLAY_TOP_MARGIN
      : row === "bottom"
        ? area.y + area.height - height - OVERLAY_TOP_MARGIN
        : area.y + (area.height - height) / 2;
  overlayWindow.setBounds({ x: Math.round(x), y: Math.round(y), width, height });
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

// --- overlay settings: panels, hot edge, alerts, appearance -----------------
// One file, userData/overlay-layout.json, validated on load
// (overlay-settings.js). Every window is told when it changes.

let settingsFile = null;
let settings = overlaySettings.defaults();

function loadSettings() {
  settingsFile = path.join(app.getPath("userData"), "overlay-layout.json");
  const result = overlaySettings.load(settingsFile);
  settings = result.settings;
  if (result.corrupt) {
    // Kept beside it rather than overwritten, in case it was hand-edited.
    const aside = `${settingsFile}.corrupt`;
    try {
      fs.renameSync(settingsFile, aside);
      log(`[panels] overlay-layout.json ${result.problems[0]} -- moved it to ${path.basename(aside)}`);
    } catch (err) {
      log(`[panels] overlay-layout.json ${result.problems[0]} (couldn't move it aside: ${err.message})`);
    }
    return;
  }
  for (const problem of result.problems) log(`[panels] overlay-layout.json: ${problem} -- using the default`);
}

/** The colours, theme and density every window applies (appearance.js). */
function appearancePayload() {
  const look = settings.appearance;
  const derived = look.accent ? overlaySettings.deriveAccent(look.accent) : null;
  return {
    theme: look.theme,
    themeChosen: look.themeChosen,
    density: look.density,
    accent: look.accent,
    vars: derived && derived.ok ? derived.vars : null,
    toastAnchor: settings.alerts.toastAnchor,
    alertsAnchor: settings.alerts.anchor,
  };
}

function updateSettings(patch) {
  const result = overlaySettings.applyPatch(settings, patch || {});
  if (!result.ok) return { ok: false, error: result.error, settings };
  const previous = settings;
  settings = result.settings;
  try {
    overlaySettings.save(settingsFile, settings);
  } catch (err) {
    log(`[panels] couldn't save overlay-layout.json: ${err.message}`);
  }
  if (panels) panels.applySettings(previous, settings);
  // A new alerts anchor applies at once if the overlay is up.
  if (alive(overlayWindow) && overlayWindow.isVisible() && JSON.stringify(previous.alerts) !== JSON.stringify(settings.alerts)) {
    positionOverlay();
  }
  const appearance = appearancePayload();
  for (const win of [devWindow, overlayWindow, ...panelWindows()]) {
    if (!alive(win)) continue;
    win.webContents.send("appearance", appearance);
    win.webContents.send("overlay-settings", settings);
  }
  const notes = settings.appearance.accent ? overlaySettings.deriveAccent(settings.appearance.accent).notes : [];
  return { ok: true, settings, notes };
}

/* A `panel` message from the agent (show_panel / hide_panel skills):
   {"type":"panel","action":"show"|"hide"|"toggle","panel":"<id>"|"all","query"?}.
   "all" only makes sense with hide. Unknown ids and actions are logged and
   dropped -- the agent's reply still arrives as the normal response. */
function handlePanelMessage(payload) {
  if (!panels) return;
  const id = payload.panel;
  const action = payload.action;
  if (action === "hide" && (id === "all" || id === undefined || id === null)) {
    panels.hideAll("agent");
    return;
  }
  if (!overlaySettings.PANEL_IDS.includes(id)) {
    log(`[panels] agent asked to ${action} unknown panel ${JSON.stringify(id)} -- ignored`);
    return;
  }
  if (action === "show") panels.show(id, { query: payload.query, source: "agent" });
  else if (action === "hide") panels.hide(id, "agent");
  else if (action === "toggle") panels.toggle(id, { query: payload.query, source: "agent" });
  else log(`[panels] unknown panel action ${JSON.stringify(action)} -- ignored`);
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
  // The agent requires this token on every connection (and removes it from
  // its own environment once read, so commands it runs don't inherit it).
  agentProcess = spawn(AGENT_PYTHON, ["-m", "agent"], { cwd: AGENT_DIR, env: childEnv(agentAuth.current()) });

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
  } else if (payload.type === "control_result" && Array.isArray(payload.skills) && "index_ok" in payload) {
    // The shared-skill index: summarized, like the inventory.
    log(
      `[bridge] received: {"type":"control_result","request_id":"${payload.request_id}"} ` +
        `<registry index: ${payload.index_ok ? "verified" : `not usable (${payload.error_code || payload.error || "?"})`}, ${payload.skills.length} entries>`
    );
  } else {
    const text = JSON.stringify(payload);
    // A skill definition (registry preview, share preview) can be long.
    log(`[bridge] received: ${text.length > 1500 ? `${text.slice(0, 1500)}... <${text.length} chars>` : text}`);
  }
}

function connectToAgent() {
  ws = new WebSocket(AGENT_URL, connectOptions(agentAuth.current()));

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
      confirmRequest(payload.id, payload.tool, payload.args, payload.reason, payload.voice_approvable);
      captureWindows("confirm-card");
      return;
    }

    if (payload.type === "confirmation_resolved") {
      // Answered out-of-band -- by voice, or on the Qt overlay (by:"overlay")
      // -- so settle the on-screen cards. Only a voice answer comes with an
      // empty-response ack; an overlay click is a bare confirm, like a click
      // here.
      if (payload.by === "voice") expectVoiceConfirmAck = true;
      confirmResolved(payload);
      return;
    }

    if (payload.type === "panel") {
      // show_panel / hide_panel. Arrives before the skill's own response,
      // so it must never touch pendingIsChat (BUGS.md #17's lesson).
      handlePanelMessage(payload);
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

    if (payload.type === "skill_save_failed") {
      // Must be caught here: an unknown type falls through to pendingIsChat
      // below and could be shown in place of a real reply still on its way.
      chat("assistant", `Couldn't save "${payload.name}" as a skill: ${payload.error}`, { error: true });
      return;
    }

    if (payload.type === "skill_saved") {
      chat("assistant", `Saved "${payload.name}" as a new skill.`, { notice: "success" });
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
    const why = explainRejection(err.message, agentAuth);
    if (why) log(`[bridge:err] ${why}`);
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
// refresh: re-read the OS device list first (a mic plugged in since launch).
ipcMain.handle("list-input-devices", (_event, refresh) =>
  sendControlRequest({ type: "list_input_devices", refresh: Boolean(refresh) })
);
ipcMain.handle("list-wake-models", (_event, paths) =>
  sendControlRequest({ type: "list_wake_models", paths: paths || [] })
);

ipcMain.handle("overlay-manifest", () => loadOverlayManifest());
ipcMain.on("overlay-interactive", (_event, interactive) => {
  if (Boolean(interactive) !== overlayInteractive) setOverlayInteractive(Boolean(interactive));
});

// Any window can answer a confirmation. The others' cards are settled here
// rather than waiting on the agent, which only reports voice answers. `by`
// says where it was answered, from each receiver's point of view: "ui" is
// the other of the main window and the overlay (what both have always
// understood it as), "main" and "panel" name the window for a chat panel.
ipcMain.on("confirm-response", (event, { id, approved, remember }) => {
  if (!isConnected()) return;
  const message = { type: "confirm", id, approved, remember };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  ws.send(JSON.stringify(message));
  const from = panels && panels.isPanel(event.sender) ? "panel" : alive(overlayWindow) && event.sender === overlayWindow.webContents ? "overlay" : "main";
  record("confirm-resolved", { id, approved, by: from });
  for (const win of [devWindow, overlayWindow, ...panelWindows()]) {
    if (!alive(win) || win.webContents === event.sender) continue;
    const by = from === "panel" ? "panel" : from === "main" && win !== overlayWindow ? "main" : "ui";
    win.webContents.send("confirm-resolved", { id, approved, by });
  }
});

ipcMain.on("skill-response", (event, { id, approved, resolutions }) => {
  if (!isConnected()) return;
  const message = { type: "skill_response", id, approved, resolutions };
  log(`[bridge] sending: ${JSON.stringify(message)}`);
  ws.send(JSON.stringify(message));
  // The same proposal is on the main window and any chat panel: settle the
  // copies that weren't clicked.
  const payload = { id, approved };
  record("skill-resolved", payload);
  for (const win of [devWindow, ...panelWindows()]) {
    if (alive(win) && win.webContents !== event.sender) win.webContents.send("skill-resolved", payload);
  }
});

// --- panels and overlay settings ----------------------------------------------

// Which screen edge the Dock is on, for the hot edge's conflict note. Read
// from the Dock's own preferences; "bottom" is its default (no key set).
function dockEdge() {
  try {
    return execSync("defaults read com.apple.dock orientation", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim() || "bottom";
  } catch (_err) {
    return "bottom";
  }
}

ipcMain.handle("get-overlay-settings", () => ({
  settings,
  presets: overlaySettings.PRESETS,
  dockEdge: dockEdge(),
  qtOverlay: OVERLAY_MODE === "qt",
}));
ipcMain.handle("set-overlay-settings", (_event, patch) => updateSettings(patch));
ipcMain.handle("check-accent", (_event, hex) => overlaySettings.deriveAccent(hex));
ipcMain.handle("reset-panel-layout", (_event, id) => {
  const ids = id === "all" ? overlaySettings.PANEL_IDS : [id];
  if (!ids.every((each) => overlaySettings.PANEL_IDS.includes(each))) return { ok: false, error: "unknown panel" };
  return updateSettings({ panels: Object.fromEntries(ids.map((each) => [each, overlaySettings.defaultPanel(each)])) });
});
// Read synchronously by preload.js, so a window paints in the right theme
// and accent from its first frame instead of flashing the defaults.
ipcMain.on("get-appearance-sync", (event) => {
  event.returnValue = appearancePayload();
});
ipcMain.handle("show-panel", (_event, id, query) => Boolean(panels && panels.show(id, { query, source: "ui" })));
ipcMain.handle("hide-panel", (_event, id) => Boolean(panels && (id === "all" ? panels.hideAll("ui") : panels.hide(id, "ui"))));
ipcMain.handle("panel-op", (event, op, data) => (panels ? panels.command(event.sender, op, data) : { ok: false }));
ipcMain.handle("chat-history", () => transcript);
ipcMain.handle("list-timers", () => sendControlRequest({ type: "list_timers" }));

// --- the shared-skill registry (agent: core/registry_client.py) --------------
// Fetching and verifying the index is network work on the agent's side, so
// these get a longer deadline than the inventory reads.
const REGISTRY_TIMEOUT_MS = 30000;

ipcMain.handle("registry-list", (_event, refresh) =>
  sendControlRequest({ type: "registry_list", ...(refresh ? { refresh: true } : {}) }, REGISTRY_TIMEOUT_MS)
);
// Read-only: the full definition the install card shows. Nothing is installed.
ipcMain.handle("registry-preview", (_event, { name, contentHash }) =>
  sendControlRequest({ type: "registry_preview", name, content_hash: contentHash }, REGISTRY_TIMEOUT_MS)
);
// Sent only from the install card's own button, after the user has seen every
// step (renderer/js/library-shared.js). The agent re-checks everything.
ipcMain.handle("registry-install", async (_event, { name, contentHash }) => {
  const result = await sendControlRequest({ type: "registry_install", name, content_hash: contentHash }, REGISTRY_TIMEOUT_MS);
  if (result.ok) invalidateInventory();
  return result;
});
ipcMain.handle("registry-share-preview", (_event, skill) =>
  sendControlRequest({ type: "registry_share_preview", skill }, REGISTRY_TIMEOUT_MS)
);

/** True only for the registry repo's "new file" page on github.com: the one
 *  place a share opens (core/registry_client.py SHARE_REPO + /new/main). */
const SHARE_NEW_PATH = "/AnuragK-O/leutheria-skills/new/main";
function isGithubUrl(text) {
  if (typeof text !== "string" || !text.startsWith(`https://github.com${SHARE_NEW_PATH}?`)) return false;
  try {
    const url = new URL(text);
    return (
      url.protocol === "https:" && url.hostname === "github.com" && !url.username && !url.password && !url.port && url.pathname === SHARE_NEW_PATH && !url.hash
    );
  } catch (_err) {
    return false;
  }
}

// Share: the agent builds the URL of GitHub's "new file" page, prefilled. This
// process opens it -- never a URL from a renderer, and never anything but
// https://github.com/. When the file is too long to go in the URL, the agent
// says so (copy_to_clipboard) and sends the JSON (clipboard_text): it goes on
// the clipboard and the page opens without it, for the user to paste. Under LEUTHERIA_TEST_HOOKS neither the
// browser nor the clipboard is touched; the capture script reads what would
// have happened from __leutheriaTest.shared().
const sharedRecord = [];
ipcMain.handle("registry-share", async (_event, { skill, acknowledged } = {}) => {
  // acknowledged: the flag ids the user ticked; the agent refuses the share
  // (unacknowledged_flags) unless every flag is among them.
  const acks = Array.isArray(acknowledged) ? acknowledged.filter((id) => typeof id === "string").slice(0, 200) : [];
  const result = await sendControlRequest({ type: "registry_share", skill, acknowledged: acks }, REGISTRY_TIMEOUT_MS);
  if (!result.ok) return { ok: false, error: result.error || "The agent couldn't prepare the share.", error_code: result.error_code };
  if (!isGithubUrl(result.url)) {
    log(`[share] refused to open a URL that isn't https://github.com/: ${String(result.url).slice(0, 80)}`);
    return { ok: false, error: "The share link didn't point at github.com/AnuragK-O/leutheria-skills, so it wasn't opened.", error_code: "bad_url" };
  }
  const copy = Boolean(result.copy_to_clipboard) && typeof result.clipboard_text === "string";
  if (result.copy_to_clipboard && !copy) return { ok: false, error: "The file is too long for a link and the agent sent no copy of it." };
  if (TEST_HOOKS) {
    sharedRecord.push({ url: result.url, copied: copy ? result.clipboard_text.length : 0 });
  } else {
    if (copy) clipboard.writeText(result.clipboard_text);
    await shell.openExternal(result.url);
  }
  log(`[share] opened GitHub for ${skill}${copy ? " (file copied to the clipboard: too long for the link)" : ""}`);
  return { ok: true, copied: copy, path: result.path || null };
});
// "Open in Library" from a panel: an explicit click, so bringing the main
// window forward is what was asked for.
ipcMain.on("open-main", (_event, { view, name }) => {
  showMainWindow();
  const send = () => sendToMain("navigate", { view, name });
  if (alive(devWindow) && devWindow.webContents.isLoading()) devWindow.webContents.once("did-finish-load", send);
  else send();
});

ipcMain.on("user-audio", (_event, { data, format }) => {
  if (!isConnected()) {
    chat("assistant", "Not connected to the agent yet.", { notice: "warning" });
    return;
  }
  const message = { type: "audio", data, format };
  log(`[bridge] sending: {"type":"audio","format":"${format}","data":"<${data.length} b64 chars>"}`);
  pendingIsChat = true;
  ws.send(JSON.stringify(message));
});

ipcMain.on("user-command", (_event, text) => {
  if (!isConnected()) {
    chat("assistant", "Not connected to the agent yet.", { notice: "warning" });
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
  loadSettings();
  panels = createPanelManager({
    rendererDir: path.join(__dirname, "renderer"),
    preload: path.join(__dirname, "preload.js"),
    log,
    getSettings: () => settings,
    updateSettings,
    captureTheme: CAPTURE_THEME,
    testHooks: TEST_HOOKS,
  });
  createTray();
  createDevWindow();
  startOverlay();
  registerShortcut();
  startAgent();
  panels.restorePinned();
  if (TEST_HOOKS) installTestHooks();
});

function installTestHooks() {
  globalThis.__leutheriaTest = {
    setCursor: (point) => {
      TEST_HOOKS.cursor = point;
    },
    setMouseDown: (down) => {
      TEST_HOOKS.mouseDown = down;
    },
    panels: () => panels.debugState(),
    focused: () => {
      const win = BrowserWindow.getFocusedWindow();
      if (!win) return null;
      if (win === devWindow) return "main";
      if (win === overlayWindow) return "overlay";
      return panels.debugState().find((entry) => entry.focused)?.id || "other";
    },
    show: (id, query, dock) => panels.show(id, { query, source: "test", dock }),
    trace: (id) => panels.trace(id),
    hideAll: () => panels.hideAll("test"),
    hide: (id) => panels.hide(id, "test"),
    toggle: (id) => panels.toggle(id, { source: "test" }),
    display: () => {
      const display = screen.getPrimaryDisplay();
      return { bounds: display.bounds, workArea: display.workArea, count: screen.getAllDisplays().length };
    },
    overlayBounds: () => (alive(overlayWindow) ? overlayWindow.getBounds() : null),
    positionOverlay: () => alive(overlayWindow) && positionOverlay(),
    settings: () => settings,
    settingsFile: () => settingsFile,
    shared: () => sharedRecord,
  };
}

// Dock icon click with the main window closed.
app.on("activate", showMainWindow);

// Closing the main window leaves the agent (and any voice session) running --
// reopen it from the tray. The children are killed in before-quit, not here:
// this used to kill the agent on the assumption that the Electron overlay
// window always exists, so "all closed" could only mean quitting. With the Qt
// overlay there is no overlay window, and closing the main window alone would
// have killed the agent out from under the tray.
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
  stopQtOverlay();
});

app.on("before-quit", () => {
  quitting = true;
  if (agentProcess) agentProcess.kill();
  stopQtOverlay();
});
