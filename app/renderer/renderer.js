const logEl = document.getElementById("log");

window.leutheria.onLog((line) => {
  logEl.textContent += line + "\n";
  window.scrollTo(0, document.body.scrollHeight);
});
