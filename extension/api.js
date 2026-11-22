// Kestrel Capture: the one client for Kestrel on this computer (issue #186).
// Loaded by the popup, the options page and the background (importScripts in Chrome's service
// worker, background.scripts in Firefox). Every request goes to 127.0.0.1 or localhost, nowhere
// else: the manifest grants no other host, and LOCAL_HOSTS below is the only place a host is chosen.
// The connection key is sent only to a server that first identified itself as Kestrel on /health.

(function (root) {
  "use strict";

  // Kestrel binds the first free port of this ladder (kestrel/web/serve.py). The extension cannot
  // read runtime.json, so it probes the same ladder. 8765 is only the first rung.
  const PORT_LADDER = [8765, 8766, 8767, 8768, 8769, 8770, 8771, 8772, 8773, 8774];
  const LOCAL_HOSTS = ["127.0.0.1", "localhost"];
  const PROBE_TIMEOUT_MS = 1500;
  const NEGATIVE_CACHE_MS = 10000;

  // chrome.storage.session is missing in some browsers (Firefox before 115, Safari before 16.4):
  // fall back to memory, which lasts as long as the page or worker does.
  const memory = {};
  const session = {
    async get(keys) {
      if (chrome.storage && chrome.storage.session) return chrome.storage.session.get(keys);
      const out = {};
      for (const k of keys) if (k in memory) out[k] = memory[k];
      return out;
    },
    async set(values) {
      if (chrome.storage && chrome.storage.session) return chrome.storage.session.set(values);
      Object.assign(memory, values);
    },
    async remove(keys) {
      if (chrome.storage && chrome.storage.session) return chrome.storage.session.remove(keys);
      for (const k of keys) delete memory[k];
    },
  };

  async function settings() {
    const c = await chrome.storage.local.get(["host", "port", "secret"]);
    const host = LOCAL_HOSTS.includes(c.host) ? c.host : "127.0.0.1";
    // Every build before port discovery saved 8765; treat it as automatic (it is rung 0 anyway).
    const port = c.port === 8765 ? 0 : Number(c.port) || 0;
    return { host, port, secret: typeof c.secret === "string" ? c.secret : "" };
  }

  function validPort(p) {
    return Number.isInteger(p) && p > 0 && p < 65536;
  }

  // Confirm Kestrel is really listening before anything is sent: an unrelated local server on a
  // ladder port must never see page text or the key. The key is not sent on the probe.
  async function probe(host, port) {
    try {
      const r = await fetch(`http://${host}:${port}/health`, {
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
        credentials: "omit",
      });
      if (!r.ok) return null;
      const body = await r.json();
      return body && body.service === "kestrel" ? { version: String(body.version || "") } : null;
    } catch (e) {
      return null;
    }
  }

  // Every call probes /health before anything is sent, the key included, so neither page text
  // nor the key ever reaches a server that took over Kestrel's port (D-EXT-770).
  async function resolve({ force = false } = {}) {
    const { host, port } = await settings();
    if (validPort(port)) {
      const found = await probe(host, port);
      return found ? { base: `http://${host}:${port}`, port, version: found.version } : null;
    }
    const cached = await session.get(["resolvedPort", "lastSweepFailedAt"]);
    if (cached.resolvedPort) {
      const found = await probe(host, cached.resolvedPort);
      if (found) {
        return { base: `http://${host}:${cached.resolvedPort}`, port: cached.resolvedPort,
                 version: found.version };
      }
    }
    if (!force && cached.lastSweepFailedAt &&
        Date.now() - cached.lastSweepFailedAt < NEGATIVE_CACHE_MS) {
      return null;
    }
    for (const p of PORT_LADDER) {
      const found = await probe(host, p);
      if (found) {
        await session.set({ resolvedPort: p, lastSweepFailedAt: 0 });
        return { base: `http://${host}:${p}`, port: p, version: found.version };
      }
    }
    await session.set({ lastSweepFailedAt: Date.now() });
    return null;
  }

  async function forget() {
    await session.remove(["resolvedPort"]);
  }

  // One request to Kestrel. Returns {ok, status, body, response}. Paths are Kestrel's own,
  // always starting with "/"; a full URL is refused so no caller can aim this anywhere else.
  async function call(path, { method = "GET", body, force = false, raw = false, secret } = {}) {
    if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//")) {
      throw new Error("Kestrel paths only");
    }
    const found = await resolve({ force });
    if (!found) return { ok: false, status: 0, body: null, offline: true };
    const key = secret !== undefined ? secret : (await settings()).secret;
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (key) headers["X-Kestrel-Key"] = key;
    let r;
    try {
      r = await fetch(found.base + path, {
        method, headers, credentials: "omit",
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      await forget();
      return { ok: false, status: 0, body: null, offline: true };
    }
    if (r.status >= 500) await forget();
    if (raw) return { ok: r.ok, status: r.status, response: r, found };
    let parsed = null;
    try {
      parsed = await r.json();
    } catch (e) {
      parsed = null;
    }
    return { ok: r.ok, status: r.status, body: parsed, found };
  }

  // The page the person has open: its URL, title and visible text, read once when they click.
  // The function runs in the page and returns plain strings; it changes nothing there.
  function isLinkedIn(url) {
    const h = hostOf(url);
    return ["linkedin.com", "linkedin.cn", "lnkd.in"].some((d) => h === d || h.endsWith("." + d));
  }

  async function readTab(tabId) {
    // LinkedIn is never read (D-SRC-5): its postings are pasted by hand in Kestrel. The tab's
    // address is known here because the person's click granted access to this tab.
    const tab = await chrome.tabs.get(tabId);
    if (!tab || !tab.url || isLinkedIn(tab.url)) throw new Error("linkedin");
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({
        url: location.href,
        title: document.title,
        text: (document.body ? document.body.innerText : "").slice(0, 40000),
      }),
    });
    const page = res && res.result ? res.result : {};
    return {
      url: typeof page.url === "string" ? page.url : "",
      title: typeof page.title === "string" ? page.title.slice(0, 500) : "",
      text: typeof page.text === "string" ? page.text.slice(0, 40000) : "",
    };
  }

  function hostOf(url) {
    try {
      const h = new URL(url).hostname.toLowerCase().replace(/\.+$/, "");
      return h.startsWith("www.") ? h.slice(4) : h;
    } catch (e) {
      return "";
    }
  }

  root.KestrelApi = {
    PORT_LADDER, LOCAL_HOSTS, settings, resolve, forget, call, readTab, hostOf,
    isLinkedIn, session,
  };
})(typeof self !== "undefined" ? self : globalThis);
