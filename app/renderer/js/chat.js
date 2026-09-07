/* The Assistant view: transcript, inline confirmation with human corrections,
   reusable skill-proposal cards, live task execution timeline, and composer. */

(function (LX) {
  const { el, clear, icon, formatArgs, toast } = LX;

  const scrollEl = document.getElementById("chat-scroll");
  const chatEl = document.getElementById("chat");
  const inputEl = document.getElementById("command-input");
  const micButton = document.getElementById("mic-button");

  const confirmCards = new Map();
  let currentTimelineEl = null;

  function scrollToEnd() {
    scrollEl.scrollTop = scrollEl.scrollHeight;
  }

  function showEmptyState() {
    clear(chatEl).appendChild(
      el(
        "div.chat-empty",
        {},
        el("img", { src: "../../assets/1.png", alt: "" }),
        el("strong", { text: "Self-Improving Desktop Agent" }),
        el("span", {
          text: "Instruct Leutheria to perform a workflow. Correct it when needed — successful workflows turn into reusable skills that earn greater autonomy over time.",
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

  function addMessage({ role, text, trace, voice, error, skill_used, mode }) {
    const message = el("div.msg", { class: role });
    const header = el("div.msg-role");

    let roleText = role === "user" ? (voice ? "you · voice" : "you") : "leutheria";
    header.appendChild(el("span", { text: roleText }));

    if (mode) {
      header.appendChild(el("span.badge", { class: `badge-${mode}`, text: mode.toUpperCase() }));
    }
    if (skill_used) {
      header.appendChild(el("span.badge.badge-accent", { text: `skill: ${skill_used}` }));
    }
    message.appendChild(header);

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

    currentTimelineEl = null; // reset active timeline on new message
    append(message);
  }

  // --- live task timeline ------------------------------------------------

  function handleTimelineEvent(data) {
    if (!currentTimelineEl) {
      currentTimelineEl = el("div.task-timeline");
      append(currentTimelineEl);
    }

    let iconText = "✓";
    let cssClass = "success";
    let desc = "";

    if (data.event === "step_start") {
      iconText = "▶";
      cssClass = "info";
      desc = `Executing: ${data.tool}`;
    } else if (data.event === "step_complete") {
      iconText = data.ok ? "✓" : "⚠";
      cssClass = data.ok ? "success" : "warning";
      desc = `${data.tool} ${data.ok ? "completed" : "failed"}`;
    } else if (data.event === "skill_matched") {
      iconText = "⚡";
      cssClass = "success";
      desc = data.text;
    } else {
      desc = data.text || JSON.stringify(data);
    }

    currentTimelineEl.appendChild(
      el(
        "div.timeline-event-row",
        { class: cssClass },
        el("span.timeline-dot", { text: iconText }),
        el("span.timeline-text", { text: desc })
      )
    );
    scrollToEnd();
  }

  // --- confirmation card with human correction ----------------------------

  function addConfirmCard(req) {
    const { id, tool, args, risk = "medium", description } = req;
    const card = el("div.prompt-card.confirm");

    const riskBadge = el("span.badge", {
      class: `badge-${risk}`,
      text: `${risk.toUpperCase()} RISK`,
    });

    card.appendChild(
      el("div.prompt-head", {}, icon("warning"), el("span", { text: "Action Approval" }), riskBadge)
    );

    const actions = el("div.prompt-actions");
    const correctionContainer = el("div.prompt-correction", { style: "display: none;" });

    const descText = description || `Leutheria wants to run ${tool}.`;
    const body = el(
      "div.prompt-body",
      {},
      el("div", { text: descText }),
      el("div.code-block", { text: `${tool}(${formatArgs(args, 400) || ""})` }),
      actions,
      correctionContainer
    );
    card.appendChild(body);

    const settle = (note) => {
      confirmCards.delete(id);
      actions.remove();
      correctionContainer.remove();
      card.appendChild(el("div.prompt-resolved", { text: note }));
      scrollToEnd();
    };

    // Correction form elements
    const corrInput = el("input.correction-input", {
      type: "text",
      placeholder: "Specify correction or alternate parameters (e.g. new path or arguments)...",
    });
    const submitCorrBtn = el("button.btn.btn-primary.btn-sm", {
      text: "Submit Correction & Run",
      onclick: () => {
        const val = corrInput.value.trim();
        if (!val) {
          toast("Please enter a correction or cancel", "warning");
          return;
        }
        let correctionData = val;
        // Try parsing JSON if user supplied JSON parameters
        if (val.startsWith("{") && val.endsWith("}")) {
          try {
            correctionData = JSON.parse(val);
          } catch (_e) {}
        } else if (tool === "run_command") {
          correctionData = { cmd: val };
        } else if (tool === "create_folder" || tool === "list_files" || tool === "read_file") {
          correctionData = { path: val };
        }

        window.leutheria.sendConfirmResponse(id, true, null, correctionData);
        settle(`Approved with user correction: "${val}"`);
      },
    });

    const cancelCorrBtn = el("button.btn.btn-sm", {
      text: "Cancel",
      onclick: () => {
        correctionContainer.style.display = "none";
      },
    });

    correctionContainer.appendChild(
      el("div", { style: "font-size: 11px; color: var(--text-muted);", text: "Human Correction:" })
    );
    correctionContainer.appendChild(corrInput);
    correctionContainer.appendChild(el("div.choice-row", {}, submitCorrBtn, cancelCorrBtn));

    // Action buttons
    actions.appendChild(
      el("button.btn.btn-primary", {
        text: "Approve",
        onclick: () => {
          window.leutheria.sendConfirmResponse(id, true, null, null);
          settle("Approved.");
        },
      })
    );

    actions.appendChild(
      el("button.btn", {
        text: "✏️ Correct",
        title: "Intervene and modify parameters or action",
        onclick: () => {
          correctionContainer.style.display =
            correctionContainer.style.display === "none" ? "flex" : "none";
          corrInput.focus();
        },
      })
    );

    actions.appendChild(
      el("button.btn", {
        text: "Session",
        title: "Allow this exact command for this session",
        onclick: () => {
          window.leutheria.sendConfirmResponse(id, true, "session", null);
          settle("Approved for session.");
        },
      })
    );

    actions.appendChild(
      el("button.btn.btn-danger", {
        text: "Decline",
        onclick: () => {
          window.leutheria.sendConfirmResponse(id, false, null, null);
          settle("Declined.");
        },
      })
    );

    confirmCards.set(id, settle);
    append(card);
  }

  function resolveConfirmCard({ id, approved, by }) {
    const settle = confirmCards.get(id);
    if (!settle) return;
    settle(`${approved ? "Approved" : "Declined"} by ${by === "voice" ? "voice" : "you"}.`);
  }

  // --- skill proposal card -----------------------------------------------

  function addProposalCard(proposal) {
    const { id, name, description, parameters, permissions = [], risk_level = "medium", steps = [], uncertain_params } = proposal;
    const card = el("div.prompt-card.proposal");

    card.appendChild(
      el(
        "div.prompt-head",
        {},
        icon("sparkle"),
        el("span", { text: "Reusable Skill Learned" }),
        el("span.badge.badge-copilot", { text: "NEW SKILL" })
      )
    );

    const body = el("div.prompt-body");
    body.appendChild(
      el(
        "div",
        {},
        el("div.step-tool", { text: name }),
        el("div.detail-desc", { text: description })
      )
    );

    // Detected parameters & permissions
    const paramNames = Object.keys(parameters || {});
    if (paramNames.length > 0) {
      body.appendChild(
        el(
          "div",
          { style: "font-size: 11.5px; margin: 4px 0; color: var(--text-muted);" },
          el("strong", { text: "Detected Parameters: " }),
          el("span", { text: paramNames.join(", ") })
        )
      );
    }

    if (permissions.length > 0) {
      body.appendChild(
        el(
          "div",
          { style: "font-size: 11.5px; margin: 4px 0; color: var(--text-muted);" },
          el("strong", { text: "Required Permissions: " }),
          el("span", { text: permissions.join(", ") })
        )
      );
    }

    body.appendChild(
      el("div.code-block", {
        text: steps.map((s) => s.tool || s.action).join("  →  "),
      })
    );

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
        text: "Save Reusable Skill",
        onclick: () => {
          window.leutheria.sendSkillResponse(id, true, resolutions);
          settle(`Saved "${name}" as a learned skill.`);
        },
      })
    );

    actions.appendChild(
      el("button.btn.btn-danger", {
        text: "Decline",
        onclick: () => {
          window.leutheria.sendSkillResponse(id, false, {});
          settle("Declined.");
        },
      })
    );

    card.appendChild(body);
    card.appendChild(actions);
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
      reader.onloadend = () =>
        window.leutheria.sendAudio(
          reader.result.split(",")[1],
          "webm",
          LX.state.executionMode || "copilot"
        );
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
    const mode = LX.state.executionMode || "copilot";
    window.leutheria.sendCommand(inputEl.value.trim(), mode);
    inputEl.value = "";
  });

  // --- wiring ------------------------------------------------------------

  window.leutheria.onChat(addMessage);
  window.leutheria.onTimelineEvent(handleTimelineEvent);
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
