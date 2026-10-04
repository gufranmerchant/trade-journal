/* Reddit sections for Keyword & Category Research ("Trending in this genre", "Recommended
   Reading") and Post Idea Finder ("Trending Right Now").

   Loaded lazily by nav.js, and ONLY when a tool response actually carries Reddit data. The
   sections' DOM is created here, at that moment, so with Reddit off - or on but with nothing to
   show - neither the served page HTML nor the rendered page contains any trace of Reddit.

   Thread titles are shown exactly as posted, but only ever through textContent (never
   innerHTML), and links are accepted only if they point at reddit.com. The server never sends
   post or comment bodies, so there is nothing else to show. */
(function () {
  "use strict";

  const SECTION_ATTR = "data-reddit-section";
  const REDDIT_URL_PREFIX = "https://www.reddit.com/";

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function link(text, url) {
    const a = el("a", null, text);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer nofollow";
    return a;
  }

  const redditUrl = (u) => (typeof u === "string" && u.startsWith(REDDIT_URL_PREFIX) ? u : null);
  const httpsUrl = (u) => (typeof u === "string" && u.startsWith("https://") ? u : null);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function age(createdUtc) {
    const hours = Math.max(0, (Date.now() / 1000 - createdUtc) / 3600);
    if (hours < 1) return "just now";
    if (hours < 48) return `${Math.round(hours)}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  const meta = (t) => `r/${t.subreddit} · ${plural(Number(t.comments) || 0, "comment")} · ${age(t.created_utc)}`;

  // A thread's title as a link when its URL is a real reddit.com URL, otherwise as plain text.
  function titleNode(text, url) {
    const safe = redditUrl(url);
    return safe ? link(text, safe) : document.createTextNode(text);
  }

  function threadCard(t) {
    const body = el("div", "idea-card-body");
    const title = el("div", "idea-card-title");
    title.appendChild(titleNode(t.title, t.url));
    body.appendChild(title);
    body.appendChild(el("div", "idea-card-rationale", meta(t)));
    const card = el("div", "card idea-card");
    card.appendChild(body);
    return card;
  }

  function section(label, hint, children) {
    const wrap = el("div", "card platform-tag-card");
    wrap.setAttribute(SECTION_ATTR, "1");
    wrap.appendChild(el("div", "section-label", label));
    wrap.appendChild(el("p", "section-hint", hint));
    const list = el("div", "idea-list");
    children.forEach((c) => list.appendChild(c));
    wrap.appendChild(list);
    return wrap;
  }

  const subredditList = (subs) => (subs || []).map((s) => `r/${s}`).join(", ");

  function recommendedCard(b, index) {
    const body = el("div", "idea-card-body");
    const title = el("div", "idea-card-title");
    const bookUrl = httpsUrl(b.book_url);
    title.appendChild(bookUrl ? link(b.title, bookUrl) : document.createTextNode(b.title));
    body.appendChild(title);
    body.appendChild(el("div", "idea-card-rationale", `by ${b.author}`));
    body.appendChild(el("div", "idea-card-rationale",
      `Readers in ${subredditList(b.subreddits)} are discussing this · ${plural(Number(b.mentions) || 0, "thread")}`));
    (b.threads || []).forEach((t) => {
      const line = el("div", "idea-card-rationale");
      line.appendChild(titleNode(t.title, t.url));
      line.appendChild(document.createTextNode(` (${meta(t)})`));
      body.appendChild(line);
    });
    const card = el("div", "card idea-card");
    card.appendChild(el("div", "idea-card-number", String(index + 1)));
    card.appendChild(body);
    return card;
  }

  function render(kind, data) {
    const wrap = document.getElementById("resultsWrap");
    if (!wrap) return;
    wrap.querySelectorAll("[" + SECTION_ATTR + "]").forEach((n) => n.remove());

    if (kind === "keyword" && data.reddit) {
      const r = data.reddit;
      if ((r.trending || []).length) {
        wrap.appendChild(section(
          "Trending in this genre",
          `Live threads from ${subredditList(r.subreddits)}, titles as posted — follow a link to read the discussion. Source: Reddit.`,
          r.trending.map(threadCard)));
      }
      if ((r.recommended || []).length) {
        wrap.appendChild(section(
          "Recommended Reading",
          "Books from the competitor research above that readers are discussing right now — a mention isn’t an endorsement. Craft inspiration, not market data. Source: Reddit.",
          r.recommended.map(recommendedCard)));
      }
    } else if (kind === "social" && data.trending && (data.trending.threads || []).length) {
      const t = data.trending;
      wrap.appendChild(section(
        "Trending Right Now",
        `What’s being discussed right now in ${subredditList(t.subreddits)} — not filtered to your topic. ` +
        "A possible angle to connect to your post, not a replacement for the ideas above. Titles as posted. Source: Reddit.",
        t.threads.map(threadCard)));
    }
  }

  window.MirrorReddit = { render };
})();
