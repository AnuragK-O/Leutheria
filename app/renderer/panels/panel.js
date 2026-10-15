/* A panel window's shell: the frame (drag handle, resize edges, the control
   strip) around one of the main window's own views.

   The panels don't have rendering code of their own where the main window
   already has it. This file builds the DOM ids a view module expects
   (#chat, #library-list, #activity-body, ...), stands in for app.js
   (LX.app: the inventory snapshot, the toolbar a view fills, its subtitle),
   then loads that module's script -- the same file the main window runs.
   Only timers and the voice readout are panel-only (timers.js,
   voice-panel.js, in this folder).

   main.js tells the page its layout (panel-config) and moves the window;
   the page only reports pointer movement and button presses. */

(function (LX) {
  const { el, icon, clear } = LX;
  const api = window.leutheria;
  const id = new URLSearchParams(location.search).get("id") || "chat";

  // view: the LX module that draws it (what currentView() reports, so the
  // module's own checks still work). scripts: loaded in order after util.js.
  const PANELS = {
    chat: { title: "Chat", subtitle: "Talk to Leutheria", view: "assistant", scripts: ["../js/chat.js"] },
    skills: { title: "Skills", view: "library", scripts: ["../js/library-groups.js", "../js/library-shared.js", "../js/library.js"] },
    activity: { title: "Activity", view: "activity", scripts: ["../js/activity.js"] },
    permissions: { title: "Permissions", view: "permissions", scripts: ["../js/permissions.js"] },
    timers: { title: "Timers", view: "timers", scripts: ["timers.js"] },
    voice: { title: "Voice", view: "voicePanel", scripts: ["../js/voice.js", "voice-panel.js"] },
  };
  const spec = PANELS[id] || PANELS.chat;

  const frameEl = document.getElementById("panel-frame");
  const headEl = document.getElementById("panel-head");
  const stripEl = document.getElementById("panel-strip");
  const popEl = document.getElementById("panel-pop");
  const toolsEl = document.getElementById("panel-tools");
  const bodyEl = document.getElementById("panel-body");
  const resizersEl = document.getElementById("panel-resizers");
  const subtitleEl = document.getElementById("panel-subtitle");

  headEl.querySelector(".panel-grip").appendChild(icon("grip"));
  document.body.dataset.panel = id;
  document.getElementById("panel-title").textContent = spec.title;
  document.title = `Leutheria: ${spec.title}`;
  // Read by library.js: a panel shows a short detail, not the full pane.
  LX.panelMode = id;

  let config = { dock: "right", orientation: "vertical", opacity: 1, pinned: false, resizeEdges: [] };

  // --- the view's DOM (the ids its module looks up when it loads) ------------

  function buildBody() {
    if (id === "chat") {
      bodyEl.append(
        el("div#chat-scroll.chat-scroll", {}, el("div#chat.chat", { role: "log", "aria-live": "polite", "aria-label": "Transcript" })),
        el(
          "div.composer",
          {},
          el(
            "div.composer-field",
            {},
            el("span.composer-prompt", { "aria-hidden": "true", text: ">" }),
            // No autofocus: the panel must not ask for keyboard focus by
            // appearing. Clicking into the field is what gives it focus.
            el("input#command-input", { type: "text", placeholder: "Tell Leutheria what to do", autocomplete: "off", "aria-label": "Command" }),
            el("button#mic-button.mic-button", { title: "Push to talk", "aria-label": "Push to talk: record one command" })
          )
        )
      );
    } else if (id === "skills") {
      bodyEl.append(
        el(
          "div.library",
          {},
          el("div#library-list.library-list"),
          el("div#library-detail.library-detail", { role: "region", "aria-label": "Capability details", tabindex: "-1" })
        )
      );
    } else if (id === "activity") {
      bodyEl.append(el("div#activity-body.stack"));
    } else if (id === "permissions") {
      bodyEl.append(el("div#permissions-body.stack"));
    } else if (id === "timers") {
      bodyEl.append(el("div#timers-body.timers"));
    } else if (id === "voice") {
      bodyEl.append(
        el(
          "div#voice-panel-body.voice-panel",
          {},
          // voice.js drives this pill exactly as it drives the header's.
          el(
            "button#voice-indicator.voice-indicator",
            { "data-state": "off" },
            el("span.voice-key", { text: "Voice" }),
            el("span.voice-dot"),
            el("span.voice-label", { text: "Off" })
          )
        )
      );
    }
  }

  // --- standing in for app.js -------------------------------------------------

  LX.state = { inventory: null };
  const mod = () => LX[spec.view] || null;

  function renderSubtitle() {
    const m = mod();
    subtitleEl.textContent = (m && m.subtitle ? m.subtitle() : spec.subtitle) || "";
  }

  function renderActions() {
    renderSubtitle();
    const m = mod();
    const nodes = m && m.actions ? m.actions() : [];
    toolsEl.hidden = nodes.length === 0;
    const same = nodes.length === toolsEl.children.length && nodes.every((node, index) => toolsEl.children[index] === node);
    if (!same) toolsEl.replaceChildren(...nodes);
  }

  let retryTimer = null;
  async function refresh() {
    const result = await api.getInventory();
    if (!result || !result.ok) {
      clearTimeout(retryTimer);
      retryTimer = setTimeout(refresh, 900);
      return;
    }
    clearTimeout(retryTimer);
    LX.state.inventory = result;
    const m = mod();
    if (m && m.render) m.render();
    renderActions();
  }

  LX.app = { renderActions, renderSubtitle, currentView: () => spec.view, refresh };

  // --- the control strip --------------------------------------------------------

  const DOCK_CHOICES = [
    ["left", "Left edge"],
    ["right", "Right edge"],
    ["top", "Top edge"],
    ["bottom", "Bottom edge"],
    ["free", "Free (where you drop it)"],
  ];

  function stripButton(name, label, onclick, extra = {}) {
    return el("button.icon-button.panel-ctl", { title: label, "aria-label": label, "data-ctl": name, onclick, ...extra }, icon(extra.iconName || name));
  }

  function renderStrip() {
    const dockIcon = `dock-${config.dock}`;
    clear(stripEl).append(
      stripButton("dock", `Position: ${config.dock}`, () => togglePop("dock"), { iconName: dockIcon, "aria-haspopup": "menu", "aria-expanded": String(popOpen === "dock") }),
      stripButton(
        "orientation",
        config.orientation === "vertical" ? "Lay out horizontally" : "Lay out vertically",
        () => api.panelOp("orientation"),
        { iconName: config.orientation === "vertical" ? "orient-horizontal" : "orient-vertical" }
      ),
      stripButton("opacity", `Opacity ${Math.round(config.opacity * 100)}%`, () => togglePop("opacity"), { "aria-haspopup": "dialog", "aria-expanded": String(popOpen === "opacity") }),
      stripButton("pin", config.pinned ? "Unpin: hide automatically again" : "Pin: keep it open", () => api.panelOp("pin"), {
        "aria-pressed": String(config.pinned),
        class: config.pinned ? "on" : "",
      }),
      stripButton("close", "Close panel", () => api.panelOp("close"), { iconName: "x" })
    );
  }

  let popOpen = null;

  function togglePop(which) {
    popOpen = popOpen === which ? null : which;
    renderPop();
    renderStrip();
  }

  function renderPop() {
    popEl.hidden = !popOpen;
    clear(popEl);
    if (popOpen === "dock") {
      popEl.setAttribute("role", "menu");
      for (const [value, label] of DOCK_CHOICES) {
        const on = config.savedDock === value;
        popEl.appendChild(
          el(
            "button.panel-menu-item",
            {
              role: "menuitemradio",
              "aria-checked": String(on),
              "data-dock": value,
              class: on ? "selected" : "",
              onclick: () => {
                popOpen = null;
                renderPop();
                api.panelOp("dock", value);
              },
            },
            el("span.pip"),
            icon(`dock-${value}`),
            el("span", { text: label })
          )
        );
      }
    } else if (popOpen === "opacity") {
      popEl.setAttribute("role", "dialog");
      const readout = el("span.range-value", { text: `${Math.round(config.opacity * 100)}%` });
      popEl.append(
        el("div.micro-label", { text: "Opacity" }),
        el(
          "div.inline-controls",
          {},
          el("input.range", {
            type: "range",
            min: "60",
            max: "100",
            step: "5",
            value: String(Math.round(config.opacity * 100)),
            "aria-label": "Panel opacity",
            oninput: (event) => {
              readout.textContent = `${event.target.value}%`;
              api.panelOp("opacity", Number(event.target.value) / 100);
            },
          }),
          readout
        )
      );
    }
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && popOpen) {
      popOpen = null;
      renderPop();
      renderStrip();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (popOpen && !popEl.contains(event.target) && !stripEl.contains(event.target)) {
      popOpen = null;
      renderPop();
      renderStrip();
    }
  });

  // --- moving and resizing --------------------------------------------------------
  // The pointer is captured, so moves keep arriving as the window slides
  // under it; main.js moves the window from where it started. One message
  // per frame at most.

  function track(target, start, onMove, onEnd) {
    return (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      target.setPointerCapture(event.pointerId);
      const origin = { x: event.screenX, y: event.screenY };
      let frame = null;
      let last = null;
      start();
      const move = (moveEvent) => {
        last = { dx: moveEvent.screenX - origin.x, dy: moveEvent.screenY - origin.y };
        if (frame) return;
        frame = requestAnimationFrame(() => {
          frame = null;
          onMove(last);
        });
      };
      const up = () => {
        target.removeEventListener("pointermove", move);
        target.removeEventListener("pointerup", up);
        target.removeEventListener("pointercancel", up);
        if (frame) cancelAnimationFrame(frame);
        if (last) onMove(last);
        onEnd();
      };
      target.addEventListener("pointermove", move);
      target.addEventListener("pointerup", up);
      target.addEventListener("pointercancel", up);
    };
  }

  headEl.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button, input, .panel-pop")) return;
    track(
      headEl,
      () => {
        document.body.classList.add("moving");
        api.panelOp("move-start");
      },
      (delta) => api.panelOp("move", delta),
      () => {
        document.body.classList.remove("moving");
        api.panelOp("move-end");
      }
    )(event);
  });

  function renderResizers() {
    clear(resizersEl);
    for (const edge of config.resizeEdges || []) {
      const handle = el("div.panel-resizer", { "data-edge": edge, "aria-hidden": "true" });
      handle.addEventListener(
        "pointerdown",
        track(
          handle,
          () => api.panelOp("resize-start", { edge }),
          (delta) => api.panelOp("resize", delta),
          () => api.panelOp("resize-end")
        )
      );
      resizersEl.appendChild(handle);
    }
  }

  function applyConfig(next) {
    config = { ...config, ...next };
    document.body.dataset.orientation = config.orientation;
    document.body.dataset.dock = config.dock;
    document.body.classList.toggle("pinned", Boolean(config.pinned));
    renderStrip();
    renderResizers();
    if (popOpen) renderPop();
    // A view laid out for the other orientation may need to redraw (chat's
    // scroll axis, say).
    if (LX.onOrientation) LX.onOrientation(config.orientation);
  }

  api.onPanelConfig(applyConfig);

  // --- input modality (as app.js does it for the main window) ---------------------

  document.body.dataset.modality = "pointer";
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Tab" || event.key.startsWith("Arrow") || event.key === "/") document.body.dataset.modality = "keyboard";
    },
    true
  );
  document.addEventListener("pointerdown", () => (document.body.dataset.modality = "pointer"), true);

  // --- boot -------------------------------------------------------------------------

  function load(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = false;
      script.onload = resolve;
      script.onerror = () => reject(new Error(`couldn't load ${src}`));
      document.body.appendChild(script);
    });
  }

  async function boot() {
    buildBody();
    for (const src of spec.scripts) await load(src);
    api.onInventoryChanged(refresh);
    api.onStatus((status) => {
      if (status === "connected") refresh();
    });
    api.onPanelQuery((query) => {
      const m = mod();
      if (m && m.setQuery) m.setQuery(query);
    });
    if (id === "chat") LX.chat.replay(await api.getChatHistory());
    const m = mod();
    if (m && m.render) m.render();
    renderActions();
    refresh();
    const result = await api.panelOp("ready");
    if (result && result.config) applyConfig(result.config);
    document.body.classList.add("ready");
  }

  renderStrip();
  boot().catch((err) => {
    bodyEl.replaceChildren(el("div.empty", { text: `This panel couldn't load: ${err.message}` }));
  });
})(window.LX);
