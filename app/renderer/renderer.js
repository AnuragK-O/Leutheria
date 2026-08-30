const logEl = document.getElementById("log");
const inputEl = document.getElementById("command-input");

window.leutheria.onLog((line) => {
  logEl.textContent += line + "\n";
  logEl.scrollTop = logEl.scrollHeight;
});

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && inputEl.value.trim()) {
    window.leutheria.sendCommand(inputEl.value.trim());
    inputEl.value = "";
  }
});
