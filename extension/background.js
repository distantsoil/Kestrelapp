// Kestrel Capture: background (Manifest V3 service worker in Chrome and Edge, event page in
// Firefox). It answers the board badges' lookups, keeps the toolbar count, and offers Capture on
// the right-click menu. It reads a page only when the person clicks, never navigates or reloads
// one, never applies, fills or submits anything, and talks only to Kestrel on this computer.

if (typeof importScripts === "function" && typeof self.KestrelApi === "undefined") {
  importScripts("api.js");
}
const Api = self.KestrelApi;

const CONFIG_EVERY_MS = 5 * 60 * 1000;
const FLASH_MS = 2500;
const flashUntil = {}; // tabId -> time the capture flash ends
// The toolbar badge API takes a color value, not CSS, so these three are copies of theme.css's
// --brand, --good and --bad (light). They are the only color values outside theme.css.
const BADGE_COLORS = { count: "#4F5BD5", ok: "#1E8E5A", fail: "#C2413B" };

// Kestrel's switches and the hosts with waiting roles, refreshed at most every five minutes.
async function kestrelConfig({ fresh = false } = {}) {
  const cached = await Api.session.get(["config", "configAt"]);
  if (!fresh && cached.config && Date.now() - (cached.configAt || 0) < CONFIG_EVERY_MS) {
    return cached.config;
  }
  const r = await Api.call("/api/extension/config");
  if (!r.ok || !r.body || typeof r.body !== "object") return cached.config || null;
  const config = {
    version: String(r.body.version || ""),
    features: Object.assign({ badges: true, waiting: true, toolbar_count: true },
      r.body.features || {}),
    waiting_hosts: Array.isArray(r.body.waiting_hosts) ? r.body.waiting_hosts : [],
  };
  await Api.session.set({ config, configAt: Date.now() });
  return config;
}

// The count of roles waiting for a description from this tab's exact host (www. aside), never a
// total and never a parent domain's: a role on greenhouse.io does not count on a company's
// boards.greenhouse.io page. The host is matched here from the list Kestrel sent, so drawing the
// count sends Kestrel nothing about the tab (D-EXT-771).
async function showCount(tabId, host) {
  if (!tabId || (flashUntil[tabId] || 0) > Date.now()) return;
  const config = await kestrelConfig();
  let n = 0;
  if (config && config.features.toolbar_count && config.features.waiting && host) {
    for (const row of config.waiting_hosts) {
      // The same rule as the server's same_site (D-EXT-774): the exact host, www. aside.
      if (row && host && String(row.host || "") === host) n += Number(row.count) || 0;
    }
  }
  await chrome.action.setBadgeText({ tabId, text: n ? String(Math.min(n, 99)) : "" });
  if (n) await chrome.action.setBadgeBackgroundColor({ tabId, color: BADGE_COLORS.count });
}

async function flash(tabId, ok, message) {
  if (!tabId) return;
  flashUntil[tabId] = Date.now() + FLASH_MS;
  await chrome.action.setBadgeText({ tabId, text: ok ? "OK" : "!" });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: ok ? BADGE_COLORS.ok : BADGE_COLORS.fail });
  if (message) await chrome.action.setTitle({ tabId, title: message });
  setTimeout(() => {
    flashUntil[tabId] = 0;
    chrome.action.setTitle({ tabId, title: "Kestrel Capture" });
  }, FLASH_MS);
}

// Capture from the right-click menu: the page the person right-clicked, once.
async function captureFromMenu(tab) {
  if (!tab || !tab.id) return;
  if (Api.isLinkedIn(tab.url || "")) {
    return flash(tab.id, false, "Kestrel doesn't read LinkedIn pages. Paste the posting in Kestrel.");
  }
  try {
    const page = await Api.readTab(tab.id);
    const r = await Api.call("/api/opportunities/ingest", {
      method: "POST", force: true, body: { url: page.url, text: page.text, title: page.title },
    });
    if (r.offline) return flash(tab.id, false, "Open Kestrel on this computer, then try again.");
    if (r.status === 403) return flash(tab.id, false, "Set up the extension: click its icon.");
    return flash(tab.id, r.ok, r.ok ? "Added to Kestrel." : "Kestrel couldn't add this page.");
  } catch (e) {
    return flash(tab.id, false, "Kestrel couldn't read this page.");
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "kestrel-capture",
    title: "Capture this page into Kestrel",
    contexts: ["page", "selection", "link"],
  });
});
chrome.contextMenus.onClicked.addListener((_info, tab) => captureFromMenu(tab));

// Messages come only from this extension's own pages and content script.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object" || sender.id !== chrome.runtime.id) return false;
  if (msg.type === "lookup") {
    (async () => {
      const config = await kestrelConfig();
      const host = Api.hostOf(sender.url || "");
      const hidden = (await chrome.storage.local.get(["hiddenSites"])).hiddenSites || [];
      if (sender.tab) showCount(sender.tab.id, host);
      if ((config && !config.features.badges) || hidden.includes(host)) {
        return sendResponse({ known: {}, off: true });
      }
      const urls = Array.isArray(msg.urls) ? msg.urls.filter((u) => typeof u === "string") : [];
      const r = await Api.call("/api/opportunities/lookup", {
        method: "POST", body: { urls: urls.slice(0, 100) },
      });
      sendResponse({ known: r.ok && r.body && typeof r.body === "object" ? r.body : {} });
    })().catch(() => sendResponse({ known: {} }));
    return true;
  }
  if (msg.type === "theme") {
    // The badges' tokens: this extension's own theme.css, read from the package, never the web.
    fetch(chrome.runtime.getURL("theme.css"))
      .then((r) => r.text())
      .then((css) => sendResponse({ css }), () => sendResponse({ css: "" }));
    return true;
  }
  if (msg.type === "count" && Number.isInteger(msg.tabId)) {
    (async () => {
      if (msg.fresh) await kestrelConfig({ fresh: true });
      await showCount(msg.tabId, typeof msg.host === "string" ? msg.host : "");
      sendResponse({ done: true });
    })().catch(() => sendResponse({ done: false }));
    return true;
  }
  if (msg.type === "flash" && Number.isInteger(msg.tabId)) {
    flash(msg.tabId, Boolean(msg.ok), typeof msg.message === "string" ? msg.message : "")
      .then(() => sendResponse({ done: true }), () => sendResponse({ done: false }));
    return true;
  }
  return false;
});
