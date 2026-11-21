// Kestrel Capture popup (issue #186, SPEC-INTERFACES 5.2). It shows one screen, chosen each time
// it opens: set up, Kestrel not running, the role summary for a posting Kestrel knows, roles
// waiting for a description, or Capture. It reads the open tab only when the person clicks Read
// or Capture, never navigates or reloads it, and never applies, fills or submits anything there.
// "I've applied" records a status in Kestrel; "Get PDF" saves a file Kestrel made.
// Every request goes to Kestrel on this computer through api.js.

(function () {
  "use strict";

  const Api = self.KestrelApi;
  const app = document.getElementById("app");
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const state = { tab: null, host: "", found: null, config: null, picked: null, page: null,
    waitingHere: null };

  // ------------------------------------------------------------------ small DOM helpers
  // Text always goes in through textContent; nothing from Kestrel or the page is parsed as HTML.
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(typeof kid === "string" ? document.createTextNode(kid) : kid);
    }
    return el;
  }

  const ICONS = {
    gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
    back: "M15 18l-6-6 6-6",
    down: "M6 9l6 6 6-6",
    up: "M18 15l-6-6-6 6",
    check: "M5 12l5 5L20 7",
    page: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5",
  };

  function icon(name) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", ICONS[name]);
    svg.append(p);
    return svg;
  }

  function shortDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
    return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : "";
  }

  function show(view, { wide = false } = {}) {
    app.classList.toggle("wide", wide);
    app.replaceChildren(view);
  }

  // ------------------------------------------------------------------ header, gear menu
  function header({ title, back, extra } = {}) {
    const kids = [];
    if (back) {
      kids.push(h("button", { class: "icon", type: "button", "aria-label": "Back", onclick: back }, icon("back")));
      kids.push(h("span", { class: "grow name", text: title }));
    } else {
      kids.push(h("img", { class: "logo", src: "icon.svg", alt: "" }));
      const connected = Boolean(state.found && state.config);
      kids.push(h("span", { class: "grow" },
        h("span", { class: "name", text: "Kestrel" }), " ",
        h("span", { class: "status" },
          h("span", { class: connected ? "dot" : "dot off" }),
          connected ? "Connected" : "Not set up")));
    }
    if (extra) kids.push(extra);
    kids.push(gearButton());
    return h("header", { class: "bar wrap-rel" }, kids);
  }

  function closeMenus() {
    for (const m of document.querySelectorAll(".menu")) m.remove();
  }

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menu") && !e.target.closest("[data-menu]")) closeMenus();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMenus();
  });

  function gearButton() {
    const btn = h("button", { class: "icon", type: "button", "aria-label": "Settings and help",
      "aria-haspopup": "menu", "data-menu": "gear" }, icon("gear"));
    btn.addEventListener("click", async () => {
      if (document.querySelector(".menu.gear")) return closeMenus();
      closeMenus();
      const hidden = await hiddenSites();
      const isHidden = state.host && hidden.includes(state.host);
      const menu = h("div", { class: "menu gear", role: "menu" },
        h("button", { role: "menuitem", type: "button", text: "Settings",
          onclick: () => chrome.runtime.openOptionsPage() }),
        state.host ? h("button", { role: "menuitem", type: "button",
          text: isHidden ? "Show badges on this site" : "Hide badges on this site",
          onclick: async () => { await toggleSite(state.host); closeMenus(); } }) : null,
        h("button", { role: "menuitem", type: "button", text: "Open Kestrel",
          onclick: () => openKestrel("/") }),
        h("hr"),
        h("button", { role: "menuitem", type: "button", text: "About Kestrel Capture",
          onclick: () => openOptions("about") }));
      const rect = btn.getBoundingClientRect();
      menu.style.setProperty("top", `${rect.bottom + 4}px`);
      menu.style.setProperty("right", "8px");
      document.body.append(menu);
      const first = menu.querySelector("button");
      if (first) first.focus();
    });
    return btn;
  }

  async function hiddenSites() {
    const r = await chrome.storage.local.get(["hiddenSites"]);
    return Array.isArray(r.hiddenSites) ? r.hiddenSites : [];
  }

  async function toggleSite(host) {
    const list = await hiddenSites();
    const next = list.includes(host) ? list.filter((x) => x !== host) : [...list, host];
    await chrome.storage.local.set({ hiddenSites: next });
  }

  function openOptions(section) {
    const url = chrome.runtime.getURL(`options.html#${section}`);
    chrome.tabs.create({ url });
  }

  // Kestrel's own pages, on this computer only: the base comes from the probe, the path is ours.
  function openKestrel(path) {
    // Only a path on Kestrel itself: "/x", never "//host" or "@host" (D-EXT-769).
    if (!state.found || typeof path !== "string" || !/^\/(?!\/)/.test(path)) return;
    chrome.tabs.create({ url: state.found.base + path });
  }

  // ------------------------------------------------------------------ setup (5.2.3)
  async function hostAllowed() {
    try {
      return await chrome.permissions.contains({ origins: ["http://127.0.0.1/*"] });
    } catch (e) {
      return true;
    }
  }

  async function viewSetup({ error = "" } = {}) {
    const body = h("div", { class: "body" });
    const view = h("section", { class: "view" }, header(), body);
    show(view);
    body.append(h("h1", { text: "Set up in two steps" }));
    if (!(await hostAllowed())) {
      body.append(h("div", { class: "box" },
        h("p", { text: "Allow Kestrel to talk to this computer" }),
        h("button", { class: "primary full", type: "button", text: "Allow",
          onclick: async () => {
            await chrome.permissions.request({ origins: ["http://127.0.0.1/*", "http://localhost/*"] });
            viewSetup();
          } })));
      return;
    }
    const s1 = h("div", { class: "step" }, h("span", { class: "n", text: "1" }),
      h("div", { class: "c" }, h("b", { text: "Open Kestrel on this computer" }),
        h("p", { class: "muted", text: "Looking for Kestrel..." })));
    body.append(s1);
    const found = await Api.resolve({ force: true });
    state.found = found;
    const note = s1.querySelector("p");
    if (found) {
      s1.querySelector(".n").className = "n done";
      s1.querySelector(".n").replaceChildren(icon("check"));
      note.className = "ok";
      note.textContent = `Found Kestrel ${found.version}`;
    } else {
      note.textContent = "Not found. Open Kestrel, then try again.";
      body.append(h("button", { class: "full", type: "button", text: "Try again", onclick: () => viewSetup() }));
      return;
    }
    const input = h("input", { type: "password", id: "key", placeholder: "Paste key here",
      autocomplete: "off", spellcheck: "false" });
    const s2 = h("div", { class: "step" }, h("span", { class: "n", text: "2" }),
      h("div", { class: "c" },
        h("label", { class: "lbl", for: "key", text: "Paste your connection key" }),
        input,
        h("p", { class: "muted", text: "In Kestrel: Settings, Browser extension, Copy key." })));
    body.append(s2);
    if (error) {
      body.append(h("div", { class: "box bad", role: "alert",
        text: "Kestrel didn't accept that key. Copy it again from Settings, Browser extension." }));
    }
    body.append(h("button", { class: "primary full", type: "button", text: "Connect",
      onclick: async (e) => {
        e.currentTarget.disabled = true;
        const secret = input.value.trim();
        const r = await Api.call("/api/extension/config", { secret, force: true });
        if (r.ok) {
          await chrome.storage.local.set({ secret });
          state.config = r.body;
          return viewSetupDone();
        }
        return viewSetup({ error: r.status === 403 ? "wrong" : "" });
      } }));
    input.focus();
  }

  function viewSetupDone() {
    const body = h("div", { class: "body" },
      h("div", { class: "done-mark" }, icon("check")),
      h("h1", { text: "You're set up" }),
      h("p", { class: "muted", text: "Open any job posting and click the Kestrel icon to capture it. Postings Kestrel already knows get a small badge." }),
      h("button", { class: "primary full", type: "button", text: "Done", onclick: () => window.close() }));
    show(h("section", { class: "view" }, header(), body));
  }

  function viewLinkedIn() {
    const body = h("div", { class: "body" },
      h("h1", { text: "Kestrel doesn't read LinkedIn pages" }),
      h("p", { class: "muted", text: "Copy the posting's text and paste it in Kestrel instead." }),
      h("button", { class: "primary full", type: "button", text: "Open Kestrel", onclick: () => openKestrel("/pipeline") }));
    show(h("section", { class: "view" }, header(), body));
  }

  function viewOffline() {
    const body = h("div", { class: "body" },
      h("h1", { text: "Open Kestrel on this computer" }),
      h("p", { class: "muted", text: "The extension only talks to Kestrel on your own machine." }),
      h("button", { class: "primary full", type: "button", text: "Set up the extension", onclick: () => viewSetup() }),
      h("button", { class: "link", type: "button", text: "I've opened Kestrel, try again", onclick: () => start() }));
    show(h("section", { class: "view" }, header(), body));
  }

  // ------------------------------------------------------------------ capture (state 4)
  async function capture(scoreNow, button) {
    if (button) button.disabled = true;
    let page;
    try {
      page = await Api.readTab(state.tab.id);
    } catch (e) {
      return confirmLine(null, "Kestrel couldn't read this page.");
    }
    const r = await Api.call("/api/opportunities/ingest", { method: "POST", force: true,
      body: { url: page.url, text: page.text, title: page.title, score_now: Boolean(scoreNow) } });
    if (!r.ok) return confirmLine(null, "Kestrel couldn't add this page.");
    const fresh = r.body && r.body.stage === "new";
    chrome.runtime.sendMessage({ type: "flash", tabId: state.tab.id, ok: true, message: "Added to Kestrel." });
    if (!fresh) return confirmLine(true, "Already in Kestrel.");
    return confirmLine(true, scoreNow ? "Added. It's first in Kestrel's queue." : "Added to Kestrel.");
  }

  function confirmLine(ok, text) {
    const spot = document.getElementById("confirm");
    if (!spot) return;
    spot.replaceChildren(ok ? h("p", { class: "confirm" }, icon("check"), text)
      : h("div", { class: "box warn", role: "alert", text }));
  }

  function captureBlock() {
    const main = h("button", { class: "primary main", type: "button", text: "Capture this role",
      onclick: (e) => capture(false, e.currentTarget) });
    const arrow = h("button", { class: "primary arrow", type: "button", "aria-label": "More ways to capture",
      "aria-haspopup": "menu", "data-menu": "capture" }, icon("down"));
    const wrap = h("div", { class: "wrap-rel" }, h("div", { class: "split" }, main, arrow));
    arrow.addEventListener("click", () => {
      if (wrap.querySelector(".menu")) return closeMenus();
      closeMenus();
      const menu = h("div", { class: "menu", role: "menu" },
        h("button", { role: "menuitem", type: "button", onclick: () => { closeMenus(); capture(true, main); } },
          "Capture and score now", h("small", { text: "Puts it first in Kestrel's queue." })),
        h("button", { role: "menuitem", type: "button", onclick: () => openKestrel("/pipeline") },
          "Paste it in Kestrel instead", h("small", { text: "For pages the extension can't read well." })));
      menu.style.setProperty("right", "0");
      wrap.append(menu);
    });
    return wrap;
  }

  function viewNothingWaiting() {
    const body = h("div", { class: "body" },
      captureBlock(),
      h("p", { class: "muted", text: "New roles are scored in Kestrel's next run." }),
      h("div", { id: "confirm" }));
    show(h("section", { class: "view" }, header(), body));
  }

  // ------------------------------------------------------------------ waiting (states 1 and 5)
  function roleRows(items, { otherSite }) {
    const set = h("fieldset", { class: "list" }, h("legend", { class: "hidden", text: "Waiting roles" }));
    items.forEach((it, i) => {
      const input = h("input", { type: "radio", name: "role", value: String(it.id), checked: i === 0 });
      input.addEventListener("change", () => { state.picked = it; });
      const pill = !it.host_match && it.source_label && (otherSite || state.host)
        ? h("span", { class: "pill", text: it.source_label }) : null;
      set.append(h("label", { class: "opt" }, input,
        h("span", null, h("span", { class: "t" }, it.title, pill),
          h("span", { class: "s", text: [it.company, shortDate(it.found)].filter(Boolean).join(" · ") }))));
    });
    state.picked = items[0] || null;
    return set;
  }

  function readButton() {
    return h("button", { class: "primary full", type: "button", text: "Read this page",
      onclick: (e) => readPage(e.currentTarget) });
  }

  function viewWaiting(list) {
    const body = h("div", { class: "body" },
      h("h2", { text: "Waiting for a description" }),
      roleRows(list.items, { otherSite: false }),
      list.total > list.items.length
        ? h("p", { class: "muted" }, `Showing ${list.items.length} of ${list.total} waiting · `,
          h("button", { class: "link", type: "button", text: "Search all", onclick: () => viewNoneHere(list.total) }))
        : null,
      readButton(),
      h("button", { class: "link", type: "button", text: "Capture as a new role instead",
        onclick: () => viewNothingWaiting() }),
      h("div", { id: "confirm" }));
    show(h("section", { class: "view" }, header(), body));
  }

  function viewNoneHere(total) {
    const results = h("div", { id: "results" });
    const search = h("input", { type: "search", placeholder: "Search by title or company",
      "aria-label": "Search by title or company" });
    let timer = 0;
    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = search.value.trim();
        if (!q) return results.replaceChildren();
        const r = await Api.call(`/api/extension/waiting?q=${encodeURIComponent(q)}&host=${encodeURIComponent(state.host)}&limit=5`);
        const items = r.ok && r.body && Array.isArray(r.body.items) ? r.body.items : [];
        results.replaceChildren(items.length ? roleRows(items, { otherSite: true }) : h("p", { class: "muted", text: "No waiting role matches." }),
          items.length ? readButton() : null);
      }, 200);
    });
    const body = h("div", { class: "body" },
      captureBlock(),
      h("div", { id: "confirm" }),
      h("h2", { text: "Or fill in a waiting role" }),
      h("p", { class: "muted", text: `${total} ${total === 1 ? "role is" : "roles are"} waiting. None are from this site.` }),
      search, results);
    show(h("section", { class: "view" }, header(), body));
  }

  // ------------------------------------------------------------------ Read (states 2 and 3)
  async function readPage(button) {
    if (!state.picked) return;
    if (button) button.disabled = true;
    try {
      state.page = await Api.readTab(state.tab.id);
    } catch (e) {
      if (button) button.disabled = false;
      return confirmLine(null, "Kestrel couldn't read this page.");
    }
    const r = await Api.call(`/api/extension/roles/${state.picked.id}/description`, { method: "POST",
      body: { text: state.page.text, page_url: state.page.url, page_title: state.page.title, dry_run: true } });
    if (!r.ok || !r.body) {
      if (button) button.disabled = false;
      return confirmLine(null, r.body && r.body.message ? r.body.message : "Kestrel couldn't check this page.");
    }
    if (r.body.looks_like_check_page || r.body.too_short) return viewCheckPage(r.body);
    return viewCheckBeforeSaving(r.body);
  }

  function viewCheckBeforeSaving(preview) {
    const role = state.picked;
    const words = Number(preview.words || 0).toLocaleString("en-US");
    const body = h("div", { class: "body" },
      h("dl", { class: "grid2" },
        h("dt", { text: "Saving to" }), h("dd", { text: [role.title, role.company].filter(Boolean).join(", ") }),
        h("dt", { text: "This page" }), h("dd", { text: state.page.title || "Untitled page" })),
      preview.title_match
        ? h("p", { class: "ok" }, icon("check"), " Titles match")
        : h("p", { class: "amber", text: "This page's title doesn't match the role you picked." }),
      h("div", { class: "box preview", text: preview.first_lines || "" }),
      h("p", { class: "muted", text: `${words} words from the page you have open. Sent only to Kestrel on this computer.` }),
      h("div", { id: "confirm" }),
      h("div", { class: "row-btns" },
        h("button", { type: "button", text: "Cancel", onclick: () => start() }),
        h("button", { class: "primary", type: "button", text: "Save", onclick: (e) => save(e.currentTarget) })));
    show(h("section", { class: "view" }, header({ title: "Check before saving", back: () => start() }), body));
  }

  function viewCheckPage(preview) {
    const role = state.picked;
    const body = h("div", { class: "body" },
      h("div", { class: "box preview", text: preview.first_lines || "" }),
      h("div", { class: "box warn" },
        h("b", { text: "This looks like a check page, not a job posting." }), " ",
        "Finish the check in the page yourself, then click Read again."),
      h("button", { class: "primary full", type: "button", text: "Paste it in Kestrel instead",
        onclick: () => openKestrel(`/opportunities/${role.id}`) }));
    show(h("section", { class: "view" }, header({ title: "Not a job posting", back: () => start() }), body));
  }

  async function save(button) {
    button.disabled = true;
    const r = await Api.call(`/api/extension/roles/${state.picked.id}/description`, { method: "POST",
      body: { text: state.page.text, page_url: state.page.url, page_title: state.page.title, dry_run: false } });
    if (!r.ok) {
      button.disabled = false;
      return confirmLine(null, r.body && r.body.message ? r.body.message : "Kestrel couldn't save it.");
    }
    chrome.runtime.sendMessage({ type: "count", tabId: state.tab.id, host: state.host, fresh: true });
    const body = h("div", { class: "body" },
      h("p", { class: "confirm" }, icon("check"), "Saved. Kestrel will score it in its next run."));
    show(h("section", { class: "view" }, header(), body));
    setTimeout(() => start(), 1800);
  }

  // ------------------------------------------------------------------ role summary (state 7)
  function viewSummary(sum) {
    state.summaryPath = sum.open_path;
    const scroller = h("div", { class: "body" });
    const cueUp = h("button", { class: "cue up", type: "button", "aria-label": "Scroll up" }, icon("up"));
    const cueDown = h("button", { class: "cue down", type: "button", "aria-label": "Scroll down" }, icon("down"));
    cueUp.addEventListener("click", () => scroller.scrollBy({ top: -scroller.clientHeight * 0.8 }));
    cueDown.addEventListener("click", () => scroller.scrollBy({ top: scroller.clientHeight * 0.8 }));
    scroller.append(cueUp);
    scroller.append(h("h1", { text: sum.title }),
      h("p", { class: "muted", text: [sum.company, sum.place].filter(Boolean).join(" · ") }));
    if (typeof sum.score === "number") {
      const ring = h("div", { class: "ring" }, h("span", null, h("b", { text: sum.score.toFixed(1) }), h("small", { text: "out of 5" })));
      ring.style.setProperty("--pct", String(Math.round(sum.score / 5 * 100)));
      const word = sum.verdict && sum.verdict.word ? sum.verdict.word : "";
      scroller.append(h("div", { class: "scoreline" }, ring,
        word ? h("span", { class: `chip ${sum.verdict.tone === "good" ? "good" : "warn"}`, text: word[0].toUpperCase() + word.slice(1) }) : null));
    } else if (sum.waiting) {
      scroller.append(h("div", { class: "box warn", text: "This role has no description yet. Use Read on the posting to fill it in." }));
    }
    for (const d of sum.dimensions || []) {
      const fill = h("i");
      fill.style.setProperty("transform", `scaleX(${Math.max(0, Math.min(100, Number(d.pct) || 0)) / 100})`);
      scroller.append(h("div", { class: "dim" }, h("span", { text: d.name }), h("b", { text: Number(d.score).toFixed(1) }), h("span", { class: "meter" }, fill)));
    }
    if (sum.dimension_count > (sum.dimensions || []).length) {
      scroller.append(h("button", { class: "link", type: "button", text: `All ${sum.dimension_count} scores in Kestrel`,
        onclick: () => openKestrel(sum.open_path) }));
    }
    const tiles = [];
    if (sum.pay && sum.pay.text) tiles.push(tile("PAY", sum.pay.text, sum.pay.note));
    if (sum.level && sum.level.text) tiles.push(tile("LEVEL", sum.level.text, sum.level.note));
    if (tiles.length) scroller.append(h("div", { class: "tiles" }, tiles));
    if ((sum.fits || []).length) {
      scroller.append(h("h2", { text: "Why it fits" }), h("ul", { class: "items" },
        sum.fits.map((f) => h("li", null, h("span", { class: "m", text: f.mark || "+" }), h("span", { class: "tx", text: f.text }),
          f.word ? h("span", { class: "chip", text: f.word }) : null))));
    }
    if ((sum.gaps || []).length) {
      scroller.append(h("h2", { text: "Gaps to address" }), h("ul", { class: "items" },
        sum.gaps.map((g) => h("li", null, h("span", { class: "m", text: "!" }), h("span", { class: "tx", text: g.text })))));
    }
    scroller.append(cueDown);
    const docsReady = ["resume", "cover_letter"].filter((k) => sum.documents && sum.documents[k] && sum.documents[k].available).length;
    const panel = h("div", { class: "docs hidden", id: "docs" });
    const docsBtn = h("button", { type: "button", "aria-expanded": "false", "aria-controls": "docs" },
      icon("page"), " Documents", h("span", { class: "count", text: String(docsReady) }));
    docsBtn.addEventListener("click", () => {
      const open = panel.classList.toggle("hidden") === false;
      docsBtn.setAttribute("aria-expanded", String(open));
      if (open) fillDocs(panel, sum);
    });
    const applied = h("button", { class: "primary", type: "button",
      text: sum.applied_at ? `Applied ${shortDate(sum.applied_at)}` : "I've applied", disabled: Boolean(sum.applied_at),
      onclick: async (e) => {
        const button = e.currentTarget; // currentTarget is gone after the first await
        button.disabled = true;
        const r = await Api.call(`/api/extension/roles/${sum.id}/applied`, { method: "POST" });
        if (r.ok && r.body && r.body.applied_at) button.textContent = `Applied ${shortDate(r.body.applied_at)}`;
        else button.disabled = false;
      } });
    const open = h("button", { class: "link", type: "button", text: "Open in Kestrel ↗", onclick: () => openKestrel(sum.open_path) });
    const view = h("section", { class: "view" },
      h("header", { class: "bar wrap-rel" }, h("img", { class: "logo", src: "icon.svg", alt: "" }), h("span", { class: "grow" }), open, gearButton()),
      scroller, panel, h("footer", { class: "foot" }, docsBtn, applied));
    show(view, { wide: true });
    const cues = () => {
      cueUp.classList.toggle("show", scroller.scrollTop > 4);
      cueDown.classList.toggle("show", scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 4);
    };
    scroller.addEventListener("scroll", cues, { passive: true });
    requestAnimationFrame(cues);
  }

  function tile(k, v, note) {
    return h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v", text: v }), note ? h("div", { class: "n2", text: note }) : null);
  }

  function fillDocs(panel, sum) {
    const rows = [["resume", "Resume"], ["cover_letter", "Cover letter"]].map(([kind, label]) => {
      const d = (sum.documents || {})[kind] || {};
      if (!d.available) {
        return h("div", { class: "doc" }, h("span", null, h("b", { text: label }), h("br"), h("span", { class: "muted", text: "Not made yet" })),
          h("span"), h("button", { type: "button", text: "Make one in Kestrel", onclick: () => openKestrel(sum.open_path) }));
      }
      return h("div", { class: "doc" },
        h("span", null, h("b", { text: label }), h("br"), h("span", { class: "muted", text: `Tailored · ${shortDate(d.updated)}` })),
        h("button", { type: "button", text: "Get PDF", onclick: (e) => getPdf(d.pdf_path, e.currentTarget) }),
        h("button", { type: "button", text: "Copy text", onclick: (e) => copyText(sum.id, kind, e.currentTarget) }));
    });
    panel.replaceChildren(
      h("div", { class: "row-btns" }, h("b", { text: "Your documents for this role" })),
      ...rows,
      h("p", { class: "muted", text: "Kestrel won't fill in this application. Attach these yourself." }));
  }

  // The PDF comes from Kestrel's own download route, fetched with the key and saved through a
  // blob link on this popup page, so the extension needs no downloads permission.
  async function getPdf(path, button) {
    if (typeof path !== "string" || !path.startsWith("/")) return;
    button.disabled = true;
    const r = await Api.call(path, { raw: true });
    if (!r.ok || !r.response) {
      // Kestrel could not hand over the PDF (a line still needs a pick, or no browser to make
      // it): the person finishes it in Kestrel, from a fresh button that only opens Kestrel.
      const open = h("button", { type: "button", text: "Open Kestrel", onclick: () => openKestrel(state.summaryPath) });
      button.replaceWith(open);
      return;
    }
    const blob = await r.response.blob();
    const cd = r.response.headers.get("Content-Disposition") || "";
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
    const name = m ? decodeURIComponent(m[1]).replace(/[\\/:*?"<>|]/g, "_") : "document.pdf";
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: name, class: "hidden" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    button.disabled = false;
  }

  async function copyText(id, kind, button) {
    const r = await Api.call(`/api/extension/roles/${id}/documents/${kind}.txt`, { raw: true });
    if (!r.ok || !r.response) {
      button.textContent = "Open Kestrel";
      button.addEventListener("click", () => openKestrel(state.summaryPath), { once: true });
      return;
    }
    await navigator.clipboard.writeText(await r.response.text());
    const was = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = was; }, 2000);
  }

  // ------------------------------------------------------------------ choose a screen (5.2.1)
  async function start() {
    closeMenus();
    state.tab = (await chrome.tabs.query({ active: true, currentWindow: true }))[0] || null;
    const tab = state.tab;
    state.host = tab && tab.url ? Api.hostOf(tab.url) : "";
    const { secret } = await Api.settings();
    state.found = await Api.resolve({ force: true });
    if (!state.found) return secret ? viewOffline() : viewSetup();
    if (!secret) return viewSetup();
    const cfg = await Api.call("/api/extension/config");
    if (cfg.status === 403) return viewSetup({ error: "wrong" });
    if (!cfg.ok) return viewOffline();
    state.config = cfg.body;
    if (state.tab) chrome.runtime.sendMessage({ type: "count", tabId: state.tab.id, host: state.host, fresh: true });
    if (state.tab && Api.isLinkedIn(state.tab.url || "")) return viewLinkedIn();
    if (state.tab && /^https?:/.test(state.tab.url || "")) {
      const known = await Api.call("/api/opportunities/lookup", { method: "POST", body: { urls: [state.tab.url] } });
      const hit = known.ok && known.body ? known.body[state.tab.url] : null;
      // A known role that is waiting for a description goes to the waiting list with that role
      // first: Read is what the person can do on its posting, not the summary.
      if (hit && Number.isInteger(hit.id) && hit.needs_description) state.waitingHere = hit.id;
      else if (hit && Number.isInteger(hit.id)) {
        const sum = await Api.call(`/api/extension/roles/${hit.id}/summary`);
        if (sum.ok && sum.body) return viewSummary(sum.body);
      }
    }
    const features = (state.config && state.config.features) || {};
    if (features.waiting !== false) {
      const w = await Api.call(`/api/extension/waiting?host=${encodeURIComponent(state.host)}&limit=5`);
      const list = w.ok && w.body ? w.body : { total: 0, items: [] };
      if (list.items.length && list.items[0].host_match) {
        const first = (it) => (it.id === state.waitingHere ? 0 : 1);
        return viewWaiting({ total: list.total, items: [...list.items].sort((a, b) => first(a) - first(b)) });
      }
      if (list.total > 0) return viewNoneHere(list.total);
    }
    return viewNothingWaiting();
  }

  start();
})();
