/* The Logs view: the raw bridge/agent stream, kept out of the transcript so
   the conversation stays readable. */

(function (LX) {
  const { el, icon, toast } = LX;

  const logsEl = document.getElementById("logs");
  const MAX_LINES = 2000; // long sessions shouldn't grow this node unbounded

  let lines = [];

  function paint() {
    // Only autoscroll if the user is already at the bottom -- otherwise
    // scrolling back to read something gets yanked away on the next line.
    const atBottom = logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight < 40;
    logsEl.textContent = lines.join("\n");
    if (atBottom) logsEl.scrollTop = logsEl.scrollHeight;
  }

  window.leutheria.onLog((line) => {
    lines.push(line);
    if (lines.length > MAX_LINES) lines = lines.slice(-MAX_LINES);
    if (LX.app.currentView() === "logs") paint();
  });

  function render() {
    paint();
  }

  function actions() {
    return [
      el(
        "button.btn.btn-sm",
        {
          onclick: () => {
            navigator.clipboard.writeText(lines.join("\n"));
            toast("Log copied to clipboard");
          },
        },
        el("span", { text: "Copy" })
      ),
      el(
        "button.btn.btn-sm",
        {
          onclick: () => {
            lines = [];
            paint();
          },
        },
        icon("trash"),
        el("span", { text: "Clear" })
      ),
    ];
  }

  LX.logs = { render, actions, subtitle: () => "Raw messages between the app and the agent" };
})(window.LX);
