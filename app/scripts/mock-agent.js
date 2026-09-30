#!/usr/bin/env node
/* A scripted stand-in for the Python agent, for exercising the Electron side
   of the voice session (overlay, header indicator, chat plumbing) -- and the
   native Qt overlay (overlay-qt/) -- without a mic, Whisper, Piper or an API
   key. Speaks the week-4 wire protocol, including client roles.

     node scripts/mock-agent.js            # port 8799
     MOCK_PORT=8800 node scripts/mock-agent.js

   Then run the app against it:
     env -u ELECTRON_RUN_AS_NODE LEUTHERIA_AGENT_URL=ws://127.0.0.1:8799 LEUTHERIA_NO_SPAWN=1 npx electron .
   or the Qt overlay on its own:
     overlay-qt/build/LeutheriaOverlay.app/Contents/MacOS/LeutheriaOverlay --url ws://127.0.0.1:8799

   Behaviour:
   - One voice session shared by every connection, like the real agent.
     Voice status (voice_state, audio_level, session_started/ended) goes to
     every client; conversation (transcript, confirmation_required/resolved,
     speaking, response) goes to the primary -- the newest client that did
     not say {"type":"hello","role":"overlay"} -- and is mirrored to overlays
     the way server.py does (transcript/response only for the voice session).
   - On connect: the current voice_state, then (unless MOCK_AUTOSTART=0) a
     full scripted session after 2 s if none is running.
   - control voice_session start/end: starts the script / ends it (manual).
   - A confirm from an overlay answers the session's confirmation; every
     client is sent confirmation_resolved {"by":"overlay"}. A confirm from a
     UI answers it too, and overlays are sent {"by":"ui"}.
   - Typed command containing "confirm": a push-to-talk-style confirmation
     round trip; answering it with any mic clip plays the voice-confirm ack
     path (transcript + confirmation_resolved + empty response), then the
     real reply. Any other command gets an echo reply. Overlays can't send
     commands (an error, as from the real agent).
   - MOCK_CONFIRM_TIMEOUT_MS (default 20000): how long the scripted session
     waits for a click before "answering by voice".
   - MOCK_LOOP=1: run the scripted session again after each one ends.

   More permissive than the real agent in one way: with only an overlay
   connected (no UI), the scripted confirmation is still shown to it, so the
   overlay's card can be developed without Electron. The real agent fails a
   confirmation closed when no UI is connected.

   Never binds 8765 -- that's the real agent's port. */

const WebSocket = require("ws");

const PORT = Number(process.env.MOCK_PORT || 8799);
if (PORT === 8765) {
  console.error("mock-agent: refusing to use 8765, the real agent's port");
  process.exit(1);
}
const AUTOSTART = process.env.MOCK_AUTOSTART !== "0";
const LOOP = process.env.MOCK_LOOP === "1";
const CONFIRM_TIMEOUT_MS = Number(process.env.MOCK_CONFIRM_TIMEOUT_MS || 20000);

let settings = {
  enabled: true,
  wake_models: ["hey_jarvis"],
  wake_threshold: 0.5,
  silence_timeout_s: 180,
  endpoint_silence_ms: 800,
  dismiss_phrases: ["thanks", "thank you", "that's all", "that's it", "we're done", "goodbye"],
  input_device: null,
};

const wss = new WebSocket.Server({ host: "127.0.0.1", port: PORT });
console.log(`mock-agent listening on ws://127.0.0.1:${PORT}`);
console.log("AGENT_READY");

let nextId = 1;
const uid = () => `mock-${nextId++}`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --- clients and routing (mirrors server.py) --------------------------------

const clients = []; // oldest first: { socket, role, name }
const MIRRORED = ["confirmation_required", "confirmation_resolved", "transcript", "response", "speaking"];
const SESSION_ONLY = ["transcript", "response"];

const isOverlay = (client) => client.role === "overlay";
const overlays = () => clients.filter(isOverlay);
const primary = () => [...clients].reverse().find((client) => !isOverlay(client)) || null;

function send(client, payload) {
  if (!client || client.socket.readyState !== WebSocket.OPEN) return;
  if (payload.type !== "audio_level") console.log(`[mock] -> ${client.name} ${JSON.stringify(payload)}`);
  client.socket.send(JSON.stringify(payload));
}

function broadcast(payload) {
  for (const client of clients) send(client, payload);
}

function mirror(payload) {
  if (!MIRRORED.includes(payload.type)) return;
  if (SESSION_ONLY.includes(payload.type) && payload.origin !== "voice_session") return;
  for (const client of overlays()) send(client, payload);
}

// Conversation to one UI client; mirrored to overlays when it's the primary.
function converse(client, payload) {
  send(client, payload);
  if (client === primary()) mirror(payload);
}

// Conversation belonging to the voice session: the primary, or (mock only,
// see header) straight to overlays when no UI is connected.
function toSession(payload) {
  const target = primary();
  if (target) converse(target, payload);
  else mirror(payload);
}

// --- the shared voice session ------------------------------------------------

let session = null; // { aborted } while the scripted session runs
let voice = { state: "idle", session: false };
const pending = new Map(); // confirmation id -> resolve(approved)
let pttConfirm = null; // { id, client }: a typed command's open confirmation

function setState(state, inSession) {
  voice = { state, session: inSession };
  broadcast({ type: "voice_state", ...voice });
}

// Emits audio_level at ~15 Hz for `ms`, shaped like speech when `talking`.
async function levels(ms, talking) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (!session || session.aborted) return;
    const t = (Date.now() - start) / 1000;
    const base = talking ? 0.45 + 0.35 * Math.abs(Math.sin(t * 5.3)) * Math.abs(Math.sin(t * 1.7)) : 0.04;
    broadcast({ type: "audio_level", level: Math.min(1, base + Math.random() * 0.06) });
    await sleep(66);
  }
}

function waitForConfirm(id) {
  return new Promise((resolve) => {
    pending.set(id, resolve);
    setTimeout(() => {
      if (pending.delete(id)) resolve(null); // null: nobody clicked
    }, CONFIRM_TIMEOUT_MS);
  });
}

async function runSession(trigger) {
  if (session) return;
  const current = (session = { aborted: false });
  const alive = () => !current.aborted && clients.length > 0;
  const step = async (state, ms, talking) => {
    if (!alive()) return false;
    setState(state, true);
    if (["listening", "hearing", "awaiting_confirmation"].includes(state)) await levels(ms, talking);
    else await sleep(ms);
    return alive();
  };

  broadcast({
    type: "session_started",
    trigger,
    wake_word: trigger === "wake_word" ? "hey_jarvis" : null,
    score: trigger === "wake_word" ? 0.83 : null,
  });
  if (!(await step("listening", 2500, false))) return;
  if (!(await step("hearing", 2000, true))) return;
  if (!(await step("transcribing", 1200))) return;
  const command = "clear out the build folder in my demo project";
  toSession({ type: "transcript", text: command, origin: "voice_session" });
  if (!(await step("thinking", 1500))) return;

  const id = uid();
  const args = { cmd: "rm -rf ~/Projects/demo/build" };
  // Registered before the card is sent, as dispatcher._confirm() does: a
  // click during the spoken prompt below is a real answer, not a no-op.
  let answered = null;
  waitForConfirm(id).then((value) => (answered = { value }));
  toSession({ type: "confirmation_required", id, tool: "run_command", args });
  const prompt = "I want to delete the build folder in your demo project. Should I go ahead?";
  toSession({ type: "speaking", state: "started", text: prompt, duration_ms: 2600 });
  if (!(await step("speaking", 2600))) return;
  toSession({ type: "speaking", state: "ended" });
  // Like the real session: awaiting_confirmation only if it's still open.
  if (!answered) setState("awaiting_confirmation", true);
  // Level ticks while we wait (the mic is live for a spoken yes/no).
  while (!answered && alive()) await levels(200, false);
  if (!alive()) return;

  let approved = answered.value;
  if (approved === null) {
    // Nobody clicked: pretend the user said "yeah, go ahead".
    approved = true;
    toSession({ type: "confirmation_resolved", id, approved, by: "voice" });
    toSession({ type: "response", text: "", origin: "voice_session" }); // the voice-confirm ack
  }

  if (!(await step("thinking", 1200))) return;
  const reply = approved
    ? "Done. The build folder is gone, and nothing else in the demo project was touched. Anything else?"
    : "Okay, I left the build folder alone.";
  toSession({
    type: "response",
    text: reply,
    origin: "voice_session",
    trace: [{ tool: "run_command", args, result: approved ? { ok: true } : { ok: false, error: "declined" } }],
  });
  toSession({ type: "speaking", state: "started", text: reply, duration_ms: 3500 });
  if (!(await step("speaking", 3500))) return;
  toSession({ type: "speaking", state: "ended" });
  if (!(await step("listening", 2000, false))) return;
  if (!(await step("hearing", 900, true))) return;
  if (!(await step("transcribing", 700))) return;
  // "thanks" -> dismissed; no transcript/reply for a dismissal.
  endSession("dismissed");
  if (LOOP) setTimeout(() => runSession("wake_word"), 4000);
}

function endSession(reason) {
  if (!session) return;
  session.aborted = true;
  session = null;
  for (const resolve of pending.values()) resolve(false);
  pending.clear();
  broadcast({ type: "session_ended", reason });
  setState("idle", false);
}

// --- messages ------------------------------------------------------------------

function controlResult(client, message, extra) {
  send(client, { type: "control_result", request_id: message.request_id, ok: true, ...extra });
}

function answerConfirm(client, message) {
  const approved = Boolean(message.approved);
  const resolve = pending.get(message.id);
  const isPtt = pttConfirm && pttConfirm.id === message.id;
  if (!resolve && !isPtt) return false;
  if (resolve) {
    pending.delete(message.id);
    resolve(approved);
  }
  const resolved = { type: "confirmation_resolved", id: message.id, approved };
  if (isOverlay(client)) {
    for (const other of clients) send(other, { ...resolved, by: "overlay" });
  } else {
    for (const other of overlays()) send(other, { ...resolved, by: "ui" });
  }
  return true;
}

async function handleMessage(client, message) {
  switch (message.type) {
    case "hello":
      client.role = message.role === "overlay" ? "overlay" : "ui";
      client.name = `${client.role}#${client.id}`;
      return;
    case "inventory":
      return controlResult(client, message, { tools: [], skills: [], history: [], trusted: [], grants: [] });
    case "voice_session":
      if (message.action === "start") {
        controlResult(client, message, {});
        runSession("manual");
      } else if (message.action === "end") {
        controlResult(client, message, {});
        endSession("manual");
      } else {
        send(client, { type: "control_result", request_id: message.request_id, ok: false, error: "bad action" });
      }
      return;
    case "get_voice_settings":
      return controlResult(client, message, { settings });
    case "set_voice_settings":
      settings = { ...settings, ...(message.settings || {}) };
      return controlResult(client, message, { settings });
    case "set_preference":
    case "delete_skill":
    case "revoke_trust":
    case "revoke_grant":
      return controlResult(client, message, {});
    case "confirm": {
      const ptt = pttConfirm;
      if (!answerConfirm(client, message)) return;
      if (ptt && ptt.id === message.id) {
        pttConfirm = null;
        await sleep(800);
        converse(ptt.client, { type: "response", text: message.approved ? "Ran it." : "Okay, skipped.", trace: [] });
      }
      return;
    }
  }

  if (isOverlay(client)) {
    send(client, { type: "error", text: "overlay connections can only send hello, confirm and control messages" });
    return;
  }

  switch (message.type) {
    case "command": {
      const text = message.text || "";
      if (/confirm/i.test(text)) {
        pttConfirm = { id: uid(), client };
        converse(client, { type: "confirmation_required", id: pttConfirm.id, tool: "run_command", args: { cmd: "echo hi" } });
        return;
      }
      await sleep(400);
      converse(client, { type: "response", text: `Mock reply to: ${text}`, trace: [] });
      return;
    }
    case "audio": {
      // Push-to-talk. If a typed command's confirmation is open, this clip
      // answers it -- the exact message sequence server.py produces.
      await sleep(300);
      if (pttConfirm) {
        const id = pttConfirm.id;
        pttConfirm = null;
        converse(client, { type: "transcript", text: "yeah go ahead", origin: "push_to_talk" });
        converse(client, { type: "confirmation_resolved", id, approved: true, by: "voice" });
        converse(client, { type: "response", text: "" });
        await sleep(1200);
        converse(client, {
          type: "response",
          text: "Ran it: echo printed hi.",
          trace: [{ tool: "run_command", args: { cmd: "echo hi" }, result: { ok: true } }],
        });
        return;
      }
      converse(client, { type: "transcript", text: "what time is it", origin: "push_to_talk" });
      await sleep(500);
      converse(client, { type: "response", text: "It's mock o'clock.", trace: [] });
    }
  }
}

let nextClient = 1;

wss.on("connection", (socket) => {
  const id = nextClient++;
  const client = { socket, role: "ui", id, name: `ui#${id}` };
  clients.push(client);
  console.log(`[mock] client ${id} connected`);

  socket.on("message", (raw) => {
    let message;
    try {
      message = JSON.parse(raw.toString());
    } catch (_err) {
      send(client, { type: "error", text: "invalid JSON" });
      return;
    }
    if (!message || typeof message !== "object" || Array.isArray(message)) {
      send(client, { type: "error", text: "expected a JSON object" });
      return;
    }
    const brief = message.type === "audio" ? { type: "audio", format: message.format } : message;
    console.log(`[mock] <- ${client.name} ${JSON.stringify(brief)}`);
    handleMessage(client, message);
  });

  socket.on("close", () => {
    console.log(`[mock] ${client.name} disconnected`);
    clients.splice(clients.indexOf(client), 1);
    if (pttConfirm && pttConfirm.client === client) pttConfirm = null;
    // The session belongs to the agent, not a connection -- it only stops
    // when nobody is left to show it to.
    if (!clients.length && session) {
      session.aborted = true;
      session = null;
      pending.clear();
      voice = { state: "idle", session: false };
    }
  });

  send(client, { type: "voice_state", ...voice });
  if (AUTOSTART && !session) setTimeout(() => runSession("wake_word"), 2000);
});
