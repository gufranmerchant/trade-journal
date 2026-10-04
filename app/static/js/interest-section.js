/* "Interest over time" section for Keyword & Category Research.

   Loaded lazily by nav.js, and ONLY when a response carries an `interest` object (the topic has a
   genre-level Wikipedia article with enough traffic). The section's DOM is created here, at that
   moment, so a topic without one gets nothing - no empty container, no placeholder.

   All text goes in via textContent; the only link is to en.wikipedia.org. */
(function () {
  "use strict";

  const SECTION_ATTR = "data-interest-section";
  const WIKI_PREFIX = "https://en.wikipedia.org/wiki/";
  const SVG_NS = "http://www.w3.org/2000/svg";

  const fmt = (n) => Number(n).toLocaleString("en-US");
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const monthLabel = (ym) => { const [y, m] = ym.split("-"); return `${MONTHS[Number(m) - 1]} ${y}`; };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function sparkline(months) {
    const W = 240, H = 56, PAD = 4;
    const views = months.map((m) => m.views);
    const max = Math.max(...views), min = Math.min(...views);
    const span = max - min || 1;
    const x = (i) => PAD + (i * (W - 2 * PAD)) / Math.max(1, months.length - 1);
    const y = (v) => H - PAD - ((v - min) / span) * (H - 2 * PAD);
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label",
      `Monthly Wikipedia page views, ${monthLabel(months[0].month)} to ${monthLabel(months[months.length - 1].month)}: ` +
      `from ${fmt(views[0])} to ${fmt(views[views.length - 1])}, low ${fmt(min)}, high ${fmt(max)}`);
    svg.style.cssText = "width:100%;max-width:360px;height:auto;display:block;color:var(--teal, #2dd4bf)";
    const line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", months.map((m, i) => `${x(i).toFixed(1)},${y(m.views).toFixed(1)}`).join(" "));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "currentColor");
    line.setAttribute("stroke-width", "2");
    line.setAttribute("stroke-linejoin", "round");
    line.setAttribute("stroke-linecap", "round");
    svg.appendChild(line);
    const last = document.createElementNS(SVG_NS, "circle");
    last.setAttribute("cx", x(months.length - 1).toFixed(1));
    last.setAttribute("cy", y(views[views.length - 1]).toFixed(1));
    last.setAttribute("r", "3");
    last.setAttribute("fill", "currentColor");
    svg.appendChild(last);
    return svg;
  }

  const TREND_WORD = { rising: "Rising", falling: "Falling", steady: "Steady" };

  function headline(d) {
    if (d.change_pct === null || d.change_pct === undefined || !d.trend) {
      return `About ${fmt(d.recent_monthly_avg)} views a month recently (not enough history for a year-over-year comparison).`;
    }
    const sign = d.change_pct > 0 ? "+" : d.change_pct < 0 ? "−" : "";
    return `${TREND_WORD[d.trend]}: ${sign}${Math.abs(d.change_pct)}% over the same 3 months last year · about ${fmt(d.recent_monthly_avg)} views a month recently.`;
  }

  function render(data) {
    const wrap = document.getElementById("resultsWrap");
    const d = data && data.interest;
    if (!wrap || !d || !d.article || !Array.isArray(d.months) || d.months.length < 2) return;
    wrap.querySelectorAll("[" + SECTION_ATTR + "]").forEach((n) => n.remove());

    const card = el("div", "card platform-tag-card");
    card.setAttribute(SECTION_ATTR, "1");
    card.appendChild(el("div", "section-label", "Interest over time"));

    const hint = el("p", "section-hint");
    hint.appendChild(document.createTextNode("Monthly views of Wikipedia’s “"));
    const url = typeof d.article.url === "string" && d.article.url.startsWith(WIKI_PREFIX) ? d.article.url : null;
    if (url) {
      const a = el("a", null, d.article.title);
      a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer nofollow";
      hint.appendChild(a);
    } else {
      hint.appendChild(document.createTextNode(d.article.title));
    }
    hint.appendChild(document.createTextNode(
      "” page over the last 12 months — a rough sign of public curiosity about the genre, not book sales or reader demand. " +
      (d.article.redirected_from ? `Wikipedia redirects “${d.article.redirected_from}” to this page. ` : "") +
      "Source: Wikipedia."));
    card.appendChild(hint);

    card.appendChild(el("div", "idea-card-title", headline(d)));
    card.appendChild(sparkline(d.months));
    const axis = el("div", "idea-card-rationale", `${monthLabel(d.months[0].month)} → ${monthLabel(d.months[d.months.length - 1].month)}`);
    card.appendChild(axis);

    // Before any Reddit sections, which load independently; otherwise at the end.
    wrap.insertBefore(card, wrap.querySelector("[data-reddit-section]"));
  }

  window.MirrorInterest = { render };
})();
