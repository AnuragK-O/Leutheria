/* The Library's Shared area: skills other people published to the public
   registry (Discover), the ones installed here (Installed), and the share
   flow for a skill Leutheria learned on this Mac.

   library.js owns the panes, the search field and the keyboard; when its
   kind filter is "shared" it hands the list and the detail to this module.
   The same file runs in the skills panel (LX.panelMode), so the install card
   is the same card there.

   What this module may and may not do (plans/skill-registry-spec.md):
   - Nothing installs without the install card: every step with every
     argument in full, run_command steps marked, author and review shown.
     registry_install is sent only from that card's own button.
   - An index that failed verification is a security state, not a network
     error: it's shown in red, by name, and nothing from it is listed.
   - The agent enforces all of this again; the UI is the place it's seen. */

(function (LX) {
  const { el, clear, icon, toast, plural, relativeTime } = LX;
  const api = window.leutheria;

  // --- state -----------------------------------------------------------------

  let data = null; // the last registry_list result
  let loading = false;
  let loadError = null; // the bridge itself failed (not connected, timed out)
  let filter = null; // one of FILTERS' ids
  const previews = new Map(); // "name@hash" -> { state: "loading" | "ok" | "error", result }
  const installs = new Map(); // "name@hash" -> { state: "sending" | "done" | "error", result }
  const shares = new Map(); // learned skill name -> share flow state
  let onChange = () => {};

  const FILTERS = [
    { id: "available", label: "Available", title: "Shared skills you haven't installed", test: (item) => !item.installed },
    { id: "installed", label: "Installed", title: "Shared skills installed here", test: (item) => item.installed },
    { id: "updates", label: "Updates", title: "Installed, with a newer version", test: (item) => item.update },
    { id: "withdrawn", label: "Withdrawn", title: "Installed, but withdrawn from the registry", test: (item) => item.withdrawn },
  ];

  // The index problems the agent reports. Two of them are about trust, not
  // connectivity, and are drawn as such.
  const INDEX_ERRORS = {
    verification_failed: {
      security: true,
      title: "Index refused: signature didn't verify",
      short: "The registry index failed its signature check. Nothing from it is shown or installed.",
    },
    no_signing_key: {
      security: true,
      title: "Index refused: no registry key configured",
      short: "There's no key to check the registry's signature with, so nothing from it is shown or installed.",
    },
    unavailable: {
      security: false,
      title: "Couldn't reach the registry",
      short: "The index couldn't be downloaded. Installed shared skills still work.",
    },
  };

  // registry_list's shape is in README ("Shared skills"): index_ok false
  // with an error_code means the agent answered and refused the index; a
  // stale index (registry unreachable, the last verified one shown) is
  // index_ok true with stale and error_code "unavailable", and isn't a
  // problem that hides anything.
  function indexProblem() {
    if (loadError) return { code: "unavailable", detail: loadError, ...INDEX_ERRORS.unavailable };
    if (!data || data.index_ok) return null;
    // An unknown or missing code is read the cautious way round.
    const code = INDEX_ERRORS[data.error_code] ? data.error_code : "verification_failed";
    return { code, detail: data.error || "", ...INDEX_ERRORS[code] };
  }

  // --- loading -----------------------------------------------------------------

  async function load(refresh) {
    if (loading) return;
    loading = true;
    onChange();
    let result;
    try {
      result = await api.registryList(Boolean(refresh));
    } catch (err) {
      result = { ok: false, error: err.message };
    }
    loading = false;
    // An index the agent refused is still an answer (ok false, index_ok
    // false, error_code): it's drawn as that state, not as "unreachable".
    if (result && (result.ok || result.index_ok === false)) {
      data = result;
      loadError = null;
    } else {
      loadError = (result && result.error) || "The agent didn't answer.";
    }
    onChange();
  }

  // --- the model: one item per skill name ----------------------------------------

  const imported = () => ((LX.state.inventory && LX.state.inventory.skills) || []).filter((skill) => skill.origin === "imported");
  const toolsOf = (steps) => [...new Set((steps || []).map((step) => step.tool))];

  // An installed skill described by its registry record (registry_list's
  // `installed`, or the inventory's `registry`): the index lists only the
  // newest live version of each name, so an installed older or withdrawn
  // version is known only from here.
  function entryFromRecord(name, rec, skill) {
    const requires = (skill && (skill.requires || toolsOf(skill.steps))) || [];
    return {
      name,
      version: rec.version ?? null,
      content_hash: rec.content_hash || null,
      author: rec.author || "unknown",
      summary: rec.summary || (skill && skill.description) || "",
      requires,
      has_run_command: rec.has_run_command != null ? Boolean(rec.has_run_command) : requires.includes("run_command"),
      review: rec.review || null,
      published_at: rec.published_at || null,
      yanked: Boolean(rec.withdrawn),
      fromRecord: true,
    };
  }

  function items() {
    // skills: the newest non-withdrawn version of each name; installed: one
    // record per installed name, with withdrawn / update_available decided
    // by the agent against the verified index.
    const latestByName = new Map(((data && data.index_ok && data.skills) || []).map((entry) => [entry.name, entry]));
    const records = new Map(((data && data.installed) || []).map((rec) => [rec.name, rec]));
    const local = new Map(imported().map((skill) => [skill.name, skill]));
    const names = new Set([...latestByName.keys(), ...records.keys(), ...local.keys()]);
    const list = [];
    for (const name of names) {
      const latest = latestByName.get(name) || null;
      const skill = local.get(name) || null;
      const listedRec = records.get(name) || null;
      const rec = listedRec || (skill && skill.registry) || null;
      const installed = Boolean(skill || listedRec);
      if (!installed && !latest) continue;
      const installedVersion = rec && rec.version != null ? rec.version : null;
      const withdrawn = installed && Boolean(listedRec && listedRec.withdrawn);
      const update =
        installed && Boolean(latest) && (listedRec ? Boolean(listedRec.update_available) : installedVersion != null && latest.version > installedVersion);
      const current = installed && latest && rec && latest.content_hash === rec.content_hash;
      const entry = installed ? (current ? latest : rec ? entryFromRecord(name, rec, skill) : latest) : latest;
      list.push({ name, entry, latest, skill, installed, installedVersion, withdrawn, update });
    }
    return list.sort((a, b) => a.name.localeCompare(b.name));
  }

  const installedCount = () => (data && data.installed ? data.installed.length : imported().length);
  const importedMax = () => (data && data.imported_max != null ? data.imported_max : null);

  function matches(item, terms) {
    const entry = item.entry;
    const haystack = `${item.name} ${entry.summary || ""} ${entry.author || ""} ${(entry.requires || []).join(" ")}`.toLowerCase();
    return terms.every((term) => haystack.includes(term));
  }

  function visible(terms, ignoreFilter) {
    const active = !ignoreFilter && FILTERS.find((entry) => entry.id === filter);
    return items().filter((item) => (!active || active.test(item)) && matches(item, terms));
  }

  const find = (name) => items().find((item) => item.name === name) || null;

  // --- small pieces ------------------------------------------------------------------

  const RISK_CLASS = { low: "", medium: "badge-warning", high: "badge-danger" };
  const VERDICT = { auto_approved: "AI screened", human_approved: "Human reviewed" };

  function riskBadge(review, long) {
    if (!review || !review.risk) return el("span.badge.badge-off", { text: long ? "not reviewed" : "unknown" });
    return el("span.badge", {
      class: RISK_CLASS[review.risk] || "",
      text: long ? `${review.risk} risk` : review.risk,
      title: `Screening rated it ${review.risk} risk`,
    });
  }

  function commandMark() {
    return el(
      "span.shared-cmd",
      { title: "Has run_command steps: each one asks before every run", "aria-label": "has run_command steps" },
      icon("logs")
    );
  }

  /** Every argument in full, one per line -- never truncated on the install card. */
  function fullArgs(args) {
    const node = el("div.step-args.step-args-full");
    for (const [key, value] of Object.entries(args || {})) {
      const text = typeof value === "string" ? value : JSON.stringify(value);
      const line = el("div.arg-line", {}, el("span.arg-key", { text: key }));
      const valueEl = el("span.arg-value");
      let index = 0;
      for (const match of text.matchAll(/\{(\w+)\}/g)) {
        if (match.index > index) valueEl.appendChild(document.createTextNode(text.slice(index, match.index)));
        valueEl.appendChild(el("em", { text: match[0] }));
        index = match.index + match[0].length;
      }
      valueEl.appendChild(document.createTextNode(text.slice(index)));
      line.appendChild(valueEl);
      node.appendChild(line);
    }
    if (!node.children.length) node.appendChild(el("div.arg-line", {}, el("span.arg-value", { text: "no arguments" })));
    return node;
  }

  // --- list ------------------------------------------------------------------------------

  /** Fills the state strip and the rows. `ctx` is library.js's: terms, selected, highlighted, isOpen. */
  function renderList(filtersEl, rowsEl, ctx) {
    const terms = ctx.terms;
    const pool = visible(terms, true);
    clear(filtersEl);
    filtersEl.setAttribute("aria-label", "Filter shared skills");
    for (const entry of FILTERS) {
      const count = pool.filter(entry.test).length;
      const on = filter === entry.id;
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
              filter = on ? null : entry.id;
              onChange();
              const again = filtersEl.querySelector(`[data-filter="${entry.id}"]`);
              if (again) again.focus();
            },
          },
          el("span.filter-label", { text: entry.label }),
          el("span.filter-count", { text: String(count) })
        )
      );
    }

    clear(rowsEl);
    const problem = indexProblem();
    if (problem) rowsEl.appendChild(indexNotice(problem, true));
    else if (data && data.stale) rowsEl.appendChild(staleNotice(true));
    else if (!data && loading) rowsEl.appendChild(el("div.list-none", { role: "status" }, el("div.empty", { text: "Loading the shared index…" })));

    const list = visible(terms);
    const narrowed = Boolean(terms.length || filter);
    if (!list.length && (data || problem)) {
      rowsEl.appendChild(
        el(
          "div.list-none",
          { role: "status" },
          el("div.empty", { text: narrowed ? "No matches." : problem ? "No shared skills installed." : "The registry has no skills yet." }),
          narrowed && el("button.link-button", { text: "Clear filters", onclick: ctx.clearFilters })
        )
      );
    }

    const groups = [
      { id: "shared-installed", label: "Installed", hint: "Shared skills installed here", members: list.filter((item) => item.installed) },
      { id: "shared-discover", label: "Discover", hint: "Published to the registry, not installed", members: list.filter((item) => !item.installed) },
    ];
    for (const group of groups) {
      if (!group.members.length) continue;
      const open = narrowed || ctx.isOpen(group.id);
      const body = el("div.list-group-body", { id: `library-group-${group.id}`, hidden: !open });
      for (const item of group.members) body.appendChild(renderRow(item, ctx));
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
              onclick: () => ctx.toggleGroup(group.id),
            },
            el("span.list-group-caret", { "aria-hidden": "true" }),
            el("span.list-group-name", { text: group.label }),
            el("span.list-group-count", { text: String(group.members.length), "aria-label": plural(group.members.length, "item") })
          ),
          body
        )
      );
    }
  }

  function renderRow(item, ctx) {
    const entry = item.entry;
    const flags = el("span.list-row-flags");
    if (item.withdrawn) flags.appendChild(el("span.badge.badge-danger", { text: "withdrawn", title: "Withdrawn from the registry" }));
    else if (item.update) flags.appendChild(el("span.badge.badge-warning", { text: "update", title: `Version ${item.latest.version} is available` }));
    // Only high risk earns a row tag; medium is on the detail, so the column scans.
    if (entry.review && entry.review.risk === "high") flags.appendChild(riskBadge(entry.review));
    if (entry.has_run_command) flags.appendChild(commandMark());

    const version = entry.version != null ? `v${item.installed && item.installedVersion != null ? item.installedVersion : entry.version}` : "";
    return el(
      "button.list-row.shared-row",
      {
        class: item.name === ctx.selected ? "active" : "",
        "data-nav": `row:${item.name}`,
        "data-name": item.name,
        "data-kind": "shared",
        "aria-current": item.name === ctx.selected ? "true" : null,
        "aria-controls": "library-detail",
        tabindex: "-1",
        title: entry.summary || "",
        onclick: () => ctx.select(item.name),
      },
      el("span.list-row-icon", { title: "shared skill" }, icon("package"), el("span.sr-only", { text: "shared skill: " })),
      el(
        "span.list-row-text",
        {},
        el("span.list-row-name", {}, ctx.highlighted(item.name)),
        el("span.list-row-desc.shared-row-meta", {}, ctx.highlighted(entry.author || "unknown"), version ? ` · ${version}` : "")
      ),
      flags
    );
  }

  // --- index state -------------------------------------------------------------------------

  function indexNotice(problem, short) {
    const kind = problem.security ? "callout-danger shared-index-refused" : "callout-neutral";
    return el(
      `div.callout.shared-index.${kind.split(" ").join(".")}`,
      { role: problem.security ? "alert" : "status", "data-index": problem.code },
      icon(problem.security ? "shield-x" : "info"),
      el(
        "div.shared-index-text",
        {},
        el("strong", { text: problem.title }),
        el("span", { text: problem.short }),
        !short && problem.detail && el("code.shared-index-detail", { text: problem.detail }),
        !short &&
          problem.security &&
          el("span", {
            text:
              "This is what a tampered or substituted index looks like, so it's treated as one. Installed shared skills keep working; updates and withdrawals can't be checked until a verified index loads.",
          }),
        el("button.link-button", { text: loading ? "Checking…" : "Try again", "data-focus": "retry", onclick: () => load(true) })
      )
    );
  }

  // The registry couldn't be reached; the agent showed the last index it
  // verified. Neutral: nothing here was refused.
  function staleNotice(short) {
    return el(
      "div.callout.shared-index.callout-neutral",
      { role: "status", "data-index": "stale" },
      icon("info"),
      el(
        "div.shared-index-text",
        {},
        el("strong", { text: "Showing the last verified index" }),
        el("span", { text: "The registry couldn't be reached just now, so this is the list from the last check." }),
        !short && data.error && el("code.shared-index-detail", { text: data.error }),
        el("button.link-button", { text: loading ? "Checking…" : "Try again", "data-focus": "retry", onclick: () => load(true) })
      )
    );
  }

  // --- detail ----------------------------------------------------------------------------

  function renderDetail(detailEl, selected, compact) {
    const focusKey = detailEl.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
    clear(detailEl);
    const item = selected ? find(selected) : null;
    if (!item) {
      if (!compact) detailEl.appendChild(renderOverview());
      return;
    }
    const detail = el("div.detail.shared-detail");
    const entry = item.entry;

    const badges = [el("span.badge", { text: "shared" })];
    if (item.installed) badges.push(el("span.badge", { text: item.installedVersion != null ? `installed v${item.installedVersion}` : "installed" }));
    else if (entry.version != null) badges.push(el("span.badge", { text: `v${entry.version}` }));
    if (item.withdrawn) badges.push(el("span.badge.badge-danger", { text: "withdrawn" }));
    if (item.update) badges.push(el("span.badge.badge-warning", { text: `v${item.latest.version} available` }));

    detail.appendChild(
      el(
        "div.detail-head",
        {},
        el("div.detail-title", {}, el("h2", { text: item.name }), ...badges),
        el("p.detail-summary", { text: entry.summary || "No summary." }),
        el(
          "div.shared-byline",
          {},
          el("span", {}, "by ", el("b", { text: entry.author || "unknown" })),
          entry.published_at && el("span", { text: `published ${relativeTime(Date.parse(entry.published_at) / 1000)}` })
        )
      )
    );

    if (item.withdrawn) detail.appendChild(withdrawnNotice(item));

    detail.appendChild(reviewSection(entry, compact));

    // What would be installed: the latest version for an available skill or
    // an update; nothing for a current or withdrawn one (their steps are in
    // the Library's own detail).
    // A card just approved stays up, settled, rather than vanishing.
    const settled = item.latest && (installs.get(`${item.latest.name}@${item.latest.content_hash}`) || {}).state === "done";
    const target = !item.installed || item.update || settled ? item.latest : null;
    if (target && !indexProblem()) detail.appendChild(installCard(item, target));
    else if (item.installed && !item.withdrawn) {
      detail.appendChild(
        el(
          "p.section-note",
          {},
          item.update ? "" : "Installed and current. ",
          "Its steps, switches and usage are in the Library. ",
          el("button.link-button", {
            text: "Show in the Library",
            "data-focus": "show",
            onclick: () => (compact ? api.openMain("library", item.name) : LX.library.reveal(item.name)),
          })
        )
      );
    }

    if (compact && !target) {
      detail.appendChild(
        el("button.link-button.detail-open", {
          text: "Open in the Library",
          "data-focus": "open",
          onclick: () => api.openMain("library", `shared:${item.name}`),
        })
      );
    }

    detailEl.appendChild(detail);
    if (focusKey) {
      const again = detailEl.querySelector(`[data-focus="${focusKey}"]`);
      if (again) again.focus();
    }
  }

  function withdrawnNotice(item) {
    const zone = el(
      "div.callout.callout-danger.shared-withdrawn",
      { role: "status" },
      icon("warning"),
      el(
        "div.shared-index-text",
        {},
        el("strong", { text: "Withdrawn from the registry" }),
        el("span", {
          text: `Version ${item.installedVersion ?? ""} was pulled after it was published. Your copy still works and still asks before its steps; nothing removes it for you.`,
        }),
        item.skill &&
          item.skill.deletable &&
          el("button.btn.btn-sm.btn-danger", {
            text: "Delete it",
            "data-focus": "delete",
            onclick: async () => {
              const result = await api.deleteSkill(item.name);
              if (result.ok) toast(`Deleted "${item.name}"`);
              else toast(result.error || "Couldn't delete that skill", "error");
            },
          })
      )
    );
    return zone;
  }

  function reviewSection(entry, compact) {
    const review = entry.review;
    const readout = el(
      "div.stat-grid.shared-review",
      {},
      el("div.stat", {}, el("div.stat-value.shared-verdict", { text: review ? VERDICT[review.verdict] || review.verdict : "--" }), el("div.stat-label", { text: "review" })),
      el("div.stat", {}, el("div.stat-value", {}, riskBadge(review, true)), el("div.stat-label", { text: "risk" })),
      el(
        "div.stat",
        {},
        el("div.stat-value.shared-cmd-value", {
          class: entry.has_run_command ? "is-cmd" : "",
          text: entry.has_run_command ? "yes" : "no",
        }),
        el("div.stat-label", { text: "runs commands" })
      )
    );
    const flags = (review && review.flags) || [];
    return el(
      "section",
      {},
      el("div.section-label", { text: "Screening" }),
      readout,
      flags.length > 0 &&
        el(
          "ul.shared-flags",
          { "aria-label": "Screening flags" },
          flags.map((flag) => el("li", {}, icon("warning"), el("span", { text: flag })))
        ),
      el(
        "div.shared-requires",
        {},
        el("span.micro-key", { text: "Uses" }),
        (entry.requires || []).map((tool) => el("span.shared-tool", { class: tool === "run_command" ? "is-cmd" : "", text: tool }))
      ),
      !compact &&
        review &&
        review.screened_by &&
        el("p.section-note", { text: `Screened by ${review.screened_by} before it was published. Screening lowers the odds of a bad skill; it doesn't replace reading the steps.` })
    );
  }

  // --- the install card --------------------------------------------------------------------
  // Waits on the user, so it's a prompt card: amber, cut corner, brackets.
  // It loads the full definition (registry_preview, read-only) and shows all
  // of it before the Install button is enabled.

  function installCard(item, target) {
    const key = `${target.name}@${target.content_hash}`;
    const record = installs.get(key);
    // Once approved, the card keeps saying what it was approved as.
    const isUpdate = record && record.state === "done" ? record.isUpdate : item.installed;
    let preview = previews.get(key);
    if (!preview) {
      preview = { state: "loading" };
      previews.set(key, preview);
      api.registryPreview(target.name, target.content_hash).then((result) => {
        previews.set(key, result && result.ok ? { state: "ok", result } : { state: "error", result: result || {} });
        onChange();
      });
    }
    const install = installs.get(key);
    const done = install && install.state === "done";

    const card = el("div.prompt-card.confirm.install-card", {
      class: done ? "resolved" : "",
      role: "group",
      "aria-label": `${isUpdate ? "Update" : "Install"} ${target.name}`,
    });
    const head = el(
      "div.prompt-head",
      {},
      icon(done ? "check" : "warning"),
      el("span.micro-label", { text: done ? (isUpdate ? "Updated" : "Installed") : isUpdate ? `Update to v${target.version}` : "Install a shared skill" }),
      riskBadge(target.review, true)
    );
    const body = el("div.prompt-body");
    card.append(head, body);

    body.appendChild(
      el(
        "p.prompt-lead",
        {},
        el("b", { text: target.name }),
        ` v${target.version} by `,
        el("b", { text: target.author }),
        isUpdate
          ? `. Version ${item.installedVersion} keeps working until you approve this. Every step of the new version:`
          : ". Every step it would run, with its arguments:"
      )
    );

    if (preview.state === "loading") {
      body.appendChild(el("div.empty.empty-inline", { role: "status", text: "Loading the full definition…" }));
      return card;
    }
    if (preview.state === "error") {
      const code = preview.result.error_code;
      const problem = INDEX_ERRORS[code];
      body.appendChild(
        el(
          "div.callout.callout-danger",
          { role: "alert" },
          icon(problem && problem.security ? "shield-x" : "x"),
          el("span", { text: `${problem ? `${problem.title}. ` : ""}${preview.result.error || "The definition couldn't be loaded."} Nothing can be installed from it.` })
        )
      );
      return card;
    }

    // registry_preview (README "Shared skills"): the card's fields at the
    // top level -- steps[{tool, args, safety, run_command}], input_schema,
    // author_description (never sent to the model), model_description.
    const card_ = preview.result;
    const steps = card_.steps || [];
    const commands = steps.filter((step) => step.tool === "run_command").length;
    if (commands) {
      body.appendChild(
        el(
          "div.needs-click.shared-cmd-warning",
          { role: "note" },
          el("span.needs-click-key", { text: "Runs shell commands" }),
          el("span", {
            text: `${commands} of ${plural(steps.length, "step")} ${commands === 1 ? "uses" : "use"} run_command. Each one asks before every run, for as long as it's installed, and can never be trusted "always".`,
          })
        )
      );
    }
    const flags = (target.review && target.review.flags) || [];
    if (flags.length && target.review.risk === "high") {
      body.appendChild(
        el(
          "div.callout.callout-danger.shared-high-risk",
          {},
          icon("warning"),
          el("span", {}, el("strong", { text: "High risk. " }), `Screening flagged: ${flags.join("; ")}.`)
        )
      );
    }

    // every step, in full
    const previous = isUpdate && item.skill ? item.skill.steps || [] : null;
    const list = el("ol.card.steps.install-steps", { "aria-label": `${steps.length} steps` });
    steps.forEach((step, index) => {
      const isCmd = step.tool === "run_command";
      let change = null;
      if (previous) {
        if (index >= previous.length) change = "new";
        else if (previous[index].tool !== step.tool || JSON.stringify(previous[index].args) !== JSON.stringify(step.args)) change = "changed";
      }
      list.appendChild(
        el(
          "li.step",
          { class: isCmd ? "step-command" : "" },
          el("div.step-index", { text: String(index + 1).padStart(2, "0") }),
          el(
            "div.step-body",
            {},
            el(
              "div.step-tool",
              {},
              step.tool,
              isCmd && el("span.badge.badge-warning", { text: "shell command · asks every run" }),
              change && el("span.badge.badge-accent", { text: change })
            ),
            fullArgs(step.args)
          )
        )
      );
    });
    body.appendChild(list);
    if (previous && previous.length > steps.length) {
      body.appendChild(el("p.section-note", { text: `${plural(previous.length - steps.length, "step")} from v${item.installedVersion} removed.` }));
    }

    // what it asks for, and how the model will see it
    const properties = (card_.input_schema && card_.input_schema.properties) || {};
    const required = (card_.input_schema && card_.input_schema.required) || [];
    body.appendChild(
      el(
        "dl.shared-facts",
        {},
        el("dt", { text: "Asks for" }),
        el(
          "dd",
          {},
          Object.keys(properties).length
            ? Object.keys(properties).map((name) => el("code", { text: `${name}${required.includes(name) ? "" : "?"}`, title: properties[name].description || "" }))
            : "nothing"
        ),
        preview.result.model_description && el("dt", { text: "The model sees" }),
        preview.result.model_description && el("dd", { class: "shared-model-desc", text: preview.result.model_description }),
        card_.author_description && el("dt", { text: "Author's text" }),
        card_.author_description &&
          el("dd", {}, el("span.shared-author-desc", { text: card_.author_description }), el("span.shared-aside", { text: " Never sent to the model." }))
      )
    );

    // the decision
    if (done) {
      body.appendChild(
        el(
          "div.prompt-outcome",
          { role: "status" },
          icon("check"),
          el("span", { text: `${isUpdate ? "Updated to" : "Installed"} v${target.version}. Until it has run once, every step asks first.` })
        )
      );
      return card;
    }

    // The list's numbers are the fresher (a preview is loaded once and kept).
    const max = importedMax() != null ? importedMax() : card_.imported_max;
    const count = data && data.installed ? installedCount() : Math.max(card_.imported_count || 0, installedCount());
    const atCap = !isUpdate && max != null && count >= max;
    const failed = install && install.state === "error" ? install.result : null;
    if (failed) {
      const problem = INDEX_ERRORS[failed.error_code];
      body.appendChild(
        el(
          "div.callout.callout-danger",
          { role: "alert" },
          icon(problem && problem.security ? "shield-x" : "x"),
          el("span", { text: `Not installed. ${failed.error || "The agent refused it."}` })
        )
      );
    } else if (atCap) {
      body.appendChild(
        el(
          "div.needs-click.shared-cap",
          { role: "note" },
          el("span.needs-click-key", { text: "At the limit" }),
          el("span", {
            text: `${count} of ${max} shared skills installed. Each one adds to every request Leutheria makes, so delete one before installing another.`,
          })
        )
      );
    }

    const sending = install && install.state === "sending";
    body.appendChild(
      el(
        "div.prompt-actions",
        {},
        el(
          "button.btn.btn-hot",
          {
            "data-focus": "install",
            disabled: sending || atCap,
            onclick: () => doInstall(key, target, isUpdate),
          },
          icon("check"),
          sending ? "Installing…" : isUpdate ? `Update to v${target.version}` : "Install"
        ),
        el("span.prompt-hint", {
          text: isUpdate ? "Replaces the installed version." : "Each step asks first until it has run once.",
        })
      )
    );
    return card;
  }

  async function doInstall(key, target, isUpdate) {
    installs.set(key, { state: "sending" });
    onChange();
    const result = await api.registryInstall(target.name, target.content_hash);
    if (result && result.ok) {
      installs.set(key, { state: "done", result, isUpdate });
      toast(`${isUpdate ? "Updated" : "Installed"} "${target.name}" v${target.version}`);
      load(false);
    } else {
      installs.set(key, { state: "error", result: result || {} });
      onChange();
    }
  }

  // --- overview (nothing selected) ------------------------------------------------------------

  function renderOverview() {
    const problem = indexProblem();
    const all = items();
    const stat = (value, label, extra) => el("div.stat", extra || {}, el("div.stat-value", { text: value }), el("div.stat-label", { text: label }));
    const detail = el(
      "div.detail",
      {},
      el(
        "div.detail-head",
        {},
        el("div.section-label", { text: "Shared skills" }),
        el("p.detail-summary", {
          text: "Step lists other people published, screened before they went up. Nothing installs until you've seen every step it runs.",
        })
      )
    );
    if (problem) detail.appendChild(indexNotice(problem, false));
    else if (data && data.stale) detail.appendChild(staleNotice(false));
    else if (!data) detail.appendChild(el("div.empty", { role: "status", text: loading ? "Loading the shared index…" : "Not loaded." }));

    const count = data ? installedCount() : all.filter((item) => item.installed).length;
    const cap = importedMax();
    detail.appendChild(
      el(
        "section",
        {},
        el("div.section-label", { text: "Registry" }),
        el(
          "div.stat-grid.stat-grid-3",
          {},
          el(
            "div.stat",
            {},
            el(
              "div.stat-value.shared-index-state",
              { "data-state": problem ? (problem.security ? "refused" : "offline") : data ? (data.stale ? "offline" : "verified") : "none" },
              el("span.shared-led"),
              problem ? (problem.security ? "refused" : "offline") : data ? (data.stale ? "cached" : "verified") : "--"
            ),
            el("div.stat-label", { text: "index signature" })
          ),
          stat(cap != null ? `${count} / ${cap}` : String(count), "installed", { class: cap != null && count >= cap ? "stat-at-cap" : "" }),
          stat(problem ? "--" : String(all.filter((item) => !item.installed).length), "available"),
          stat(problem ? "--" : String(all.filter((item) => item.update).length), "updates"),
          stat(problem ? "--" : String(all.filter((item) => item.withdrawn).length), "withdrawn"),
          stat(data && data.checked_at ? relativeTime(Date.parse(data.checked_at) / 1000) : "--", "checked")
        ),
        data && data.key_id && !problem && el("p.section-note", { text: `Signed with registry key ${data.key_id}; every file is checked against the index before it installs.` })
      )
    );
    detail.appendChild(
      el(
        "section",
        {},
        el("div.section-label", { text: "How shared skills run" }),
        el(
          "div.guidance",
          {},
          el("p", { text: "The model never sees an author's own description. It sees one Leutheria writes: who shared it, a cleaned summary, and the tools it runs." }),
          el("p", { text: "Until a shared skill has run once, every step asks first. Its run_command steps ask on every run, for good." }),
          el("p", { text: "A newer version is offered, never applied: updating is a new approval. A withdrawn skill stays until you delete it." }),
          el("p", { text: "To share a skill Leutheria learned, open it in the Library and use Share." })
        )
      )
    );
    return detail;
  }

  // --- share (a learned skill's detail, full Library only) -------------------------------------

  // What each kind of flagged constant means for someone else (the kinds
  // core/registry_text.py:share_sanitize reports).
  const FLAG_REASONS = {
    email: "An email address. Other people would send to it: make it a parameter, or remove it.",
    url: "A web address from your setup. Check it's meant for everyone.",
    ip_address: "An IP address from your network. Other people won't have it.",
    phone_number: "Looks like a phone number. Check it isn't personal.",
    token_or_id: "Looks like a token or an ID. Never publish a secret.",
    someone_elses_home: "A path in another user's home folder. Other people won't have it.",
    personal_path: "A folder from this Mac. Other people may not have it: make it a parameter, or leave it if that's intended.",
    your_username: "Your user name. It would be published with the skill.",
    other: "Check this before publishing.",
  };

  function shareSection(entry, rerender) {
    const state = shares.get(entry.name) || { phase: "idle" };
    const set = (next) => {
      shares.set(entry.name, next);
      rerender();
    };
    const section = el("section.share", {}, el("div.section-label", { text: "Share" }));

    if (state.phase === "idle" || state.phase === "loading") {
      section.appendChild(
        el(
          "div.share-start",
          {},
          el(
            "div.setting-text",
            {},
            el("strong", { text: "Share to the public registry" }),
            el("span", {
              text: "Makes a copy without your paths or usage, shows you exactly what would be published, then opens GitHub for you to submit it.",
            })
          ),
          el(
            "button.btn",
            {
              "data-focus": "share",
              disabled: state.phase === "loading",
              onclick: async () => {
                set({ phase: "loading" });
                const result = await api.registrySharePreview(entry.name);
                set(result && result.ok ? { phase: "preview", preview: result } : { phase: "error", error: (result && result.error) || "Couldn't build the preview." });
              },
            },
            state.phase === "loading" ? "Preparing…" : "Preview share"
          )
        )
      );
      return section;
    }

    if (state.phase === "error" && !state.preview) {
      section.appendChild(
        el(
          "div.callout.callout-danger",
          { role: "alert" },
          icon("x"),
          el("span", {}, `Can't share this one. ${state.error} `, el("button.link-button", { text: "Back", "data-focus": "share", onclick: () => set({ phase: "idle" }) }))
        )
      );
      return section;
    }

    const preview = state.preview;
    const definition = preview.definition || {};
    const card = el("div.card.share-preview");
    card.appendChild(
      el("div.card-header", {}, el("h2", { text: "What would be published" }), el("p", { text: preview.repo || "AnuragK-O/leutheria-skills" }))
    );
    // registry_share_preview (README "Shared skills"): path, changes
    // [{where, before, after, why}], flags [{id, where, value, kind}].
    const rows = el("div.share-rows");
    rows.appendChild(el("div.share-row", {}, el("span.micro-key", { text: "File" }), el("code.share-path", { text: preview.path || "--" })));

    const changes = preview.changes || [];
    rows.appendChild(
      el(
        "div.share-row",
        {},
        el("span.micro-key", { text: "Changed" }),
        changes.length
          ? el(
              "ul.share-changes",
              {},
              changes.map((change) =>
                el(
                  "li",
                  {},
                  el("code.share-field", { text: change.where }),
                  el(
                    "span.share-diff",
                    {},
                    change.before != null && el("del", { text: String(change.before) }),
                    change.after != null ? [el("span.share-arrow", { text: " → " }), el("ins", { text: String(change.after) })] : el("span.share-removed", { text: " removed" })
                  ),
                  change.why && el("span.share-note", { text: change.why })
                )
              )
            )
          : el("span.empty.empty-inline", { text: "Nothing: it's published as it is." })
      )
    );

    // Every flag has to be ticked before the share opens (the agent refuses
    // it otherwise: unacknowledged_flags). Ticks live in the share state, so
    // they survive a re-render but not a new preview.
    const flagged = preview.flags || [];
    const acked = state.acked || [];
    const allAcked = flagged.every((item) => acked.includes(item.id));
    if (flagged.length) {
      rows.appendChild(
        el(
          "div.share-row",
          {},
          el("span.micro-key", { text: "Check" }),
          el(
            "ul.share-flagged",
            { "aria-label": "Check these before publishing" },
            flagged.map((item) =>
              el(
                "li.needs-click",
                {},
                el("code.share-field", { text: item.where }),
                el("code.share-value", { text: String(item.value) }),
                el("span", { text: FLAG_REASONS[item.kind] || FLAG_REASONS.other }),
                el(
                  "label.inline-check.share-ack",
                  {},
                  el("input", {
                    type: "checkbox",
                    checked: acked.includes(item.id),
                    "data-flag": item.id,
                    onchange: (event) =>
                      set({ ...state, phase: state.phase === "error" ? "preview" : state.phase, acked: event.target.checked ? [...new Set([...acked, item.id])] : acked.filter((id) => id !== item.id) }),
                  }),
                  el("span", { text: "I checked this" })
                )
              )
            )
          )
        )
      );
    }
    card.appendChild(rows);
    card.appendChild(el("pre.code-block.share-json", { tabindex: "0", "aria-label": "The file that would be published" }, JSON.stringify(definition, null, 2)));
    section.appendChild(card);

    const tooLong = Boolean(preview.too_long);
    section.appendChild(
      el(
        "p.section-note",
        {},
        tooLong
          ? "This file is too long to fit in a link, so Open on GitHub copies it to your clipboard first; paste it into GitHub's editor. "
          : "GitHub opens in your browser with this file filled in. ",
        "You open the pull request from your own GitHub account (Leutheria holds no GitHub credentials), and it's screened automatically before it's published."
      )
    );

    if (state.phase === "opened") {
      section.appendChild(
        el(
          "div.prompt-outcome.share-done",
          { role: "status" },
          icon("check"),
          el("span", {
            text: state.copied
              ? "Copied the file and opened GitHub. Paste it into the editor there, then propose the new file."
              : "Opened GitHub with the file filled in. Propose the new file there to submit it.",
          }),
          el("button.link-button", { text: "Done", "data-focus": "share", onclick: () => set({ phase: "idle" }) })
        )
      );
      return section;
    }
    if (state.phase === "error") {
      section.appendChild(el("div.callout.callout-danger", { role: "alert" }, icon("x"), el("span", { text: state.error })));
    }
    section.appendChild(
      el(
        "div.prompt-actions",
        {},
        el(
          "button.btn.btn-primary",
          {
            "data-focus": "open-github",
            disabled: state.phase === "opening" || !allAcked,
            title: allAcked ? null : "Tick each flagged value first",
            onclick: async () => {
              set({ ...state, phase: "opening" });
              const result = await api.registryShare(entry.name, acked);
              if (result && result.ok) set({ phase: "opened", preview, copied: result.copied });
              else set({ phase: "error", preview, acked, error: (result && result.error) || "GitHub couldn't be opened." });
            },
          },
          state.phase === "opening" ? "Opening…" : tooLong ? "Copy and open GitHub" : "Open on GitHub"
        ),
        el("button.btn", { text: "Cancel", "data-focus": "share", onclick: () => set({ phase: "idle" }) })
      )
    );
    return section;
  }

  // --- for library.js ----------------------------------------------------------------------

  function subtitle(terms) {
    const problem = indexProblem();
    if (problem) return problem.security ? "Shared index refused" : "Registry unreachable";
    if (!data) return loading ? "Loading shared skills" : "Shared skills";
    const all = items();
    const shown = visible(terms).length;
    const installed = all.filter((item) => item.installed).length;
    if (terms.length || filter) return `${shown} of ${all.length} shared skills shown`;
    return `${all.length} shared · ${installed} installed`;
  }

  /** What the Library's own rows and detail say about an imported skill. */
  function statusOf(name) {
    const item = data ? items().find((entry) => entry.name === name) : null;
    return item ? { withdrawn: item.withdrawn, update: item.update, latest: item.latest } : null;
  }

  LX.libraryShared = {
    bind: (callback) => (onChange = callback),
    load,
    ensureLoaded: () => {
      if (!data && !loading && !loadError) load(false);
    },
    renderList,
    renderDetail,
    shareSection,
    subtitle,
    statusOf,
    has: (name) => Boolean(find(name)),
    clearFilter: () => (filter = null),
    hasFilter: () => Boolean(filter),
  };
})(window.LX);
