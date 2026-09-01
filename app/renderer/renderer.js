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

window.leutheria.onChat(({ role, text }) => addBubble(role, text));

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
