/* The header's voice-session indicator: a dot + label mirroring the agent's
   voice_state, and a click target to start or stop a session. The session
   itself (mic, wake word, playback) lives entirely in the Python sidecar;
   this only reflects it. */

(function (LX) {
  const { toast } = LX;

  const button = document.getElementById("voice-indicator");
  const labelEl = button.querySelector(".voice-label");

  // voice_state -> [indicator data-state, label]. Collapsed to five labels on
  // purpose; the overlay shows the fine-grained ones.
  const DISPLAY = {
    off: ["off", "Off"],
    idle: ["idle", "Idle"],
    listening: ["listening", "Listening"],
    hearing: ["listening", "Listening"],
    transcribing: ["thinking", "Thinking"],
    thinking: ["thinking", "Thinking"],
    speaking: ["speaking", "Speaking"],
    awaiting_confirmation: ["confirm", "Needs approval"],
  };

  let voice = { state: "off", session: false, connected: false };

  function render() {
    const [kind, label] = voice.connected ? DISPLAY[voice.state] || DISPLAY.off : DISPLAY.off;
    button.dataset.state = kind;
    labelEl.textContent = label;
    button.disabled = !voice.connected;
    button.title = !voice.connected
      ? "Voice is unavailable: not connected to the agent"
      : voice.session
        ? "Stop listening (⌥Space)"
        : "Start listening (⌥Space)";
  }

  button.addEventListener("click", async () => {
    const result = await window.leutheria.voiceSession(voice.session ? "end" : "start");
    if (!result || !result.ok) toast((result && result.error) || "Couldn't change the voice session", "error");
  });

  window.leutheria.onVoiceEvent((event) => {
    if (event.type !== "voice_state") return;
    voice = { state: event.state, session: Boolean(event.session), connected: event.connected !== false };
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

  render();
})(window.LX);
