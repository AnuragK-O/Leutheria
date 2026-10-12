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

   It starts its own mock agent (port 8797 by default, never 8765) and its own
   Electron with an isolated userData directory, and stops exactly those two
   processes when it finishes. The app's windows do appear on screen while it
   runs. Nothing in the app's normal path knows about this script: it talks to
   the renderer over Chromium's remote-debugging port, which is only open
   because this script passed the flag.

   One line per check; exits non-zero if any failed. */

const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");
const WebSocket = require("ws");

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

  if (!REAL_URL) {
    const mock = spawn(process.execPath, [path.join(__dirname, "mock-agent.js")], {
      env: { ...process.env, MOCK_PORT: String(MOCK_PORT), MOCK_AUTOSTART: "0", MOCK_CONFIRM_TIMEOUT_MS: "15000" },
      stdio: "ignore",
    });
    children.push(mock);
  }

  const env = {
    ...process.env,
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
      await main.eval(`[...document.querySelectorAll("#library-list .list-group-label")].every((n) => !/^Tools/i.test(n.textContent.trim()))`)
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
    await main.shot(file("logs"));

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
        if (cardUp && !answered) {
          answered = true;
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
          await overlay.shot(file("overlay-confirm-settled"));
        }
        await sleep(150);
      }
      const expected = ["listening", "hearing", "transcribing", "thinking", "speaking"];
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
  await main.eval(`document.querySelector("#library-list .list-row").click()`);
  await main.shot(file("real-library-detail-first"));
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
