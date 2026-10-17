// Kestrel Capture — seen-badge content script. On supported job boards it collects the visible
// posting URLs, asks the background which are already KNOWN (POST /api/opportunities/lookup), and
// badges each with its score and status. It reads the page the operator is already on; it never
// navigates, crawls, or acts. This is the "put the system's judgment into the moment of browsing"
// feature (SPEC-INTERFACES 5).

(function () {
  const SEEN = new WeakSet();

  function postingLinks() {
    // Anchors that look like a specific posting on a supported board.
    const re = /(\/jobs?\/|\/job\/|greenhouse\.io\/.+\/jobs\/|lever\.co\/.+\/|ashbyhq\.com\/.+\/|myworkdayjobs\.com\/.+\/job\/|smartrecruiters\.com\/.+\/\d|viewjob|currentJobId=)/i;
    return [...document.querySelectorAll("a[href]")].filter(
      (a) => re.test(a.href) && !SEEN.has(a)
    );
  }

  function badge(anchor, info) {
    SEEN.add(anchor);
    const b = document.createElement("span");
    b.className = "kestrel-badge";
    const verdict = info.verdict || info.stage || "seen";
    const score = info.score != null ? Number(info.score).toFixed(1) : null;
    b.textContent = score ? `Kestrel ${score} · ${verdict}` : `Kestrel · ${verdict}`;
    b.style.cssText =
      "display:inline-block;margin-left:6px;padding:1px 6px;border-radius:10px;font-size:11px;" +
      "font-weight:600;vertical-align:middle;background:#fbe9e0;color:#b9511f;" +
      (info.ghost_score && info.ghost_score >= 0.7 ? "outline:1px solid #b3261e;" : "");
    if (info.liveness === "closed") b.style.opacity = "0.5";
    anchor.insertAdjacentElement("afterend", b);
  }

  async function scan() {
    const anchors = postingLinks();
    if (!anchors.length) return;
    const urls = [...new Set(anchors.map((a) => a.href))].slice(0, 100);
    chrome.runtime.sendMessage({ type: "lookup", urls }, (known) => {
      if (!known || typeof known !== "object") return;
      for (const a of anchors) {
        const info = known[a.href];
        if (info) badge(a, info);
      }
    });
  }

  // Boards paginate/scroll; re-scan on DOM changes, throttled.
  let pending = false;
  const observer = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    setTimeout(() => {
      pending = false;
      scan();
    }, 800);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  scan();
})();
