/* The timers panel: what's counting down, read with the agent's list_timers
   control message (the same shape as the list_timers tool's result).

   Cancelling goes the way it always has: as a command ("Cancel timer 3"),
   through the model to the cancel_timer tool, so it shows in the transcript
   like anything else Leutheria was asked to do. There is no control message
   that cancels a timer; see NOTES.md. */

(function (LX) {
  const { el, clear, toast } = LX;
  const bodyEl = document.getElementById("timers-body");

  // How often the list is re-read from the agent. Between reads the
  // remaining time counts down here, from when it was read.
  const REFRESH_MS = 5000;

  let timers = null; // null until the first read
  let readAt = 0;
  let error = null;
  const cancelling = new Set();

  async function load() {
    const result = await window.leutheria.listTimers();
    if (result && result.ok) {
      timers = result.timers || [];
      error = null;
      readAt = Date.now();
      for (const id of [...cancelling]) if (!timers.some((timer) => timer.timer_id === id)) cancelling.delete(id);
    } else {
      error = (result && result.error) || "Couldn't read the timers";
      if (/unknown|unsupported|not a control/i.test(error)) error = "This agent doesn't report timers yet. Update it to see them here.";
    }
    render();
  }

  function clock(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const two = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
  }

  function remaining(timer) {
    return timer.remaining_seconds - (Date.now() - readAt) / 1000;
  }

  function cancel(timer) {
    cancelling.add(timer.timer_id);
    window.leutheria.sendCommand(`Cancel timer ${timer.timer_id}${timer.label ? ` (${timer.label})` : ""}`);
    toast(`Asked Leutheria to cancel ${timer.label || `timer ${timer.timer_id}`}`);
    render();
    setTimeout(load, 1500);
  }

  function render() {
    if (LX.app) LX.app.renderSubtitle();
    clear(bodyEl);
    if (error) {
      bodyEl.appendChild(el("div.empty", { text: error }));
      return;
    }
    if (timers === null) {
      bodyEl.appendChild(el("div.empty", { text: "Reading the timers…" }));
      return;
    }
    if (!timers.length) {
      bodyEl.appendChild(
        el("div.timers-none", {}, el("div.empty", { text: "No timers running." }), el("p.section-note", { text: "Say “set a timer for ten minutes” to start one." }))
      );
      return;
    }
    for (const timer of timers) {
      const left = remaining(timer);
      const busy = cancelling.has(timer.timer_id);
      bodyEl.appendChild(
        el(
          "div.timer",
          { "data-timer": timer.timer_id, class: left <= 0 ? "done" : "" },
          el(
            "div.timer-head",
            {},
            el("span.timer-label", { text: timer.label || `Timer ${timer.timer_id}` }),
            el("span.timer-id", { text: `#${timer.timer_id}` })
          ),
          el("div.timer-left", { text: clock(left), "aria-label": `${timer.remaining || clock(left)} left` }),
          el(
            "div.timer-meta",
            {},
            el("span", {}, el("span.micro-label", { text: "Ends" }), el("span", { text: timer.ends_at || "--" })),
            el("span", {}, el("span.micro-label", { text: "Length" }), el("span", { text: timer.duration || "--" }))
          ),
          el("button.btn.btn-sm.btn-danger", {
            text: busy ? "Cancelling…" : "Cancel",
            disabled: busy,
            onclick: () => cancel(timer),
          })
        )
      );
    }
  }

  // The countdown, between reads. Only the numbers change.
  setInterval(() => {
    if (!timers || error) return;
    for (const timer of timers) {
      const node = bodyEl.querySelector(`[data-timer="${CSS.escape(String(timer.timer_id))}"] .timer-left`);
      if (node) node.textContent = clock(remaining(timer));
    }
  }, 1000);
  setInterval(load, REFRESH_MS);

  LX.timers = {
    render: () => (timers === null && !error ? load() : render()),
    subtitle: () => (timers && timers.length ? LX.plural(timers.length, "timer") + " running" : "Nothing counting down"),
    actions: () => [],
    load,
  };
  load();
})(window.LX);
