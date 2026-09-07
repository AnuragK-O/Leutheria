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
        el("img", { src: "../../assets/1.png", alt: "" }),
        el("strong", { text: "Nothing yet" }),
        el("span", {
          text: "Type a command below, or hold a conversation out loud with the mic. Everything Leutheria can do lives in the Library.",
        })
      )
    );
  }

  function append(node) {
    if (chatEl.querySelector(".chat-empty")) clear(chatEl);
    chatEl.appendChild(node);
    scrollToEnd();
  }

  // --- messages ----------------------------------------------------------

  function addMessage({ role, text, trace, voice, error }) {
    const message = el("div.msg", { class: role });
    message.appendChild(
      el("div.msg-role", { text: role === "user" ? (voice ? "you · voice" : "you") : "leutheria" })
    );

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
            el("span", { text: formatArgs(step.args) })
          )
        );
      }
      message.appendChild(list);
    }

    if (text || !trace || !trace.length) {
      message.appendChild(
        el("div.msg-body", { text: text || "(no reply)", class: error ? "error" : "" })
      );
    }

    append(message);
  }

  // --- confirmation card -------------------------------------------------

  function addConfirmCard({ id, tool, args }) {
    const card = el("div.prompt-card.confirm");
    card.appendChild(el("div.prompt-head", {}, icon("warning"), el("span", { text: "Approval needed" })));

    const actions = el("div.prompt-actions");
    const body = el(
      "div.prompt-body",
      {},
      el("div", {
        text: `Leutheria wants to run ${tool}. This one always asks first.`,
      }),
      el("div.code-block", { text: `${tool}(${formatArgs(args, 400) || ""})` }),
      actions
    );
    card.appendChild(body);

    const buttons = [
      { label: "Approve", kind: "btn btn-primary", approved: true, remember: null },
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

    const settle = (note) => {
      confirmCards.delete(id);
      actions.remove();
      card.appendChild(el("div.prompt-resolved", { text: note }));
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
                    : "Approved."
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
    settle(`${approved ? "Approved" : "Declined"} by ${by === "voice" ? "voice" : "you"}.`);
  }

  // --- skill proposal card -----------------------------------------------

  function addProposalCard({ id, name, description, steps, uncertain_params }) {
    const card = el("div.prompt-card.proposal");
    card.appendChild(el("div.prompt-head", {}, icon("sparkle"), el("span", { text: "New skill suggested" })));

    const body = el("div.prompt-body");
    body.appendChild(
      el(
        "div",
        {},
        el("div", { class: "step-tool", text: name }),
        el("div", { class: "detail-desc", text: description })
      )
    );
    body.appendChild(el("div.code-block", { text: steps.map((step) => step.tool).join("  →  ") }));

    // Default every open question to "it varies" -- keeping a value as a real
    // parameter is the recoverable choice; silently baking in a wrong literal
    // isn't. Matches finalize_definition()'s own default on the agent side.
    const resolutions = {};

    for (const item of uncertain_params || []) {
      resolutions[item.name] = true;

      const varies = el("button.choice.selected", { text: "It varies" });
      const fixed = el("button.choice", { text: `Always "${item.guessed_value}"` });

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
    const settle = (note) => {
      actions.remove();
      card.appendChild(el("div.prompt-resolved", { text: note }));
      scrollToEnd();
    };

    actions.appendChild(
      el("button.btn.btn-primary", {
        text: "Save skill",
        onclick: () => {
          window.leutheria.sendSkillResponse(id, true, resolutions);
          settle(`Saved as "${name}" — you can manage it in the Library.`);
        },
      })
    );
    actions.appendChild(
      el("button.btn", {
        text: "No thanks",
        onclick: () => {
          window.leutheria.sendSkillResponse(id, false, resolutions);
          settle("Declined — this sequence won't be suggested again.");
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
  window.leutheria.onSpeech((base64Wav) => {
    new Audio(`data:audio/wav;base64,${base64Wav}`)
      .play()
      .catch((err) => toast(`Couldn't play speech: ${err.message}`, "error"));
  });

  setMicIcon(false);
  showEmptyState();

  LX.chat = { focus: () => inputEl.focus() };
})(window.LX);
