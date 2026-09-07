/* The Activity view: how the skill set has grown over time, and what
   Leutheria actually reaches for. */

(function (LX) {
  const { el, clear, icon, relativeTime } = LX;

  const bodyEl = document.getElementById("activity-body");

  const EVENT_COPY = {
    proposed: "was suggested",
    saved: "was saved",
    declined: "was turned down",
  };

  function statCard(value, label) {
    return el("div.stat", {}, el("div.stat-value", { text: String(value) }), el("div.stat-label", { text: label }));
  }

  function renderSummary(inventory) {
    const learned = inventory.skills.filter((skill) => skill.origin === "generated");
    const runs = [...inventory.tools, ...inventory.skills].reduce((total, entry) => total + entry.runs, 0);
    const disabled = [...inventory.tools, ...inventory.skills].filter((entry) => !entry.enabled).length;

    return el(
      "div.stat-grid",
      {},
      statCard(inventory.skills.length, "skills"),
      statCard(learned.length, "learned on its own"),
      statCard(inventory.tools.length, "tools"),
      statCard(runs, "actions run"),
      statCard(disabled, "turned off")
    );
  }

  function renderHistory(history) {
    const card = el("div.card");
    card.appendChild(
      el(
        "div.card-header",
        {},
        el("h2", { text: "Skill history" }),
        el("p", { text: "Every skill Leutheria has proposed, saved, or had turned down." })
      )
    );

    if (!history.length) {
      card.appendChild(
        el("div.empty", {
          text: "No skills proposed yet. Leutheria suggests one after it completes a multi-step task.",
        })
      );
      return card;
    }

    const timeline = el("div.timeline");
    for (const event of history) {
      const detail =
        event.event === "proposed"
          ? event.confident
            ? "recognised a repeat — proposed with confidence"
            : `first time seeing this — ${event.uncertain_count || 0} open question(s)`
          : EVENT_COPY[event.event];

      timeline.appendChild(
        el(
          "div.event",
          {},
          el(
            "div.event-icon",
            { class: event.event },
            icon(event.event === "saved" ? "check" : event.event === "declined" ? "x" : "sparkle")
          ),
          el(
            "div.event-text",
            {},
            el("strong", { text: event.name || "unnamed skill" }),
            el("span", { text: ` ${detail}` })
          ),
          el("div.event-time", { text: relativeTime(event.ts) })
        )
      );
    }
    card.appendChild(timeline);
    return card;
  }

  function renderUsage(inventory) {
    const used = [...inventory.tools, ...inventory.skills]
      .filter((entry) => entry.runs > 0)
      .sort((a, b) => b.runs - a.runs)
      .slice(0, 10);

    const card = el("div.card");
    card.appendChild(
      el("div.card-header", {}, el("h2", { text: "Most used" }), el("p", { text: "Across every session." }))
    );

    if (!used.length) {
      card.appendChild(el("div.empty", { text: "Nothing has been run yet." }));
      return card;
    }

    const max = used[0].runs;
    const list = el("div");
    for (const entry of used) {
      list.appendChild(
        el(
          "div.usage-row",
          {},
          el("div.usage-name", { text: entry.name, title: entry.name }),
          el("div.usage-bar", {}, el("div.usage-fill", { style: `width:${(entry.runs / max) * 100}%` })),
          el("div.usage-count", { text: `${entry.runs}×` })
        )
      );
    }
    card.appendChild(list);
    return card;
  }

  async function renderProductMetrics() {
    try {
      const res = await window.leutheria.getMetrics();
      if (!res || !res.ok || !res.metrics) return null;
      const m = res.metrics;
      const card = el("div.card");
      card.appendChild(
        el(
          "div.card-header",
          {},
          el("h2", { text: "Autonomy & Learning Telemetry" }),
          el("p", { text: "Measuring the flywheel: tasks completed, human corrections, and autonomous execution." })
        )
      );
      card.appendChild(
        el(
          "div.stat-grid",
          {},
          statCard(`${m.completion_rate_pct}%`, "task completion rate"),
          statCard(m.total_tasks, "total tasks"),
          statCard(m.total_interventions, "human interventions"),
          statCard(m.total_corrections, "human corrections"),
          statCard(`${m.skill_reuse_rate_pct}%`, "skill reuse rate"),
          statCard(`${m.autonomous_completion_rate_pct}%`, "autonomous completions")
        )
      );
      return card;
    } catch (_err) {
      return null;
    }
  }

  async function render() {
    const inventory = LX.state.inventory;
    clear(bodyEl);
    if (!inventory) {
      bodyEl.appendChild(el("div.empty", { text: "Waiting for the agent…" }));
      return;
    }
    const metricsCard = await renderProductMetrics();
    if (metricsCard) bodyEl.appendChild(metricsCard);
    bodyEl.append(renderSummary(inventory), renderHistory(inventory.history), renderUsage(inventory));
  }

  LX.activity = {
    render,
    actions: () => [],
    subtitle: () => "Autonomy metrics and skill history",
  };
})(window.LX);
