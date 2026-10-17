// Kestrel Capture — background service worker (Manifest V3).
// Capture: the toolbar button and a right-click item send the CURRENT page's URL plus its extracted
// readable text to POST /api/opportunities/ingest. It reads the page the operator is already looking
// at. It never crawls, never navigates, and never acts externally.

const DEFAULTS = { host: "127.0.0.1" };

// The app binds the first free port from this ladder and records it in runtime.json. A service
// worker cannot read runtime.json (no filesystem access), so we probe the same ladder instead.
// 8765 is only the first rung, never an assumption.
const PORT_LADDER = [8765, 8766, 8767, 8768, 8769, 8770, 8771, 8772, 8773, 8774];

// A rung that accepts the connection but never answers must not wedge a capture forever.
const PROBE_TIMEOUT_MS = 1500;

async function config() {
  const c = await chrome.storage.local.get(["host", "port", "secret"]);
  // Legacy migration: every build before port discovery shipped 8765 as the options default and
  // saved it unconditionally, so nearly every existing install has 8765 pinned. Left as-is that
  // stored value would suppress probing and reintroduce the exact bug discovery fixes. 8765 is the
  // first rung anyway, so treating it as "auto" costs nothing when it is right and self-heals when
  // it is not. A port the operator genuinely wants pinned to rung 0 is indistinguishable here, and
  // discovery still finds it first.
  const stored = c.port === 8765 ? 0 : c.port || 0;
  return { host: c.host || DEFAULTS.host, port: stored, secret: c.secret || "" };
}

// Confirm a Kestrel is really listening here. We POST the text of the page the operator is reading,
// so "something answered" is not good enough: it must identify itself as Kestrel before we send
// anything to it. An unrelated local server on a ladder port must never receive page content.
// The shared secret is deliberately NOT sent on the probe: /health needs no auth, and sending it
// would hand the operator's key to every unidentified server on the ladder before we know what
// they are, which is the very thing this check exists to prevent.
async function isKestrel(host, port) {
  try {
    const r = await fetch(`http://${host}:${port}/health`, {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!r.ok) return false;
    const body = await r.json();
    return body && body.service === "kestrel";
  } catch (e) {
    return false; // not listening, not Kestrel, or too slow to be the local app
  }
}

// How long a failed sweep is remembered. content.js re-scans on every DOM mutation batch, and job
// boards mutate constantly, so without this a Kestrel that is simply not running would cost ten
// loopback connections per scan, forever. That is both wasteful and indistinguishable from a port
// scan to endpoint security software.
const NEGATIVE_CACHE_MS = 10000;

// Resolve once, then cache in session storage. Callers drop the cache when a request fails in a way
// that suggests the app moved, so a restart onto another rung self-heals.
// `force` skips the negative cache. The operator clicking Capture is an explicit request and must
// never be answered from a stale "nothing found" note; the passive content-script lookup is the
// caller that needs the throttle.
async function resolveBase({ force = false } = {}) {
  const { host, port } = await config();
  // An explicitly configured port is authoritative, so it is checked BEFORE the cache. Consulting
  // the cache first would let a previously discovered rung outrank a port the operator just pinned.
  if (port) {
    return (await isKestrel(host, port)) ? `http://${host}:${port}` : null;
  }
  const cached = await chrome.storage.session.get(["resolvedPort", "lastSweepFailedAt"]);
  if (cached.resolvedPort && (await isKestrel(host, cached.resolvedPort))) {
    return `http://${host}:${cached.resolvedPort}`;
  }
  if (
    !force &&
    cached.lastSweepFailedAt &&
    Date.now() - cached.lastSweepFailedAt < NEGATIVE_CACHE_MS
  ) {
    return null; // swept recently and found nothing; do not walk the ladder again yet
  }
  for (const p of PORT_LADDER) {
    if (await isKestrel(host, p)) {
      await chrome.storage.session.set({ resolvedPort: p, lastSweepFailedAt: 0 });
      return `http://${host}:${p}`;
    }
  }
  await chrome.storage.session.set({ lastSweepFailedAt: Date.now() });
  return null;
}

async function clearResolved() {
  await chrome.storage.session.remove(["resolvedPort"]);
}

// Only a transport failure or a server-side fault means "it moved". A 4xx came from a confirmed
// Kestrel answering about this particular request, so keep the resolution.
function movedAway(status) {
  return status >= 500;
}

function headers(secret) {
  const h = { "Content-Type": "application/json" };
  // Required on every real request: Kestrel refuses an extension-origin request without a valid
  // key (403), which is what stops any OTHER installed extension with a localhost permission from
  // driving it. Sent only to a server that already identified itself as Kestrel (see isKestrel).
  if (secret) h["X-Kestrel-Key"] = secret;
  return h;
}

// Kestrel's own explanation for a refused extension request, so the badge title tells the operator
// what to do (generate the secret under Settings > LAN, paste it into options) rather than "403".
async function refusalMessage(r) {
  if (r.status !== 403) return `Kestrel capture failed (${r.status})`;
  try {
    const body = await r.json();
    if (body && body.detail) return body.detail;
  } catch (e) {
    // not JSON; fall through to the generic message
  }
  return "Kestrel refused the capture: set the shared secret (Settings > LAN) in the extension options.";
}

// Ask the tab for its own URL + readable text. The extension only reads the active tab the operator
// chose; it injects a one-shot extractor, never a persistent crawler.
async function extract(tabId) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const text = (document.body ? document.body.innerText : "").slice(0, 40000);
      return { url: location.href, title: document.title, text };
    },
  });
  return res.result;
}

async function capture(tab) {
  if (!tab || !tab.id) return;
  try {
    const page = await extract(tab.id);
    const { secret } = await config();
    const base = await resolveBase({ force: true }); // operator clicked; never answer from cache
    if (!base) {
      await notify("Kestrel not running, or not on its usual ports. Start it, or set a port in options.", false);
      return;
    }
    const r = await fetch(`${base}/api/opportunities/ingest`, {
      method: "POST",
      headers: headers(secret),
      body: JSON.stringify({ url: page.url, text: page.text, title: page.title }),
    });
    const ok = r.ok;
    const body = ok ? await r.json() : null;
    if (!ok && movedAway(r.status)) await clearResolved();
    await notify(ok ? `Captured: ${body.title || page.title}` : await refusalMessage(r), ok);
  } catch (e) {
    await clearResolved(); // transport failure: it may have restarted onto another rung
    await notify(`Kestrel not reachable. Set the port in the extension options. (${e.message})`, false);
  }
}

async function notify(message, ok = true) {
  // Best-effort badge text on the toolbar icon; no notifications permission needed. The badge must
  // reflect the outcome: a failure that renders as "OK" is worse than no badge at all.
  await chrome.action.setBadgeText({ text: ok ? "OK" : "!" });
  await chrome.action.setBadgeBackgroundColor({ color: ok ? "#D4622A" : "#8A1C1C" });
  await chrome.action.setTitle({ title: message });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 2500);
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "kestrel-capture",
    title: "Capture this page into Kestrel",
    contexts: ["page", "selection", "link"],
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => capture(tab));
chrome.action.onClicked.addListener((tab) => capture(tab));

// The content script asks the background to look up which visible URLs are already known, so the
// lookup (and any shared secret) stays out of the page context.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "lookup") {
    (async () => {
      try {
        const { secret } = await config();
        const base = await resolveBase();
        if (!base) return sendResponse({});
        const r = await fetch(`${base}/api/opportunities/lookup`, {
          method: "POST",
          headers: headers(secret),
          body: JSON.stringify({ urls: msg.urls || [] }),
        });
        if (!r.ok && movedAway(r.status)) await clearResolved();
        sendResponse(r.ok ? await r.json() : {});
      } catch (e) {
        await clearResolved();
        sendResponse({});
      }
    })();
    return true; // async response
  }
});
