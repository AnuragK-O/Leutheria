/* Shell: sidebar navigation, the shared inventory snapshot, theme, and the
   topbar each view fills in with its own controls. */

(function (LX) {
  const { el, clear, icon } = LX;

  const VIEWS = [
    { id: "assistant", label: "Assistant", title: "Assistant", subtitle: "Talk to Leutheria" },
    { id: "library", label: "Library", title: "Library" },
    { id: "activity", label: "Activity", title: "Activity" },
    { id: "permissions", label: "Permissions", title: "Permissions" },
    { id: "voice", label: "Voice", title: "Voice" },
    { id: "logs", label: "Logs", title: "Logs" },
  ];

  const navEl = document.getElementById("nav");
  const titleEl = document.getElementById("view-title");
  const subtitleEl = document.getElementById("view-subtitle");
  const actionsEl = document.getElementById("view-actions");
  const statusEl = document.getElementById("status");
  const themeButton = document.getElementById("theme-toggle");

  let current = "assistant";

  LX.state = { inventory: null };

  function module(id) {
    return LX[id] || null;
  }

  // --- chrome ------------------------------------------------------------

  // The nav buttons are built once and updated in place: rebuilding them on
  // every render would drop keyboard focus from the one just pressed.
  const navButtons = new Map();

  function renderNav() {
    for (const view of VIEWS) {
      let button = navButtons.get(view.id);
      if (!button) {
        button = el(
          "button.nav-item",
          { onclick: () => setView(view.id) },
          icon(view.id),
          el("span", { text: view.label }),
          el("span.nav-count", { hidden: true })
        );
        navButtons.set(view.id, button);
        navEl.appendChild(button);
      }
      const active = view.id === current;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");

      const count = module(view.id) && module(view.id).count ? module(view.id).count() : null;
      const countEl = button.querySelector(".nav-count");
      countEl.hidden = count === null || count === undefined;
      countEl.textContent = countEl.hidden ? "" : String(count);
    }
  }

  function renderSubtitle() {
    const view = VIEWS.find((entry) => entry.id === current);
    const mod = module(current);
    subtitleEl.textContent = (mod && mod.subtitle ? mod.subtitle() : view.subtitle) || "";
  }

  // inventory.settings_errors: the agent couldn't read a settings file (say,
  // a corrupt preferences.json) and is running on defaults. It matters where
  // those settings are shown and changed, so it heads those two views.
  const settingsErrorsEl = document.getElementById("settings-errors");
  const SETTINGS_VIEWS = ["library", "permissions"];

  function renderSettingsErrors() {
    const errors = (LX.state.inventory && LX.state.inventory.settings_errors) || [];
    const show = errors.length > 0 && SETTINGS_VIEWS.includes(current);
    settingsErrorsEl.hidden = !show;
    if (!show) return;
    settingsErrorsEl.replaceChildren(
      icon("warning"),
      el(
        "div.settings-errors-text",
        {},
        el("strong", { text: errors.length === 1 ? "A settings file couldn't be read" : "Settings files couldn't be read" }),
        ...errors.map((text) => el("span", { text }))
      )
    );
  }

  function renderActions() {
    renderSettingsErrors();
    const view = VIEWS.find((entry) => entry.id === current);
    const mod = module(current);
    titleEl.textContent = view.title;
    renderSubtitle();
    const nodes = mod && mod.actions ? mod.actions() : [];
    // A view may hand back the same nodes each time (the Library's filter
    // field); re-inserting them would take focus out of a field mid-typing.
    const same = nodes.length === actionsEl.children.length && nodes.every((node, index) => actionsEl.children[index] === node);
    if (!same) actionsEl.replaceChildren(...nodes);
  }

  function setView(id) {
    current = id;
    document.body.dataset.view = id;
    for (const view of VIEWS) {
      document.getElementById(`view-${view.id}`).hidden = view.id !== id;
    }
    renderNav();
    renderActions();
    const mod = module(id);
    if (mod && mod.render) mod.render();
    // show() is for work that belongs to opening a view (re-reading data),
    // not to every re-render -- render() also runs on each inventory refresh.
    if (mod && mod.show) mod.show();
    if (id === "assistant") LX.chat.focus();
  }

  // --- data --------------------------------------------------------------

  let retryTimer = null;

  async function refresh() {
    const result = await window.leutheria.getInventory();
    if (!result || !result.ok) {
      // The agent takes a moment to spawn and bind its port on a cold start,
      // and the "connected" event can land before this window is listening --
      // so keep trying rather than sitting on an empty Library forever.
      clearTimeout(retryTimer);
      retryTimer = setTimeout(refresh, 900);
      return;
    }
    clearTimeout(retryTimer);
    LX.state.inventory = result;
    renderNav();
    renderActions();
    const mod = module(current);
    if (mod && mod.render) mod.render();
  }

  window.leutheria.onInventoryChanged(refresh);

  function showStatus(status) {
    statusEl.dataset.state = status;
    statusEl.querySelector(".status-text").textContent = status;
  }

  window.leutheria.onStatus((status) => {
    showStatus(status);
    if (status === "connected") refresh();
  });

  // The bridge can connect before this page is listening (a fast agent, or
  // the window reopened later), so ask rather than wait for an event -- BUGS.md #18.
  window.leutheria.getStatus().then(showStatus);

  // --- input modality ---------------------------------------------------
  // Text fields match :focus-visible however they got focus, so the composer
  // can't rely on it. Track how the user is moving instead.
  document.body.dataset.modality = "pointer";
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Tab" || event.key.startsWith("Arrow") || event.key === "/") document.body.dataset.modality = "keyboard";
    },
    true
  );
  document.addEventListener("pointerdown", () => (document.body.dataset.modality = "pointer"), true);

  // --- theme -------------------------------------------------------------

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("leutheria-theme", theme);
    clear(themeButton).appendChild(icon(theme === "dark" ? "sun" : "moon"));
  }

  themeButton.addEventListener("click", () =>
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark")
  );

  // --- boot --------------------------------------------------------------

  LX.app = { renderActions, renderSubtitle, currentView: () => current, refresh };

  setTheme(localStorage.getItem("leutheria-theme") || "dark");
  setView("assistant");
  refresh();
})(window.LX);
