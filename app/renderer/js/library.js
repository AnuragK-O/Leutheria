/* The Library view: every capability Leutheria has, in a master/detail pane.
   The list is the index; the detail pane is where a capability is turned on
   or off, made to ask first, inspected, or deleted. */

(function (LX) {
  const { el, clear, icon, relativeTime, formatTemplateArgs, toast } = LX;

  const listEl = document.getElementById("library-list");
  const detailEl = document.getElementById("library-detail");

  let filter = "all";
  let query = "";
  let selected = null;

  // --- topbar controls ---------------------------------------------------

  function actions() {
    const search = el(
      "div.search",
      {},
      icon("search"),
      el("input", {
        type: "search",
        placeholder: "Search capabilities",
        value: query,
        oninput: (event) => {
          query = event.target.value.trim().toLowerCase();
          renderList();
        },
      })
    );

    const segmented = el("div.segmented");
    for (const [value, label] of [["all", "All"], ["tool", "Tools"], ["skill", "Skills"]]) {
      segmented.appendChild(
        el("button", {
          text: label,
          class: filter === value ? "active" : "",
          onclick: () => {
            filter = value;
            render();
          },
        })
      );
    }

    const importBtn = el("button.btn.btn-sm", {
      text: "Import Skill",
      onclick: async () => {
        const pkg = prompt("Paste exported .leutheria-skill JSON content:");
        if (!pkg) return;
        const res = await window.leutheria.importSkill(pkg);
        if (res && res.ok) {
          toast(`Imported skill "${res.name}" successfully!`, "success");
          LX.app.refresh();
        } else {
          toast((res && res.error) || "Failed to import skill", "error");
        }
      },
    });

    return [segmented, search, importBtn];
  }

  // --- data --------------------------------------------------------------

  function allEntries() {
    const inventory = LX.state.inventory;
    if (!inventory) return [];
    return [...inventory.tools, ...inventory.skills];
  }

  function visibleEntries() {
    return allEntries().filter((entry) => {
      if (filter !== "all" && entry.kind !== filter) return false;
      if (!query) return true;
      return (entry.name + " " + entry.description).toLowerCase().includes(query);
    });
  }

  function find(name) {
    return allEntries().find((entry) => entry.name === name) || null;
  }

  // --- list --------------------------------------------------------------

  function renderList() {
    const entries = visibleEntries();
    clear(listEl);

    if (!entries.length) {
      listEl.appendChild(el("div.empty", { text: query ? "No matches." : "Nothing here yet." }));
      return;
    }

    const groups = [
      ["Skills", entries.filter((entry) => entry.kind === "skill")],
      ["Tools", entries.filter((entry) => entry.kind === "tool")],
    ];

    for (const [label, group] of groups) {
      if (!group.length) continue;
      listEl.appendChild(el("div.list-group-label", { text: `${label} · ${group.length}` }));
      for (const entry of group) listEl.appendChild(renderRow(entry));
    }
  }

  function renderRow(entry) {
    const flags = el("div.list-row-flags");
    if (!entry.enabled) flags.appendChild(el("span.flag-dot.off", { title: "Disabled" }));
    else if (entry.requires_confirmation)
      flags.appendChild(el("span.flag-dot.ask", { title: "Asks before running" }));

    return el(
      "button.list-row",
      {
        class: [entry.name === selected ? "active" : "", entry.enabled ? "" : "disabled"]
          .filter(Boolean)
          .join(" "),
        onclick: () => {
          selected = entry.name;
          render();
        },
      },
      el("span.list-row-icon", {}, icon(entry.kind === "skill" ? "sparkle" : "tool")),
      el(
        "span.list-row-text",
        {},
        el("span.list-row-name", { text: entry.name }),
        el("span.list-row-desc", { text: entry.description })
      ),
      flags
    );
  }

  // --- detail ------------------------------------------------------------

  function badges(entry) {
    const list = [el("span.badge", { text: entry.kind })];
    if (entry.kind === "skill") {
      list.push(
        el("span.badge", {
          class: entry.origin === "generated" ? "badge-accent" : "",
          text: entry.origin === "generated" ? "learned" : "built-in",
        })
      );
      if (entry.autonomy_tier) {
        list.push(
          el("span.badge", {
            class: `badge-${entry.autonomy_tier}`,
            text: entry.autonomy_tier.toUpperCase(),
          })
        );
      }
      if (entry.risk_level) {
        list.push(
          el("span.badge", {
            class: `badge-${entry.risk_level}`,
            text: `${entry.risk_level} risk`,
          })
        );
      }
    } else if (entry.safety === "destructive") {
      list.push(el("span.badge.badge-warning", { text: "destructive" }));
    }
    if (!entry.enabled) list.push(el("span.badge.badge-danger", { text: "disabled" }));
    return list;
  }

  async function apply(name, changes) {
    const result = await window.leutheria.setPreference(name, changes);
    if (!result.ok) toast(result.error || "Couldn't save that change", "error");
    return result.ok;
  }

  function settingRow(title, description, checked, onToggle) {
    const toggle = el("button.switch", {
      role: "switch",
      "aria-checked": String(checked),
      onclick: () => onToggle(!checked),
    });
    return el(
      "div.setting",
      {},
      el("div.setting-text", {}, el("strong", { text: title }), el("span", { text: description })),
      toggle
    );
  }

  function renderDetail() {
    clear(detailEl);
    const entry = selected ? find(selected) : null;

    if (!entry) {
      detailEl.appendChild(
        el("div.empty", { text: "Select a tool or skill to inspect and configure it." })
      );
      return;
    }

    const detail = el("div.detail");

    detail.appendChild(
      el(
        "div.detail-head",
        {},
        el("div.detail-title", {}, el("h2", { text: entry.name }), ...badges(entry)),
        el("p.detail-desc", { text: entry.description })
      )
    );

    // --- controls ---
    const controls = el("div.card");
    controls.appendChild(
      settingRow(
        "Available to Leutheria",
        entry.enabled
          ? "Offered to the model and runnable."
          : "Hidden from the model entirely — it can't be used or planned around.",
        entry.enabled,
        (next) => apply(entry.name, { enabled: next })
      )
    );
    controls.appendChild(
      settingRow(
        "Ask before running",
        entry.requires_confirmation
          ? "Pauses for your approval — spoken aloud, answerable by voice."
          : entry.kind === "skill"
            ? "Runs straight through. Its individual destructive steps still ask."
            : "Runs without interrupting you.",
        entry.requires_confirmation,
        (next) => apply(entry.name, { requiresConfirmation: next })
      )
    );

    if (entry.is_overridden) {
      controls.appendChild(
        el(
          "div.setting-note",
          {},
          icon("info"),
          el("span", { text: "Customized — differs from the built-in default." }),
          el("button.link-button", {
            text: "Reset",
            onclick: () => apply(entry.name, { reset: true }),
          })
        )
      );
    }

    detail.appendChild(el("section", {}, el("div.section-label", { text: "Permissions" }), controls));

    // --- parameters ---
    const properties = (entry.input_schema && entry.input_schema.properties) || {};
    const required = (entry.input_schema && entry.input_schema.required) || [];
    const names = Object.keys(properties);
    const params = el("div.card.params");
    if (names.length) {
      for (const name of names) {
        const schema = properties[name] || {};
        params.appendChild(
          el(
            "div.param",
            {},
            el("div.param-name", { text: name }),
            el(
              "div.param-meta",
              {},
              el("p", { text: schema.description || "—" }),
              el("div.param-type", {
                text: `${schema.type || "any"}${required.includes(name) ? " · required" : " · optional"}`,
              })
            )
          )
        );
      }
    } else {
      params.appendChild(el("div.empty", { text: "Takes no parameters." }));
    }
    detail.appendChild(el("section", {}, el("div.section-label", { text: "Parameters" }), params));

    // --- steps (learned skills only) ---
    if (entry.steps && entry.steps.length) {
      const steps = el("div.card.steps");
      entry.steps.forEach((step, index) => {
        steps.appendChild(
          el(
            "div.step",
            {},
            el("div.step-index", { text: String(index + 1) }),
            el(
              "div.step-body",
              {},
              el("div.step-tool", { text: step.tool }),
              formatTemplateArgs(step.args)
            )
          )
        );
      });
      detail.appendChild(
        el(
          "section",
          {},
          el("div.section-label", { text: `Steps · ${entry.steps.length}` }),
          steps,
          el("p", {
            class: "composer-hint",
            style: "text-align:left;margin-left:2px",
            text: "A learned skill is data, not code — each step is dispatched through the same safety checks as anything else.",
          })
        )
      );
    }

    // --- usage ---
    const statCards = [
      el("div.stat", {}, el("div.stat-value", { text: String(entry.runs) }), el("div.stat-label", { text: "times run" })),
      el(
        "div.stat",
        {},
        el("div.stat-value", { text: String(entry.failures) }),
        el("div.stat-label", { text: "failed or declined" })
      ),
    ];

    if (entry.kind === "skill") {
      statCards.push(
        el("div.stat", {}, el("div.stat-value", { text: `${entry.success_rate || 100}%` }), el("div.stat-label", { text: "success rate" })),
        el("div.stat", {}, el("div.stat-value", { text: String(entry.intervention_count || 0) }), el("div.stat-label", { text: "interventions" }))
      );
    }

    statCards.push(
      el(
        "div.stat",
        {},
        el("div.stat-value", { text: relativeTime(entry.last_used) }),
        el("div.stat-label", { text: "last used" })
      )
    );

    detail.appendChild(
      el(
        "section",
        {},
        el("div.section-label", { text: "Usage & Reliability" }),
        el("div.stat-grid", {}, ...statCards)
      )
    );

    // --- export (for skills) ---
    if (entry.kind === "skill") {
      detail.appendChild(
        el(
          "section",
          {},
          el("div.section-label", { text: "Portability" }),
          el(
            "div.card",
            {},
            el(
              "button.btn.btn-sm",
              {
                text: "Export .leutheria-skill Package",
                onclick: async () => {
                  const res = await window.leutheria.exportSkill(entry.name);
                  if (res && res.ok && res.package) {
                    navigator.clipboard.writeText(JSON.stringify(res.package, null, 2));
                    toast(`Copied "${entry.name}" package JSON to clipboard!`, "success");
                  } else {
                    toast((res && res.error) || "Export failed", "error");
                  }
                },
              }
            )
          )
        )
      );
    }

    // --- delete ---
    if (entry.deletable) {
      detail.appendChild(
        el(
          "section",
          {},
          el("div.section-label", { text: "Danger zone" }),
          el(
            "div.danger-zone",
            {},
            el(
              "div.setting-text",
              {},
              el("strong", { text: "Delete this skill" }),
              el("span", {
                text: "Removes it for good. Leutheria can propose it again the next time it sees the same sequence.",
              })
            ),
            el("button.btn.btn-danger", { text: "Delete", onclick: () => confirmDelete(entry) })
          )
        )
      );
    }

    detailEl.appendChild(detail);
  }

  function confirmDelete(entry) {
    const zone = detailEl.querySelector(".danger-zone");
    if (!zone) return;
    clear(zone).append(
      el(
        "div.setting-text",
        {},
        el("strong", { text: `Delete "${entry.name}"?` }),
        el("span", { text: "This can't be undone." })
      ),
      el("button.btn", { text: "Cancel", onclick: renderDetail }),
      el("button.btn.btn-danger", {
        text: "Delete skill",
        onclick: async () => {
          const result = await window.leutheria.deleteSkill(entry.name);
          if (result.ok) {
            selected = null;
            toast(`Deleted "${entry.name}"`);
          } else {
            toast(result.error || "Couldn't delete that skill", "error");
          }
        },
      })
    );
  }

  // --- entry point -------------------------------------------------------

  function render() {
    // Drop a selection that no longer exists (a deleted skill) or that the
    // current filter hides, rather than leaving a stale detail pane up.
    if (selected && !visibleEntries().some((entry) => entry.name === selected)) selected = null;
    renderList();
    renderDetail();
    LX.app.renderActions();
  }

  function count() {
    const inventory = LX.state.inventory;
    return inventory ? inventory.tools.length + inventory.skills.length : null;
  }

  function subtitle() {
    const entries = allEntries();
    if (!entries.length) return "Everything Leutheria can do";
    const off = entries.filter((entry) => !entry.enabled).length;
    const label = entries.length === 1 ? "capability" : "capabilities";
    return `${entries.length} ${label}${off ? ` · ${off} disabled` : ""}`;
  }

  LX.library = { render, actions, count, subtitle };
})(window.LX);
