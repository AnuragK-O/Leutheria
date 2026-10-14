/* Everything the user can change about the floating parts of the interface:
   the panels' layout, the hot edge, where alerts appear, and appearance.
   Persisted as one JSON file in Electron's userData (overlay-layout.json).

   Plain Node, no Electron: main.js and panels.js use it, and so does
   scripts/panels-check.js, which runs it without a window.

   The rule for the file is the same as the agent's settings files: it is
   validated field by field on every load, and anything wrong is replaced by
   its default and reported -- a hand-edited typo or a half-written file
   resets that field (or, if the JSON won't parse, the whole file), never
   crashes the app or leaves a panel off-screen. */

const fs = require("fs");

const PANEL_IDS = ["chat", "skills", "activity", "permissions", "timers", "voice"];
const DOCKS = ["left", "right", "top", "bottom", "free"];
const EDGES = ["left", "right", "top", "bottom"];
const ORIENTATIONS = ["vertical", "horizontal"];
const DISPLAYS = ["cursor", "primary"];
const AUTO_HIDE = ["leave", "timeout", "never"];
const ANCHORS = ["top-left", "top-center", "top-right", "center", "bottom-left", "bottom-center", "bottom-right"];
const THEMES = ["dark", "light", "system"];
const DENSITIES = ["comfortable", "compact"];
const LEVELS = ["floating", "normal"];

const OPACITY_MIN = 0.6;
const DWELL_MIN = 150;
const DWELL_MAX = 2000;
const AUTO_HIDE_SECONDS_MAX = 600;
// Window sizes (the window includes the panel's shadow margin). Below these
// the panels stop being usable; above, they're bigger than any display.
const MIN_SIZE = { vertical: { w: 300, h: 260 }, horizontal: { w: 520, h: 200 } };
const MAX_SIDE = 4000;

// Where each panel opens the first time. Different edges on purpose, so two
// asked for in a row don't sit exactly on top of each other.
const PANEL_DEFAULTS = {
  chat: { dock: "right", orientation: "vertical" },
  skills: { dock: "left", orientation: "vertical" },
  activity: { dock: "left", orientation: "vertical" },
  permissions: { dock: "left", orientation: "vertical" },
  timers: { dock: "top", orientation: "horizontal" },
  voice: { dock: "bottom", orientation: "horizontal" },
};

function defaultPanel(id) {
  const base = PANEL_DEFAULTS[id] || PANEL_DEFAULTS.chat;
  return {
    dock: base.dock,
    orientation: base.orientation,
    // Free-mode position (top-left of the window, screen coordinates); null
    // until the panel is first moved.
    x: null,
    y: null,
    // One size per orientation: flipping a panel and back restores the
    // shape it had, rather than squashing one into the other.
    vertical: { w: 400, h: 620 },
    horizontal: { w: 1080, h: id === "voice" || id === "timers" ? 220 : 300 },
    // null: follow appearance.panelOpacity.
    opacity: null,
    display: "cursor",
    autoHide: { mode: "leave", seconds: 20 },
    pinned: false,
  };
}

function defaults() {
  return {
    version: 1,
    hotEdge: { enabled: true, edge: "right", dwellMs: 350, panel: "chat" },
    alerts: { anchor: "top-center", display: "cursor", toastAnchor: "bottom-left" },
    appearance: {
      // null: the built-in violet, untouched (DESIGN.md's tokens).
      accent: null,
      theme: "dark",
      panelOpacity: 0.97,
      density: "comfortable",
      panelLevel: "floating",
    },
    panels: Object.fromEntries(PANEL_IDS.map((id) => [id, defaultPanel(id)])),
  };
}

// --- validation ---------------------------------------------------------------

const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

/* Each check takes the raw value and returns [value, problem]: the value to
   keep (the default when the raw one is unusable) and a reason, or null. */

function oneOf(raw, allowed, fallback, where) {
  if (raw === undefined) return [fallback, null];
  return allowed.includes(raw) ? [raw, null] : [fallback, `${where}: ${JSON.stringify(raw)} is not one of ${allowed.join(", ")}`];
}

function number(raw, min, max, fallback, where, { integer = false, nullable = false } = {}) {
  if (raw === undefined) return [fallback, null];
  if (raw === null && nullable) return [null, null];
  if (!isNumber(raw) || raw < min || raw > max) {
    return [fallback, `${where}: ${JSON.stringify(raw)} is not a number from ${min} to ${max}`];
  }
  return [integer ? Math.round(raw) : raw, null];
}

function bool(raw, fallback, where) {
  if (raw === undefined) return [fallback, null];
  return typeof raw === "boolean" ? [raw, null] : [fallback, `${where}: ${JSON.stringify(raw)} is not true or false`];
}

const HEX = /^#[0-9a-fA-F]{6}$/;

function accent(raw, where) {
  if (raw === undefined || raw === null) return [null, null];
  if (typeof raw !== "string" || !HEX.test(raw)) return [null, `${where}: ${JSON.stringify(raw)} is not a #RRGGBB colour`];
  const verdict = deriveAccent(raw);
  return verdict.ok ? [raw.toLowerCase(), null] : [null, `${where}: ${verdict.error}`];
}

function size(raw, orientation, fallback, where, problems) {
  if (raw === undefined) return { ...fallback };
  if (!isObject(raw)) {
    problems.push(`${where}: not an object`);
    return { ...fallback };
  }
  const min = MIN_SIZE[orientation];
  const out = {};
  for (const key of ["w", "h"]) {
    const [value, problem] = number(raw[key], min[key], MAX_SIDE, fallback[key], `${where}.${key}`, { integer: true });
    if (problem) problems.push(problem);
    out[key] = value;
  }
  return out;
}

function validatePanel(id, raw, problems) {
  const base = defaultPanel(id);
  if (raw === undefined) return base;
  const where = `panels.${id}`;
  if (!isObject(raw)) {
    problems.push(`${where}: not an object`);
    return base;
  }
  const take = ([value, problem]) => {
    if (problem) problems.push(problem);
    return value;
  };
  const panel = {
    dock: take(oneOf(raw.dock, DOCKS, base.dock, `${where}.dock`)),
    orientation: take(oneOf(raw.orientation, ORIENTATIONS, base.orientation, `${where}.orientation`)),
    x: take(number(raw.x, -100000, 100000, null, `${where}.x`, { integer: true, nullable: true })),
    y: take(number(raw.y, -100000, 100000, null, `${where}.y`, { integer: true, nullable: true })),
    vertical: size(raw.vertical, "vertical", base.vertical, `${where}.vertical`, problems),
    horizontal: size(raw.horizontal, "horizontal", base.horizontal, `${where}.horizontal`, problems),
    opacity: take(number(raw.opacity, OPACITY_MIN, 1, null, `${where}.opacity`, { nullable: true })),
    display: take(oneOf(raw.display, DISPLAYS, base.display, `${where}.display`)),
    autoHide: { ...base.autoHide },
    pinned: take(bool(raw.pinned, base.pinned, `${where}.pinned`)),
  };
  if (raw.autoHide !== undefined) {
    if (!isObject(raw.autoHide)) problems.push(`${where}.autoHide: not an object`);
    else {
      panel.autoHide.mode = take(oneOf(raw.autoHide.mode, AUTO_HIDE, base.autoHide.mode, `${where}.autoHide.mode`));
      panel.autoHide.seconds = take(
        number(raw.autoHide.seconds, 2, AUTO_HIDE_SECONDS_MAX, base.autoHide.seconds, `${where}.autoHide.seconds`, { integer: true })
      );
    }
  }
  // A free panel with no position yet has nowhere to be: start it docked.
  if (panel.dock === "free" && (panel.x === null || panel.y === null)) panel.dock = base.dock;
  return panel;
}

/** raw (anything) -> { settings, problems }. Never throws. */
function validate(raw) {
  const problems = [];
  const out = defaults();
  if (!isObject(raw)) {
    if (raw !== undefined) problems.push("the file is not a JSON object");
    return { settings: out, problems };
  }
  const take = ([value, problem]) => {
    if (problem) problems.push(problem);
    return value;
  };
  const section = (name) => {
    if (raw[name] === undefined) return {};
    if (isObject(raw[name])) return raw[name];
    problems.push(`${name}: not an object`);
    return {};
  };

  const hot = section("hotEdge");
  out.hotEdge = {
    enabled: take(bool(hot.enabled, out.hotEdge.enabled, "hotEdge.enabled")),
    edge: take(oneOf(hot.edge, EDGES, out.hotEdge.edge, "hotEdge.edge")),
    dwellMs: take(number(hot.dwellMs, DWELL_MIN, DWELL_MAX, out.hotEdge.dwellMs, "hotEdge.dwellMs", { integer: true })),
    panel: take(oneOf(hot.panel, PANEL_IDS, out.hotEdge.panel, "hotEdge.panel")),
  };

  const alerts = section("alerts");
  out.alerts = {
    anchor: take(oneOf(alerts.anchor, ANCHORS, out.alerts.anchor, "alerts.anchor")),
    display: take(oneOf(alerts.display, DISPLAYS, out.alerts.display, "alerts.display")),
    toastAnchor: take(oneOf(alerts.toastAnchor, ANCHORS, out.alerts.toastAnchor, "alerts.toastAnchor")),
  };

  const look = section("appearance");
  out.appearance = {
    accent: take(accent(look.accent, "appearance.accent")),
    theme: take(oneOf(look.theme, THEMES, out.appearance.theme, "appearance.theme")),
    panelOpacity: take(number(look.panelOpacity, OPACITY_MIN, 1, out.appearance.panelOpacity, "appearance.panelOpacity")),
    density: take(oneOf(look.density, DENSITIES, out.appearance.density, "appearance.density")),
    panelLevel: take(oneOf(look.panelLevel, LEVELS, out.appearance.panelLevel, "appearance.panelLevel")),
  };
  // Remembered so the main window can carry over a theme chosen before this
  // file existed (it used to live in the window's localStorage).
  out.appearance.themeChosen = look.theme !== undefined && THEMES.includes(look.theme);

  const panels = section("panels");
  for (const id of PANEL_IDS) out.panels[id] = validatePanel(id, panels[id], problems);
  for (const id of Object.keys(panels)) if (!PANEL_IDS.includes(id)) problems.push(`panels.${id}: unknown panel, ignored`);

  return { settings: out, problems };
}

/** Deep-merge `patch` into `settings`, then validate the result. A patch
    that would make anything invalid is refused whole, with the reason, so
    the settings view can show it rather than silently snapping back. */
function applyPatch(settings, patch) {
  const merged = merge(JSON.parse(JSON.stringify(settings)), patch);
  const { settings: next, problems } = validate(merged);
  if (problems.length) return { ok: false, error: problems[0], settings };
  next.appearance.themeChosen = settings.appearance.themeChosen || Boolean(patch.appearance && patch.appearance.theme);
  return { ok: true, settings: next };
}

function merge(target, patch) {
  if (!isObject(patch)) return target;
  for (const [key, value] of Object.entries(patch)) {
    if (isObject(value) && isObject(target[key])) merge(target[key], value);
    else target[key] = value;
  }
  return target;
}

// --- file -----------------------------------------------------------------------

/** -> { settings, problems }. A missing file is not a problem. */
function load(file) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch (err) {
    if (err.code === "ENOENT") return { settings: defaults(), problems: [] };
    return { settings: defaults(), problems: [`couldn't read ${file}: ${err.message}`] };
  }
  let raw;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    return { settings: defaults(), problems: [`not valid JSON (${err.message}); using defaults`], corrupt: true };
  }
  return validate(raw);
}

/** Written to a temporary file and renamed, so a crash mid-write leaves the
    old file rather than half a new one. */
function save(file, settings) {
  const { themeChosen: _derived, ...appearance } = settings.appearance;
  const data = { ...settings, appearance: { ...appearance } };
  const temp = `${file}.tmp`;
  fs.writeFileSync(temp, `${JSON.stringify(data, null, 2)}\n`);
  fs.renameSync(temp, file);
}

// --- colour --------------------------------------------------------------------
// The accent replaces the brand violet (selection, the primary button, the
// mark, meters). It is checked against the same contrast bar as DESIGN.md's
// own tokens, in both themes, and adjusted where it falls short. The state
// colours are not user-editable at all: colour means state, so an accent that
// would read as one of them is refused.

// DESIGN.md's grounds, per theme.
const GROUNDS = {
  dark: ["#0a0810", "#07060b", "#110e19", "#191524", "#231d31"],
  light: ["#efedf4", "#e5e2ed", "#f8f7fb", "#ffffff", "#e9e4f4"],
};

// Hues the state colours own (degrees). An accent within STATE_HUE_WINDOW
// of one, saturated enough to read as colour, is refused.
// The amber window is wider: a yellow accent would still read as caution.
const STATE_HUES = { cyan: 184, green: 148, amber: 39, red: 349 };
const STATE_HUE_WINDOW = { cyan: 16, green: 16, amber: 24, red: 16 };

const PRESETS = [
  // null: DESIGN.md's own hand-tuned violet tokens, not a derived set.
  { name: "Violet", hex: null },
  { name: "Indigo", hex: "#6366f1" },
  { name: "Blue", hex: "#3b82f6" },
  { name: "Magenta", hex: "#c04fc4" },
  { name: "Rose", hex: "#e0569b" },
  { name: "Slate", hex: "#8792a8" },
];

function parseHex(hex) {
  const value = parseInt(hex.slice(1), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function toHex(rgb) {
  return `#${rgb.map((c) => Math.round(Math.min(255, Math.max(0, c))).toString(16).padStart(2, "0")).join("")}`;
}

function luminance(hex) {
  const [r, g, b] = parseHex(hex).map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function mix(hex, toward, amount) {
  const a = parseHex(hex);
  const b = parseHex(toward);
  return toHex(a.map((c, i) => c + (b[i] - c) * amount));
}

function hueSat(hex) {
  const [r, g, b] = parseHex(hex).map((c) => c / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  const light = (max + min) / 2;
  const sat = delta === 0 ? 0 : delta / (1 - Math.abs(2 * light - 1));
  let hue = 0;
  if (delta) {
    if (max === r) hue = 60 * (((g - b) / delta) % 6);
    else if (max === g) hue = 60 * ((b - r) / delta + 2);
    else hue = 60 * ((r - g) / delta + 4);
  }
  return { hue: (hue + 360) % 360, sat, light };
}

/** Step `hex` toward `toward` until `ok(candidate)`; null if it never is. */
function adjust(hex, toward, ok) {
  for (let step = 0; step <= 50; step += 1) {
    const candidate = mix(hex, toward, step / 50);
    if (ok(candidate)) return { hex: candidate, moved: step > 0 };
  }
  return null;
}

const minContrast = (hex, grounds) => Math.min(...grounds.map((ground) => contrast(hex, ground)));

/** hex -> { ok, error } or { ok, vars: {dark, light}, notes: [] }. */
function deriveAccent(hex) {
  if (typeof hex !== "string" || !HEX.test(hex)) return { ok: false, error: "Enter a colour as #RRGGBB." };
  hex = hex.toLowerCase();
  const { hue, sat, light } = hueSat(hex);
  if (sat >= 0.35 && light > 0.12 && light < 0.92) {
    for (const [state, stateHue] of Object.entries(STATE_HUES)) {
      const distance = Math.min(Math.abs(hue - stateHue), 360 - Math.abs(hue - stateHue));
      if (distance <= STATE_HUE_WINDOW[state]) {
        return {
          ok: false,
          error: `That colour reads as ${state}, which already means ${STATE_MEANING[state]}. Pick a hue further from it.`,
        };
      }
    }
  }

  const notes = [];
  const vars = {};
  for (const theme of ["dark", "light"]) {
    const grounds = GROUNDS[theme];
    const lift = theme === "dark" ? "#ffffff" : "#000000";
    // As text or an icon: 4.5:1 on every ground, like every other text colour.
    const hi = adjust(hex, lift, (c) => minContrast(c, grounds) >= 4.5);
    // Marks, rules, meters: 3:1 (non-text contrast).
    const mark = adjust(hex, lift, (c) => minContrast(c, grounds) >= 3);
    // The primary button's fill, under white text: 4.5:1 against white.
    const fill = adjust(hex, "#000000", (c) => contrast(c, "#ffffff") >= 4.5);
    if (!hi || !mark || !fill) return { ok: false, error: "That colour can't be made readable in both themes. Try a mid-tone." };
    if (fill.moved) notes.push(`${theme}: buttons use ${fill.hex} so white text on them reads at 4.5:1`);
    if (hi.moved) notes.push(`${theme}: text in the accent uses ${hi.hex} to stay readable on the background`);
    const [r, g, b] = parseHex(mark.hex);
    vars[theme] = {
      "--violet": mark.hex,
      "--violet-hi": hi.hex,
      "--violet-lo": fill.hex,
      "--on-violet": "#ffffff",
      "--grid": `rgba(${r}, ${g}, ${b}, ${theme === "dark" ? 0.045 : 0.06})`,
      "--mark-empty": `rgba(${r}, ${g}, ${b}, ${theme === "dark" ? 0.62 : 0.7})`,
    };
  }
  return { ok: true, vars, notes };
}

const STATE_MEANING = {
  cyan: "something is live",
  green: "something succeeded",
  amber: "something is waiting on you",
  red: "something failed",
};

module.exports = {
  PANEL_IDS,
  DOCKS,
  EDGES,
  ORIENTATIONS,
  DISPLAYS,
  AUTO_HIDE,
  ANCHORS,
  THEMES,
  DENSITIES,
  LEVELS,
  MIN_SIZE,
  OPACITY_MIN,
  DWELL_MIN,
  DWELL_MAX,
  PRESETS,
  defaults,
  defaultPanel,
  validate,
  applyPatch,
  load,
  save,
  deriveAccent,
  contrast,
};
