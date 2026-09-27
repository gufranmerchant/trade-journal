/* Shared across every top-level page (home, /trading's index.html, and each
   /marketer/... page): "last used tool" tracking (so a returning visit to
   "/" skips straight past the home page) and the persistent tool-switcher
   dropdown mounted into each page's topbar. No build step, no framework —
   plain DOM, loaded via a plain <script> tag same as app.js. */

(() => {
  "use strict";

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
