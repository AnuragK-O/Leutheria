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

  static async attach(match, timeoutMs = 20000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const targets = await getJson(`http://127.0.0.1:${DEBUG_PORT}/json`);
        const target = targets.find((entry) => entry.type === "page" && entry.url.includes(match));
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
const NAV = { assistant: 0, library: 1, activity: 2, permissions: 3, voice: 4, logs: 5 };

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
    check(
      `[${theme}] theme toggle sets and persists the theme`,
      await main.eval(
        `document.documentElement.dataset.theme === "${theme}" && (localStorage.getItem("leutheria-theme") || "dark") === "${theme}"`
      )
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

// The reduced pass for a real agent: real catalog, real control messages,
// nothing that runs a tool.
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
    try {
      await runTheme(theme);
    } catch (err) {
      check(`[${theme}] run completed`, false, err.message);
    }
  }
  console.log(failures ? `${failures} check(s) failed` : `all checks passed; screenshots in ${OUT}`);
  process.exit(failures ? 1 : 0);
})();
