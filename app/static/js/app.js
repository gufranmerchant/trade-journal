/* Home dashboard — pulls GET /users/{id}/dashboard, GET /trades and
   GET /strategies, then renders the ring, stats, filter chips and
   trade list. Also drives the "Log trade from screenshot" screen,
   which POSTs to /trades and renders the parsed + judged result.
   No build step, no framework: plain DOM rendering, one page. */

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
  const OFFPLAN_VALUE = "offplan";

  const el = (id) => document.getElementById(id);

  const iconArrowUp = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>`;
  const iconArrowDown = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 7 7 17"/><path d="M16 17H7V8"/></svg>`;
  const iconFlagOff = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 22V4"/><path d="M4 4h13l-2 4 2 4H4"/></svg>`;
  const iconCheck = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>`;
  const iconCross = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="M6 6l12 12"/></svg>`;
  const iconEdit = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`;
  const iconGear = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;

  // ---- Log-trade screen state ----
  let strategiesCache = [];
  let selectedFile = null;
  let previewUrl = null;
  let selectedStrategyValue = OFFPLAN_VALUE;
  let dashboardDirty = false;
  let lastLoggedTradeId = null;
  let lastOffPlanSuggestion = null; // {name, rules} from the most recent off-plan result, or null

  // ---- Strategy screen state ----
  let editingStrategyId = null;
  let strategyReturnView = "home"; // "home" | "log" | "manage"

  // ---- Manage-strategies screen state ----
  let manageStrategiesCache = [];

  // ---- Trade-detail screen state ----
  let currentDetailTradeId = null;

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

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
    if (value === null || value === undefined) return null;
    const n = Math.round(Number(value));
    const sign = n > 0 ? "+" : n < 0 ? "-" : "";
    return `${sign}$${Math.abs(n)}`;
  }

  function fmtPriceValue(value) {
    return value === null || value === undefined ? "—" : String(value);
  }

  // Pure R:R math shared by the detail screen's live-editable form and the
  // read-only fact grid on the post-submit result screen.
  function computeRRFromValues(entry, exit, sl) {
    if (!Number.isFinite(entry) || !Number.isFinite(exit) || !Number.isFinite(sl)) return null;
    const risk = Math.abs(entry - sl);
    const reward = Math.abs(exit - entry);
    if (risk === 0) return null;
    return reward / risk;
  }

  function fmtRR(entry, exit, sl) {
    // Number(null) is 0, not NaN — guard explicitly or a missing SL/entry/
    // exit reads as a real zero price and produces a bogus ratio.
    if ([entry, exit, sl].some((v) => v === null || v === undefined)) return "—";
    const rr = computeRRFromValues(Number(entry), Number(exit), Number(sl));
    return rr === null ? "—" : `1 : ${rr.toFixed(2)}`;
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

  // Both cards describe exactly the trades currently visible below — whatever
  // the active filter chip scopes them to. The discipline ring is the one
  // global, unfiltered number on this page; these two react to filters.
  function renderStats(trades, filterLabel) {
    const passed = trades.reduce((sum, t) => sum + (t.rules_passed || 0), 0);
    const total = trades.reduce((sum, t) => sum + (t.rules_total || 0), 0);
    el("rulesFollowed").textContent = `${passed}/${total}`;

    el("netPnlLabel").textContent =
      filterLabel === "All" ? "Net P&L" : `Net P&L · ${filterLabel}`;

    // R is always shown — it's the discipline unit and every trade has one.
    // Dollars are supplementary: summed from whichever trades have a
    // pnl_usd, and only shown alongside R when at least one of them does.
    const netR = trades.reduce((sum, t) => sum + (Number(t.r_multiple) || 0), 0);
    const usdTrades = trades.filter(
      (t) => t.pnl_usd !== null && t.pnl_usd !== undefined
    );

    if (usdTrades.length > 0) {
      const netUsd = usdTrades.reduce((sum, t) => sum + Number(t.pnl_usd), 0);
      el("netPnl").textContent = `${fmtUsd(netUsd)} · ${fmtR(netR)}`;
    } else {
      el("netPnl").textContent = fmtR(netR);
    }
  }

  function buildFilters(strategies, trades, onChange) {
    const container = el("filters");
    container.innerHTML = "";

    // Manage-strategies entry point — sits before "All" (the "+" new-strategy
    // chip stays at the end) and is icon-only/circular so it reads as a
    // settings control, not another filter option.
    const manageBtn = document.createElement("button");
    manageBtn.type = "button";
    manageBtn.className = "icon-button chip-manage-btn";
    manageBtn.setAttribute("aria-label", "Manage strategies");
    manageBtn.innerHTML = iconGear;
    manageBtn.addEventListener("click", () => {
      openManageStrategiesScreen();
    });
    container.appendChild(manageBtn);

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
      onChange(trades.filter(chip.test), chip.label);
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
      strategyReturnView = "home";
      openNewStrategyScreen();
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
      const hasUsd = trade.pnl_usd !== null && trade.pnl_usd !== undefined;

      const row = document.createElement("div");
      row.className = "trade-row card";
      row.innerHTML = `
        <div class="trade-icon ${cls}">${svg}</div>
        <div class="trade-main">
          <div class="trade-title">${trade.instrument || "Unknown"} <span class="dir">${trade.direction || ""}</span></div>
          <div class="trade-sub">${tradeSub(trade, strategyName)}</div>
        </div>
        <div class="trade-r">
          <span class="trade-r-primary">${fmtR(trade.r_multiple)}</span>
          ${hasUsd ? `<span class="trade-r-usd">${fmtUsd(trade.pnl_usd)}</span>` : ""}
        </div>
      `;
      row.addEventListener("click", () => openTradeDetail(trade.id));
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

  // Fetches + renders everything on the home screen. Re-run after a trade is
  // logged so the ring, XP, streak and trade list reflect it immediately.
  async function loadDashboard() {
    let dashboard, trades, strategies;
    try {
      [dashboard, trades, strategies] = await Promise.all([
        fetchJSON(`/users/${USER_ID}/dashboard`),
        fetchJSON(`/trades?user_id=${USER_ID}`),
        fetchJSON(`/strategies?user_id=${USER_ID}&is_active=true`),
      ]);
    } catch (err) {
      renderLoadError();
      return false;
    }

    strategiesCache = strategies;
    renderHero(dashboard);

    const strategyById = new Map(strategies.map((s) => [s.id, s.name]));
    buildFilters(strategies, trades, (filtered, filterLabel) => {
      renderStats(filtered, filterLabel);
      renderTradeList(filtered, strategyById);
    });
    return true;
  }

  // ---------------------------------------------------------------------
  // Log-trade screen
  // ---------------------------------------------------------------------

  function showView(view) {
    el("homeView").classList.toggle("hidden", view !== "home");
    el("logView").classList.toggle("hidden", view !== "log");
    el("detailView").classList.toggle("hidden", view !== "detail");
    el("strategyView").classList.toggle("hidden", view !== "strategy");
    el("manageView").classList.toggle("hidden", view !== "manage");
    el("homeTopbar").classList.toggle("hidden", view !== "home");
    el("logTopbar").classList.toggle("hidden", view !== "log");
    el("detailTopbar").classList.toggle("hidden", view !== "detail");
    el("strategyTopbar").classList.toggle("hidden", view !== "strategy");
    el("manageTopbar").classList.toggle("hidden", view !== "manage");
    el("homeCta").classList.toggle("hidden", view !== "home");
    window.scrollTo(0, 0);
  }

  function showLogSubView(sub) {
    el("logFormView").classList.toggle("hidden", sub !== "form");
    el("logLoadingView").classList.toggle("hidden", sub !== "loading");
    el("logResultView").classList.toggle("hidden", sub !== "result");
  }

  function showError(message) {
    el("logErrorText").textContent = message;
    el("logErrorBanner").classList.remove("hidden");
  }

  function hideError() {
    el("logErrorBanner").classList.add("hidden");
  }

  function setFile(file) {
    if (!file || !file.type || !file.type.startsWith("image/")) {
      showError("Please choose an image file.");
      return;
    }
    selectedFile = file;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(file);
    el("previewImg").src = previewUrl;
    el("dropzoneEmpty").classList.add("hidden");
    el("dropzonePreview").classList.remove("hidden");
    el("submitTradeBtn").disabled = false;
    hideError();
  }

  function clearFile() {
    selectedFile = null;
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }
    el("screenshotInput").value = "";
    el("previewImg").src = "";
    el("dropzoneEmpty").classList.remove("hidden");
    el("dropzonePreview").classList.add("hidden");
    el("submitTradeBtn").disabled = true;
  }

  function selectStrategy(value) {
    selectedStrategyValue = value;
    el("strategyPicker").querySelectorAll(".strategy-option").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.value === value);
    });
  }

  function renderStrategyPicker() {
    const container = el("strategyPicker");
    container.innerHTML = "";

    const offBtn = document.createElement("button");
    offBtn.type = "button";
    offBtn.className = "strategy-option offplan-option";
    offBtn.dataset.value = OFFPLAN_VALUE;
    offBtn.innerHTML = `
      <span class="strategy-option-name">Off-plan / no setup</span>
      <span class="strategy-option-hint">Not one of your defined setups</span>
    `;
    container.appendChild(offBtn);

    strategiesCache.forEach((s) => {
      const row = document.createElement("div");
      row.className = "strategy-row";

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "strategy-option";
      btn.dataset.value = String(s.id);
      const ruleCount = (s.rules || []).length;
      btn.innerHTML = `
        <span class="strategy-option-name">${escapeHtml(s.name)}</span>
        <span class="strategy-option-hint">${ruleCount} rule${ruleCount === 1 ? "" : "s"}</span>
      `;

      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "strategy-edit-btn";
      editBtn.setAttribute("aria-label", `Edit ${s.name}`);
      editBtn.innerHTML = iconEdit;
      editBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        strategyReturnView = "log";
        openEditStrategyScreen(s.id);
      });

      row.appendChild(btn);
      row.appendChild(editBtn);
      container.appendChild(row);
    });

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "strategy-add-btn";
    addBtn.textContent = "+ New strategy";
    addBtn.addEventListener("click", () => {
      strategyReturnView = "log";
      openNewStrategyScreen();
    });
    container.appendChild(addBtn);

    container.querySelectorAll(".strategy-option").forEach((btn) => {
      btn.addEventListener("click", () => selectStrategy(btn.dataset.value));
    });

    selectStrategy(OFFPLAN_VALUE);
  }

  function resetLogForm() {
    clearFile();
    el("contextNote").value = "";
    hideError();
    renderStrategyPicker();
    lastOffPlanSuggestion = null;
    el("setupSuggestionCard").classList.add("hidden");
    showLogSubView("form");
  }

  function openLogScreen() {
    resetLogForm();
    showView("log");
  }

  async function refreshIfDirty() {
    if (dashboardDirty) {
      await loadDashboard();
      dashboardDirty = false;
    }
  }

  // Shared by the log-screen result and the trade-detail screen — both show
  // the same icon/title/R/$/rule-checklist/coach-note shape, just under
  // different element ids, so the id map is the only thing that varies.
  function renderVerdictBlock(ids, trade, strategyName) {
    const { cls, svg } = tradeIcon(trade);
    const iconEl = el(ids.icon);
    iconEl.className = `trade-icon ${cls}`;
    iconEl.innerHTML = svg;

    el(ids.title).innerHTML =
      escapeHtml(trade.instrument || "Unknown instrument") +
      (trade.direction ? ` <span class="dir">${escapeHtml(trade.direction)}</span>` : "");
    el(ids.sub).textContent = trade.is_off_plan ? "Off-plan" : (strategyName || "—");

    el(ids.r).textContent = fmtR(trade.r_multiple);
    const usdEl = el(ids.usd);
    if (trade.pnl_usd !== null && trade.pnl_usd !== undefined) {
      usdEl.textContent = fmtUsd(trade.pnl_usd);
      usdEl.classList.remove("hidden");
    } else {
      usdEl.classList.add("hidden");
    }

    // Read-only parsed-field summary — only the result screen passes a
    // `facts` id map; the detail screen shows these same fields via its own
    // editable form instead, so there's nothing to do here for it.
    if (ids.facts) {
      el(ids.facts.entry).textContent = fmtPriceValue(trade.entry_price);
      el(ids.facts.exit).textContent = fmtPriceValue(trade.exit_price);
      el(ids.facts.sl).textContent = fmtPriceValue(trade.sl_price);
      el(ids.facts.tp).textContent = fmtPriceValue(trade.tp_price);
      el(ids.facts.rr).textContent = fmtRR(trade.entry_price, trade.exit_price, trade.sl_price);
    }

    const offplanBanner = el(ids.offplanBanner);
    const ruleList = el(ids.ruleList);
    const xpBadge = el(ids.xpBadge);
    const coachCard = el(ids.coachCard);
    const didWellCard = el(ids.didWellCard);

    if (trade.is_off_plan) {
      offplanBanner.classList.remove("hidden");
      el(ids.offplanText).textContent =
        trade.coach_note || "No setup matched this trade.";
      ruleList.classList.add("hidden");
      ruleList.innerHTML = "";
      xpBadge.classList.add("hidden");
      coachCard.classList.add("hidden");
      didWellCard.classList.add("hidden");
    } else {
      offplanBanner.classList.add("hidden");

      const results = trade.rule_results || [];
      ruleList.innerHTML = results
        .map(
          (r) => `
        <div class="rule-tile ${r.passed ? "pass" : "fail"}">
          <div class="rule-tile-icon">${r.passed ? iconCheck : iconCross}</div>
          <div class="rule-tile-text">${escapeHtml(r.text)}</div>
        </div>`
        )
        .join("");
      ruleList.classList.remove("hidden");

      xpBadge.textContent = `+${trade.xp_earned || 0} XP`;
      xpBadge.classList.remove("hidden");

      el(ids.coachText).textContent = trade.coach_note || "";
      coachCard.classList.remove("hidden");

      if (trade.did_well) {
        el(ids.didWellText).textContent = trade.did_well;
        didWellCard.classList.remove("hidden");
      } else {
        didWellCard.classList.add("hidden");
      }
    }
  }

  const RESULT_IDS = {
    icon: "resultIcon", title: "resultTitle", sub: "resultSub",
    r: "resultR", usd: "resultUsd",
    facts: {
      entry: "resultFactEntry", exit: "resultFactExit",
      sl: "resultFactSl", tp: "resultFactTp", rr: "resultFactRR",
    },
    offplanBanner: "offplanBanner", offplanText: "offplanBannerText",
    ruleList: "ruleList", xpBadge: "xpEarnedBadge",
    coachCard: "coachNoteCard", coachText: "coachNoteText",
    didWellCard: "didWellCard", didWellText: "didWellText",
  };

  // Off-plan "smart suggestion" — result screen only. Not part of
  // renderVerdictBlock since it's not part of trade.rule_results and the
  // trade-detail screen never receives setup_suggestion (POST /trades-only,
  // single-trade scoped — see main.py).
  function renderSetupSuggestion(trade) {
    const card = el("setupSuggestionCard");
    const rulesEl = el("setupSuggestionRules");
    const saveBtn = el("saveSuggestedStrategyBtn");
    lastOffPlanSuggestion = null;

    const suggestion = trade.is_off_plan ? trade.setup_suggestion : null;
    if (!suggestion) {
      card.classList.add("hidden");
      rulesEl.innerHTML = "";
      rulesEl.classList.add("hidden");
      saveBtn.classList.add("hidden");
      return;
    }

    if (suggestion.is_setup) {
      lastOffPlanSuggestion = {
        name: suggestion.suggested_name,
        rules: suggestion.suggested_rules || [],
      };
      el("setupSuggestionText").textContent =
        "This looks like a repeatable setup — want to save it as a strategy?";
      rulesEl.innerHTML = lastOffPlanSuggestion.rules
        .map((r) => `<div class="suggested-rule-item">${escapeHtml(r.text)}</div>`)
        .join("");
      rulesEl.classList.remove("hidden");
      saveBtn.classList.remove("hidden");
    } else {
      el("setupSuggestionText").textContent =
        "This looks like a discretionary or impulse entry rather than a repeatable setup.";
      rulesEl.innerHTML = "";
      rulesEl.classList.add("hidden");
      saveBtn.classList.add("hidden");
    }
    card.classList.remove("hidden");
  }

  function renderResult(trade, strategyName) {
    renderVerdictBlock(RESULT_IDS, trade, strategyName);
    renderSetupSuggestion(trade);
  }

  async function handleSubmit() {
    if (!selectedFile) return;
    hideError();
    showLogSubView("loading");

    try {
      const form = new FormData();
      form.append("user_id", String(USER_ID));
      form.append("context_note", el("contextNote").value.trim());
      if (selectedStrategyValue !== OFFPLAN_VALUE) {
        form.append("strategy_id", selectedStrategyValue);
      }
      form.append("screenshot", selectedFile, selectedFile.name);

      const res = await fetch("/trades", { method: "POST", body: form });
      if (!res.ok) {
        let message = `Request failed (${res.status})`;
        try {
          const body = await res.json();
          if (body && body.detail) message = body.detail;
        } catch (_) {
          // response body wasn't JSON — fall back to the generic message
        }
        throw new Error(message);
      }

      const trade = await res.json();
      dashboardDirty = true;
      lastLoggedTradeId = trade.id;

      renderResult(trade, trade.strategy_name);
      showLogSubView("result");
    } catch (err) {
      showLogSubView("form");
      showError(err.message || "Something went wrong — check your connection and try again.");
    }
  }

  function wireLogScreen() {
    el("dropzoneEmpty").addEventListener("click", () => el("screenshotInput").click());
    el("screenshotInput").addEventListener("change", (e) => {
      if (e.target.files && e.target.files[0]) setFile(e.target.files[0]);
    });
    el("removeScreenshotBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      clearFile();
    });

    const dz = el("dropzone");
    ["dragenter", "dragover"].forEach((evt) =>
      dz.addEventListener(evt, (e) => {
        e.preventDefault();
        dz.classList.add("dragging");
      })
    );
    ["dragleave", "drop"].forEach((evt) =>
      dz.addEventListener(evt, (e) => {
        e.preventDefault();
        dz.classList.remove("dragging");
      })
    );
    dz.addEventListener("drop", (e) => {
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) setFile(file);
    });

    el("submitTradeBtn").addEventListener("click", handleSubmit);

    el("viewTradeDetailBtn").addEventListener("click", () => {
      if (lastLoggedTradeId !== null) openTradeDetail(lastLoggedTradeId);
    });

    el("saveSuggestedStrategyBtn").addEventListener("click", () => {
      if (!lastOffPlanSuggestion) return;
      strategyReturnView = "log";
      openNewStrategyScreenFromSuggestion(lastOffPlanSuggestion.name, lastOffPlanSuggestion.rules);
    });

    el("logBackBtn").addEventListener("click", async () => {
      await refreshIfDirty();
      showView("home");
    });

    el("logDoneBtn").addEventListener("click", async () => {
      await refreshIfDirty();
      showView("home");
    });

    el("logAnotherBtn").addEventListener("click", async () => {
      await refreshIfDirty();
      resetLogForm();
    });
  }

  // ---------------------------------------------------------------------
  // Trade-detail screen
  // ---------------------------------------------------------------------

  const DETAIL_IDS = {
    icon: "detailIcon", title: "detailTitle", sub: "detailSub",
    r: "detailR", usd: "detailUsd",
    offplanBanner: "detailOffplanBanner", offplanText: "detailOffplanText",
    ruleList: "detailRuleList", xpBadge: "detailXpBadge",
    coachCard: "detailCoachCard", coachText: "detailCoachText",
    didWellCard: "detailDidWellCard", didWellText: "detailDidWellText",
  };

  function renderDetailVerdict(trade, strategyName) {
    renderVerdictBlock(DETAIL_IDS, trade, strategyName);
  }

  function showDetailSubView(sub) {
    el("detailLoadingView").classList.toggle("hidden", sub !== "loading");
    el("detailLoadErrorView").classList.toggle("hidden", sub !== "error");
    el("detailBodyView").classList.toggle("hidden", sub !== "body");
  }

  function numOrNull(id) {
    const v = el(id).value;
    return v === "" ? null : Number(v);
  }

  function strOrNull(id) {
    const v = el(id).value.trim();
    return v === "" ? null : v;
  }

  function computeRR() {
    const entry = parseFloat(el("editEntryPrice").value);
    const exit = parseFloat(el("editExitPrice").value);
    const sl = parseFloat(el("editSlPrice").value);
    return computeRRFromValues(entry, exit, sl);
  }

  function updateRRDisplay() {
    const rr = computeRR();
    el("editRR").textContent = rr === null ? "—" : `1 : ${rr.toFixed(2)}`;
  }

  function populateEditForm(trade) {
    el("editInstrument").value = trade.instrument || "";
    el("editDirection").value = trade.direction || "";
    el("editSession").value = trade.session || "";
    el("editEntryPrice").value = trade.entry_price ?? "";
    el("editExitPrice").value = trade.exit_price ?? "";
    el("editSlPrice").value = trade.sl_price ?? "";
    el("editTpPrice").value = trade.tp_price ?? "";
    el("editRiskPct").value = trade.risk_pct ?? "";
    el("editRMultiple").value = trade.r_multiple ?? "";
    el("editPnlUsd").value = trade.pnl_usd ?? "";
    updateRRDisplay();
  }

  async function openTradeDetail(tradeId) {
    currentDetailTradeId = tradeId;
    el("saveConfirm").classList.add("hidden");
    el("detailErrorBanner").classList.add("hidden");
    showView("detail");
    showDetailSubView("loading");

    let trade;
    try {
      trade = await fetchJSON(`/trades/${tradeId}?user_id=${USER_ID}`);
    } catch (err) {
      showDetailSubView("error");
      return;
    }

    el("detailTopbarTitle").textContent = trade.instrument || "Trade";
    renderDetailVerdict(trade, trade.strategy_name);

    const contextCard = el("detailContextCard");
    if (trade.context_note) {
      el("detailContextText").textContent = trade.context_note;
      contextCard.classList.remove("hidden");
    } else {
      contextCard.classList.add("hidden");
    }

    populateEditForm(trade);
    showDetailSubView("body");
  }

  async function handleSaveTrade() {
    if (currentDetailTradeId === null) return;
    el("detailErrorBanner").classList.add("hidden");
    el("saveConfirm").classList.add("hidden");
    const saveBtn = el("saveTradeBtn");
    saveBtn.disabled = true;

    const payload = {
      user_id: USER_ID,
      instrument: strOrNull("editInstrument"),
      direction: strOrNull("editDirection"),
      entry_price: numOrNull("editEntryPrice"),
      exit_price: numOrNull("editExitPrice"),
      sl_price: numOrNull("editSlPrice"),
      tp_price: numOrNull("editTpPrice"),
      risk_pct: numOrNull("editRiskPct"),
      r_multiple: numOrNull("editRMultiple"),
      pnl_usd: numOrNull("editPnlUsd"),
      session: strOrNull("editSession"),
    };

    try {
      const res = await fetch(`/trades/${currentDetailTradeId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let message = `Request failed (${res.status})`;
        try {
          const body = await res.json();
          if (body && body.detail) message = body.detail;
        } catch (_) {
          // response body wasn't JSON — fall back to the generic message
        }
        throw new Error(message);
      }

      const trade = await res.json();
      dashboardDirty = true;

      el("detailTopbarTitle").textContent = trade.instrument || "Trade";
      renderDetailVerdict(trade, trade.strategy_name);
      populateEditForm(trade);
      el("saveConfirm").classList.remove("hidden");
    } catch (err) {
      el("detailErrorText").textContent =
        err.message || "Couldn't save changes — try again.";
      el("detailErrorBanner").classList.remove("hidden");
    } finally {
      saveBtn.disabled = false;
    }
  }

  async function handleDeleteTrade() {
    if (currentDetailTradeId === null) return;
    const res = await fetch(`/trades/${currentDetailTradeId}?user_id=${USER_ID}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      let message = `Request failed (${res.status})`;
      try {
        const body = await res.json();
        if (body && body.detail) message = body.detail;
      } catch (_) {
        // response body wasn't JSON — fall back to the generic message
      }
      throw new Error(message);
    }
    dashboardDirty = true;
    await refreshIfDirty();
    showView("home");
  }

  function wireDetailScreen() {
    el("detailBackBtn").addEventListener("click", async () => {
      await refreshIfDirty();
      showView("home");
    });
    el("saveTradeBtn").addEventListener("click", handleSaveTrade);
    el("deleteTradeBtn").addEventListener("click", () => {
      openConfirmModal({
        title: "Delete this trade?",
        message: "This can't be undone.",
        confirmLabel: "Delete",
        onConfirm: handleDeleteTrade,
      });
    });
    ["editEntryPrice", "editExitPrice", "editSlPrice"].forEach((id) => {
      el(id).addEventListener("input", updateRRDisplay);
    });
  }

  // ---------------------------------------------------------------------
  // Strategy create/edit screen
  // ---------------------------------------------------------------------

  function hideStrategyError() {
    el("strategyErrorBanner").classList.add("hidden");
  }

  function showStrategyError(message) {
    el("strategyErrorText").textContent = message;
    el("strategyErrorBanner").classList.remove("hidden");
  }

  function selectDirection(value) {
    el("strategyDirection").querySelectorAll(".segment-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.value === value);
    });
  }

  function createRuleRow(ruleId, text) {
    const row = document.createElement("div");
    row.className = "rule-row";
    if (ruleId !== null && ruleId !== undefined) row.dataset.ruleId = String(ruleId);
    row.innerHTML = `
      <input type="text" class="text-input rule-input" maxlength="300"
             placeholder="e.g. Entered only after the confirmation candle closed"
             value="${escapeHtml(text || "")}">
      <button type="button" class="rule-remove-btn" aria-label="Remove rule">${iconCross}</button>
    `;
    row.querySelector(".rule-remove-btn").addEventListener("click", () => row.remove());
    return row;
  }

  function addRuleRow(ruleId, text) {
    const row = createRuleRow(ruleId, text);
    el("strategyRuleRows").appendChild(row);
    return row;
  }

  function resetStrategyForm() {
    editingStrategyId = null;
    el("strategyTopbarTitle").textContent = "New Strategy";
    el("saveStrategyBtn").textContent = "Save strategy";
    el("strategyName").value = "";
    selectDirection("both");
    el("strategyRuleRows").innerHTML = "";
    addRuleRow(null, "");
    hideStrategyError();
    el("deleteStrategyBtn").classList.add("hidden");
  }

  function openNewStrategyScreen() {
    resetStrategyForm();
    showView("strategy");
  }

  // Reuses the same create screen as openNewStrategyScreen — prefilled with
  // a name + rules the off-plan "smart suggestion" pass drafted, still
  // editable, still a POST (not a PATCH) since nothing has been saved yet.
  function openNewStrategyScreenFromSuggestion(name, rules) {
    resetStrategyForm();
    el("strategyName").value = name || "";
    el("strategyRuleRows").innerHTML = "";
    const validRules = (rules || []).filter((r) => r && r.text && r.text.trim());
    if (validRules.length === 0) {
      addRuleRow(null, "");
    } else {
      // No ids — these are freshly suggested, not existing rule rows, so
      // the server assigns real ids on save just like any other new rule.
      validRules.forEach((r) => addRuleRow(null, r.text));
    }
    showView("strategy");
  }

  function openEditStrategyScreen(strategyId) {
    // strategiesCache is active-only (used by the dashboard/log picker);
    // manageStrategiesCache includes inactive ones — a strategy opened for
    // edit from the Manage screen may only exist in the latter.
    const strategy = strategiesCache.find((s) => s.id === strategyId)
      || manageStrategiesCache.find((s) => s.id === strategyId);
    if (!strategy) return;

    editingStrategyId = strategyId;
    el("strategyTopbarTitle").textContent = "Edit Strategy";
    el("saveStrategyBtn").textContent = "Save changes";
    el("strategyName").value = strategy.name || "";
    selectDirection(strategy.direction_bias || "both");

    el("strategyRuleRows").innerHTML = "";
    const rules = strategy.rules || [];
    if (rules.length === 0) {
      addRuleRow(null, "");
    } else {
      rules.forEach((r) => addRuleRow(r.id, r.text));
    }
    hideStrategyError();
    el("deleteStrategyBtn").classList.remove("hidden");
    showView("strategy");
  }

  async function refreshStrategiesCache() {
    try {
      strategiesCache = await fetchJSON(`/strategies?user_id=${USER_ID}&is_active=true`);
    } catch (err) {
      // keep whatever was cached before — the picker/filters just won't
      // reflect the latest save until the next successful load
    }
    return strategiesCache;
  }

  // Shared by handleSaveStrategy and handleDeleteStrategy — both need to
  // return to wherever the edit screen was opened from (log picker,
  // Manage Strategies, or the dashboard) with that view's data refreshed.
  async function returnFromStrategyEdit() {
    if (strategyReturnView === "log") {
      const prevSelected = selectedStrategyValue;
      await refreshStrategiesCache();
      renderStrategyPicker();
      if (prevSelected !== OFFPLAN_VALUE && strategiesCache.some((s) => String(s.id) === prevSelected)) {
        selectStrategy(prevSelected);
      }
      showView("log");
    } else if (strategyReturnView === "manage") {
      await refreshStrategiesCache();
      await loadManageStrategies();
      dashboardDirty = true;
      showView("manage");
    } else {
      await loadDashboard();
      showView("home");
    }
  }

  async function handleSaveStrategy() {
    hideStrategyError();

    const name = el("strategyName").value.trim();
    if (!name) {
      showStrategyError("Give this strategy a name.");
      return;
    }

    const directionBtn = el("strategyDirection").querySelector(".segment-btn.active");
    const directionBias = directionBtn ? directionBtn.dataset.value : "both";

    const rules = Array.from(el("strategyRuleRows").querySelectorAll(".rule-row"))
      .map((row) => ({
        id: row.dataset.ruleId ? Number(row.dataset.ruleId) : null,
        text: row.querySelector(".rule-input").value.trim(),
      }))
      .filter((r) => r.text.length > 0);

    if (rules.length === 0) {
      showStrategyError("Add at least one checkable rule.");
      return;
    }

    const saveBtn = el("saveStrategyBtn");
    saveBtn.disabled = true;

    try {
      const payload = { user_id: USER_ID, name, direction_bias: directionBias, rules };
      const url = editingStrategyId === null ? "/strategies" : `/strategies/${editingStrategyId}`;
      const method = editingStrategyId === null ? "POST" : "PATCH";

      const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let message = `Request failed (${res.status})`;
        try {
          const body = await res.json();
          if (body && body.detail) message = body.detail;
        } catch (_) {
          // response body wasn't JSON — fall back to the generic message
        }
        throw new Error(message);
      }
      await res.json();
      await returnFromStrategyEdit();
    } catch (err) {
      showStrategyError(err.message || "Couldn't save this strategy — try again.");
    } finally {
      saveBtn.disabled = false;
    }
  }

  // Soft-delete only — trades already checked against this strategy keep
  // their strategy_id and rule_results, they just won't see it in the
  // active-strategy chips/picker anymore. Mirrors the existing PATCH
  // .../is_active toggle, not a new deletion path on the backend.
  async function handleDeleteStrategy() {
    if (editingStrategyId === null) return;
    const res = await fetch(`/strategies/${editingStrategyId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: USER_ID, is_active: false }),
    });
    if (!res.ok) {
      let message = `Request failed (${res.status})`;
      try {
        const body = await res.json();
        if (body && body.detail) message = body.detail;
      } catch (_) {
        // response body wasn't JSON — fall back to the generic message
      }
      throw new Error(message);
    }
    await res.json();
    await returnFromStrategyEdit();
  }

  function wireStrategyScreen() {
    el("strategyBackBtn").addEventListener("click", () => {
      showView(strategyReturnView);
    });

    el("strategyDirection").querySelectorAll(".segment-btn").forEach((btn) => {
      btn.addEventListener("click", () => selectDirection(btn.dataset.value));
    });

    el("addRuleBtn").addEventListener("click", () => {
      addRuleRow(null, "").querySelector(".rule-input").focus();
    });

    el("saveStrategyBtn").addEventListener("click", handleSaveStrategy);

    el("deleteStrategyBtn").addEventListener("click", () => {
      openConfirmModal({
        title: "Remove this strategy?",
        message: "It'll be hidden from your setups, but past trades keep their results.",
        confirmLabel: "Remove",
        onConfirm: handleDeleteStrategy,
      });
    });
  }

  // ---------------------------------------------------------------------
  // Manage-strategies screen — lists ALL of the user's strategies, active
  // and inactive, and is the only place an inactive one can be reactivated
  // (deactivating from here, or via "Remove strategy" on the edit screen,
  // is otherwise a dead end). GET /strategies with no is_active filter
  // already returns everything, so no new backend endpoint is needed.
  // ---------------------------------------------------------------------

  function hideManageError() {
    el("manageErrorBanner").classList.add("hidden");
  }

  function showManageError(message) {
    el("manageErrorText").textContent = message;
    el("manageErrorBanner").classList.remove("hidden");
  }

  async function loadManageStrategies() {
    try {
      manageStrategiesCache = await fetchJSON(`/strategies?user_id=${USER_ID}`);
    } catch (err) {
      manageStrategiesCache = [];
    }
    renderManageStrategyList();
  }

  async function setStrategyActive(strategyId, isActive) {
    const res = await fetch(`/strategies/${strategyId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: USER_ID, is_active: isActive }),
    });
    if (!res.ok) {
      let message = `Request failed (${res.status})`;
      try {
        const body = await res.json();
        if (body && body.detail) message = body.detail;
      } catch (_) {
        // response body wasn't JSON — fall back to the generic message
      }
      throw new Error(message);
    }
    await res.json();
    dashboardDirty = true;
    await refreshStrategiesCache();
    await loadManageStrategies();
  }

  function renderManageStrategyList() {
    const container = el("manageStrategyList");
    container.innerHTML = "";

    if (manageStrategiesCache.length === 0) {
      container.innerHTML = `
        <div class="empty-state card">
          <h3>No strategies yet</h3>
          <p>Create one from the "+" on your dashboard's filter row.</p>
        </div>`;
      return;
    }

    manageStrategiesCache.forEach((s) => {
      const ruleCount = (s.rules || []).length;
      const row = document.createElement("div");
      row.className = "manage-row card" + (s.is_active ? "" : " inactive");
      row.innerHTML = `
        <div class="manage-row-main">
          <div class="manage-row-name">${escapeHtml(s.name)}</div>
          <div class="manage-row-hint">${ruleCount} rule${ruleCount === 1 ? "" : "s"}${s.is_active ? "" : " · Inactive"}</div>
        </div>
        <button type="button" class="strategy-edit-btn" aria-label="Edit ${escapeHtml(s.name)}">${iconEdit}</button>
        <button type="button" class="toggle-switch ${s.is_active ? "on" : "off"}" role="switch"
                aria-checked="${s.is_active}" aria-label="${s.is_active ? "Deactivate" : "Reactivate"} ${escapeHtml(s.name)}">
          <span class="toggle-knob"></span>
        </button>
      `;

      row.querySelector(".strategy-edit-btn").addEventListener("click", () => {
        strategyReturnView = "manage";
        openEditStrategyScreen(s.id);
      });

      row.querySelector(".toggle-switch").addEventListener("click", () => {
        hideManageError();
        if (s.is_active) {
          openConfirmModal({
            title: "Deactivate this strategy?",
            message: "It'll be hidden from your setups but past trades keep their results.",
            confirmLabel: "Deactivate",
            onConfirm: () => setStrategyActive(s.id, false),
          });
        } else {
          // Reactivating just undoes a hide — no data loss, so no confirm
          // dialog, unlike deactivating.
          setStrategyActive(s.id, true).catch((err) => {
            showManageError(err.message || "Couldn't reactivate — try again.");
          });
        }
      });

      container.appendChild(row);
    });
  }

  async function openManageStrategiesScreen() {
    hideManageError();
    showView("manage");
    await loadManageStrategies();
  }

  function wireManageScreen() {
    el("manageBackBtn").addEventListener("click", async () => {
      await refreshIfDirty();
      showView("home");
    });
  }

  // ---------------------------------------------------------------------
  // Shared confirm modal — used by both the trade-delete and
  // strategy-remove flows below. `onConfirm` does the actual request; if it
  // throws, the modal stays open (with whatever error the caller surfaced)
  // instead of silently closing on a failed delete.
  // ---------------------------------------------------------------------

  let confirmModalOnConfirm = null;

  function openConfirmModal({ title, message, confirmLabel = "Delete", onConfirm }) {
    el("confirmModalTitle").textContent = title;
    el("confirmModalMessage").textContent = message;
    el("confirmModalErrorBanner").classList.add("hidden");
    const confirmBtn = el("confirmModalConfirm");
    confirmBtn.textContent = confirmLabel;
    confirmBtn.disabled = false;
    confirmModalOnConfirm = onConfirm;
    el("confirmModal").classList.remove("hidden");
  }

  function closeConfirmModal() {
    el("confirmModal").classList.add("hidden");
    confirmModalOnConfirm = null;
  }

  function wireConfirmModal() {
    el("confirmModalCancel").addEventListener("click", closeConfirmModal);
    el("confirmModal").addEventListener("click", (e) => {
      if (e.target.id === "confirmModal") closeConfirmModal();
    });
    el("confirmModalConfirm").addEventListener("click", async () => {
      if (!confirmModalOnConfirm) return;
      const confirmBtn = el("confirmModalConfirm");
      confirmBtn.disabled = true;
      el("confirmModalErrorBanner").classList.add("hidden");
      try {
        await confirmModalOnConfirm();
        closeConfirmModal();
      } catch (err) {
        el("confirmModalErrorText").textContent =
          err.message || "Something went wrong — try again.";
        el("confirmModalErrorBanner").classList.remove("hidden");
        confirmBtn.disabled = false;
      }
    });
  }

  async function init() {
    el("logTradeBtn").addEventListener("click", openLogScreen);
    wireLogScreen();
    wireDetailScreen();
    wireStrategyScreen();
    wireManageScreen();
    wireConfirmModal();
    await loadDashboard();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
