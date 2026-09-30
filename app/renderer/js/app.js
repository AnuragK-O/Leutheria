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

  function renderNav() {
    clear(navEl);
    for (const view of VIEWS) {
      const count = module(view.id) && module(view.id).count ? module(view.id).count() : null;
      navEl.appendChild(
        el(
          "button.nav-item",
          { class: view.id === current ? "active" : "", onclick: () => setView(view.id) },
          icon(view.id),
          el("span", { text: view.label }),
          count !== null && count !== undefined ? el("span.nav-count", { text: String(count) }) : null
        )
      );
    }
  }

  function renderActions() {
    const view = VIEWS.find((entry) => entry.id === current);
    const mod = module(current);
    titleEl.textContent = view.title;
    subtitleEl.textContent = (mod && mod.subtitle ? mod.subtitle() : view.subtitle) || "";
    clear(actionsEl);
    if (mod && mod.actions) {
      for (const node of mod.actions()) actionsEl.appendChild(node);
    }
  }

  function setView(id) {
    current = id;
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

  LX.app = { renderActions, currentView: () => current, refresh };

  setTheme(localStorage.getItem("leutheria-theme") || "dark");
  setView("assistant");
  refresh();
})(window.LX);
