// Kestrel Capture settings page (issue #186, SPEC-INTERFACES 5.4). Connection, On this browser,
// About. It talks only to Kestrel on this computer: the host is always 127.0.0.1 (D-EXT-740);
// only the port can be set, for a Kestrel that is not on its usual ports.

(function () {
  "use strict";

  const Api = self.KestrelApi;
  const $ = (id) => document.getElementById(id);
  let found = null;

  function say(id, text, tone) {
    const el = $(id);
    el.className = tone ? `confirm ${tone}` : "muted";
    el.textContent = text;
  }

  function showSection(name) {
    const sec = ["connection", "browser", "about"].includes(name) ? name : "connection";
    for (const s of document.querySelectorAll(".sec")) s.classList.toggle("on", s.id === sec);
    for (const a of document.querySelectorAll(".side a")) a.classList.toggle("on", a.dataset.sec === sec);
  }
  window.addEventListener("hashchange", () => showSection(location.hash.slice(1)));

  async function refreshConnection() {
    const { secret, port } = await Api.settings();
    $("key").value = secret ? "••••••••••••••••" : "";
    $("port").value = port ? String(port) : "";
    found = await Api.resolve({ force: true });
    const pill = $("conn-pill");
    if (!found) {
      pill.className = "pillbig";
      pill.textContent = "Kestrel isn't running on this computer";
      $("conn-to").textContent = "Not connected";
      return null;
    }
    $("found-line").textContent = `Found automatically on 127.0.0.1, port ${found.port}. Set a port only if Kestrel is not on its usual ports.`;
    const r = await Api.call("/api/extension/config");
    if (r.ok) {
      pill.className = "pillbig good";
      pill.textContent = `Connected to Kestrel ${found.version}`;
      $("conn-to").textContent = `Kestrel ${found.version} on 127.0.0.1:${found.port}`;
      return r.body;
    }
    pill.className = "pillbig";
    pill.textContent = r.status === 403 ? "Kestrel didn't accept the key" : "Not connected";
    $("conn-to").textContent = "Not connected";
    return null;
  }

  function showFeatures(cfg) {
    const feats = (cfg && cfg.features) || {};
    for (const box of document.querySelectorAll("[data-feature]")) {
      box.checked = feats[box.dataset.feature] !== false;
      box.disabled = !cfg;
    }
  }

  async function showHidden() {
    const r = await chrome.storage.local.get(["hiddenSites"]);
    const list = Array.isArray(r.hiddenSites) ? r.hiddenSites : [];
    const ul = $("hidden-sites");
    ul.replaceChildren();
    if (!list.length) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "None. Use Hide badges on this site in the popup's menu.";
      ul.append(li);
      return;
    }
    for (const host of list) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = host;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "Show again";
      btn.addEventListener("click", async () => {
        const now = (await chrome.storage.local.get(["hiddenSites"])).hiddenSites || [];
        await chrome.storage.local.set({ hiddenSites: now.filter((x) => x !== host) });
        showHidden();
      });
      li.append(name, btn);
      ul.append(li);
    }
  }

  for (const box of document.querySelectorAll("[data-feature]")) {
    box.addEventListener("change", async () => {
      const r = await Api.call("/api/extension/features", { method: "PUT", body: { [box.dataset.feature]: box.checked } });
      if (r.ok) say("sw-msg", "Saved.", "good");
      else {
        box.checked = !box.checked;
        say("sw-msg", "Kestrel couldn't save that. Is it running?");
      }
    });
  }

  $("key-change").addEventListener("click", () => {
    $("key-edit").classList.remove("hidden");
    $("key-new").focus();
  });
  $("key-cancel").addEventListener("click", () => $("key-edit").classList.add("hidden"));
  $("key-save").addEventListener("click", async () => {
    const secret = $("key-new").value.trim();
    const r = await Api.call("/api/extension/config", { secret, force: true });
    if (!r.ok) {
      return say("key-msg", r.status === 403
        ? "Kestrel didn't accept that key. Copy it again from Settings, Browser extension."
        : "Open Kestrel on this computer, then try again.");
    }
    await chrome.storage.local.set({ secret });
    $("key-new").value = "";
    $("key-edit").classList.add("hidden");
    say("key-msg", "Saved.", "good");
    showFeatures(await refreshConnection());
  });
  $("port-save").addEventListener("click", async () => {
    const raw = $("port").value.trim();
    const port = raw ? Number(raw) : 0;
    if (raw && !(Number.isInteger(port) && port > 0 && port < 65536)) {
      return say("port-msg", "A port is a number from 1 to 65535, or empty for automatic.");
    }
    await chrome.storage.local.set({ port, host: "127.0.0.1" });
    await Api.forget();
    say("port-msg", "Saved.", "good");
    showFeatures(await refreshConnection());
  });
  $("help").addEventListener("click", () => {
    if (found) chrome.tabs.create({ url: `${found.base}/help` });
  });

  $("ver").textContent = chrome.runtime.getManifest().version;
  showSection(location.hash.slice(1));
  showHidden();
  refreshConnection().then(showFeatures);
})();
