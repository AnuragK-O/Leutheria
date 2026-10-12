/* The Assistant view: transcript, inline confirmation / skill-proposal cards,
   and the composer (text + push-to-talk). */

(function (LX) {
  const { el, clear, icon, formatArgs, toast } = LX;

  const scrollEl = document.getElementById("chat-scroll");
  const chatEl = document.getElementById("chat");
  const inputEl = document.getElementById("command-input");
  const micButton = document.getElementById("mic-button");

  // Confirmation cards keyed by id, so one answered by voice can be settled
  // from the outside rather than being left with live buttons.
  const confirmCards = new Map();

  function scrollToEnd() {
    scrollEl.scrollTop = scrollEl.scrollHeight;
  }

  function showEmptyState() {
    clear(chatEl).appendChild(
      el(
        "div.chat-empty",
        {},
        // The logo mark as a watermark: a CSS mask, tinted by the theme.
        el("div.empty-mark", { "aria-hidden": "true" }),
        el("div.empty-eyebrow", { text: "No commands this session" }),
        el("div.empty-title", { text: "Standing by" }),
        el("div.empty-sub", {
          text: "Type a command, or say it. Everything Leutheria can do is listed in the Library.",
        }),
        el(
          "div.empty-keys",
          {},
          el("kbd", { text: "Enter" }),
          el("span", { text: "Send what you typed" }),
          el("kbd", { text: "Mic" }),
          el("span", { text: "Record one command, click again to send" }),
          el("kbd", { text: "\u2325 Space" }),
          el("span", { text: "Start or stop a hands-free session" })
        )
      )
    );
  }

  function append(node) {
    if (chatEl.querySelector(".chat-empty")) clear(chatEl);
    chatEl.appendChild(node);
    scrollToEnd();
  }

  // --- messages ----------------------------------------------------------

  // When the line reached this window. The agent doesn't stamp messages, and
  // a session log without times isn't one.
  function clock() {
    return new Date().toLocaleTimeString([], { hour12: false });
  }

  // notice: "success" | "warning" -- an app notice shown in the assistant's
  // voice (a skill was saved, the agent isn't connected), marked with an icon.
  function addMessage({ role, text, trace, voice, error, notice }) {
    const message = el("div.msg", { class: role });
    message.appendChild(
      el(
        "div.msg-gutter",
        {},
        el("span.msg-role", { text: role === "user" ? (voice ? "you / voice" : "you") : "leutheria" }),
        el("span.msg-time", { text: clock() })
      )
    );
    const content = el("div.msg-content");
    message.appendChild(content);

    if (trace && trace.length) {
      const list = el("div.msg-trace");
      for (const step of trace) {
        const failed = step.result && step.result.ok === false;
        list.appendChild(
          el(
            "div.trace-step",
            { class: failed ? "failed" : "" },
            el("span.dot"),
            el("b", { text: step.tool }),
            el("span.trace-args", { text: formatArgs(step.args) }),
            el("span.trace-status", { text: failed ? "FAILED" : "OK" })
          )
        );
      }
      content.appendChild(list);
    }

    if (text || !trace || !trace.length) {
      const mark = error ? "x" : notice === "success" ? "check" : notice === "warning" ? "warning" : null;
      content.appendChild(
        el(
          "div.msg-body",
          { class: error ? "error" : notice ? `notice-${notice}` : "" },
          mark && icon(mark),
          el("span", { text: text || "(no reply)" })
        )
      );
    }

    append(message);
  }

  // --- confirmation card -------------------------------------------------

  function resolvedRow(note, approved) {
    return el(
      "div.prompt-resolved",
      { class: approved ? "yes" : "no" },
      icon(approved ? "check" : "x"),
      el("span", { text: note })
    );
  }

  function addConfirmCard({ id, tool, args }) {
    const card = el("div.prompt-card.confirm");
    // The tool's safety tier, when the catalog has loaded: why it's asking.
    const inventory = LX.state && LX.state.inventory;
    const entry = inventory && [...inventory.tools, ...inventory.skills].find((item) => item.name === tool);
    const tier = entry && (entry.safety === "destructive" || entry.safety === "scoped") ? entry.safety : null;
    card.appendChild(
      el(
        "div.prompt-head",
        {},
        icon("warning"),
        el("span", { text: "Approval required" }),
        tier && el("span.badge.badge-warning", { text: tier })
      )
    );

    const actions = el("div.prompt-actions");
    const body = el(
      "div.prompt-body",
      {},
      el("div.prompt-lead", {}, "Leutheria wants to run ", el("b", { text: tool }), ". This one always asks first."),
      el("div.code-block", { text: `${tool}(${formatArgs(args, 400) || ""})` }),
      actions
    );
    card.appendChild(body);

    const buttons = [
      { label: "Approve", kind: "btn btn-hot", approved: true, remember: null },
      {
        label: "Approve for this session",
        kind: "btn",
        approved: true,
        remember: "session",
        title: "Don't ask again for this exact command until the app restarts",
      },
      {
        label: "Always approve",
        kind: "btn",
        approved: true,
        remember: "always",
        title: "Never ask again for this exact command, even after restarting",
      },
      { label: "Decline", kind: "btn btn-danger", approved: false, remember: null },
    ];

    // Once answered the card goes neutral: amber means "waiting on you".
    const settle = (note, approved) => {
      confirmCards.delete(id);
      actions.remove();
      card.classList.add("resolved");
      card.appendChild(resolvedRow(note, approved));
      scrollToEnd();
    };

    for (const spec of buttons) {
      actions.appendChild(
        el("button", {
          class: spec.kind,
          text: spec.label,
          title: spec.title,
          onclick: () => {
            window.leutheria.sendConfirmResponse(id, spec.approved, spec.remember);
            settle(
              !spec.approved
                ? "Declined."
                : spec.remember === "always"
                  ? "Approved — and remembered for good."
                  : spec.remember === "session"
                    ? "Approved — and remembered for this session."
                    : "Approved.",
              spec.approved
            );
          },
        })
      );
    }

    confirmCards.set(id, settle);
    append(card);
  }

  function resolveConfirmCard({ id, approved, by }) {
    const settle = confirmCards.get(id);
    if (!settle) return;
    // "ui" here means the Electron overlay's card (main.js relays its click);
    // "overlay" is the native Qt overlay, answered over its own connection.
    const how = by === "voice" ? "by voice" : by === "ui" || by === "overlay" ? "from the overlay" : "by you";
    settle(`${approved ? "Approved" : "Declined"} ${how}.`, approved);
  }

  // --- skill proposal card -----------------------------------------------

  function addProposalCard({ id, name, description, steps, uncertain_params }) {
    const card = el("div.prompt-card.proposal");
    card.appendChild(el("div.prompt-head", {}, icon("sparkle"), el("span", { text: "Skill proposal" })));

    const body = el("div.prompt-body");
    body.appendChild(
      el(
        "div",
        {},
        el("div.prompt-title", { text: name }),
        el("div.prompt-lead", { text: description })
      )
    );
    // The steps in the order they run; the numbers are that order.
    body.appendChild(
      el(
        "div.pipeline",
        {},
        steps.map((step, index) =>
          el(
            "span.pipe-step",
            {},
            el("span.pipe-n", { text: String(index + 1).padStart(2, "0") }),
            el("span", { text: step.tool })
          )
        )
      )
    );

    // Default every open question to "it varies" -- keeping a value as a real
    // parameter is the recoverable choice; silently baking in a wrong literal
    // isn't. Matches finalize_definition()'s own default on the agent side.
    const resolutions = {};

    for (const item of uncertain_params || []) {
      resolutions[item.name] = true;

      const varies = el("button.choice.selected", {}, el("span.pip"), "It varies");
      const fixed = el("button.choice", {}, el("span.pip"), `Always "${item.guessed_value}"`);

      varies.addEventListener("click", () => {
        resolutions[item.name] = true;
        varies.classList.add("selected");
        fixed.classList.remove("selected");
      });
      fixed.addEventListener("click", () => {
        resolutions[item.name] = false;
        fixed.classList.add("selected");
        varies.classList.remove("selected");
      });

      body.appendChild(
        el(
          "div.prompt-question",
          {},
          el("p", { text: item.question }),
          el("div.choice-row", {}, varies, fixed)
        )
      );
    }

    const actions = el("div.prompt-actions");
    const settle = (note, approved) => {
      actions.remove();
      card.classList.add("resolved");
      card.appendChild(resolvedRow(note, approved));
      scrollToEnd();
    };

    actions.appendChild(
      el("button.btn.btn-primary", {
        text: "Save skill",
        onclick: () => {
          window.leutheria.sendSkillResponse(id, true, resolutions);
          settle(`Saved as "${name}" — you can manage it in the Library.`, true);
        },
      })
    );
    actions.appendChild(
      el("button.btn", {
        text: "No thanks",
        onclick: () => {
          window.leutheria.sendSkillResponse(id, false, resolutions);
          settle("Declined — this sequence won't be suggested again.", false);
        },
      })
    );

    body.appendChild(actions);
    card.appendChild(body);
    append(card);
  }

  // --- push to talk ------------------------------------------------------

  let mediaRecorder = null;
  let audioChunks = [];
  let isRecording = false;

  function setMicIcon(recording) {
    clear(micButton).appendChild(icon(recording ? "stop" : "mic"));
    micButton.classList.toggle("recording", recording);
  }

  async function startRecording() {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (event) => audioChunks.push(event.data);
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((track) => track.stop());
      const reader = new FileReader();
      reader.onloadend = () => window.leutheria.sendAudio(reader.result.split(",")[1], "webm");
      reader.readAsDataURL(new Blob(audioChunks, { type: "audio/webm" }));
    };
    mediaRecorder.start();
    isRecording = true;
    setMicIcon(true);
  }

  function stopRecording() {
    if (mediaRecorder && isRecording) mediaRecorder.stop();
    isRecording = false;
    setMicIcon(false);
  }

  micButton.addEventListener("click", () => {
    if (isRecording) stopRecording();
    else startRecording().catch((err) => toast(`Mic error: ${err.message}`, "error"));
  });

  inputEl.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !inputEl.value.trim()) return;
    window.leutheria.sendCommand(inputEl.value.trim());
    inputEl.value = "";
  });

  // --- wiring ------------------------------------------------------------

  window.leutheria.onChat(addMessage);
  window.leutheria.onConfirmRequest(addConfirmCard);
  window.leutheria.onConfirmResolved(resolveConfirmCard);
  window.leutheria.onSkillProposed(addProposalCard);

  setMicIcon(false);
  showEmptyState();

  LX.chat = { focus: () => inputEl.focus() };
})(window.LX);
