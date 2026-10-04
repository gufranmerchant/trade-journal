/* "On Bluesky this week" section for Keyword & Category Research.

   Loaded lazily by nav.js, and ONLY when a response carries a `bluesky` object (the integration is
   switched on and found usable posts). The section's DOM is created here, at that moment, so with
   it off - or with nothing to show - the page contains no trace of it.

   Posts are shown exactly as posted, only ever through textContent (never innerHTML), with a link
   back to the post on bsky.app. Nothing else about the author is shown. */
(function () {
  "use strict";

  const SECTION_ATTR = "data-bluesky-section";
  const BSKY_PREFIX = "https://bsky.app/profile/";

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  function age(iso) {
    const t = Date.parse(iso);
    if (!t) return "";
    const hours = Math.max(0, (Date.now() - t) / 3600000);
    if (hours < 1) return "just now";
    if (hours < 48) return `${Math.round(hours)}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  function postCard(p) {
    const body = el("div", "idea-card-body");
    const text = el("div", "idea-card-title", p.text);
    text.style.whiteSpace = "pre-wrap";
    text.style.fontWeight = "500";   // posts can be long; the title weight is too heavy for running text
    text.style.overflowWrap = "anywhere";
    body.appendChild(text);
    const meta = el("div", "idea-card-rationale");
    meta.appendChild(document.createTextNode(
      `@${p.handle} · ${count(Number(p.likes) || 0, "like", "likes")} · ${count(Number(p.replies) || 0, "reply", "replies")}` +
      (age(p.created_at) ? ` · ${age(p.created_at)}` : "") + " · "));
    if (typeof p.url === "string" && p.url.startsWith(BSKY_PREFIX)) {
      const a = el("a", null, "View on Bluesky");
      a.href = p.url; a.target = "_blank"; a.rel = "noopener noreferrer nofollow";
      a.style.color = "var(--teal, #2dd4bf)";
      a.style.fontWeight = "600";
      meta.appendChild(a);
    }
    body.appendChild(meta);
    const card = el("div", "card idea-card");
    card.appendChild(body);
    return card;
  }

  function render(data) {
    const wrap = document.getElementById("resultsWrap");
    const b = data && data.bluesky;
    if (!wrap || !b || !Array.isArray(b.posts) || !b.posts.length) return;
    wrap.querySelectorAll("[" + SECTION_ATTR + "]").forEach((n) => n.remove());

    const section = el("div", "card platform-tag-card");
    section.setAttribute(SECTION_ATTR, "1");
    section.appendChild(el("div", "section-label", "On Bluesky this week"));
    section.appendChild(el("p", "section-hint",
      `Popular recent posts mentioning “${b.query}”, exactly as posted — a glimpse of what people are saying, ` +
      "not market data or an endorsement, and many are promotional. Source: Bluesky."));
    const list = el("div", "idea-list");
    b.posts.forEach((p) => list.appendChild(postCard(p)));
    section.appendChild(list);
    wrap.appendChild(section);
  }

  window.MirrorBluesky = { render };
})();
