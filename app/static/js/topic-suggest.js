/* Topic suggestions for Keyword & Category Research: as the user types, show a few clickable
   genre/category chips under the input (Wikipedia genre titles blended with category terms from
   Google Books, via /tools/topic-suggestions). Clicking a chip only fills the input - it never
   submits - so the user can refine the topic first.

   Quiet by design: debounced, stale/in-flight requests are cancelled, and any failure just shows
   no chips. Suggestion text goes into the DOM via textContent only. */
(function () {
  "use strict";

  const input = document.getElementById("topicInput");
  if (!input) return;

  const MIN_CHARS = 3;
  const DEBOUNCE_MS = 300;

  const box = document.createElement("div");
  box.className = "chip-static-list topic-suggest hidden";
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Suggested topics");
  box.style.marginTop = "8px";
  input.insertAdjacentElement("afterend", box);

  const SOURCE_LABEL = { wikipedia: "Genre from Wikipedia", books: "Category seen in Google Books" };

  let timer = null;
  let controller = null;

  function clear() {
    clearTimeout(timer);
    if (controller) controller.abort();
    box.replaceChildren();
    box.classList.add("hidden");
  }

  function show(suggestions) {
    box.replaceChildren();
    suggestions.forEach((s) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = s.text;
      chip.title = SOURCE_LABEL[s.source] || "";
      chip.addEventListener("click", () => {
        input.value = s.text;
        clear();
        input.focus();
      });
      box.appendChild(chip);
    });
    box.classList.toggle("hidden", suggestions.length === 0);
  }

  function fetchSuggestions(query) {
    if (controller) controller.abort();
    controller = new AbortController();
    const mine = controller;
    fetch("/tools/topic-suggestions?q=" + encodeURIComponent(query), { signal: mine.signal })
      .then((res) => (res.ok ? res.json() : { suggestions: [] }))
      .then((data) => {
        if (mine !== controller || input.value.trim() !== query) return;   // the user has typed on since
        show(Array.isArray(data.suggestions) ? data.suggestions : []);
      })
      .catch(() => {});
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const query = input.value.trim();
    if (query.length < MIN_CHARS) return clear();
    timer = setTimeout(() => fetchSuggestions(query), DEBOUNCE_MS);
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Escape" || e.key === "Enter") clear(); });
  const researchBtn = document.getElementById("researchBtn");
  if (researchBtn) researchBtn.addEventListener("click", clear);
})();
