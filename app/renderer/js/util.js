/* Shared DOM helpers, the icon set, and small formatters.
   Everything hangs off one global so the renderer can stay on plain classic
   scripts -- ES modules don't load over file:// without relaxing Chromium's
   file access, which isn't worth doing for five files. */

window.LX = window.LX || {};

(function (LX) {
  // --- DOM ---------------------------------------------------------------

  /** el("div.card", {onclick}, child, child…) -- tag#id.class.class shorthand. */
  function el(spec, props, ...children) {
    const [tagAndId, ...classes] = String(spec).split(".");
    const [tag, id] = tagAndId.split("#");
    const node = document.createElement(tag || "div");
    if (id) node.id = id;
    if (classes.length) node.className = classes.join(" ");

    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = [node.className, value].filter(Boolean).join(" ");
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, value === true ? "" : value);
    }

    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  function clear(node) {
    node.replaceChildren();
    return node;
  }

  // --- icons -------------------------------------------------------------

  const PATHS = {
    assistant: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
    library:
      "M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z|m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65|m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65",
    activity: "M3 12h4l3 8 4-16 3 8h4",
    permissions:
      "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z|m9 12 2 2 4-4",
    logs: "m4 17 6-6-6-6|M12 19h8",
    search: "M11 3a8 8 0 1 0 0 16 8 8 0 0 0 0-16z|m21 21-4.3-4.3",
    mic: "M12 19v3|M19 10v2a7 7 0 0 1-14 0v-2|M12 2a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z",
    stop: "M6 6h12v12H6z",
    sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z|M12 2v2|M12 20v2|m4.9 4.9 1.4 1.4|m17.7 17.7 1.4 1.4|M2 12h2|M20 12h2|m6.3 17.7-1.4 1.4|m19.1 4.9-1.4 1.4",
    moon: "M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z",
    check: "M20 6 9 17l-5-5",
    x: "M18 6 6 18|M6 6l12 12",
    warning: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z|M12 9v4|M12 17h.01",
    sparkle: "m12 3 1.9 5.8H20l-4.9 3.6 1.9 5.8-5-3.6-5 3.6 1.9-5.8L4 8.8h6.1z",
    tool: "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z",
    trash: "M3 6h18|M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6|M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
    info: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z|M12 16v-4|M12 8h.01",
    refresh: "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8|M21 3v5h-5|M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16|M8 16H3v5",
  };

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.7");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    for (const d of (PATHS[name] || "").split("|")) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      svg.appendChild(path);
    }
    return svg;
  }

  // --- formatting --------------------------------------------------------

  /** "just now" / "12m ago" / "Mar 4" -- from a float epoch-seconds timestamp. */
  function relativeTime(ts) {
    if (!ts) return "never";
    const seconds = Date.now() / 1000 - ts;
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  /** Compact one-line rendering of a tool's arguments: key=value, truncated. */
  function formatArgs(args, limit = 120) {
    const parts = Object.entries(args || {}).map(([key, value]) => {
      const text = typeof value === "string" ? value : JSON.stringify(value);
      return `${key}=${text}`;
    });
    const joined = parts.join("  ");
    return joined.length > limit ? `${joined.slice(0, limit - 1)}…` : joined;
  }

  /** Same, but with {param} placeholders highlighted -- returns a node. */
  function formatTemplateArgs(args) {
    const node = el("div.step-args");
    const text = formatArgs(args, 240);
    let index = 0;
    for (const match of text.matchAll(/\{(\w+)\}/g)) {
      if (match.index > index) node.appendChild(document.createTextNode(text.slice(index, match.index)));
      node.appendChild(el("em", { text: match[0] }));
      index = match.index + match[0].length;
    }
    node.appendChild(document.createTextNode(text.slice(index)));
    return node;
  }

  function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
  }

  // --- toasts ------------------------------------------------------------

  function toast(message, kind = "success") {
    const node = el(`div.toast.${kind}`, {}, icon(kind === "error" ? "x" : "check"), el("span", { text: message }));
    document.getElementById("toasts").appendChild(node);
    setTimeout(() => {
      node.classList.add("leaving");
      node.addEventListener("animationend", () => node.remove());
    }, 2600);
  }

  Object.assign(LX, { el, clear, icon, relativeTime, formatArgs, formatTemplateArgs, plural, toast });
})(window.LX);
