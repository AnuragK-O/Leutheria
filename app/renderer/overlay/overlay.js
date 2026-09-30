/* The activation overlay: mirrors the voice session's state, mounts the
   manifest's asset for it, and shows a compact confirmation card when the
   agent is waiting on an approve/decline. main.js owns the window itself
   (show/hide, position, click-through); this page only tells it when it has
   something clickable on screen. */

(function (LX) {
  const { el, clear, icon, formatArgs } = LX;
  const api = window.leutheria;

  const islandEl = document.getElementById("island");
  const slotEl = document.getElementById("slot");
  const labelEl = document.getElementById("label");
  const lineEl = document.getElementById("line");
  const confirmEl = document.getElementById("confirm");
  const confirmHeadEl = document.getElementById("confirm-head");
  const confirmCmdEl = document.getElementById("confirm-cmd");
  const confirmHintEl = document.getElementById("confirm-hint");
  const confirmActionsEl = document.getElementById("confirm-actions");

  const VISUAL_STATES = ["listening", "hearing", "transcribing", "thinking", "speaking", "awaiting_confirmation"];

  const LABELS = {
    listening: "Listening",
    hearing: "Hearing you",
    transcribing: "Transcribing",
    thinking: "Thinking",
    speaking: "Speaking",
    awaiting_confirmation: "Needs your OK",
  };

  const ENDED = {
    dismissed: "Talk soon",
    timeout: "Stopped listening",
    manual: "Stopped listening",
    error: "Something went wrong",
  };

  // Longest reply shown in the island; the full text is in the main window.
  const REPLY_CHARS = 150;

  let manifest = { states: {} };
  let inSession = false;
  let state = "listening";
  let transcript = "";
  let reply = "";
  let confirmation = null; // { id, tool, args } while the card is up
  let confirmHideTimer = null;

  // --- theme -------------------------------------------------------------
  // Follows the main window's choice (same file:// origin, so the same
  // localStorage), live via the storage event; the OS appearance otherwise.
  // ?theme= wins, for the debug capture path only.

  const forcedTheme = new URLSearchParams(location.search).get("theme");
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");

  function applyTheme() {
    let theme = forcedTheme;
    if (!theme) {
      try {
        theme = localStorage.getItem("leutheria-theme");
      } catch (_err) {
        theme = null;
      }
    }
    document.documentElement.dataset.theme = theme || (systemDark.matches ? "dark" : "light");
  }

  window.addEventListener("storage", (event) => {
    if (event.key === "leutheria-theme") applyTheme();
  });
  systemDark.addEventListener("change", applyTheme);

  // --- asset slot --------------------------------------------------------

  const placeholder = el("div.orb");
  const mounted = new Map(); // state -> element, so a video isn't reloaded on every visit

  function assetFor(name) {
    return manifest.states[name] || { type: "css" };
  }

  function elementFor(name) {
    const entry = assetFor(name);
    if (entry.type === "css") return placeholder;
    if (mounted.has(name)) return mounted.get(name);

    const src = `assets/${entry.src}`;
    const node =
      entry.type === "video"
        ? el("video", { src, muted: true, playsinline: true, loop: entry.loop !== false, autoplay: true })
        : el("img", { src, alt: "" });
    if (entry.type === "video") node.muted = true; // the attribute alone doesn't unmute-proof autoplay
    node.dataset.react = entry.react || "none";
    // A broken asset shouldn't leave a hole: fall back to the placeholder.
    node.addEventListener("error", () => {
      console.warn(`[overlay] asset for ${name} failed to load: ${src}`);
      manifest.states[name] = { type: "css" };
      mounted.delete(name);
      if (state === name) mountAsset(name);
    });
    mounted.set(name, node);
    return node;
  }

  function mountAsset(name) {
    const node = elementFor(name);
    if (slotEl.firstChild !== node) {
      clear(slotEl).appendChild(node);
      if (node.tagName === "VIDEO") {
        node.currentTime = 0;
        node.play().catch(() => {});
      }
    }
  }

  // --- rendering ---------------------------------------------------------

  function shortReply(text) {
    const clean = String(text || "").replace(/\s+/g, " ").trim();
    if (clean.length <= REPLY_CHARS) return clean;
    // Prefer ending on a sentence boundary if there's one reasonably late.
    const cut = clean.slice(0, REPLY_CHARS);
    const stop = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("? "), cut.lastIndexOf("! "));
    return stop > REPLY_CHARS * 0.5 ? cut.slice(0, stop + 1) : `${cut.replace(/\s+\S*$/, "")}…`;
  }

  function setLine(text, kind) {
    lineEl.textContent = text;
    lineEl.className = `island-line${kind ? ` ${kind}` : ""}`;
  }

  function render() {
    islandEl.dataset.state = state;
    labelEl.textContent = LABELS[state] || "Listening";
    mountAsset(state);

    switch (state) {
      case "listening":
        setLine(reply ? shortReply(reply) : "Go ahead. Say “thanks” when you're done.", "hint");
        break;
      case "hearing":
        setLine("…", "hint");
        break;
      case "transcribing":
        setLine("Catching that…", "hint");
        break;
      case "thinking":
        if (transcript) setLine(transcript, "quote");
        else setLine("Working on it…", "hint");
        break;
      case "speaking":
        setLine(reply ? shortReply(reply) : "…", reply ? "" : "hint");
        break;
      case "awaiting_confirmation":
        setLine(confirmation ? `Okay to run ${confirmation.tool}?` : "Waiting for your answer", "");
        break;
    }
  }

  // --- confirmation card -------------------------------------------------

  function setInteractive(on) {
    api.setOverlayInteractive(on);
  }

  function hideConfirm() {
    clearTimeout(confirmHideTimer);
    confirmation = null;
    confirmEl.hidden = true;
    setInteractive(false);
  }

  function showConfirm({ id, tool, args }) {
    clearTimeout(confirmHideTimer);
    confirmation = { id, tool, args };

    clear(confirmHeadEl).append(icon("warning"), el("span", { text: `Leutheria wants to run ${tool}` }));
    confirmCmdEl.textContent = `${tool}(${formatArgs(args, 200) || ""})`;
    confirmHintEl.textContent = "Say yes or no, or choose";
    clear(confirmActionsEl).append(
      el("button.btn.btn-sm", { text: "Decline", onclick: () => answer(false) }),
      el("button.btn.btn-sm.btn-primary", { text: "Approve", onclick: () => answer(true) })
    );

    confirmEl.hidden = false;
    setInteractive(true);
    render();
  }

  // Replace the buttons with what was decided, then get out of the way. The
  // window goes click-through again immediately -- nothing left to click.
  function settleConfirm(approved, how) {
    if (!confirmation) return;
    confirmation = null;
    setInteractive(false);
    clear(confirmActionsEl);
    confirmHintEl.textContent = "";
    confirmActionsEl.appendChild(
      el("div.ov-confirm-resolved", {}, icon(approved ? "check" : "x"), el("span", { text: `${approved ? "Approved" : "Declined"}${how}` }))
    );
    confirmHideTimer = setTimeout(() => (confirmEl.hidden = true), 1100);
    render();
  }

  function answer(approved) {
    if (!confirmation) return;
    // The overlay only offers the plain answer; "remember" choices stay in
    // the main window's full card.
    api.sendConfirmResponse(confirmation.id, approved, null);
    settleConfirm(approved, "");
  }

  // --- events ------------------------------------------------------------

  function startSession() {
    inSession = true;
    transcript = "";
    reply = "";
    state = "listening";
    hideConfirm();
    document.body.classList.remove("leaving");
    render();
  }

  function endSession(reason) {
    inSession = false;
    hideConfirm();
    labelEl.textContent = ENDED[reason] || "Stopped listening";
    document.body.classList.add("leaving");
  }

  api.onVoiceEvent((event) => {
    switch (event.type) {
      case "audio_level": {
        const level = Math.max(0, Math.min(1, Number(event.level) || 0));
        document.documentElement.style.setProperty("--level", level.toFixed(3));
        break;
      }
      case "session_started":
        startSession();
        break;
      case "session_ended":
        endSession(event.reason);
        break;
      case "voice_state":
        if (event.session && !inSession) startSession();
        if (!event.session) {
          if (inSession) endSession(event.connected === false ? "error" : "manual");
          break;
        }
        if (VISUAL_STATES.includes(event.state)) {
          state = event.state;
          // The mic level only means something while the mic is live.
          if (!["listening", "hearing", "awaiting_confirmation"].includes(state)) {
            document.documentElement.style.setProperty("--level", "0");
          }
          if (state === "hearing") reply = "";
          render();
        }
        break;
      case "transcript":
        if (inSession && event.origin === "voice_session") {
          transcript = event.text || "";
          render();
        }
        break;
      case "speaking":
        if (event.state === "started" && event.text) {
          reply = event.text;
          render();
        }
        break;
      case "response":
        // The command this session was running has finished, so whatever it
        // was waiting on is over -- answered, declined, or timed out.
        if (confirmation) hideConfirm();
        break;
    }
  });

  api.onConfirmRequest((request) => {
    if (inSession) showConfirm(request);
  });

  api.onConfirmResolved(({ id, approved, by }) => {
    if (!confirmation || confirmation.id !== id) return;
    // by:"overlay" is the Qt overlay (only both run if it was started by hand).
    const how = by === "voice" ? " by voice" : by === "overlay" ? " on the other overlay" : " in the main window";
    settleConfirm(approved, how);
  });

  // --- boot --------------------------------------------------------------

  applyTheme();
  render();

  api.getOverlayManifest().then((result) => {
    manifest = result || { states: {} };
    if (manifest.size) {
      const px = Math.max(32, Math.min(120, Number(manifest.size) || 56));
      document.documentElement.style.setProperty("--slot-size", `${px}px`);
    }
    render();
  });

  // Catch up if the window loaded mid-session (e.g. after a reload).
  api.getVoiceState().then((voice) => {
    if (voice && voice.session && !inSession) {
      startSession();
      if (VISUAL_STATES.includes(voice.state)) state = voice.state;
      render();
    }
  });
})(window.LX);
