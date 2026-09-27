/* Shared across every top-level page (home, /trading's index.html, and each
   /marketer/... page): "last used tool" tracking, the persistent tool-
   switcher dropdown mounted into each page's topbar, and the light/dark
   theme toggle — one shared preference and one shared implementation
   (previously duplicated per page/only on Trading Mirror) so switching it
   anywhere applies everywhere. No build step, no framework — plain DOM,
   loaded via a plain <script> tag same as app.js. */

(() => {
  "use strict";

  // ---------------------------------------------------------------------
  // Theme (light/dark) — an explicit choice (data-theme attribute) always
  // wins; with none set, style.css's own prefers-color-scheme media query
  // decides, so the app just follows the OS live. Every page also carries a
  // small inline script in <head> that applies any stored choice before
  // first paint (this file loads too late for that — avoids a flash of the
  // wrong theme); this is the version that wires the toggle button (if the
  // page has one — see #themeToggleBtn/#themeColorMeta) and keeps it in
  // sync if the OS theme changes mid-session. Self-wires immediately below
  // (no page has to call anything) so it works the same whether or not
  // Clerk/auth is involved on that page.
  // ---------------------------------------------------------------------
  const THEME_KEY = "mirror_theme";
  const darkMediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const iconSun = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>`;
  const iconMoon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/></svg>`;

  function systemTheme() {
    return darkMediaQuery.matches ? "dark" : "light";
  }

  function activeTheme() {
    return document.documentElement.getAttribute("data-theme") || systemTheme();
  }

  function updateThemeToggleUI(theme) {
    const btn = document.getElementById("themeToggleBtn");
    if (btn) {
      btn.innerHTML = theme === "dark" ? iconMoon : iconSun;
      btn.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
    }
    const meta = document.getElementById("themeColorMeta");
    if (meta) meta.setAttribute("content", theme === "dark" ? "#171613" : "#1D9E75");
  }

  // explicitTheme is the user's stored choice ("light"/"dark"), or null to
  // follow the OS preference — null means "no data-theme attribute", which
  // is exactly what lets style.css's media query take over.
  function applyTheme(explicitTheme) {
    if (explicitTheme) {
      document.documentElement.setAttribute("data-theme", explicitTheme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    updateThemeToggleUI(explicitTheme || systemTheme());
  }

  function toggleTheme() {
    const next = activeTheme() === "dark" ? "light" : "dark";
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    applyTheme(next);
  }

  function wireThemeToggle() {
    let stored = null;
    try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
    applyTheme(stored === "light" || stored === "dark" ? stored : null);

    const btn = document.getElementById("themeToggleBtn");
    if (btn) btn.addEventListener("click", toggleTheme);

    darkMediaQuery.addEventListener("change", () => {
      let current = null;
      try { current = localStorage.getItem(THEME_KEY); } catch (e) {}
      if (current !== "light" && current !== "dark") updateThemeToggleUI(systemTheme());
    });
  }

  wireThemeToggle();

  const LAST_TOOL_KEY = "mirror_last_tool";

  function markLastUsed(tool) {
    try {
      localStorage.setItem(LAST_TOOL_KEY, tool);
    } catch (e) {}
  }

  function getLastUsed() {
    try {
      return localStorage.getItem(LAST_TOOL_KEY);
    } catch (e) {
      return null;
    }
  }

  const iconGrid = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>`;
  const iconHome = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12l9-9 9 9"/><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/></svg>`;
  const iconTrading = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>`;
  const iconMarketer = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/></svg>`;

  // opts.active: "trading" | "marketer" | undefined (home) — highlights the
  // current tool in the dropdown so it's clear where you already are.
  function mountSwitcher(container, opts) {
    const active = (opts && opts.active) || null;

    const wrap = document.createElement("div");
    wrap.className = "nav-switcher";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "icon-button";
    btn.setAttribute("aria-label", "Switch tool");
    btn.setAttribute("aria-haspopup", "true");
    btn.innerHTML = iconGrid;

    const menu = document.createElement("div");
    menu.className = "card nav-switcher-menu hidden";
    menu.innerHTML =
      `<a class="nav-switcher-link${active === null ? " active" : ""}" href="/">${iconHome}Home</a>` +
      `<a class="nav-switcher-link${active === "trading" ? " active" : ""}" href="/trading">${iconTrading}Trading Mirror</a>` +
      `<a class="nav-switcher-link${active === "marketer" ? " active" : ""}" href="/marketer">${iconMarketer}Marketer Mirror</a>`;

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.classList.toggle("hidden");
    });
    menu.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => menu.classList.add("hidden"));

    wrap.appendChild(btn);
    wrap.appendChild(menu);
    container.appendChild(wrap);
    return wrap;
  }

  window.MirrorNav = { markLastUsed, getLastUsed, mountSwitcher };
})();
