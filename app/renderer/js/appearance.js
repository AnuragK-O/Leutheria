/* Appearance, applied the same way in every window (main, overlay, panels):
   theme, density, the accent colour, and where toasts and the overlay sit.
   The values come from main.js (overlay-layout.json); this only applies
   them, as attributes on <html>/<body> and CSS-variable overrides on top of
   DESIGN.md's tokens. Loaded in <head>, after the stylesheet, so the first
   frame is already right.

   Only the brand accent (the --violet family) is user-editable. The state
   colours (cyan live, amber waiting, red failed, green succeeded) are not:
   colour means state, and main.js refuses an accent that would read as one. */

(function () {
  const api = window.leutheria;
  const root = document.documentElement;
  // ?theme= wins, for the debug capture path only (main.js passes it).
  const forcedTheme = new URLSearchParams(location.search).get("theme");
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  let current = (api && api.initialAppearance) || { theme: "dark", density: "comfortable" };

  const style = document.createElement("style");
  style.id = "appearance-overrides";
  document.head.appendChild(style);

  function themeOf(appearance) {
    if (forcedTheme) return forcedTheme;
    if (appearance.theme === "system") return systemDark.matches ? "dark" : "light";
    return appearance.theme || "dark";
  }

  function block(selector, vars) {
    return `${selector} {\n${Object.entries(vars)
      .map(([name, value]) => `  ${name}: ${value};`)
      .join("\n")}\n}`;
  }

  function apply(appearance) {
    current = appearance || current;
    root.dataset.theme = themeOf(current);
    root.dataset.density = current.density || "comfortable";
    // Doubled :root so the override outranks the token blocks without !important.
    style.textContent = current.vars
      ? [block(":root:root", current.vars.dark), block(':root:root[data-theme="light"]', current.vars.light)].join("\n")
      : "";
    const body = () => {
      document.body.dataset.toastAnchor = current.toastAnchor || "bottom-left";
      document.body.dataset.anchor = current.alertsAnchor || "top-center";
    };
    if (document.body) body();
    else document.addEventListener("DOMContentLoaded", body, { once: true });
  }

  systemDark.addEventListener("change", () => apply());
  if (api && api.onAppearance) api.onAppearance(apply);
  apply(current);

  window.LX = window.LX || {};
  window.LX.appearance = { current: () => current, theme: () => root.dataset.theme };
})();
