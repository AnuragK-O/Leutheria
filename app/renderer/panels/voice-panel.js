/* The voice panel: the session's state as a readout, the same start/stop
   pill as the main window's header (voice.js drives it), and the last thing
   heard and said in the live session. */

(function (LX) {
  const { el } = LX;
  const bodyEl = document.getElementById("voice-panel-body");
  const pill = document.getElementById("voice-indicator");

  // voice_state -> [tone, word, line]. Tone is the overlay's: cyan while the
  // mic is live, violet while Leutheria works or talks, amber when it waits.
  const STATES = {
    off: ["off", "Off", "Not listening. Start a session to talk hands-free."],
    idle: ["idle", "Idle", "Waiting for the wake word."],
    listening: ["live", "Listening", "Go ahead."],
    hearing: ["live", "Hearing you", "Keep going; it stops when you pause."],
    transcribing: ["work", "Transcribing", "Turning what you said into text."],
    thinking: ["work", "Thinking", "Working on it."],
    speaking: ["work", "Speaking", "Answering out loud."],
    awaiting_confirmation: ["wait", "Needs approval", "Say yes or no, or answer the card."],
  };

  let voice = { state: "off", session: false, connected: false };
  let heard = null;
  let said = null;

  const stateEl = el("div.vp-state", {}, el("span.vp-led"), el("span.vp-word"));
  const lineEl = el("p.vp-line");
  const heardEl = el("p.vp-text");
  const saidEl = el("p.vp-text");

  bodyEl.prepend(
    el("div.vp-readout", {}, el("div.micro-label", { text: "Session" }), stateEl, lineEl),
  );
  bodyEl.append(
    el("div.vp-io", {}, el("div.micro-label", { text: "Last heard" }), heardEl),
    el("div.vp-io", {}, el("div.micro-label", { text: "Last reply" }), saidEl),
    el("p.vp-hint", {}, el("kbd", { text: "⌥ Space" }), el("span", { text: "starts or stops a session from anywhere" }))
  );
  // The pill sits under the readout, as the panel's one control.
  bodyEl.querySelector(".vp-readout").appendChild(pill);

  function render() {
    const [tone, word, line] = voice.connected ? STATES[voice.state] || STATES.off : ["off", "Unavailable", "Not connected to the agent."];
    stateEl.dataset.tone = tone;
    stateEl.querySelector(".vp-word").textContent = word;
    lineEl.textContent = line;
    heardEl.textContent = heard || "--";
    heardEl.classList.toggle("none", !heard);
    saidEl.textContent = said || "--";
    saidEl.classList.toggle("none", !said);
  }

  window.leutheria.onVoiceEvent((event) => {
    if (event.type === "voice_state") {
      voice = { state: event.state, session: Boolean(event.session), connected: event.connected !== false };
    } else if (event.type === "transcript") {
      heard = event.text;
    } else if (event.type === "response" && event.origin === "voice_session" && event.text) {
      said = event.text;
    } else {
      return;
    }
    render();
  });
  window.leutheria.onStatus((status) => {
    voice.connected = status === "connected";
    render();
  });
  window.leutheria.getVoiceState().then((state) => {
    if (state) voice = state;
    render();
  });

  LX.voicePanel = {
    render,
    actions: () => [],
    subtitle: () => (voice.session ? "Session live" : "Hands-free sessions"),
  };
  render();
})(window.LX);
