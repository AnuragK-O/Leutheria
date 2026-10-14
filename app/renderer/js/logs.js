/* The Logs view: the raw bridge/agent stream, kept out of the transcript so
   the conversation stays readable. Set as a readout: one row per line, its
   source tag tinted, stderr in red. Still one selectable text block, and
   new lines are appended rather than the whole log being rebuilt, because a
   long session holds thousands of them. */

(function (LX) {
  const { el, icon, toast } = LX;

  const logsEl = document.getElementById("logs");
  const MAX_LINES = 2000; // long sessions shouldn't grow this node unbounded

  let lines = [];
  let painted = false; // whether logsEl currently mirrors `lines`

  // "[bridge] ...", "[agent:err] ...", "[overlay-qt] ...": the tag, its
  // source, and whether it is an error stream.
  const TAG = /^\[([a-z][\w-]*?)(:err)?\]/i;

  function lineNode(line) {
    const node = document.createElement("div");
    node.className = "log-line";
    const match = TAG.exec(line);
    if (match) {
      const source = match[1].toLowerCase();
      const kind = source.startsWith("overlay") ? "overlay" : source === "bridge" || source === "agent" ? source : "other";
      node.classList.add(`log-${kind}`);
      if (match[2]) node.classList.add("log-err");
      const tag = document.createElement("span");
      tag.className = "log-tag";
      tag.textContent = match[0];
      node.append(tag, line.slice(match[0].length));
    } else {
      node.textContent = line;
    }
    return node;
  }

  function atBottom() {
    return logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight < 40;
  }

  function paint() {
    // Only autoscroll if the user is already at the bottom -- otherwise
    // scrolling back to read something gets yanked away on the next line.
    const stick = !painted || atBottom();
    const fragment = document.createDocumentFragment();
    for (const line of lines) fragment.appendChild(lineNode(line));
    logsEl.replaceChildren(fragment);
    painted = true;
    if (stick) logsEl.scrollTop = logsEl.scrollHeight;
  }

  window.leutheria.onLog((line) => {
    lines.push(line);
    if (lines.length > MAX_LINES) lines = lines.slice(-MAX_LINES);
    if (LX.app.currentView() !== "logs") {
      painted = false;
      return;
    }
    if (!painted) return paint();
    const stick = atBottom();
    logsEl.appendChild(lineNode(line));
    while (logsEl.childElementCount > MAX_LINES) logsEl.firstElementChild.remove();
    if (stick) logsEl.scrollTop = logsEl.scrollHeight;
  });

  function render() {
    if (!painted) paint();
  }

  function actions() {
    return [
      el(
        "button.btn.btn-sm",
        {
          onclick: () => {
            navigator.clipboard
              .writeText(lines.join("\n"))
              .then(() => toast(`Copied ${LX.plural(lines.length, "line")}`))
              .catch(() => toast("Couldn't copy the log", "error"));
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

  LX.logs = {
    render,
    actions,
    subtitle: () => "Raw messages between the app and the agent",
    lineCount: () => lines.length,
  };
})(window.LX);
