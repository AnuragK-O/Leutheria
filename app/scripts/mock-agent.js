#!/usr/bin/env node
/* A scripted stand-in for the Python agent, for exercising the Electron side
   of the voice session (overlay, header indicator, chat plumbing) without a
   mic, Whisper, Piper or an API key. Speaks the week-4 wire protocol.

     node scripts/mock-agent.js            # port 8799
     MOCK_PORT=8800 node scripts/mock-agent.js

   Then run the app against it:
     env -u ELECTRON_RUN_AS_NODE LEUTHERIA_AGENT_URL=ws://127.0.0.1:8799 LEUTHERIA_NO_SPAWN=1 npx electron .

   Behaviour:
   - On connect: voice_state idle, then (unless MOCK_AUTOSTART=0) a full
     scripted session after 2 s.
   - control voice_session start/end: starts the script / ends it (manual).
   - Typed command containing "confirm": a push-to-talk-style confirmation
     round trip; answering it with any mic clip plays the voice-confirm ack
     path (transcript + confirmation_resolved + empty response), then the
     real reply. Any other command gets an echo reply.
   - MOCK_CONFIRM_TIMEOUT_MS (default 20000): how long the scripted session
     waits for a click before "answering by voice".
   - MOCK_LOOP=1: run the scripted session again after each one ends.

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

wss.on("connection", (socket) => {
  console.log("[mock] client connected");
  const send = (payload) => {
    if (socket.readyState !== WebSocket.OPEN) return;
    if (payload.type !== "audio_level") console.log(`[mock] -> ${JSON.stringify(payload)}`);
    socket.send(JSON.stringify(payload));
  };

  let session = null; // { aborted } while the scripted session runs
  const pending = new Map(); // confirmation id -> resolve(approved)
  let pttConfirm = null; // push-to-talk scenario's open confirmation id

  function setState(state, inSession) {
    send({ type: "voice_state", state, session: inSession });
  }

  // Emits audio_level at ~15 Hz for `ms`, shaped like speech when `talking`.
  async function levels(ms, talking) {
    const start = Date.now();
    while (Date.now() - start < ms) {
      if (!session || session.aborted) return;
      const t = (Date.now() - start) / 1000;
      const base = talking ? 0.45 + 0.35 * Math.abs(Math.sin(t * 5.3)) * Math.abs(Math.sin(t * 1.7)) : 0.04;
      send({ type: "audio_level", level: Math.min(1, base + Math.random() * 0.06) });
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
    const alive = () => !current.aborted && socket.readyState === WebSocket.OPEN;
    const step = async (state, ms, talking) => {
      if (!alive()) return false;
      setState(state, true);
      if (["listening", "hearing", "awaiting_confirmation"].includes(state)) await levels(ms, talking);
      else await sleep(ms);
      return alive();
    };

    send({
      type: "session_started",
      trigger,
      wake_word: trigger === "wake_word" ? "hey_jarvis" : null,
      score: trigger === "wake_word" ? 0.83 : null,
    });
    if (!(await step("listening", 2500, false))) return;
    if (!(await step("hearing", 2000, true))) return;
    if (!(await step("transcribing", 1200))) return;
    const command = "clear out the build folder in my demo project";
    send({ type: "transcript", text: command, origin: "voice_session" });
    if (!(await step("thinking", 1500))) return;

    const id = uid();
    const args = { cmd: "rm -rf ~/Projects/demo/build" };
    send({ type: "confirmation_required", id, tool: "run_command", args });
    const prompt = "I want to delete the build folder in your demo project. Should I go ahead?";
    send({ type: "speaking", state: "started", text: prompt, duration_ms: 2600 });
    if (!(await step("speaking", 2600))) return;
    send({ type: "speaking", state: "ended" });
    setState("awaiting_confirmation", true);
    const answer = waitForConfirm(id);
    // Level ticks while we wait (the mic is live for a spoken yes/no).
    let answered = null;
    answer.then((value) => (answered = { value }));
    while (!answered && alive()) await levels(200, false);
    if (!alive()) return;

    let approved = answered.value;
    if (approved === null) {
      // Nobody clicked: pretend the user said "yeah, go ahead".
      approved = true;
      send({ type: "confirmation_resolved", id, approved, by: "voice" });
      send({ type: "response", text: "", origin: "voice_session" }); // the voice-confirm ack
    }

    if (!(await step("thinking", 1200))) return;
    const reply = approved
      ? "Done. The build folder is gone, and nothing else in the demo project was touched. Anything else?"
      : "Okay, I left the build folder alone.";
    send({ type: "speaking", state: "started", text: reply, duration_ms: 3500 });
    if (!(await step("speaking", 3500))) return;
    send({ type: "speaking", state: "ended" });
    send({
      type: "response",
      text: reply,
      origin: "voice_session",
      trace: [{ tool: "run_command", args, result: approved ? { ok: true } : { ok: false, error: "declined" } }],
    });
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
    send({ type: "session_ended", reason });
    setState("idle", false);
  }

  function controlResult(message, extra) {
    send({ type: "control_result", request_id: message.request_id, ok: true, ...extra });
  }

  socket.on("message", async (raw) => {
    let message;
    try {
      message = JSON.parse(raw.toString());
    } catch (_err) {
      return;
    }
    const brief = message.type === "audio" ? { type: "audio", format: message.format } : message;
    console.log(`[mock] <- ${JSON.stringify(brief)}`);

    switch (message.type) {
      case "inventory":
        return controlResult(message, { tools: [], skills: [], history: [], trusted: [], grants: [] });
      case "voice_session":
        if (message.action === "start") {
          controlResult(message, {});
          runSession("manual");
        } else if (message.action === "end") {
          controlResult(message, {});
          endSession("manual");
        } else {
          send({ type: "control_result", request_id: message.request_id, ok: false, error: "bad action" });
        }
        return;
      case "get_voice_settings":
        return controlResult(message, { settings });
      case "set_voice_settings":
        settings = { ...settings, ...(message.settings || {}) };
        return controlResult(message, { settings });
      case "set_preference":
      case "delete_skill":
      case "revoke_trust":
      case "revoke_grant":
        return controlResult(message, {});
      case "confirm": {
        const resolve = pending.get(message.id);
        if (resolve) {
          pending.delete(message.id);
          resolve(Boolean(message.approved));
        }
        if (message.id === pttConfirm) {
          pttConfirm = null;
          await sleep(800);
          send({ type: "response", text: message.approved ? "Ran it." : "Okay, skipped.", trace: [] });
        }
        return;
      }
      case "command": {
        const text = message.text || "";
        if (/confirm/i.test(text)) {
          pttConfirm = uid();
          send({ type: "confirmation_required", id: pttConfirm, tool: "run_command", args: { cmd: "echo hi" } });
          return;
        }
        await sleep(400);
        send({ type: "response", text: `Mock reply to: ${text}`, trace: [] });
        return;
      }
      case "audio": {
        // Push-to-talk. If a typed command's confirmation is open, this clip
        // answers it -- the exact message sequence server.py produces.
        await sleep(300);
        if (pttConfirm) {
          const id = pttConfirm;
          pttConfirm = null;
          send({ type: "transcript", text: "yeah go ahead", origin: "push_to_talk" });
          send({ type: "confirmation_resolved", id, approved: true, by: "voice" });
          send({ type: "response", text: "" });
          await sleep(1200);
          send({ type: "response", text: "Ran it: echo printed hi.", trace: [{ tool: "run_command", args: { cmd: "echo hi" }, result: { ok: true } }] });
          return;
        }
        send({ type: "transcript", text: "what time is it", origin: "push_to_talk" });
        await sleep(500);
        send({ type: "response", text: "It's mock o'clock.", trace: [] });
        return;
      }
    }
  });

  socket.on("close", () => {
    console.log("[mock] client disconnected");
    if (session) session.aborted = true;
    session = null;
  });

  setState("idle", false);
  if (AUTOSTART) setTimeout(() => runSession("wake_word"), 2000);
});
