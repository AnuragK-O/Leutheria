/* The Permissions view: standing approvals Leutheria no longer asks about,
   and every capability whose settings differ from the built-in defaults. */

(function (LX) {
  const { el, clear, icon, formatArgs, toast } = LX;

  const bodyEl = document.getElementById("permissions-body");

  function renderTrusted(trusted) {
    const card = el("div.card");
    card.appendChild(
      el(
        "div.card-header",
        {},
        el("h2", { text: "Standing approvals" }),
        el("p", { text: "Commands approved once and not asked about again." })
      )
    );

    if (!trusted.length) {
      card.appendChild(
        el("div.empty", {
          text: "None yet. Choosing “Approve for this session” or “Always approve” on a prompt adds one here.",
        })
      );
      return card;
    }

    const list = el("div");
    for (const entry of trusted) {
      const isPermanent = entry.scope === "always";
      list.appendChild(
        el(
          "div.trust-row",
          {},
          el(
            "div.trust-text",
            {},
            el(
              "div.trust-tool",
              {},
              el("span", { text: entry.tool }),
              el("span", {
                class: isPermanent ? "badge badge-warning" : "badge",
                text: isPermanent ? "always" : "this session",
              })
            ),
            el("div.trust-args", { text: formatArgs(entry.args, 200) || "no arguments" })
          ),
          el("button.btn.btn-sm.btn-danger", {
            text: "Revoke",
            onclick: async () => {
              const result = await window.leutheria.revokeTrust(entry.key);
              if (result.ok) toast(`Revoked — ${entry.tool} will ask again`);
              else toast(result.error || "Couldn't revoke that", "error");
            },
          })
        )
      );
    }
    card.appendChild(list);
    return card;
  }

  function renderOverrides(inventory) {
    const changed = [...inventory.tools, ...inventory.skills].filter((entry) => entry.is_overridden);

    const card = el("div.card");
    card.appendChild(
      el(
        "div.card-header",
        {},
        el("h2", { text: "Customized capabilities" }),
        el("p", { text: "Anything you've changed from its default behaviour." })
      )
    );

    if (!changed.length) {
      card.appendChild(el("div.empty", { text: "Everything is at its default setting." }));
      return card;
    }

    const list = el("div");
    for (const entry of changed) {
      const notes = [];
      if (!entry.enabled) notes.push("disabled");
      if (entry.requires_confirmation !== entry.default_requires_confirmation) {
        notes.push(entry.requires_confirmation ? "always asks first" : "never asks first");
      }

      list.appendChild(
        el(
          "div.trust-row",
          {},
          el(
            "div.trust-text",
            {},
            el("div.trust-tool", {}, el("span", { text: entry.name }), el("span.badge", { text: entry.kind })),
            el("div.trust-args", { text: notes.join(" · ") || "customized" })
          ),
          el("button.btn.btn-sm", {
            text: "Reset",
            onclick: async () => {
              const result = await window.leutheria.setPreference(entry.name, { reset: true });
              if (result.ok) toast(`${entry.name} reset to defaults`);
              else toast(result.error || "Couldn't reset that", "error");
            },
          })
        )
      );
    }
    card.appendChild(list);
    return card;
  }

  function render() {
    const inventory = LX.state.inventory;
    clear(bodyEl);
    if (!inventory) {
      bodyEl.appendChild(el("div.empty", { text: "Waiting for the agent…" }));
      return;
    }

    bodyEl.append(
      el(
        "div.callout",
        {},
        icon("info"),
        el("span", {
          text:
            "Approvals are tied to the exact command, arguments and all — approving one shell command never approves a different one. Session approvals disappear when the app restarts.",
        })
      ),
      renderTrusted(inventory.trusted),
      renderOverrides(inventory)
    );
  }

  LX.permissions = {
    render,
    actions: () => [],
    subtitle: () => "What Leutheria is allowed to do without asking",
  };
})(window.LX);
