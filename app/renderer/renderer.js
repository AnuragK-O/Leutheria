const chatEl = document.getElementById("chat");
const logEl = document.getElementById("log");
const logToggleEl = document.getElementById("log-toggle");
const inputEl = document.getElementById("command-input");

function addBubble(role, text) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  chatEl.appendChild(bubble);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function addConfirmBubble({ id, tool, args }) {
  const bubble = document.createElement("div");
  bubble.className = "bubble assistant confirm";

  const label = document.createElement("div");
  label.textContent = `⚠️ wants to run ${tool}(${JSON.stringify(args)}) — approve?`;
  bubble.appendChild(label);

  const buttonRow = document.createElement("div");
  buttonRow.className = "confirm-buttons";

  const approveBtn = document.createElement("button");
  approveBtn.textContent = "Approve";
  approveBtn.className = "approve";

  const declineBtn = document.createElement("button");
  declineBtn.textContent = "Decline";
  declineBtn.className = "decline";

  const respond = (approved) => {
    approveBtn.disabled = true;
    declineBtn.disabled = true;
    label.textContent += approved ? " — approved" : " — declined";
    window.leutheria.sendConfirmResponse(id, approved);
  };

  approveBtn.addEventListener("click", () => respond(true));
  declineBtn.addEventListener("click", () => respond(false));

  buttonRow.appendChild(approveBtn);
  buttonRow.appendChild(declineBtn);
  bubble.appendChild(buttonRow);

  chatEl.appendChild(bubble);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function addSkillProposalBubble({ id, name, description, steps, uncertain_params }) {
  const bubble = document.createElement("div");
  bubble.className = "bubble assistant skill-proposal";

  const stepList = steps.map((s) => s.tool).join(" → ");
  const label = document.createElement("div");
  label.textContent = `💡 New skill idea — "${name}"\n${description}\nSteps: ${stepList}`;
  bubble.appendChild(label);

  // resolutions[param] = true means "it varies, keep as a parameter" (the
  // default if a question is never answered -- fail toward flexibility).
  const resolutions = {};

  (uncertain_params || []).forEach((item) => {
    resolutions[item.name] = true;

    const question = document.createElement("div");
    question.className = "skill-question";
    question.textContent = `❓ ${item.question}`;
    bubble.appendChild(question);

    const row = document.createElement("div");
    row.className = "confirm-buttons";

    const variesBtn = document.createElement("button");
    variesBtn.textContent = "It varies";
    variesBtn.className = "toggle approve selected";

    const fixedBtn = document.createElement("button");
    fixedBtn.textContent = `Always "${item.guessed_value}"`;
    fixedBtn.className = "toggle decline";

    variesBtn.addEventListener("click", () => {
      resolutions[item.name] = true;
      variesBtn.classList.add("selected");
      fixedBtn.classList.remove("selected");
    });
    fixedBtn.addEventListener("click", () => {
      resolutions[item.name] = false;
      fixedBtn.classList.add("selected");
      variesBtn.classList.remove("selected");
    });

    row.appendChild(variesBtn);
    row.appendChild(fixedBtn);
    bubble.appendChild(row);
  });

  const buttonRow = document.createElement("div");
  buttonRow.className = "confirm-buttons";

  const saveBtn = document.createElement("button");
  saveBtn.textContent = "Save skill";
  saveBtn.className = "approve";

  const noBtn = document.createElement("button");
  noBtn.textContent = "No thanks";
  noBtn.className = "decline";

  const respond = (approved) => {
    bubble.querySelectorAll("button").forEach((btn) => (btn.disabled = true));
    label.textContent += approved ? "\n— saved" : "\n— declined";
    window.leutheria.sendSkillResponse(id, approved, resolutions);
  };

  saveBtn.addEventListener("click", () => respond(true));
  noBtn.addEventListener("click", () => respond(false));

  buttonRow.appendChild(saveBtn);
  buttonRow.appendChild(noBtn);
  bubble.appendChild(buttonRow);

  chatEl.appendChild(bubble);
  chatEl.scrollTop = chatEl.scrollHeight;
}

window.leutheria.onChat(({ role, text }) => addBubble(role, text));
window.leutheria.onConfirmRequest((request) => addConfirmBubble(request));
window.leutheria.onSkillProposed((proposal) => addSkillProposalBubble(proposal));
window.leutheria.onSpeech((base64Wav) => {
  new Audio(`data:audio/wav;base64,${base64Wav}`).play().catch((err) => {
    addBubble("assistant", `⚠️ couldn't play speech: ${err.message}`);
  });
});

window.leutheria.onLog((line) => {
  logEl.textContent += line + "\n";
  logEl.scrollTop = logEl.scrollHeight;
});

logToggleEl.addEventListener("click", () => {
  logEl.hidden = !logEl.hidden;
  logToggleEl.textContent = (logEl.hidden ? "▸" : "▾") + " raw debug log";
});

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && inputEl.value.trim()) {
    window.leutheria.sendCommand(inputEl.value.trim());
    inputEl.value = "";
  }
});

// Push-to-talk: click to start recording, click again to stop and send.
const micButton = document.getElementById("mic-button");
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  audioChunks = [];
  mediaRecorder = new MediaRecorder(stream);
  mediaRecorder.ondataavailable = (event) => audioChunks.push(event.data);
  mediaRecorder.onstop = () => {
    stream.getTracks().forEach((track) => track.stop());
    const blob = new Blob(audioChunks, { type: "audio/webm" });
    const reader = new FileReader();
    reader.onloadend = () => {
      const base64Data = reader.result.split(",")[1];
      window.leutheria.sendAudio(base64Data, "webm");
    };
    reader.readAsDataURL(blob);
  };
  mediaRecorder.start();
  isRecording = true;
  micButton.textContent = "⏹";
  micButton.classList.add("recording");
}

function stopRecording() {
  if (mediaRecorder && isRecording) {
    mediaRecorder.stop();
  }
  isRecording = false;
  micButton.textContent = "🎤";
  micButton.classList.remove("recording");
}

micButton.addEventListener("click", () => {
  if (isRecording) {
    stopRecording();
  } else {
    startRecording().catch((err) => addBubble("assistant", `⚠️ mic error: ${err.message}`));
  }
});
