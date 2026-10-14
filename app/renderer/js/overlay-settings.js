/* The Overlay view: everything that floats outside the main window -- the
   hot edge, where the chat panel and alerts appear, the colours and density
   of every window, and each panel's saved layout.

   Changes apply as they're made (no Save button): each control sends a
   small patch to main.js, which validates it, writes overlay-layout.json and
   tells every window. A refused patch (an accent that would read as a state
   colour, say) is shown on the control that sent it. */

(function (LX) {
  const { el, clear, icon, toast } = LX;
  const bodyEl = document.getElementById("overlay-body");

  const PANEL_NAMES = {
    chat: "Chat",
    skills: "Skills",
    activity: "Activity",
    permissions: "Permissions",
    timers: "Timers",
    voice: "Voice",
  };
  const EDGE_NAMES = { left: "Left", right: "Right", top: "Top", bottom: "Bottom", free: "Free" };
  const ANCHOR_NAMES = {
    "top-left": "Top left",
    "top-center": "Top center",
    "top-right": "Top right",
    center: "Center",
    "bottom-left": "Bottom left",
    "bottom-center": "Bottom center",
    "bottom-right": "Bottom right",
  };

  let settings = null;
  let presets = [];
  let dockEdge = "bottom";
  let qtOverlay = false;
  let accentError = null;
  let accentNotes = [];
  let loadError = null;
  let pendingRender = false;

  // --- data --------------------------------------------------------------

  async function load() {
    const result = await window.leutheria.getOverlaySettings();
    if (!result || !result.settings) {
      loadError = "Couldn't read the overlay settings.";
      render();
      return;
    }
    settings = result.settings;
    presets = result.presets || [];
    dockEdge = result.dockEdge || "bottom";
    qtOverlay = Boolean(result.qtOverlay);
    render();
  }

  /** Send a patch; on refusal, `onError` gets the reason (default: a toast). */
  async function save(patch, onError) {
    const result = await window.leutheria.setOverlaySettings(patch);
    if (!result || !result.ok) {
      const message = (result && result.error) || "Couldn't save that";
      if (onError) onError(message);
      else toast(message, "error");
      render();
      return false;
    }
    settings = result.settings;
    if (patch.appearance && "accent" in patch.appearance) {
      accentError = null;
      accentNotes = result.notes || [];
    }
    render();
    return true;
  }

  // Changed elsewhere (a panel's own strip, the theme button in the rail).
  // Not mid-edit, though: re-rendering would take the field away.
  window.leutheria.onOverlaySettings((next) => {
    settings = next;
    if (editing()) pendingRender = true;
    else if (LX.app && LX.app.currentView() === "overlay") render();
  });

  function editing() {
    const active = document.activeElement;
    return Boolean(active && bodyEl.contains(active) && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName) && active.type !== "range");
  }

  bodyEl.addEventListener("focusout", () => {
    if (pendingRender) {
      pendingRender = false;
      setTimeout(() => {
        if (!editing()) render();
      }, 0);
    }
  });

  // --- building blocks ---------------------------------------------------

  function card(title, description, ...rows) {
    return el("div.card", {}, el("div.card-header", {}, el("h2", { text: title }), el("p", { text: description })), ...rows);
  }

  function row(key, title, description, control, below) {
    return el(
      "div.setting.setting-stack",
      { "data-key": key },
      el("div.setting-main", {}, el("div.setting-text", {}, el("strong", { text: title }), description && el("span", { text: description })), control),
      below
    );
  }

  /** A segmented control. options: [[value, label], ...] */
  function segmented(key, label, options, current, onPick) {
    return el(
      "div.segmented",
      { role: "group", "aria-label": label, "data-focus-group": key },
      options.map(([value, text]) =>
        el("button", {
          text,
          class: value === current ? "active" : "",
          "aria-pressed": String(value === current),
          "data-focus": `${key}:${value}`,
          "data-value": value,
          onclick: () => {
            if (value !== current) onPick(value);
          },
        })
      )
    );
  }

  function switchButton(key, label, checked, onToggle) {
    return el("button.switch", {
      role: "switch",
      "aria-checked": String(checked),
      "aria-label": label,
      "data-focus": key,
      onclick: () => onToggle(!checked),
    });
  }

  /* A screen, drawn: seven places an alert can sit. The chosen one is a
     filled block with a ring and is named beside the picture -- selection
     isn't colour alone. */
  function anchorPicker(key, label, current, onPick) {
    const screenEl = el("div.anchor-screen", { role: "group", "aria-label": label });
    for (const [value, name] of Object.entries(ANCHOR_NAMES)) {
      screenEl.appendChild(
        el("button.anchor-spot", {
          "data-anchor": value,
          "data-focus": `${key}:${value}`,
          class: value === current ? "selected" : "",
          "aria-pressed": String(value === current),
          "aria-label": name,
          title: name,
          onclick: () => {
            if (value !== current) onPick(value);
          },
        })
      );
    }
    return el("div.anchor-picker", {}, screenEl, el("span.anchor-name", { text: ANCHOR_NAMES[current] || current }));
  }

  // --- sections ----------------------------------------------------------

  function hotEdgeCard() {
    const hot = settings.hotEdge;
    const dwellReadout = el("span.range-value", { text: `${hot.dwellMs} ms` });
    const conflict =
      hot.edge === "top"
        ? "The menu bar is on this edge, and a full-screen app reveals it when the cursor rests there too."
        : hot.edge === dockEdge
          ? "Your Dock is on this edge. If it hides itself, resting there also slides it out."
          : null;
    const select = el("select.field-input.select", {
      "aria-label": "Panel the hot edge opens",
      "data-focus": "hot-panel",
      onchange: (event) => save({ hotEdge: { panel: event.target.value } }),
    });
    for (const [value, name] of Object.entries(PANEL_NAMES)) {
      const option = el("option", { value, text: name });
      if (value === hot.panel) option.selected = true;
      select.appendChild(option);
    }
    return card(
      "Hot edge",
      "Rest the cursor against an edge of the screen to slide a panel in, without leaving what you're doing.",
      row(
        "hot-enabled",
        "Open a panel at a screen edge",
        hot.enabled ? "On. Holding a mouse button (dragging) never triggers it." : "Off. Panels open only when you ask for one.",
        switchButton("hot-enabled", "Open a panel at a screen edge", hot.enabled, (next) => save({ hotEdge: { enabled: next } }))
      ),
      row(
        "hot-edge",
        "Edge",
        "The panel docks against the edge you used.",
        segmented("hot-edge", "Hot edge", ["left", "right", "top", "bottom"].map((edge) => [edge, EDGE_NAMES[edge]]), hot.edge, (edge) =>
          save({ hotEdge: { edge } })
        ),
        conflict ? el("div.setting-hint", { text: conflict }) : null
      ),
      row(
        "hot-dwell",
        "Wait before opening",
        "How long the cursor rests on the edge first. Shorter opens faster and more often by accident.",
        el(
          "div.inline-controls",
          {},
          el("input.range", {
            type: "range",
            min: "150",
            max: "1500",
            step: "50",
            value: String(hot.dwellMs),
            "aria-label": "Wait before opening, in milliseconds",
            "data-focus": "hot-dwell",
            oninput: (event) => (dwellReadout.textContent = `${event.target.value} ms`),
            onchange: (event) => save({ hotEdge: { dwellMs: Number(event.target.value) } }),
          }),
          dwellReadout
        )
      ),
      row("hot-panel", "Panel", "Which panel the edge opens.", el("div.select-wrap", {}, select))
    );
  }

  function placementRows(id, layout) {
    const autoHide = layout.autoHide;
    const seconds = el("input.field-input.number", {
      type: "number",
      min: "2",
      max: "600",
      value: String(autoHide.seconds),
      "aria-label": "Seconds before it hides",
      "data-focus": `${id}-seconds`,
      onchange: (event) => save({ panels: { [id]: { autoHide: { seconds: Number(event.target.value) } } } }),
    });
    return [
      row(
        `${id}-dock`,
        "Position",
        layout.dock === "free" ? "Where you last dropped it. Drag its heading to move it." : `Docked to the ${layout.dock} edge of the screen.`,
        segmented(`${id}-dock`, "Position", Object.entries(EDGE_NAMES), layout.dock, (dock) => save({ panels: { [id]: { dock } } }))
      ),
      row(
        `${id}-orientation`,
        "Layout",
        layout.orientation === "vertical" ? "A column, one thing under the next." : "A strip, laid out side by side.",
        segmented(`${id}-orientation`, "Layout", [["vertical", "Vertical"], ["horizontal", "Horizontal"]], layout.orientation, (orientation) =>
          save({ panels: { [id]: { orientation } } })
        )
      ),
      row(
        `${id}-display`,
        "Display",
        "With more than one screen, which one a docked panel opens on.",
        segmented(`${id}-display`, "Display", [["cursor", "Where the cursor is"], ["primary", "Main display"]], layout.display, (display) =>
          save({ panels: { [id]: { display } } })
        )
      ),
      row(
        `${id}-autohide`,
        "Hide on its own",
        layout.pinned
          ? "Pinned: it stays until you close it."
          : autoHide.mode === "leave"
            ? "Once you've moved the cursor over it and away again."
            : autoHide.mode === "timeout"
              ? "When the cursor has been elsewhere for a while."
              : "Never: it stays until you close it.",
        segmented(`${id}-autohide`, "Hide on its own", [["leave", "Cursor leaves"], ["timeout", "After a delay"], ["never", "Never"]], autoHide.mode, (mode) =>
          save({ panels: { [id]: { autoHide: { mode } } } })
        ),
        autoHide.mode === "timeout" ? el("div.inline-controls.below", {}, seconds, el("span.unit", { text: "seconds" })) : null
      ),
    ];
  }

  function chatCard() {
    const layout = settings.panels.chat;
    return card(
      "Chat panel",
      "Where the chat appears when you ask for it, or when the hot edge opens it.",
      ...placementRows("chat", layout),
      el(
        "div.setting.card-actions",
        {},
        el("button.btn", { text: "Open the chat panel", "data-focus": "open-chat", onclick: () => window.leutheria.showPanel("chat") })
      )
    );
  }

  function alertsCard() {
    const alerts = settings.alerts;
    return card(
      "Alerts",
      "Where the activation overlay (and its approval card) and the small confirmations called toasts appear.",
      row(
        "alerts-anchor",
        "Activation overlay",
        qtOverlay
          ? "The native overlay is running (LEUTHERIA_OVERLAY=qt); it always sits top center. This applies to the built-in one."
          : "Shown while a voice session is live. On a bottom position the approval card opens above it.",
        anchorPicker("alerts-anchor", "Activation overlay position", alerts.anchor, (anchor) => save({ alerts: { anchor } }))
      ),
      row(
        "alerts-display",
        "Overlay display",
        "With more than one screen.",
        segmented("alerts-display", "Overlay display", [["cursor", "Where the cursor is"], ["primary", "Main display"]], alerts.display, (display) =>
          save({ alerts: { display } })
        )
      ),
      row(
        "toast-anchor",
        "Toasts",
        "Inside the window that raised them. Bottom left keeps them clear of the row buttons on the right.",
        anchorPicker("toast-anchor", "Toast position", alerts.toastAnchor, (toastAnchor) => save({ alerts: { toastAnchor } }))
      )
    );
  }

  function accentRow() {
    const look = settings.appearance;
    const current = look.accent;
    const swatches = el(
      "div.choice-row.wrap",
      { role: "group", "aria-label": "Accent presets" },
      presets.map((preset) => {
        const on = (preset.hex || null) === (current || null);
        return el(
          "button.choice",
          {
            class: on ? "selected" : "",
            "aria-pressed": String(on),
            "data-focus": `accent:${preset.name}`,
            onclick: () => save({ appearance: { accent: preset.hex } }, (message) => (accentError = message)),
          },
          el("span.pip"),
          el("span.swatch", { style: `--swatch:${preset.hex || "var(--violet)"}`, "aria-hidden": "true" }),
          preset.name
        );
      })
    );
    const hex = el("input.field-input.mono.hex", {
      type: "text",
      value: current || "",
      placeholder: "#RRGGBB",
      maxlength: "7",
      spellcheck: "false",
      "aria-label": "Custom accent colour, as #RRGGBB",
      "data-focus": "accent-hex",
      onkeydown: (event) => {
        if (event.key === "Enter") apply(event.target.value.trim());
      },
    });
    const picker = el("input.color-well", {
      type: "color",
      value: current || "#8e6fd4",
      "aria-label": "Pick a custom accent colour",
      "data-focus": "accent-picker",
      onchange: (event) => apply(event.target.value),
    });
    function apply(value) {
      const normal = value.startsWith("#") ? value : `#${value}`;
      save({ appearance: { accent: normal.toLowerCase() } }, (message) => {
        accentError = message;
      });
    }
    return row(
      "accent",
      "Accent",
      "Selection, the primary button, the mark. Live, waiting, failed and succeeded keep their own colours: they mean something.",
      null,
      el(
        "div.setting-body",
        {},
        swatches,
        el("div.add-row", {}, picker, hex, el("button.btn.btn-sm", { text: "Use colour", onclick: () => apply(hex.value.trim()) })),
        accentError ? el("div.field-error", {}, icon("warning"), el("span", { text: accentError })) : null,
        !accentError && accentNotes.length
          ? el("div.setting-hint", {}, el("strong", { text: "Adjusted for contrast. " }), el("span", { text: accentNotes.join("; ") + "." }))
          : null
      )
    );
  }

  function appearanceCard() {
    const look = settings.appearance;
    const opacityReadout = el("span.range-value", { text: `${Math.round(look.panelOpacity * 100)}%` });
    let opacityTimer = null;
    return card(
      "Appearance",
      "Applies to the main window, the overlay and every panel.",
      row(
        "theme",
        "Theme",
        look.theme === "system" ? "Follows macOS: dark or light." : null,
        segmented("theme", "Theme", [["dark", "Dark"], ["light", "Light"], ["system", "System"]], look.theme, (theme) => save({ appearance: { theme } }))
      ),
      accentRow(),
      row(
        "panel-opacity",
        "Panel opacity",
        "For panels that haven't been given their own from their control strip.",
        el(
          "div.inline-controls",
          {},
          el("input.range", {
            type: "range",
            min: "60",
            max: "100",
            step: "5",
            value: String(Math.round(look.panelOpacity * 100)),
            "aria-label": "Panel opacity, percent",
            "data-focus": "panel-opacity",
            oninput: (event) => {
              opacityReadout.textContent = `${event.target.value}%`;
              clearTimeout(opacityTimer);
              const value = Number(event.target.value) / 100;
              opacityTimer = setTimeout(() => save({ appearance: { panelOpacity: value } }), 120);
            },
          }),
          opacityReadout
        )
      ),
      row(
        "density",
        "Density",
        look.density === "compact" ? "Tighter spacing and smaller controls." : null,
        segmented("density", "Density", [["comfortable", "Comfortable"], ["compact", "Compact"]], look.density, (density) => save({ appearance: { density } }))
      ),
      row(
        "panel-level",
        "Panels float",
        look.panelLevel === "floating" ? "Above other apps' windows." : "Like a normal window: another app's window can cover them.",
        segmented("panel-level", "Panels float", [["floating", "Above windows"], ["normal", "Normal"]], look.panelLevel, (panelLevel) =>
          save({ appearance: { panelLevel } })
        )
      )
    );
  }

  function describe(layout) {
    const where = layout.dock === "free" ? "free" : `${layout.dock} edge`;
    const hide = layout.pinned ? "pinned" : layout.autoHide.mode === "never" ? "stays open" : layout.autoHide.mode === "leave" ? "hides when left" : `hides after ${layout.autoHide.seconds} s`;
    const opacity = layout.opacity === null ? null : `${Math.round(layout.opacity * 100)}%`;
    return [where, layout.orientation, hide, opacity].filter(Boolean).join(" · ");
  }

  function panelsCard() {
    const list = el("div");
    for (const [id, name] of Object.entries(PANEL_NAMES)) {
      const layout = settings.panels[id];
      list.appendChild(
        el(
          "div.trust-row.panel-row",
          { "data-panel": id },
          el(
            "div.trust-text",
            {},
            el("div.trust-tool", {}, el("span", { text: name }), layout.pinned ? el("span.badge.badge-accent", { text: "pinned" }) : null),
            el("div.trust-args", { text: describe(layout) })
          ),
          el("button.btn.btn-sm", { text: "Open", "data-focus": `open:${id}`, onclick: () => window.leutheria.showPanel(id) }),
          el("button.btn.btn-sm", {
            text: "Reset",
            "data-focus": `reset:${id}`,
            title: "Put this panel back where it started, with its default size and behaviour",
            onclick: async () => {
              const result = await window.leutheria.resetPanelLayout(id);
              if (result && result.ok) {
                settings = result.settings;
                toast(`${name} panel layout reset`);
                render();
              } else toast((result && result.error) || "Couldn't reset that", "error");
            },
          })
        )
      );
    }
    return el(
      "div.card",
      {},
      el(
        "div.card-header",
        {},
        el("h2", { text: "Panels" }),
        el("p", { text: "Each remembers its own place. Move, dock, resize and pin one from its heading, or ask: “show me my timers”." })
      ),
      list,
      el(
        "div.setting.card-actions",
        {},
        el("button.btn", {
          text: "Reset every panel",
          "data-focus": "reset-all",
          onclick: async () => {
            const result = await window.leutheria.resetPanelLayout("all");
            if (result && result.ok) {
              settings = result.settings;
              toast("Every panel layout reset");
              render();
            }
          },
        }),
        el("button.btn", { text: "Hide all panels", "data-focus": "hide-all", onclick: () => window.leutheria.hidePanel("all") })
      )
    );
  }

  // --- entry points ------------------------------------------------------

  function render() {
    const focusKey = bodyEl.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
    const scroll = document.getElementById("view-overlay").scrollTop;
    clear(bodyEl);
    if (!settings) {
      bodyEl.appendChild(el("div.empty", { text: loadError || "Loading…" }));
      return;
    }
    bodyEl.append(hotEdgeCard(), chatCard(), alertsCard(), appearanceCard(), panelsCard());
    document.getElementById("view-overlay").scrollTop = scroll;
    if (focusKey) {
      const again = bodyEl.querySelector(`[data-focus="${CSS.escape(focusKey)}"]`);
      if (again) again.focus();
    }
  }

  LX.overlay = {
    render,
    show: load,
    actions: () => [],
    subtitle: () => "Panels, the hot edge, alerts and appearance",
  };
})(window.LX);
