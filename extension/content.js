// Kestrel Capture: board badges (issue #186, SPEC-INTERFACES 5.5). On a supported job board it
// collects the posting links already on the page, asks Kestrel on this computer which it knows,
// and puts a small badge after each known link. It reads the page the person is on; it never
// navigates, reloads, clicks, fills or submits anything, and never runs on LinkedIn.
//
// Isolation: each badge is a new <span> inserted after the link, holding a CLOSED shadow root.
// Everything the badge shows lives inside that root; the page's own elements are never written,
// restyled or moved, and nothing Kestrel knows goes into the page's DOM, attributes, URL or
// storage. If a link is not where a badge can go, no badge appears.

(function () {
  "use strict";

  const SEEN = new WeakSet();
  const BIRD = "M256 92 C270 92 282 102 284 118 C286 134 292 142 300 150 C348 134 420 112 472 116 C452 170 380 220 312 236 C306 266 300 294 294 318 C324 326 348 352 358 392 C324 398 290 400 256 400 C222 400 188 398 154 392 C164 352 188 326 218 318 C212 294 206 266 200 236 C132 220 60 170 40 116 C92 112 164 134 212 150 C220 142 226 134 228 118 C230 102 242 92 256 92 Z";
  const POSTING = /(\/jobs?\/|\/job\/|greenhouse\.io\/.+\/jobs\/|lever\.co\/.+\/|ashbyhq\.com\/.+\/|myworkdayjobs\.com\/.+\/job\/|smartrecruiters\.com\/.+\/\d|viewjob|currentJobId=|glassdoor\.[a-z.]+\/(job-listing|Job)\/)/i;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  let sheet = null;
  let sheetText = "";

  function shortDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
    return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : "";
  }

  // What one badge says, from what Kestrel sent. Only these fields are read (D-SEC-20).
  function look(info) {
    const score = typeof info.score === "number" ? info.score.toFixed(1) : "";
    if (info.applied_at) return { kind: "applied", strong: "", text: `Applied ${shortDate(info.applied_at)}` };
    if (info.liveness === "closed") return { kind: "closed", strong: "", text: "Closed" };
    if (info.needs_description) return { kind: "waiting", strong: "", text: "No description yet" };
    if (typeof info.ghost_score === "number" && info.ghost_score >= 0.7) {
      return { kind: "ghost", strong: score, text: "Likely ghost" };
    }
    const word = typeof info.fit_word === "string" ? info.fit_word : "";
    if (!score && !word) return null;
    const tone = ["good", "warn", "muted"].includes(info.tone) ? info.tone : "muted";
    return { kind: "scored", tone, strong: score, text: word ? word[0].toUpperCase() + word.slice(1) : "" };
  }

  function logo() {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 512 512");
    svg.setAttribute("aria-hidden", "true");
    const ground = document.createElementNS(ns, "rect");
    ground.setAttribute("width", "512");
    ground.setAttribute("height", "512");
    ground.setAttribute("rx", "112");
    ground.setAttribute("class", "ground");
    const bird = document.createElementNS(ns, "path");
    bird.setAttribute("d", BIRD);
    bird.setAttribute("class", "bird");
    svg.append(ground, bird);
    return svg;
  }

  function badge(anchor, info) {
    SEEN.add(anchor);
    const what = look(info);
    if (!what || !anchor.parentNode) return;
    const host = document.createElement("span");
    const root = host.attachShadow({ mode: "closed" });
    try {
      if (sheet) root.adoptedStyleSheets = [sheet];
    } catch (e) {
      // A browser that will not adopt a content script's sheet gets the same rules in a style
      // element inside the closed root; still nothing reaches the page.
      const style = document.createElement("style");
      style.textContent = sheetText;
      root.append(style);
    }
    const pill = document.createElement("span");
    pill.className = `pill ${what.kind} ${what.tone || ""}`;
    pill.title = "Kestrel: click the toolbar icon for details.";
    pill.append(logo());
    if (what.strong) {
      const b = document.createElement("b");
      b.textContent = what.strong;
      pill.append(b);
    }
    const t = document.createElement("span");
    t.className = "word";
    t.textContent = what.text;
    pill.append(t);
    root.append(pill);
    anchor.insertAdjacentElement("afterend", host);
  }

  const BADGE_CSS = `
    :host { all: initial; display: inline-block; margin-left: 6px; vertical-align: middle; }
    .pill { display: inline-flex; align-items: center; gap: 4px; padding: 1px 7px 1px 3px;
      border: 1px solid var(--line); border-radius: var(--r-pill); background: var(--paper);
      color: var(--muted); font: 600 11px/16px var(--font); white-space: nowrap;
      animation: k-in var(--dur-2) var(--ease); }
    svg { width: 14px; height: 14px; flex: none; }
    .ground { fill: var(--brand); } .bird { fill: var(--brand-ink); }
    b { color: var(--ink); font-weight: 800; }
    .scored.good .word { color: var(--good-ink); } .scored.warn .word { color: var(--warn-ink); }
    .waiting { border-color: var(--warn); color: var(--warn-ink); }
    .ghost { border-style: dashed; } .closed { color: var(--muted); }
    @keyframes k-in { from { opacity: 0; transform: translateY(2px); } to { opacity: 1; transform: none; } }
  `;

  // The tokens come from the extension's own theme.css, handed over by the background, so no
  // color is written here. A constructed sheet is not inline style, so the page's CSP is untouched.
  async function makeSheet() {
    const theme = await new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "theme" }, (r) => resolve(r && typeof r.css === "string" ? r.css : ""));
    });
    sheetText = theme.replace(/@font-face\s*{[^}]*}/g, "").replace(/:root/g, ":host") + BADGE_CSS;
    try {
      const s = new CSSStyleSheet();
      s.replaceSync(sheetText);
      return s;
    } catch (e) {
      return null;
    }
  }

  // A LinkedIn link on a board page is never looked up or badged (D-SRC-5).
  function isLinkedIn(href) {
    try {
      const h = new URL(href).hostname.toLowerCase().replace(/\.+$/, "");
      return ["linkedin.com", "linkedin.cn", "lnkd.in"].some((d) => h === d || h.endsWith("." + d));
    } catch (e) {
      return true; // an address that does not parse is not asked about either
    }
  }

  function postingLinks() {
    return [...document.querySelectorAll("a[href]")]
      .filter((a) => !SEEN.has(a) && POSTING.test(a.href) && !isLinkedIn(a.href));
  }

  function scan() {
    const anchors = postingLinks();
    if (!anchors.length) return;
    for (const a of anchors) SEEN.add(a); // asked once; an unknown link is not asked again
    const urls = [...new Set(anchors.map((a) => a.href))].slice(0, 100);
    chrome.runtime.sendMessage({ type: "lookup", urls }, (reply) => {
      if (!reply || reply.off || !reply.known || typeof reply.known !== "object") return;
      for (const a of anchors) {
        const info = Object.prototype.hasOwnProperty.call(reply.known, a.href) ? reply.known[a.href] : null;
        if (info && typeof info === "object") badge(a, info);
      }
    });
  }

  let pending = false;
  const observer = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    setTimeout(() => {
      pending = false;
      scan();
    }, 800);
  });

  makeSheet().then((s) => {
    sheet = s;
    observer.observe(document.documentElement, { childList: true, subtree: true });
    scan();
  });
})();
