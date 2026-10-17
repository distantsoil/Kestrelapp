// Options: API host/port, shared secret, boards, badge style. Stored in chrome.storage.local.
const $ = (id) => document.getElementById(id);
const KEYS = ["host", "port", "secret", "badgeStyle", "boards"];

async function load() {
  const c = await chrome.storage.local.get(KEYS);
  if (c.host) $("host").value = c.host;
  // Same legacy migration the background worker applies: every build before port discovery saved
  // 8765 from the old options default, and treating that as a deliberate pin would suppress
  // discovery. Persist the mapping so the field stops showing a port that is not really pinned,
  // and so this page and the worker never disagree about what is configured.
  if (c.port === 8765) {
    await chrome.storage.local.set({ port: 0 });
  } else if (c.port) {
    $("port").value = c.port;
  }
  if (c.secret) $("secret").value = c.secret;
  if (c.badgeStyle) $("badgeStyle").value = c.badgeStyle;
  if (c.boards) $("boards").value = c.boards;
}

async function save() {
  const host = $("host").value.trim() || "127.0.0.1";
  // A non-loopback (LAN) host is not in the manifest's required host_permissions, so request it as
  // an optional permission; without the grant the background fetch would be blocked. Loopback needs
  // no grant. The extension still only ever talks to this one configured host.
  if (host !== "127.0.0.1" && host !== "localhost") {
    const granted = await chrome.permissions.request({ origins: [`http://${host}/*`] });
    if (!granted) {
      $("status").textContent = "Permission for the LAN host was not granted; not saved.";
      return;
    }
  }
  await chrome.storage.local.set({
    host,
    // 0 means "auto-detect": the background worker probes the port ladder. Only a port the operator
    // typed on purpose is stored, and that one then overrides discovery.
    port: parseInt($("port").value, 10) || 0,
    secret: $("secret").value,
    badgeStyle: $("badgeStyle").value,
    boards: $("boards").value,
  });
  $("status").textContent = "Saved.";
  setTimeout(() => ($("status").textContent = ""), 1500);
}

// Must stay in step with PORT_LADDER in background.js and kestrel/web/serve.py.
const PORT_LADDER = [8765, 8766, 8767, 8768, 8769, 8770, 8771, 8772, 8773, 8774];

// Once a server has positively identified itself as Kestrel, the secret can safely be exercised.
// Test connection is the operator's only way to find out the secret is wrong before every capture
// starts failing, so identifying the app but not checking the credential would be a half-test.
// The secret is required on every extension request, not only over the LAN: Kestrel refuses an
// extension-origin request without it (403), so any other installed extension cannot drive it.
async function checkSecret(host, port) {
  const secret = $("secret").value;
  if (!secret) return "No shared secret set; captures will be refused. Generate one under Settings > LAN in Kestrel.";
  try {
    const r = await fetch(`http://${host}:${port}/api/opportunities/lookup`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Kestrel-Key": secret },
      body: JSON.stringify({ urls: [] }),
      signal: AbortSignal.timeout(1500),
    });
    if (r.ok) return "Shared secret accepted.";
    return r.status === 401 || r.status === 403
      ? "Shared secret REJECTED; captures will fail."
      : `Shared secret check inconclusive (HTTP ${r.status}).`;
  } catch (e) {
    return "Shared secret could not be checked.";
  }
}

async function test() {
  const host = $("host").value.trim() || "127.0.0.1";
  const typed = parseInt($("port").value, 10) || 0;
  // The shared secret is NOT sent while probing: /health needs no auth, and a rung may be some
  // other local server we have not identified yet. Same rule as the background worker.
  // Blank port means auto-detect, so test what the background worker would actually do.
  const candidates = typed ? [typed] : PORT_LADDER;
  for (const p of candidates) {
    try {
      const r = await fetch(`http://${host}:${p}/health`, {
        signal: AbortSignal.timeout(1500),
      });
      if (!r.ok) continue;
      const j = await r.json();
      if (j && j.service === "kestrel") {
        $("status").textContent =
          `Connected to Kestrel ${j.version || ""} on port ${p}. ` + (await checkSecret(host, p));
        return;
      }
    } catch (e) {
      // This rung is not listening, or is not Kestrel. Try the next one.
    }
  }
  $("status").textContent = typed
    ? `Nothing answering as Kestrel on port ${typed}.`
    : "No Kestrel found on its usual ports. Is it running?";
}

$("save").addEventListener("click", save);
$("test").addEventListener("click", test);
load();
