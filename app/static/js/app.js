/* Home dashboard — pulls GET /users/{id}/dashboard, GET /trades and
   GET /strategies, then renders the ring, stats, filter chips and
   trade list. No build step, no framework: plain DOM rendering. */

(() => {
  "use strict";

  // No auth yet, so the active user comes from ?user_id= or localStorage,
  // falling back to 1. Swap this out once real login exists.
  const params = new URLSearchParams(window.location.search);
  const USER_ID = Number(
    params.get("user_id") || window.localStorage.getItem("jt_user_id") || 1
  );
  window.localStorage.setItem("jt_user_id", String(USER_ID));

  const RING_RADIUS = 76;
  const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;
  const XP_PER_LEVEL = 100; // flat/dumb v1, mirrors ai.XP_PER_RULE
  const LEVEL_TITLES = [
    "Rookie", "Trainee", "Disciplined", "Operator",
    "Strategist", "Veteran", "Elite", "Master",
  ];

  const el = (id) => document.getElementById(id);

  const iconArrowUp = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>`;
  const iconArrowDown = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 7 7 17"/><path d="M16 17H7V8"/></svg>`;
  const iconFlagOff = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 22V4"/><path d="M4 4h13l-2 4 2 4H4"/></svg>`;

  async function fetchJSON(url) {
    const res = await fetch(url);
    if (!res.ok) {
      const err = new Error(`${res.status} ${res.statusText}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function fmtR(value) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    const sign = n > 0 ? "+" : "";
    return `${sign}${n.toFixed(1)}R`;
  }

  function fmtUsd(value) {
    const n = Number(value);
    const sign = n > 0 ? "+" : n < 0 ? "-" : "";
    return `${sign}$${Math.abs(n).toFixed(2)}`;
  }

  function levelFromXp(xp) {
    const level = Math.floor(xp / XP_PER_LEVEL) + 1;
    const title = LEVEL_TITLES[Math.min(level, LEVEL_TITLES.length) - 1];
    const intoLevel = xp % XP_PER_LEVEL;
    return { level, title, intoLevel, progress: intoLevel / XP_PER_LEVEL };
  }

  function renderRing(score) {
    const circle = el("ringProgress");
    circle.style.strokeDasharray = `${RING_CIRCUMFERENCE}`;
    // Set full offset first so the fill-in transition has something to animate from.
    circle.style.strokeDashoffset = `${RING_CIRCUMFERENCE}`;
    // Double rAF: the first frame commits the "full" starting offset, the
    // second changes it so the CSS transition actually has something to animate from.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const clamped = Math.max(0, Math.min(100, score));
        const offset = RING_CIRCUMFERENCE * (1 - clamped / 100);
        circle.style.strokeDashoffset = `${offset}`;
      });
    });
    el("disciplineValue").textContent = Math.round(score);
  }

  function renderHero(dashboard) {
    renderRing(dashboard.discipline_score);

    const { level, title, intoLevel } = levelFromXp(dashboard.xp);
    el("levelBadge").textContent = `Lv ${level} · ${title}`;
    el("streakValue").textContent = dashboard.current_streak;

    const pct = Math.round((intoLevel / XP_PER_LEVEL) * 100);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        el("xpFill").style.width = `${pct}%`;
      });
    });
    el("xpCaption").textContent = `${intoLevel} / ${XP_PER_LEVEL} XP to next level`;
  }

  function renderStats(dashboard, trades) {
    const window_ = trades.slice(0, dashboard.discipline_window_trades);
    const passed = window_.reduce((sum, t) => sum + (t.rules_passed || 0), 0);
    const total = window_.reduce((sum, t) => sum + (t.rules_total || 0), 0);
    el("rulesFollowed").textContent = `${passed}/${total}`;

    const cutoff = Date.now() - 30 * 24 * 60 * 60 * 1000;
    const windowTrades = trades.filter(
      (t) => t.created_at && new Date(t.created_at).getTime() >= cutoff
    );

    // r_multiple stays the primary discipline unit; dollars are shown only
    // when every trade in the window has one, so the total isn't a mix of units.
    const hasFullPnlUsd =
      windowTrades.length > 0 &&
      windowTrades.every((t) => t.pnl_usd !== null && t.pnl_usd !== undefined);

    if (hasFullPnlUsd) {
      const netUsd = windowTrades.reduce((sum, t) => sum + Number(t.pnl_usd), 0);
      el("netPnl").textContent = fmtUsd(netUsd);
    } else {
      const netR = windowTrades.reduce((sum, t) => sum + (Number(t.r_multiple) || 0), 0);
      el("netPnl").textContent = fmtR(netR);
    }
  }

  function buildFilters(strategies, trades, onChange) {
    const container = el("filters");
    container.innerHTML = "";

    const chips = [
      { key: "all", label: "All", test: () => true },
      ...strategies.map((s) => ({
        key: `strategy:${s.id}`,
        label: s.name,
        test: (t) => t.strategy_id === s.id,
      })),
      { key: "direction:short", label: "Shorts", test: (t) => t.direction === "short" },
      { key: "direction:long", label: "Longs", test: (t) => t.direction === "long" },
    ];

    let active = "all";

    function apply() {
      const chip = chips.find((c) => c.key === active) || chips[0];
      onChange(trades.filter(chip.test));
    }

    chips.forEach((chip) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip" + (chip.key === active ? " active" : "");
      btn.textContent = chip.label;
      btn.addEventListener("click", () => {
        active = chip.key;
        container.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
        btn.classList.add("active");
        apply();
      });
      container.appendChild(btn);
    });

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "chip chip-add";
    addBtn.textContent = "+";
    addBtn.setAttribute("aria-label", "New strategy");
    addBtn.addEventListener("click", () => {
      window.alert("Strategy setup is coming soon — for now, add one via POST /strategies.");
    });
    container.appendChild(addBtn);

    apply();
  }

  function tradeIcon(trade) {
    if (trade.is_off_plan) return { cls: "offplan", svg: iconFlagOff };
    const won = Number(trade.r_multiple) > 0;
    return {
      cls: trade.rules_passed === trade.rules_total ? "success" : "warning",
      svg: won ? iconArrowUp : iconArrowDown,
    };
  }

  function tradeSub(trade, strategyName) {
    const date = fmtDate(trade.created_at);
    if (trade.is_off_plan) {
      return `<span class="rules-offplan">Off-plan</span> · ${date}`;
    }
    const ok = trade.rules_passed === trade.rules_total;
    const ruleCls = ok ? "rules-ok" : "rules-bad";
    const setup = strategyName ? `${strategyName} · ` : "";
    return `${setup}${date} · <span class="${ruleCls}">${trade.rules_passed}/${trade.rules_total} rules</span>`;
  }

  function renderTradeList(trades, strategyById) {
    const container = el("tradeList");
    container.innerHTML = "";

    if (trades.length === 0) {
      container.innerHTML = `
        <div class="empty-state card">
          <div class="empty-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 11l3 3L22 4"/>
              <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
            </svg>
          </div>
          <h3>No trades yet</h3>
          <p>Log your first trade from a screenshot and it'll show up here, judged against your own rules.</p>
        </div>`;
      return;
    }

    trades.forEach((trade) => {
      const { cls, svg } = tradeIcon(trade);
      const strategyName = trade.strategy_id ? strategyById.get(trade.strategy_id) : null;

      const row = document.createElement("div");
      row.className = "trade-row card";
      row.innerHTML = `
        <div class="trade-icon ${cls}">${svg}</div>
        <div class="trade-main">
          <div class="trade-title">${trade.instrument || "Unknown"} <span class="dir">${trade.direction || ""}</span></div>
          <div class="trade-sub">${tradeSub(trade, strategyName)}</div>
        </div>
        <div class="trade-r">${fmtR(trade.r_multiple)}</div>
      `;
      container.appendChild(row);
    });
  }

  function renderLoadError() {
    el("tradeList").innerHTML = `
      <div class="empty-state card">
        <h3>Couldn't load this user</h3>
        <p>No user #${USER_ID} yet — create one via POST /users, or pass ?user_id= in the URL.</p>
      </div>`;
    el("filters").innerHTML = "";
  }

  async function init() {
    el("logTradeBtn").addEventListener("click", () => {
      window.alert("Screenshot upload is coming soon.");
    });

    let dashboard, trades, strategies;
    try {
      [dashboard, trades, strategies] = await Promise.all([
        fetchJSON(`/users/${USER_ID}/dashboard`),
        fetchJSON(`/trades?user_id=${USER_ID}`),
        fetchJSON(`/strategies?user_id=${USER_ID}&is_active=true`),
      ]);
    } catch (err) {
      renderLoadError();
      return;
    }

    renderHero(dashboard);
    renderStats(dashboard, trades);

    const strategyById = new Map(strategies.map((s) => [s.id, s.name]));

    buildFilters(strategies, trades, (filtered) => renderTradeList(filtered, strategyById));
  }

  document.addEventListener("DOMContentLoaded", init);
})();
