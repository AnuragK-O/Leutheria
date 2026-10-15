/* The Library view: every capability Leutheria has, in a master/detail pane.
   The list is the index -- grouped by what a capability is for
   (library-groups.js), filterable by name, description, kind and state, and
   drivable from the keyboard. The detail pane is where one capability is
   turned on or off, made to ask first, inspected, or deleted. */

(function (LX) {
  const { el, clear, icon, relativeTime, formatTemplateArgs, toast, plural } = LX;
  const { GROUPS, groupOf } = LX.libraryGroups;
  const shared = LX.libraryShared;
  const inShared = () => kind === "shared";

  const listEl = document.getElementById("library-list");
  const detailEl = document.getElementById("library-detail");
  // In the skills panel (panels/panel.js) the detail is short: what it is,
  // the two switches, and a way into the full Library.
  const compact = Boolean(LX.panelMode);

  // The list pane is two parts: the state filters, which stay put, and the
  // rows, which scroll under sticky group headings.
  const filtersEl = el("div.library-filters", { role: "group", "aria-label": "Filter by state" });
  const rowsEl = el("div.library-rows", { role: "group", "aria-label": "Capabilities" });
  listEl.append(filtersEl, rowsEl);

  // The short labels are the words the row tags use ("ask", "off", "learned").
  const STATE_FILTERS = [
    { id: "asks", label: "Ask", title: "Asks before running", test: (entry) => entry.enabled && entry.requires_confirmation },
    { id: "off", label: "Off", title: "Disabled", test: (entry) => !entry.enabled },
    { id: "learned", label: "Learned", title: "Skills Leutheria learned", test: (entry) => entry.origin === "generated" },
    { id: "unused", label: "Unused", title: "Never run", test: (entry) => !entry.runs },
  ];

  const COLLAPSED_KEY = "leutheria-library-collapsed";

  let kind = "all"; // all | tool | skill | shared (library-shared.js draws that one)
  let stateFilter = null; // one of STATE_FILTERS' ids
  let query = "";
  let selected = null;
  let collapsed = new Set();
  try {
    collapsed = new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "[]"));
  } catch (_err) {
    // a remembered convenience, nothing more
  }

  function rememberCollapsed() {
    try {
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...collapsed]));
    } catch (_err) {
      // see above
    }
  }

  const terms = () => query.split(/\s+/).filter(Boolean);
  const narrowed = () => Boolean(query || stateFilter);

  // --- topbar controls ---------------------------------------------------
  // Built once and handed back on every render: rebuilding the search field
  // while someone is typing in it would drop their focus.

  const searchInput = el("input", {
    type: "search",
    placeholder: "Name or description",
    "aria-label": "Filter capabilities by name or description",
    "aria-keyshortcuts": "/",
    autocomplete: "off",
    spellcheck: "false",
    oninput: (event) => {
      query = event.target.value.trim().toLowerCase();
      renderList();
    },
    onkeydown: (event) => {
      if (event.key === "Escape") {
        if (searchInput.value) {
          searchInput.value = "";
          query = "";
          renderList();
        } else {
          focusNav(tabStop());
        }
        event.preventDefault();
      } else if (event.key === "ArrowDown") {
        focusNav(firstRow() || tabStop());
        event.preventDefault();
      } else if (event.key === "Enter") {
        // Enter opens the top match, the way a launcher does.
        const row = firstRow();
        if (row) select(row.dataset.name);
      }
    },
  });
  const searchEl = el("div.search", {}, icon("search"), searchInput, el("kbd", { text: "/", "aria-hidden": "true" }));

  const segmentedEl = el("div.segmented", { role: "group", "aria-label": "Filter by kind" });
  for (const [value, label] of [["all", "All"], ["tool", "Tools"], ["skill", "Skills"], ["shared", "Shared"]]) {
    segmentedEl.appendChild(
      el("button", {
        text: label,
        "data-kind": value,
        onclick: () => {
          kind = value;
          if (value === "shared") shared.ensureLoaded();
          render();
        },
      })
    );
  }

  function actions() {
    for (const button of segmentedEl.children) {
      const on = button.dataset.kind === kind;
      button.classList.toggle("active", on);
      button.setAttribute("aria-pressed", String(on));
    }
    return inShared() ? [segmentedEl, searchEl, refreshEl] : [segmentedEl, searchEl];
  }

  // Shared only: fetch and verify the index again.
  const refreshEl = el(
    "button.icon-button.shared-refresh",
    { title: "Check the registry again", "aria-label": "Check the registry again", onclick: () => shared.load(true) },
    icon("refresh")
  );

  // --- data --------------------------------------------------------------

  function allEntries() {
    const inventory = LX.state.inventory;
    if (!inventory) return [];
    return [...inventory.tools, ...inventory.skills];
  }

  function matchesQuery(entry) {
    const haystack = `${entry.name} ${entry.description}`.toLowerCase();
    return terms().every((term) => haystack.includes(term));
  }

  // `ignoreState` is for the state filters' own counts: each shows how many
  // it would leave, given the kind and the query but not the other filters.
  function visibleEntries(ignoreState) {
    const state = !ignoreState && STATE_FILTERS.find((entry) => entry.id === stateFilter);
    return allEntries().filter(
      (entry) => (kind === "all" || entry.kind === kind) && (!state || state.test(entry)) && (!query || matchesQuery(entry))
    );
  }

  function find(name) {
    return allEntries().find((entry) => entry.name === name) || null;
  }

  // --- text helpers ------------------------------------------------------

  /** `text` as nodes, with every occurrence of a search term wrapped in <mark>. */
  function highlighted(text) {
    const wanted = terms();
    if (!wanted.length) return [text];
    const lower = text.toLowerCase();
    const nodes = [];
    let index = 0;
    while (index < text.length) {
      let at = -1;
      let length = 0;
      for (const term of wanted) {
        const found = lower.indexOf(term, index);
        if (found !== -1 && (at === -1 || found < at)) {
          at = found;
          length = term.length;
        }
      }
      if (at === -1) break;
      if (at > index) nodes.push(text.slice(index, at));
      nodes.push(el("mark", { text: text.slice(at, at + length) }));
      index = at + length;
    }
    if (index < text.length) nodes.push(text.slice(index));
    return nodes;
  }

  /** The stretch of a description around the first search hit, for a row. */
  function excerpt(description) {
    const lower = description.toLowerCase();
    const hits = terms()
      .map((term) => lower.indexOf(term))
      .filter((at) => at !== -1);
    if (!hits.length) return null;
    const at = Math.min(...hits);
    if (at < 28) return description;
    // Back up to a word boundary a few words before the hit.
    const start = description.lastIndexOf(" ", at - 18) + 1;
    return `…${description.slice(start)}`;
  }

  // Descriptions are written for the model: one sentence on what the thing
  // does, then when to use it and when not to. Split on sentence ends so the
  // first can be shown as the summary. Nothing is reworded or dropped.
  function sentences(text) {
    return String(text || "")
      .replace(/\s+/g, " ")
      .trim()
      .split(/(?<=[.!?])(?<!\b(?:e\.g|i\.e|vs|etc)\.)\s+(?=[A-Z0-9'"`(])/)
      .filter(Boolean);
  }

  /** A sentence as nodes, with the names of other capabilities made into links. */
  function linked(text, self) {
    const names = new Set(allEntries().map((entry) => entry.name));
    const nodes = [];
    let index = 0;
    for (const match of text.matchAll(/[a-z][a-z0-9]*(?:_[a-z0-9]+)+/g)) {
      if (!names.has(match[0]) || match[0] === self) continue;
      if (match.index > index) nodes.push(text.slice(index, match.index));
      nodes.push(
        el("button.ref-link", {
          text: match[0],
          title: `Show ${match[0]}`,
          onclick: () => reveal(match[0]),
        })
      );
      index = match.index + match[0].length;
    }
    if (index < text.length) nodes.push(text.slice(index));
    return nodes;
  }

  // --- list --------------------------------------------------------------

  function renderFilters() {
    const pool = visibleEntries(true);
    clear(filtersEl);
    for (const entry of STATE_FILTERS) {
      const count = pool.filter(entry.test).length;
      const on = stateFilter === entry.id;
      filtersEl.appendChild(
        el(
          "button.filter-chip",
          {
            class: on ? "active" : "",
            "data-filter": entry.id,
            "aria-pressed": String(on),
            "aria-label": `${entry.title}: ${count}`,
            title: entry.title,
            disabled: !count && !on,
            onclick: () => {
              stateFilter = on ? null : entry.id;
              render();
              const again = filtersEl.querySelector(`[data-filter="${entry.id}"]`);
              if (again) again.focus();
            },
          },
          el("span.filter-label", { text: entry.label }),
          el("span.filter-count", { text: String(count) })
        )
      );
    }
  }

  function renderList() {
    const entries = visibleEntries();
    const hadFocus = rowsEl.contains(document.activeElement) ? document.activeElement.dataset.nav : null;
    const scrollTop = rowsEl.scrollTop;

    if (inShared()) {
      filtersEl.classList.add("library-filters-shared");
      shared.renderList(filtersEl, rowsEl, {
        terms: terms(),
        selected,
        highlighted,
        select,
        clearFilters,
        isOpen: (id) => !collapsed.has(id),
        toggleGroup: (id) => toggleGroup(id),
      });
      finishList(hadFocus, scrollTop);
      return;
    }
    filtersEl.setAttribute("aria-label", "Filter by state");
    filtersEl.classList.remove("library-filters-shared");
    renderFilters();
    clear(rowsEl);

    if (!entries.length) {
      rowsEl.appendChild(
        el(
          "div.list-none",
          { role: "status" },
          el("div.empty", { text: narrowed() || kind !== "all" ? "No matches." : "Nothing here yet." }),
          (narrowed() || kind !== "all") &&
            el("button.link-button", { text: "Clear filters", onclick: clearFilters })
        )
      );
    }

    const byGroup = new Map();
    for (const entry of entries) {
      const id = groupOf(entry);
      if (!byGroup.has(id)) byGroup.set(id, []);
      byGroup.get(id).push(entry);
    }

    for (const group of GROUPS) {
      const members = byGroup.get(group.id);
      if (!members) continue;
      members.sort((a, b) => a.name.localeCompare(b.name));
      // A search or a state filter opens every group it has results in: a
      // match hidden inside a collapsed group would look like no match.
      const open = narrowed() || !collapsed.has(group.id);

      const body = el("div.list-group-body", { id: `library-group-${group.id}`, hidden: !open });
      for (const entry of members) body.appendChild(renderRow(entry));

      rowsEl.appendChild(
        el(
          "div.list-group",
          {},
          el(
            "button.list-group-label",
            {
              "data-nav": `group:${group.id}`,
              "data-group": group.id,
              "aria-expanded": String(open),
              "aria-controls": body.id,
              tabindex: "-1",
              title: group.hint,
              onclick: () => toggleGroup(group.id),
            },
            el("span.list-group-caret", { "aria-hidden": "true" }),
            el("span.list-group-name", { text: group.label }),
            el("span.list-group-count", { text: String(members.length), "aria-label": plural(members.length, "item") })
          ),
          body
        )
      );
    }

    finishList(hadFocus, scrollTop);
  }

  function finishList(hadFocus, scrollTop) {
    rowsEl.scrollTop = scrollTop;
    setTabStop();
    if (hadFocus) {
      const again = rowsEl.querySelector(`[data-nav="${CSS.escape(hadFocus)}"]`);
      if (again && !again.closest("[hidden]")) again.focus();
    }
    if (LX.app) LX.app.renderSubtitle();
  }

  function renderRow(entry) {
    const flags = el("span.list-row-flags");
    // Only what's out of the ordinary gets a tag, so the column scans.
    if (entry.origin === "generated") flags.appendChild(el("span.badge.badge-accent", { text: "learned" }));
    if (entry.origin === "imported") {
      const status = shared.statusOf(entry.name);
      if (status && status.withdrawn) flags.appendChild(el("span.badge.badge-danger", { text: "withdrawn", title: "Withdrawn from the registry" }));
      else if (status && status.update) flags.appendChild(el("span.badge.badge-warning", { text: "update", title: "A newer version is in the registry" }));
      else flags.appendChild(el("span.badge", { text: "shared", title: "Installed from the registry" }));
    }
    if (!entry.enabled) flags.appendChild(el("span.badge.badge-off", { text: "off", title: "Disabled" }));
    else if (entry.requires_confirmation)
      flags.appendChild(el("span.badge.badge-warning", { text: "ask", title: "Asks before running" }));

    // The description is long and written for the model, so a row is the name
    // alone -- unless a search matched inside the description, when the row
    // shows where.
    const nameHit = terms().some((term) => entry.name.toLowerCase().includes(term));
    const snippet = query && !nameHit ? excerpt(entry.description) : null;

    return el(
      "button.list-row",
      {
        class: [entry.name === selected ? "active" : "", entry.enabled ? "" : "disabled"].filter(Boolean).join(" "),
        "data-nav": `row:${entry.name}`,
        "data-name": entry.name,
        "data-kind": entry.kind,
        "aria-current": entry.name === selected ? "true" : null,
        "aria-controls": "library-detail",
        tabindex: "-1",
        title: sentences(entry.description)[0] || "",
        onclick: () => select(entry.name),
      },
      el(
        "span.list-row-icon",
        { title: entry.kind },
        icon(entry.kind === "skill" ? "sparkle" : "tool"),
        el("span.sr-only", { text: `${entry.kind}: ` })
      ),
      el(
        "span.list-row-text",
        {},
        el("span.list-row-name", {}, highlighted(entry.name)),
        snippet && el("span.list-row-desc", {}, highlighted(snippet))
      ),
      flags
    );
  }

  function clearFilters() {
    if (inShared()) {
      shared.clearFilter();
      query = "";
      searchInput.value = "";
      render();
      focusNav(tabStop());
      return;
    }
    kind = "all";
    stateFilter = null;
    query = "";
    searchInput.value = "";
    render();
    focusNav(tabStop());
  }

  function toggleGroup(id, open) {
    if (narrowed() || (inShared() && shared.hasFilter())) return; // every group is held open while narrowed
    const makeOpen = open === undefined ? collapsed.has(id) : open;
    if (makeOpen) collapsed.delete(id);
    else collapsed.add(id);
    rememberCollapsed();
    const label = rowsEl.querySelector(`.list-group-label[data-group="${id}"]`);
    if (!label) return;
    label.setAttribute("aria-expanded", String(makeOpen));
    label.nextElementSibling.hidden = !makeOpen;
    setTabStop();
  }

  // --- selection and keyboard ----------------------------------------------
  // One tab stop for the whole list (a roving tabindex); arrows move within it
  // and the detail pane follows the row that has focus.

  const navItems = () => [...rowsEl.querySelectorAll("[data-nav]")].filter((node) => !node.closest("[hidden]"));
  const firstRow = () => navItems().find((node) => node.classList.contains("list-row")) || null;

  function tabStop() {
    const items = navItems();
    return items.find((node) => node.dataset.name === selected) || items[0] || null;
  }

  function setTabStop(node) {
    const stop = node || tabStop();
    for (const item of rowsEl.querySelectorAll("[data-nav]")) item.tabIndex = item === stop ? 0 : -1;
  }

  function focusNav(node) {
    if (!node) return;
    setTabStop(node);
    node.focus();
    node.scrollIntoView({ block: "nearest" });
  }

  /** Show `name` in the detail pane. The list is updated in place. */
  function select(name) {
    selected = name;
    for (const row of rowsEl.querySelectorAll(".list-row")) {
      const on = row.dataset.name === name;
      row.classList.toggle("active", on);
      if (on) row.setAttribute("aria-current", "true");
      else row.removeAttribute("aria-current");
    }
    renderDetail();
    detailEl.scrollTop = 0;
  }

  /** Select `name` even if a filter or a collapsed group is hiding it. */
  function reveal(name) {
    // "shared:<name>": a registry entry, from the skills panel.
    if (String(name).startsWith("shared:")) {
      kind = "shared";
      query = "";
      searchInput.value = "";
      selected = String(name).slice(7);
      shared.ensureLoaded();
      render();
      return;
    }
    const entry = find(name);
    if (!entry) return;
    if (!visibleEntries().includes(entry)) {
      kind = "all";
      stateFilter = null;
      query = "";
      searchInput.value = "";
    }
    collapsed.delete(groupOf(entry));
    rememberCollapsed();
    selected = name;
    render();
    detailEl.scrollTop = 0;
    focusNav(rowsEl.querySelector(`[data-nav="row:${CSS.escape(name)}"]`));
  }

  rowsEl.addEventListener("keydown", (event) => {
    const here = event.target.closest("[data-nav]");
    if (!here || event.metaKey || event.ctrlKey || event.altKey) return;
    const items = navItems();
    const index = items.indexOf(here);
    const isGroup = here.classList.contains("list-group-label");
    let target = null;

    switch (event.key) {
      case "ArrowDown":
        target = items[Math.min(items.length - 1, index + 1)];
        break;
      case "ArrowUp":
        target = items[Math.max(0, index - 1)];
        break;
      case "Home":
        target = items[0];
        break;
      case "End":
        target = items[items.length - 1];
        break;
      case "ArrowLeft":
        if (isGroup) toggleGroup(here.dataset.group, false);
        else target = here.closest(".list-group").querySelector(".list-group-label");
        break;
      case "ArrowRight":
        if (isGroup && here.getAttribute("aria-expanded") === "false") toggleGroup(here.dataset.group, true);
        else if (isGroup) target = items[index + 1];
        break;
      default:
        return;
    }

    event.preventDefault();
    if (!target || target === here) return;
    focusNav(target);
    if (target.classList.contains("list-row")) select(target.dataset.name);
  });

  rowsEl.addEventListener("focusin", (event) => {
    const here = event.target.closest("[data-nav]");
    if (here) setTabStop(here);
  });

  // "/" from anywhere in the view jumps to the filter.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    if (!LX.app || LX.app.currentView() !== "library") return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
    event.preventDefault();
    searchInput.focus();
    searchInput.select();
  });

  // --- detail ------------------------------------------------------------

  function badges(entry) {
    const list = [el("span.badge", { text: entry.kind })];
    if (entry.kind === "skill") {
      list.push(
        el("span.badge", {
          class: entry.origin === "generated" ? "badge-accent" : "",
          text: entry.origin === "generated" ? "learned" : entry.origin === "imported" ? "shared" : "built-in",
        })
      );
    } else if (entry.safety === "destructive") {
      list.push(el("span.badge.badge-warning", { text: "destructive", title: "Unbounded: can change or remove data" }));
    } else if (entry.safety === "scoped") {
      list.push(el("span.badge.badge-warning", { text: "controls apps", title: "Scoped: types into another app, per-app grant" }));
    }
    if (!entry.enabled) list.push(el("span.badge.badge-off", { text: "disabled" }));
    return list;
  }

  async function apply(name, changes) {
    const result = await window.leutheria.setPreference(name, changes);
    if (!result.ok) toast(result.error || "Couldn't save that change", "error");
    return result.ok;
  }

  let settingId = 0;

  function settingRow(key, title, description, checked, onToggle) {
    const id = `library-setting-${++settingId}`;
    const toggle = el("button.switch", {
      role: "switch",
      "aria-checked": String(checked),
      "aria-labelledby": `${id}-title`,
      "aria-describedby": `${id}-desc`,
      "data-focus": key,
      onclick: () => onToggle(!checked),
    });
    return el(
      "div.setting",
      {},
      el(
        "div.setting-text",
        {},
        el("strong", { id: `${id}-title`, text: title }),
        el("span", { id: `${id}-desc`, text: description })
      ),
      toggle
    );
  }

  // With nothing selected the pane is the catalog at a glance, and how to
  // drive the list.
  function renderOverview() {
    const entries = allEntries();
    const count = (test) => String(entries.filter(test).length);
    const stat = (value, label) =>
      el("div.stat", {}, el("div.stat-value", { text: value }), el("div.stat-label", { text: label }));

    return el(
      "div.detail",
      {},
      el(
        "div.detail-head",
        {},
        el("div.section-label", { text: "Nothing selected" }),
        el("p.detail-summary", {
          text: "Pick a capability to see what it does, turn it off, or make it ask before it runs.",
        })
      ),
      el(
        "section",
        {},
        el("div.section-label", { text: "Catalog" }),
        el(
          "div.stat-grid.stat-grid-3",
          {},
          stat(count((entry) => entry.kind === "tool"), "tools"),
          stat(count((entry) => entry.kind === "skill"), "skills"),
          stat(count((entry) => entry.origin === "generated"), "learned"),
          stat(count((entry) => entry.enabled && entry.requires_confirmation), "ask first"),
          stat(count((entry) => !entry.enabled), "disabled"),
          stat(count((entry) => !entry.runs), "never used")
        )
      ),
      el(
        "section",
        {},
        el("div.section-label", { text: "Keyboard" }),
        el(
          "div.key-list",
          {},
          el("kbd", { text: "/" }),
          el("span", { text: "Filter by name or description" }),
          el("kbd", { text: "↑ ↓" }),
          el("span", { text: "Move through the list; the details follow" }),
          el("kbd", { text: "← →" }),
          el("span", { text: "Collapse or expand a group" }),
          el("kbd", { text: "Esc" }),
          el("span", { text: "Clear the filter" })
        )
      )
    );
  }

  function renderDetail() {
    // A re-render (after a toggle, say) must not drop keyboard focus: put it
    // back on the control that had it.
    const focusKey = detailEl.contains(document.activeElement) ? document.activeElement.dataset.focus : null;

    if (inShared()) {
      shared.renderDetail(detailEl, selected, compact);
      return;
    }
    clear(detailEl);
    const entry = selected ? find(selected) : null;

    if (!entry) {
      if (compact) return; // the panel's list is the whole view until something is picked
      if (allEntries().length) detailEl.appendChild(renderOverview());
      else detailEl.appendChild(el("div.empty", { text: "Waiting for the agent's catalog." }));
      return;
    }

    const detail = el("div.detail");

    const [summary, ...guidance] = sentences(entry.description);
    detail.appendChild(
      el(
        "div.detail-head",
        {},
        el("div.detail-title", {}, el("h2", { text: entry.name }), ...badges(entry)),
        el("p.detail-summary", {}, linked(summary || "No description.", entry.name))
      )
    );

    // The rest of the description: what the model is told about when to use
    // it. Set as one line per sentence, in the secondary tier.
    if (guidance.length && !compact) {
      detail.appendChild(
        el(
          "section",
          {},
          el("div.section-label", { text: "When Leutheria uses it" }),
          el(
            "div.guidance",
            {},
            guidance.map((line) => el("p", {}, linked(line, entry.name)))
          )
        )
      );
    }

    // --- controls ---
    const controls = el("div.card");
    controls.appendChild(
      settingRow(
        "enabled",
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
        "confirm",
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
            "data-focus": "reset",
            onclick: () => apply(entry.name, { reset: true }),
          })
        )
      );
    }

    detail.appendChild(el("section", {}, el("div.section-label", { text: "Permissions" }), controls));

    if (compact) {
      detail.appendChild(
        el("button.link-button.detail-open", {
          text: "Open in the Library",
          "data-focus": "open",
          onclick: () => window.leutheria.openMain("library", entry.name),
        })
      );
      detailEl.appendChild(detail);
      if (focusKey) {
        const again = detailEl.querySelector(`[data-focus="${focusKey}"]`) || detailEl.querySelector(".switch");
        if (again) again.focus();
      }
      return;
    }

    // --- parameters ---
    const properties = (entry.input_schema && entry.input_schema.properties) || {};
    const required = (entry.input_schema && entry.input_schema.required) || [];
    const names = Object.keys(properties);
    let params;
    if (names.length) {
      params = el("div.card.params");
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
      // A null value in a readout, not a box around a sentence.
      params = el("div.empty.empty-inline", { text: "Takes no parameters." });
    }
    detail.appendChild(
      el("section", {}, el("div.section-label", { text: names.length ? `Parameters / ${names.length}` : "Parameters" }), params)
    );

    // --- steps (learned skills only) ---
    if (entry.steps && entry.steps.length) {
      const steps = el("div.card.steps");
      entry.steps.forEach((step, index) => {
        steps.appendChild(
          el(
            "div.step",
            {},
            el("div.step-index", { text: String(index + 1).padStart(2, "0") }),
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
          el("div.section-label", { text: `Steps / ${entry.steps.length}` }),
          steps,
          el("p", {
            class: "section-note",
            text: "A learned skill is data, not code — each step is dispatched through the same safety checks as anything else.",
          })
        )
      );
    }

    // --- usage ---
    detail.appendChild(
      el(
        "section",
        {},
        el("div.section-label", { text: "Usage" }),
        el(
          "div.stat-grid",
          {},
          el("div.stat", {}, el("div.stat-value", { text: String(entry.runs) }), el("div.stat-label", { text: "times run" })),
          el(
            "div.stat",
            {},
            el("div.stat-value", { text: String(entry.failures) }),
            el("div.stat-label", { text: "failed or declined" })
          ),
          el(
            "div.stat",
            {},
            el("div.stat-value", { text: relativeTime(entry.last_used) }),
            el("div.stat-label", { text: "last used" })
          ),
          entry.created_at &&
            el(
              "div.stat",
              {},
              el("div.stat-value", { text: relativeTime(entry.created_at) }),
              el("div.stat-label", { text: "learned" })
            )
        )
      )
    );

    // --- where a shared skill came from ---
    if (entry.origin === "imported") detail.appendChild(provenance(entry));

    // --- share (a skill learned here) ---
    if (entry.origin === "generated") detail.appendChild(shared.shareSection(entry, renderDetail));

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
            el("button.btn.btn-danger", { text: "Delete", "data-focus": "delete", onclick: () => confirmDelete(entry) })
          )
        )
      );
    }

    detailEl.appendChild(detail);

    if (focusKey) {
      const again = detailEl.querySelector(`[data-focus="${focusKey}"]`) || detailEl.querySelector(".switch");
      if (again) again.focus();
    }
  }

  function provenance(entry) {
    const info = entry.registry || {};
    const status = shared.statusOf(entry.name);
    return el(
      "section",
      {},
      el("div.section-label", { text: "From the registry" }),
      el(
        "div.stat-grid.stat-grid-3",
        {},
        el("div.stat", {}, el("div.stat-value", { text: info.author || "--" }), el("div.stat-label", { text: "shared by" })),
        el("div.stat", {}, el("div.stat-value", { text: info.version != null ? `v${info.version}` : "--" }), el("div.stat-label", { text: "version" })),
        el(
          "div.stat",
          {},
          el("div.stat-value", { text: info.review && info.review.risk ? info.review.risk : "--" }),
          el("div.stat-label", { text: "screened risk" })
        )
      ),
      el(
        "p.section-note",
        {},
        status && status.withdrawn
          ? "Withdrawn from the registry. It still works here until you delete it. "
          : status && status.update
            ? `Version ${status.latest.version} is available; updating is a new approval. `
            : "",
        info.first_run_pending ? "Every step asks first until it has run once. " : "",
        "Its run_command steps ask on every run. ",
        el("button.link-button", { text: "Show in Shared", "data-focus": "show-shared", onclick: () => reveal(`shared:${entry.name}`) })
      )
    );
  }

  function confirmDelete(entry) {
    const zone = detailEl.querySelector(".danger-zone");
    if (!zone) return;
    // Cancel puts focus back on Delete (renderDetail restores by data-focus).
    const cancel = el("button.btn", { text: "Cancel", "data-focus": "delete", onclick: renderDetail });
    zone.setAttribute("role", "alertdialog");
    zone.setAttribute("aria-label", `Delete ${entry.name}?`);
    clear(zone).append(
      el(
        "div.setting-text",
        {},
        el("strong", { text: `Delete "${entry.name}"?` }),
        el("span", { text: "This can't be undone." })
      ),
      cancel,
      el("button.btn.btn-danger", {
        text: "Delete skill",
        onclick: async () => {
          const result = await window.leutheria.deleteSkill(entry.name);
          if (result.ok) {
            selected = null;
            toast(`Deleted "${entry.name}"`);
            // The pane is about to be replaced; keep the keyboard in the list.
            focusNav(tabStop());
          } else {
            toast(result.error || "Couldn't delete that skill", "error");
          }
        },
      })
    );
    cancel.focus();
  }

  // --- entry point -------------------------------------------------------

  function render() {
    // Drop a selection that no longer exists (a deleted skill) or that the
    // current filter hides, rather than leaving a stale detail pane up.
    // Imported skills' rows say "update" or "withdrawn" from the index.
    if (allEntries().some((entry) => entry.origin === "imported")) shared.ensureLoaded();
    if (selected && !inShared() && !visibleEntries().some((entry) => entry.name === selected)) selected = null;
    renderList();
    renderDetail();
    LX.app.renderActions();
  }

  function count() {
    const inventory = LX.state.inventory;
    return inventory ? inventory.tools.length + inventory.skills.length : null;
  }

  function subtitle() {
    if (inShared()) return shared.subtitle(terms());
    const entries = allEntries();
    if (!entries.length) return "Everything Leutheria can do";
    const label = entries.length === 1 ? "capability" : "capabilities";
    if (narrowed() || kind !== "all") return `${visibleEntries().length} of ${entries.length} ${label} shown`;
    const off = entries.filter((entry) => !entry.enabled).length;
    return `${entries.length} ${label}${off ? ` · ${off} disabled` : ""}`;
  }

  /** Pre-fill the filter (a panel opened with "show me my file skills"). */
  function setQuery(text) {
    searchInput.value = String(text || "");
    query = searchInput.value.trim().toLowerCase();
    stateFilter = null;
    kind = "all";
    render();
  }

  // The registry answers asynchronously; redraw when it does (and only when
  // what it changed is on screen).
  shared.bind(() => {
    if (!LX.app || LX.app.currentView() !== "library") return;
    if (inShared()) render();
    else {
      // imported rows' "update" / "withdrawn" tags come from the index
      renderList();
      if (selected) renderDetail();
    }
  });

  LX.library = { render, actions, count, subtitle, setQuery, reveal };
})(window.LX);
