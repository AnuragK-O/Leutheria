/* The floating panels: one frameless BrowserWindow per open panel (chat,
   skills, activity, permissions, timers, voice), placed by its layout in
   overlay-layout.json, plus the hot edge that opens one when the cursor rests
   against a screen edge.

   The focus rule: a panel never takes focus by appearing. It is shown with
   showInactive() whether a voice command, the hot edge or a button opened it,
   and it is an NSPanel (type "panel"), so clicking into it lets you type
   without bringing the main window forward. Nothing here calls focus().

   main.js owns the settings (it loads, validates, persists and broadcasts
   them); this module asks it for the current ones and hands it patches. */

const { BrowserWindow, screen } = require("electron");
const { execFile } = require("child_process");
const path = require("path");
const { PANEL_IDS, MIN_SIZE } = require("./overlay-settings");

// How often the cursor is sampled. Electron has no global mouse-move event,
// so the hot edge and "hide when the cursor leaves" both poll; only while one
// of them is in use.
const POLL_MS = 80;
// Within this many pixels of the display's edge counts as "at the edge".
const EDGE_PX = 2;
// "Hide when the cursor leaves": how long it must stay out first, so
// brushing past the panel's edge doesn't close it.
const LEAVE_GRACE_MS = 700;
// Horizontal panels docked top/bottom fill the work area's width up to this,
// centred; past it a strip of cards is mostly empty space.
const MAX_STRIP_W = 1600;

function createPanelManager({ rendererDir, preload, log, getSettings, updateSettings, captureTheme, testHooks }) {
  const entries = new Map(); // id -> entry (see ensure())
  let pollTimer = null;
  let hot = { since: null, edge: null, fired: false, checking: false };

  const alive = (win) => Boolean(win && !win.isDestroyed());
  const layoutOf = (id) => getSettings().panels[id];

  // --- cursor (overridable by the env-gated test hook only) -------------------

  function cursor() {
    if (testHooks && testHooks.cursor) return testHooks.cursor;
    return screen.getCursorScreenPoint();
  }

  /* Whether a mouse button is held: no Electron API reports this outside our
     own windows, so it is asked of AppKit (NSEvent.pressedMouseButtons) via
     osascript -- ~80 ms, and only once per dwell, never per poll. A failure
     answers "not held": a panel appearing during a drag is a nuisance, not
     a hazard. */
  function mouseHeld() {
    if (testHooks && typeof testHooks.mouseDown === "boolean") return Promise.resolve(testHooks.mouseDown);
    return new Promise((resolve) => {
      execFile(
        "/usr/bin/osascript",
        ["-l", "JavaScript", "-e", 'ObjC.import("AppKit"); $.NSEvent.pressedMouseButtons'],
        { timeout: 600 },
        (err, stdout) => resolve(!err && Number(String(stdout).trim()) > 0)
      );
    });
  }

  // --- placement --------------------------------------------------------------

  function displayFor(layout) {
    if (layout.display === "primary") return screen.getPrimaryDisplay();
    return screen.getDisplayNearestPoint(cursor());
  }

  function clampTo(area, bounds) {
    const w = Math.min(bounds.width, area.width);
    const h = Math.min(bounds.height, area.height);
    return {
      x: Math.round(Math.min(Math.max(bounds.x, area.x), area.x + area.width - w)),
      y: Math.round(Math.min(Math.max(bounds.y, area.y), area.y + area.height - h)),
      width: Math.round(w),
      height: Math.round(h),
    };
  }

  const intersects = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

  /** Where panel `id` goes, given its layout and (for the hot edge) a dock
      that applies to this showing only. */
  function placement(id, dockOverride) {
    const layout = layoutOf(id);
    const orientation = layout.orientation;
    const size = layout[orientation];
    const dock = dockOverride || layout.dock;

    if (dock === "free") {
      const wanted = { x: layout.x, y: layout.y, width: size.w, height: size.h };
      // The display it was left on, if that display is still attached;
      // otherwise the primary one (a monitor that was unplugged).
      const display = screen.getAllDisplays().find((entry) => intersects(entry.workArea, wanted)) || screen.getPrimaryDisplay();
      const area = display.workArea;
      const onIt = intersects(area, wanted)
        ? wanted
        : { ...wanted, x: area.x + (area.width - size.w) / 2, y: area.y + (area.height - size.h) / 2 };
      return { bounds: clampTo(area, onIt), dock };
    }

    const area = displayFor(layout).workArea;
    let bounds;
    if (dock === "left" || dock === "right") {
      // A vertical panel on a side edge runs the work area's full height.
      const h = orientation === "vertical" ? area.height : size.h;
      const x = dock === "left" ? area.x : area.x + area.width - size.w;
      bounds = { x, y: area.y + (area.height - h) / 2, width: size.w, height: h };
    } else {
      // A horizontal panel on the top or bottom edge is a strip along it.
      const w = orientation === "horizontal" ? Math.min(area.width, MAX_STRIP_W) : size.w;
      const y = dock === "top" ? area.y : area.y + area.height - size.h;
      bounds = { x: area.x + (area.width - w) / 2, y, width: w, height: size.h };
    }
    return { bounds: clampTo(area, bounds), dock };
  }

  /** Which edges the user may drag to resize: all of them when free; when
      docked, only the one facing into the screen. */
  function resizeEdges(dock) {
    return { free: ["n", "s", "e", "w", "ne", "nw", "se", "sw"], left: ["e"], right: ["w"], top: ["s"], bottom: ["n"] }[dock];
  }

  // --- windows ----------------------------------------------------------------

  function ensure(id) {
    let entry = entries.get(id);
    if (entry && alive(entry.win)) return entry;
    const win = new BrowserWindow({
      width: 400,
      height: 600,
      show: false,
      frame: false,
      transparent: true,
      backgroundColor: "#00000000",
      // The drop shadow is drawn in CSS (DESIGN.md: the one shadow, for
      // things that float over other apps) so it follows the cut corner.
      hasShadow: false,
      // Resized from its own edges (panel.js), not the OS's.
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      hiddenInMissionControl: true,
      // NSPanel: floats over full-screen apps, and clicking it doesn't
      // activate the app (the main window stays where it was).
      type: "panel",
      // A click on a button presses it, rather than first making the panel key.
      acceptFirstMouse: true,
      webPreferences: { preload, backgroundThrottling: false },
    });
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
    applyLevel(win);
    const query = { id };
    if (captureTheme) query.theme = captureTheme;
    win.loadFile(path.join(rendererDir, "panels", "panel.html"), { query });
    entry = {
      id,
      win,
      ready: false, // the page has said panelReady
      dockOverride: null, // the hot edge's dock, for this showing only
      visited: false,
      outSince: null,
      gesture: null, // { kind: "move"|"resize", edge, start }
      pendingQuery: null,
    };
    entries.set(id, entry);
    win.on("closed", () => {
      if (entries.get(id) === entry) entries.delete(id);
      syncPolling();
    });
    // The page asked to be shown before it had painted: show it once it has.
    win.once("ready-to-show", () => {
      if (entry.showWhenReady && alive(win)) {
        entry.showWhenReady = false;
        win.showInactive();
      }
    });
    return entry;
  }

  function applyLevel(win) {
    const level = getSettings().appearance.panelLevel;
    if (level === "normal") win.setAlwaysOnTop(false);
    else win.setAlwaysOnTop(true, "floating");
  }

  function opacityOf(id) {
    const layout = layoutOf(id);
    return layout.opacity === null ? getSettings().appearance.panelOpacity : layout.opacity;
  }

  function configFor(entry) {
    const layout = layoutOf(entry.id);
    const dock = entry.dockOverride || layout.dock;
    return {
      id: entry.id,
      dock,
      savedDock: layout.dock,
      orientation: layout.orientation,
      opacity: opacityOf(entry.id),
      pinned: layout.pinned,
      autoHide: layout.autoHide,
      resizeEdges: resizeEdges(dock),
    };
  }

  function sendConfig(entry) {
    if (alive(entry.win)) entry.win.webContents.send("panel-config", configFor(entry));
  }

  function place(entry) {
    if (!alive(entry.win)) return;
    const { bounds } = placement(entry.id, entry.dockOverride);
    entry.win.setBounds(bounds);
    entry.win.setOpacity(opacityOf(entry.id));
    sendConfig(entry);
  }

  const isVisible = (entry) => Boolean(entry && alive(entry.win) && (entry.win.isVisible() || entry.showWhenReady));

  /** Show panel `id`. source: "agent" | "hot-edge" | "ui" | "restore" (logged).
      dock: the hot edge's edge, for this showing only. */
  function show(id, { query = null, source = "ui", dock = null } = {}) {
    if (!PANEL_IDS.includes(id)) {
      log(`[panels] unknown panel "${id}" -- ignored`);
      return false;
    }
    const entry = ensure(id);
    if (query !== null && query !== undefined) deliverQuery(entry, String(query));
    if (isVisible(entry)) {
      sendConfig(entry);
      return true; // already up: don't jump it around
    }
    entry.dockOverride = dock && dock !== layoutOf(id).dock ? dock : null;
    entry.visited = false;
    entry.outSince = null;
    place(entry);
    // showInactive: opened by voice or the hot edge while the user is in
    // another app -- appearing must never take their focus.
    if (entry.ready || entry.win.webContents.isLoading() === false) entry.win.showInactive();
    else entry.showWhenReady = true;
    log(`[panels] ${id} shown (${source}${entry.dockOverride ? `, docked ${entry.dockOverride} for now` : ""})`);
    syncPolling();
    return true;
  }

  function hide(id, source = "ui") {
    const entry = entries.get(id);
    if (!isVisible(entry)) return false;
    entry.showWhenReady = false;
    entry.gesture = null;
    entry.win.hide();
    entry.dockOverride = null;
    log(`[panels] ${id} hidden (${source})`);
    syncPolling();
    return true;
  }

  function hideAll(source = "ui") {
    let any = false;
    for (const id of entries.keys()) any = hide(id, source) || any;
    return any;
  }

  function toggle(id, options = {}) {
    return isVisible(entries.get(id)) ? hide(id, options.source) : show(id, options);
  }

  function deliverQuery(entry, query) {
    if (entry.ready && alive(entry.win)) entry.win.webContents.send("panel-query", query);
    else entry.pendingQuery = query;
  }

  // --- messages from a panel's own chrome -------------------------------------

  function entryFor(webContents) {
    for (const entry of entries.values()) if (alive(entry.win) && entry.win.webContents === webContents) return entry;
    return null;
  }

  function persist(id, patch) {
    return updateSettings({ panels: { [id]: patch } });
  }

  /* The page reports pointer movement in screen coordinates (PointerEvent
     screenX/Y are DIPs, the same space as setBounds); the window is moved
     here, from the bounds it had when the gesture started. */
  function gesture(entry, op, data) {
    const win = entry.win;
    if (op === "move-start" || op === "resize-start") {
      entry.gesture = { kind: op === "move-start" ? "move" : "resize", edge: data && data.edge, start: win.getBounds() };
      return;
    }
    const g = entry.gesture;
    if (!g) return;
    const dx = Math.round(Number(data && data.dx) || 0);
    const dy = Math.round(Number(data && data.dy) || 0);
    if (op === "move") {
      win.setBounds({ ...g.start, x: g.start.x + dx, y: g.start.y + dy });
      return;
    }
    if (op === "resize") {
      win.setBounds(resized(entry.id, g.start, g.edge, dx, dy));
      return;
    }
    if (op === "move-end" || op === "resize-end") {
      entry.gesture = null;
      const layout = layoutOf(entry.id);
      const bounds = win.getBounds();
      const display = screen.getDisplayMatching(bounds);
      const clamped = clampTo(display.workArea, bounds);
      const sizePatch = { [layout.orientation]: { w: clamped.width, h: clamped.height } };
      const dock = entry.dockOverride || layout.dock;
      if (g.kind === "move" || dock === "free") {
        // Moving a docked panel undocks it: it stays where it was dropped.
        entry.dockOverride = null;
        persist(entry.id, { dock: "free", x: clamped.x, y: clamped.y, ...sizePatch });
      } else {
        // Docked: only the dimension facing into the screen was changed.
        const keep = layout[layout.orientation];
        const horizontalEdge = dock === "left" || dock === "right";
        persist(entry.id, {
          [layout.orientation]: horizontalEdge ? { w: clamped.width, h: keep.h } : { w: keep.w, h: clamped.height },
        });
      }
      place(entry);
    }
  }

  function resized(id, start, edge, dx, dy) {
    const min = MIN_SIZE[layoutOf(id).orientation];
    let { x, y, width, height } = start;
    if (edge.includes("e")) width = Math.max(min.w, start.width + dx);
    if (edge.includes("s")) height = Math.max(min.h, start.height + dy);
    if (edge.includes("w")) {
      width = Math.max(min.w, start.width - dx);
      x = start.x + start.width - width;
    }
    if (edge.includes("n")) {
      height = Math.max(min.h, start.height - dy);
      y = start.y + start.height - height;
    }
    return { x, y, width, height };
  }

  function command(webContents, op, data) {
    const entry = entryFor(webContents);
    if (!entry) return { ok: false, error: "not a panel" };
    const id = entry.id;
    const layout = layoutOf(id);
    switch (op) {
      case "ready": {
        entry.ready = true;
        sendConfig(entry);
        if (entry.pendingQuery !== null) {
          entry.win.webContents.send("panel-query", entry.pendingQuery);
          entry.pendingQuery = null;
        }
        return { ok: true, config: configFor(entry) };
      }
      case "move-start":
      case "move":
      case "move-end":
      case "resize-start":
      case "resize":
      case "resize-end":
        gesture(entry, op, data);
        return { ok: true };
      case "dock": {
        const patch = { dock: data };
        if (data === "free") {
          const bounds = entry.win.getBounds();
          Object.assign(patch, { x: bounds.x, y: bounds.y, [layout.orientation]: { w: bounds.width, h: bounds.height } });
        }
        entry.dockOverride = null;
        const result = persist(id, patch);
        place(entry);
        return result;
      }
      case "orientation": {
        const next = data === "horizontal" || data === "vertical" ? data : layout.orientation === "vertical" ? "horizontal" : "vertical";
        const result = persist(id, { orientation: next });
        place(entry);
        return result;
      }
      case "opacity":
        return persist(id, { opacity: Number(data) });
      case "pin":
        return persist(id, { pinned: typeof data === "boolean" ? data : !layout.pinned });
      case "auto-hide":
        return persist(id, { autoHide: data });
      case "close":
        hide(id, "closed");
        return { ok: true };
      default:
        return { ok: false, error: `unknown panel op ${op}` };
    }
  }

  // --- settings changed (from anywhere) ---------------------------------------

  function applySettings(previous, next) {
    for (const entry of entries.values()) {
      if (!alive(entry.win)) continue;
      applyLevel(entry.win);
      const before = JSON.stringify(previous.panels[entry.id]);
      const after = JSON.stringify(next.panels[entry.id]);
      if (isVisible(entry) && !entry.gesture && before !== after) place(entry);
      else if (isVisible(entry)) {
        entry.win.setOpacity(opacityOf(entry.id));
        sendConfig(entry);
      }
    }
    if (previous.appearance.panelOpacity !== next.appearance.panelOpacity) {
      for (const entry of entries.values()) if (alive(entry.win)) entry.win.setOpacity(opacityOf(entry.id));
    }
    syncPolling();
  }

  /** A layout back to its defaults, applied at once if the panel is up. */
  function displaysChanged() {
    for (const entry of entries.values()) if (isVisible(entry) && !entry.gesture) place(entry);
  }

  screen.on("display-removed", displaysChanged);
  screen.on("display-added", displaysChanged);
  screen.on("display-metrics-changed", displaysChanged);

  // --- the poll: hot edge and auto-hide ---------------------------------------

  function needsPolling() {
    const settings = getSettings();
    if (settings.hotEdge.enabled) return true;
    for (const entry of entries.values()) {
      const layout = settings.panels[entry.id];
      if (isVisible(entry) && !layout.pinned && layout.autoHide.mode !== "never") return true;
    }
    return false;
  }

  function syncPolling() {
    const want = needsPolling();
    if (want && !pollTimer) pollTimer = setInterval(poll, POLL_MS);
    if (!want && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
      hot = { since: null, edge: null, fired: false, checking: false };
    }
  }

  const inside = (point, b) => point.x >= b.x && point.x < b.x + b.width && point.y >= b.y && point.y < b.y + b.height;

  /** The edge of `display` the point is pressed against, if that edge is an
      outer one -- a shared edge between two monitors is a doorway the cursor
      passes through, not somewhere it rests. */
  function edgeAt(point, display) {
    const b = display.bounds;
    const at = {
      left: point.x - b.x <= EDGE_PX,
      right: b.x + b.width - 1 - point.x <= EDGE_PX,
      top: point.y - b.y <= EDGE_PX,
      bottom: b.y + b.height - 1 - point.y <= EDGE_PX,
    };
    const others = screen.getAllDisplays().filter((other) => other.id !== display.id);
    const shared = (edge) =>
      others.some((other) => {
        const o = other.bounds;
        const vOverlap = o.y < b.y + b.height && b.y < o.y + o.height;
        const hOverlap = o.x < b.x + b.width && b.x < o.x + o.width;
        if (edge === "left") return vOverlap && o.x + o.width === b.x;
        if (edge === "right") return vOverlap && o.x === b.x + b.width;
        if (edge === "top") return hOverlap && o.y + o.height === b.y;
        return hOverlap && o.y === b.y + b.height;
      });
    return Object.keys(at).filter((edge) => at[edge] && !shared(edge));
  }

  function poll() {
    const settings = getSettings();
    const point = cursor();
    const now = Date.now();

    // Auto-hide.
    for (const entry of entries.values()) {
      if (!isVisible(entry) || entry.gesture) continue;
      const layout = settings.panels[entry.id];
      if (layout.pinned || layout.autoHide.mode === "never") continue;
      if (inside(point, entry.win.getBounds())) {
        entry.visited = true;
        entry.outSince = null;
        continue;
      }
      // Someone typing in the chat panel with the mouse parked elsewhere.
      if (entry.win.isFocused()) {
        entry.outSince = null;
        continue;
      }
      if (entry.outSince === null) entry.outSince = now;
      const away = now - entry.outSince;
      if (layout.autoHide.mode === "leave" && entry.visited && away >= LEAVE_GRACE_MS) hide(entry.id, "cursor left");
      else if (layout.autoHide.mode === "timeout" && away >= layout.autoHide.seconds * 1000) hide(entry.id, "timed out");
    }

    // Hot edge.
    if (!settings.hotEdge.enabled) return;
    const edge = settings.hotEdge.edge;
    const display = screen.getDisplayNearestPoint(point);
    const atEdge = edgeAt(point, display).includes(edge);
    if (!atEdge) {
      hot = { since: null, edge: null, fired: false, checking: false };
      return;
    }
    if (hot.since === null) hot = { since: now, edge, fired: false, checking: false };
    if (hot.fired || hot.checking || now - hot.since < settings.hotEdge.dwellMs) return;
    const panel = settings.hotEdge.panel;
    if (isVisible(entries.get(panel))) {
      hot.fired = true; // already open: nothing to do until the cursor leaves the edge
      return;
    }
    hot.checking = true;
    mouseHeld().then((held) => {
      hot.checking = false;
      if (held) {
        // Dragging something to that edge (a window, a file): not a request.
        // Re-arms only once the cursor has left the edge.
        hot.fired = true;
        log("[panels] hot edge ignored: a mouse button is held");
        return;
      }
      // Still there after the check?
      const again = cursor();
      if (!edgeAt(again, screen.getDisplayNearestPoint(again)).includes(edge)) return;
      hot.fired = true;
      show(panel, { source: "hot-edge", dock: edge });
    });
  }

  // --- startup / test --------------------------------------------------------

  /** Pinned panels come back where they were when the app starts. */
  function restorePinned() {
    const settings = getSettings();
    for (const id of PANEL_IDS) if (settings.panels[id].pinned) show(id, { source: "restore" });
    syncPolling();
  }

  function windows() {
    return [...entries.values()].filter((entry) => alive(entry.win)).map((entry) => entry.win);
  }

  function debugState() {
    return [...entries.values()]
      .filter((entry) => alive(entry.win))
      .map((entry) => ({
        id: entry.id,
        visible: isVisible(entry),
        focused: entry.win.isFocused(),
        bounds: entry.win.getBounds(),
        opacity: entry.win.getOpacity(),
        dock: entry.dockOverride || layoutOf(entry.id).dock,
        orientation: layoutOf(entry.id).orientation,
        polling: Boolean(pollTimer),
      }));
  }

  return {
    show,
    hide,
    hideAll,
    toggle,
    command,
    applySettings,
    restorePinned,
    syncPolling,
    windows,
    isPanel: (webContents) => Boolean(entryFor(webContents)),
    debugState,
  };
}

module.exports = { createPanelManager };
