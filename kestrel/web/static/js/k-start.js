/* The page lifecycle start and the busy feedback for htmx requests, loaded without defer in
   <head> right after the deferred scripts are declared (moved out of base.html, D-INST-8). */
// The page's start (D-UI-210): every deferred script above, page scripts included, has run
// (definitions first) before DOMContentLoaded, so the page the content root names in its
// data-k-page attribute mounts here, once. This listener is added while the head is parsed,
// ahead of htmx's own start, so the page hears htmx's first load events as it did when page
// scripts started themselves.
document.addEventListener('DOMContentLoaded', function () {
  if (window.KUI && window.KUI.page) window.KUI.page.start();
});
// Busy feedback for every htmx request (kestrel.css body.busy): counted, so overlapping
// requests keep the cursor until the last one answers, and a failed send clears it too.
(function () {
  var inflight = 0;
  function on(name, fn) { document.addEventListener(name, fn); }
  // A polled or self-loading partial (hx-trigger "every", "load", "revealed") is not a click:
  // it never shows the cursor or the bar, so a page change does not flash a loading bar.
  // Nor is a long request that shows its own status (data-no-busy, e.g. the import): there a
  // spinning cursor for a minute reads as a hang.
  // Nor is a request the app makes on its own, with nobody waiting on it: those carry the
  // X-Kestrel-Background header (a timed refresh, a prefetch, a counts update), so only
  // what the person pressed shows the bar, however the press is wired (#99).
  function background(evt) { var rc = evt.detail && evt.detail.requestConfig; var h = rc && rc.headers; return !!(h && h['X-Kestrel-Background']); }
  function polled(evt) { var el = evt.detail && evt.detail.elt; if (background(evt)) return true; if (el && el.closest && el.closest('[data-no-busy], [data-busy-text]:not([data-busy-text=""])')) return true; var t = el && el.getAttribute && el.getAttribute('hx-trigger'); return !!(t && /\b(every|load|revealed|intersect)\b/.test(t)); }
  // An element with data-busy-text (a model call, a PDF render) gets a spinner and that text
  // beside it for as long as its request is open, instead of the cursor (D-UI-20).
  // An empty value names no status, so it is not a host (it would insert an empty spinner).
  function host(evt) { var el = evt.detail && evt.detail.elt; return el && el.closest && el.closest('[data-busy-text]:not([data-busy-text=""])'); }
  function showStatus(h) {
    if (h.__status) return;
    var s = document.createElement('span');
    s.className = 'import-status busy-status'; s.setAttribute('role', 'status'); s.setAttribute('aria-live', 'polite');
    s.style.display = 'inline-flex';
    var dot = document.createElement('span'); dot.className = 'spin'; dot.setAttribute('aria-hidden', 'true');
    s.appendChild(dot); s.appendChild(document.createTextNode(h.getAttribute('data-busy-text')));
    var btn = h.tagName === 'FORM' ? h.querySelector('button[type=submit], button:not([type])') : null;
    (btn || h).insertAdjacentElement('afterend', s);
    h.__status = s;
  }
  function hideStatus(h) { if (h.__status) { h.__status.remove(); h.__status = null; } }
  // The decision is recorded on the request itself: htmx re-fires afterRequest on an
  // ancestor when the element was swapped out mid-request, with that ancestor as elt, so
  // judging the element again on the way out would miscount.
  function xhr(evt) { return evt.detail && evt.detail.xhr; }
  on('htmx:beforeRequest', function (evt) {
    var x = xhr(evt); if (!x || x.__kseen) return; x.__kseen = true;
    var h = host(evt); if (h) { x.__khost = h; showStatus(h); return; }
    if (polled(evt)) return;
    x.__kcounted = true; inflight += 1; document.body.classList.add('busy');
    // A count that never comes back (a node swapped out mid-request) clears itself, so the
    // bar cannot run on an idle app (#99).
    counted.push(x); x.__ktimer = setTimeout(function () { release(x); }, BUSY_LIMIT_MS);
  });
  var BUSY_LIMIT_MS = 30000, counted = [];
  function release(x) {
    if (!x.__kcounted || x.__kreleased) return; x.__kreleased = true; clearTimeout(x.__ktimer);
    var i = counted.indexOf(x); if (i >= 0) counted.splice(i, 1);
    inflight = Math.max(0, inflight - 1); if (!inflight) document.body.classList.remove('busy');
  }
  function done(evt) {
    var x = xhr(evt); if (!x || x.__kdone) return; x.__kdone = true;
    if (x.__khost) { hideStatus(x.__khost); return; }
    release(x);
  }
  on('htmx:afterRequest', done);  // fired on load, error, abort and timeout alike
  on('htmx:historyRestore', function () { counted.slice().forEach(release); });
})();
