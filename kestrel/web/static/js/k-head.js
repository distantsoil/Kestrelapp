/* Before first paint, in <head>, loaded without defer (moved out of base.html for the
   Content-Security-Policy, D-INST-8): the service-worker cleanup in the desktop window, then the theme,
   scheme and motion classes and the page-change recorder. Same order and timing as the inline scripts it
   replaces. */
// Inside the desktop window there is no service worker: a page it controls loses WebView2's
// bridge (window.chrome.webview), so the window controls cannot reach the shell, and
// WebView2 turned link navigations into Save As downloads (Windows hardware check, round 3).
// Remove one an earlier run left in the window's profile, then reload once, uncontrolled.
(function () {
  if (document.documentElement.getAttribute('data-k-desktop') !== '1') return;
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.getRegistrations().then(function (regs) {
    return Promise.all(regs.map(function (r) { return r.unregister(); }));
  }).then(function () {
    if (!navigator.serviceWorker.controller) return;
    try {
      if (sessionStorage.getItem('kestrel-sw-cleared')) return;
      sessionStorage.setItem('kestrel-sw-cleared', '1');
    } catch (e) { return; }
    location.reload();
  }).catch(function () {});
})();
(function () {
  try {
    var t = localStorage.getItem('kestrel-theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
    var s = localStorage.getItem('kestrel-scheme');
    if (s === 'indigo' || s === 'teal' || s === 'plum' || s === 'graphite' || s === 'contrast') document.documentElement.setAttribute('data-scheme', s);
  } catch (e) {}
  // Motion before first paint (D-UI-187): the launch entrance once per launch
  // (the desktop app asks with #k-intro; a browser gets it on its first page of the
  // session), and the page arrival on every other in-app page change.
  try {
    var h = document.documentElement;
    var launched = sessionStorage.getItem('kestrel-launched');
    if (location.hash === '#k-intro' || !launched) {
      h.classList.add('k-intro');
      sessionStorage.setItem('kestrel-launched', '1');
      if (location.hash === '#k-intro') history.replaceState(null, '', location.pathname + location.search);
    } else if (document.referrer && document.referrer.indexOf(location.origin + '/') === 0) {
      h.classList.add('k-page-in');
      // Leaving Today settles the hero band into the slim one (D-UI-199).
      var from = document.referrer.slice(location.origin.length).split(/[?#]/)[0];
      if (from === '/' && location.pathname !== '/') h.classList.add('k-from-today');
    }
  } catch (e) {}
  // Settings > Preferences > Reduce motion also skips the page cross-fade (D-UI-201); the
  // OS setting already turns it off in CSS.
  window.addEventListener('pagereveal', function (e) {
    if (e.viewTransition && document.documentElement.getAttribute('data-reduce-motion') === '1') {
      e.viewTransition.skipTransition();
    }
    // The page-change recorder (D-UI-204), desktop window only: one log line per change.
    try {
      if (document.documentElement.getAttribute('data-k-desktop') !== '1') return;
      var nav = performance.getEntriesByType('navigation')[0] || {};
      var from = document.referrer.indexOf(location.origin) === 0
        ? document.referrer.slice(location.origin.length).split(/[?#]/)[0] : '';
      var rec = { to: location.pathname, came_from: from, vt: !!e.viewTransition,
        ttfb_ms: nav.responseStart || 0, reveal_ms: performance.now(), hidden: document.hidden };
      try {  // the page left behind recorded its side as it went (pageswap below)
        var out = JSON.parse(sessionStorage.getItem('kestrel-swap') || 'null');
        sessionStorage.removeItem('kestrel-swap');
        if (out && out.to === location.pathname) {
          rec.out_vt = out.vt; rec.out_anims = out.anims; rec.out_htmx = out.htmx;
          rec.out_busy_ms = out.busy;
        }
      } catch (err) { /* storage off: the new page's side alone */ }
      var body = JSON.stringify(rec);
      navigator.sendBeacon('/api/diag/page', new Blob([body], { type: 'application/json' }));
    } catch (err) { /* diagnostics never get in the way */ }
  });
  // D-UI-872: the page cross-fade is a browser-owned cross-document transition (components.css:
  // @view-transition { navigation: auto }). When the browser aborts it (observed when the next
  // document is a bare 404 that does not opt in), the transition's promises can reject, and no
  // page code asked for them: the console showed "Uncaught (in promise) ... Transition was
  // aborted because of invalid state". Marking them handled changes nothing else: the promises
  // settle as before and the navigation goes on. navCommit does the same for its own transition
  // (kestrel-ui.js).
  window.addEventListener('pageswap', function (e) {
    var vt = e.viewTransition;
    if (!vt) return;
    [vt.ready, vt.finished, vt.updateCallbackDone].forEach(function (pr) {
      if (pr && pr.catch) pr.catch(function () { /* the load goes on */ });
    });
  });
  // The outgoing side of the same record (D-UI-209): Chromium decides here whether the page
  // being left gets a transition, so note what this page was doing when it decided.
  if (document.documentElement.getAttribute('data-k-desktop') === '1') {
    var busy = [];
    try {
      new PerformanceObserver(function (l) {
        var now = performance.now();
        busy = busy.filter(function (t) { return t[0] + t[1] > now - 1000; });  // last second
        l.getEntries().forEach(function (t) { busy.push([t.startTime, t.duration]); });
      }).observe({ type: 'longtask', buffered: true });
    } catch (err) { /* no long-task timing here */ }
    window.addEventListener('pageswap', function (e) {
      try {
        var act = e.activation && e.activation.entry;
        var to = act && act.url ? new URL(act.url).pathname : '';
        var now = performance.now(), ms = 0;
        busy.forEach(function (t) { if (t[0] + t[1] > now - 1000) ms += t[1]; });
        sessionStorage.setItem('kestrel-swap', JSON.stringify({
          to: to, vt: !!e.viewTransition, busy: Math.round(ms),
          anims: document.getAnimations ? document.getAnimations().length : 0,
          htmx: document.querySelectorAll('.htmx-request').length }));
      } catch (err) { /* diagnostics never get in the way */ }
    });
  }
})();

/* ---------------------------------------------------------------------------------------------
   What the Content-Security-Policy (D-INST-8) took away from the templates, done here instead.
   The policy has no 'unsafe-inline' and no 'unsafe-eval', so a template carries no style=
   attribute, no inline handler, no hx-on, no htmx trigger filter and no js: value. Each is a
   data-k-* attribute that this file reads, from the first element the parser inserts onward
   (a MutationObserver delivers before the next paint, so nothing is drawn without its value).
   ------------------------------------------------------------------------------------------ */
(function () {
  'use strict';
  var doc = document;

  // data-k-style="width: 42%; --p: 3" (D-SEC-57): a value the template computes, applied
  // through the CSSOM, which the policy does not govern. Only these properties and custom
  // properties are applied, and only plain values: no url(), no quotes, no escapes.
  var PROPS = { width: 1, height: 1, left: 1, flex: 1, 'animation-delay': 1,
    'animation-duration': 1, 'stroke-dashoffset': 1, 'transform-origin': 1, 'transform-box': 1 };
  var VALUE = /^[-+\w.%#(), ]*$/;
  function parseStyle(text) {
    var out = [];
    String(text || '').split(';').forEach(function (part) {
      var i = part.indexOf(':');
      if (i < 1) return;
      var prop = part.slice(0, i).trim().toLowerCase(), value = part.slice(i + 1).trim();
      if (!(PROPS[prop] === 1 || /^--[a-z0-9-]+$/.test(prop))) return;
      if (!VALUE.test(value) || /url\s*\(/i.test(value)) return;
      out.push([prop, value]);
    });
    return out;
  }
  function applyStyle(el) {
    if (!el.style) return;
    var decl = parseStyle(el.getAttribute('data-k-style'));
    var had = el.__kStyled || [];
    var now = decl.map(function (d) { return d[0]; });
    had.forEach(function (p) { if (now.indexOf(p) < 0) el.style.removeProperty(p); });
    decl.forEach(function (d) { el.style.setProperty(d[0], d[1]); });
    el.__kStyled = now;
  }

  // Dialogs a partial opens as it arrives (they carried a script after them): data-k-show-modal
  // opens it, data-k-remove-on-close drops it once closed, data-k-backdrop-close closes it on a
  // click outside the box, data-k-no-cancel keeps Esc from closing it, and [data-sf-close] inside
  // closes it. A marker <i hidden data-k-open-kui="id"> hands the element with that id to
  // KUI.openModal when KUI has started (as the inline script did, a full page load leaves it to
  // the page).
  function setUpDialog(d) {
    if (d.__kDialog) return;
    d.__kDialog = true;
    if (d.hasAttribute('data-k-remove-on-close')) d.addEventListener('close', function () { d.remove(); });
    if (d.hasAttribute('data-k-backdrop-close')) {
      d.addEventListener('click', function (e) { if (e.target === d) d.close(); });
    }
    if (d.hasAttribute('data-k-no-cancel')) d.addEventListener('cancel', function (e) { e.preventDefault(); });
    d.querySelectorAll('[data-sf-close]').forEach(function (b) {
      b.addEventListener('click', function () { d.close(); });
    });
    if (d.hasAttribute('data-k-show-modal')) {
      if (typeof d.showModal === 'function') d.showModal(); else d.setAttribute('open', '');
    }
  }
  function openKuiModal(marker) {
    if (marker.__kKui) return;
    marker.__kKui = true;
    var el = doc.getElementById(marker.getAttribute('data-k-open-kui'));
    if (el && window.KUI && window.KUI.openModal) window.KUI.openModal(el);
  }

  // <i hidden data-k-init="name">: run the setup registered under that name (KStyle.init) as
  // the parser inserts the marker, where an inline script used to run; once per marker. A
  // setup registered after its marker arrived runs on registration.
  var inits = {}, waiting = {};
  function runInit(marker) {
    if (marker.__kInit) return;
    var name = marker.getAttribute('data-k-init');
    if (!inits[name]) { (waiting[name] = waiting[name] || []).push(marker); return; }
    marker.__kInit = true;
    inits[name](marker);
  }
  function registerInit(name, fn) {
    inits[name] = fn;
    (waiting[name] || []).splice(0).forEach(runInit);
  }
  registerInit('window-controls', function () {
  try {
    var os = sessionStorage.getItem('kestrel-frame');
    var w = document.getElementById('k-wctl');
    if (w && (os === 'win' || os === 'mac' || os === 'linux')) {
      w.hidden = false;
      w.classList.toggle('is-mac', os === 'mac');
      document.documentElement.setAttribute('data-k-frame', os);
    }
  } catch (e) { /* storage off */ }
  });

  var SEL = '[data-k-style], [data-k-show-modal], [data-k-open-kui], [data-k-init]';
  function visit(el) {
    if (el.hasAttribute('data-k-init')) runInit(el);
    if (el.hasAttribute('data-k-style')) applyStyle(el);
    if (el.hasAttribute('data-k-show-modal')) setUpDialog(el);
    if (el.hasAttribute('data-k-open-kui')) openKuiModal(el);
  }
  function scan(root) {
    if (!root || root.nodeType !== 1) return;
    visit(root);
    if (root.firstElementChild) root.querySelectorAll(SEL).forEach(visit);
  }
  window.KStyle = { apply: applyStyle, parse: parseStyle, scan: scan, init: registerInit };
  new MutationObserver(function (records) {
    records.forEach(function (r) {
      if (r.type === 'attributes') { if (r.target.hasAttribute('data-k-style')) applyStyle(r.target); else if (r.target.__kStyled) applyStyle(r.target); return; }
      r.addedNodes.forEach(scan);
    });
  }).observe(doc.documentElement, { childList: true, subtree: true, attributes: true,
    attributeFilter: ['data-k-style'] });
  scan(doc.documentElement);

  // Handlers that were inline attributes, delegated from the document. Capture phase, so each
  // runs before the element's own listeners and htmx's, as an inline handler did.
  doc.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target : null;
    if (!t) return;
    var q = t.closest('[data-k-quit-open]');
    if (q) { if (window.kestrelQuit) window.kestrelQuit.open(q); return; }
    var tg = t.closest('[data-k-toggle]');
    if (tg) {
      e.preventDefault();
      var m = doc.getElementById(tg.getAttribute('data-k-toggle'));
      if (m) { m.hidden = !m.hidden; tg.setAttribute('aria-expanded', String(!m.hidden)); }
    }
  }, true);
  doc.addEventListener('change', function (e) {
    var t = e.target;
    var spec = t && t.getAttribute && t.getAttribute('data-k-change-set');  // "name=value"
    if (!spec || !t.form) return;
    var i = spec.indexOf('=');
    var field = t.form.querySelector('[name="' + spec.slice(0, i) + '"]');
    if (field) field.value = spec.slice(i + 1);
  }, true);
  doc.addEventListener('submit', function (e) {
    if (e.target && e.target.hasAttribute && e.target.hasAttribute('data-k-no-submit')) e.preventDefault();
  }, true);

  // htmx without eval (htmx.config.allowEval is off; see the htmx-config meta in base.html).
  // data-k-when="poll=no-modal; run-refresh=no-modal" replaces a trigger filter: the request a
  // trigger of that kind would send is not sent while the named check says no ("poll" is a
  // timed trigger, which carries no event; any other word is the event's type).
  function modalIn(sel) { return function () { return !doc.querySelector(sel); }; }
  var WHEN = {
    'no-modal': modalIn('dialog:modal'),
    'no-td-top-modal': modalIn('#td-top dialog:modal'),
    'no-td-quick-modal': modalIn('#td-quick dialog:modal'),
    'sources-poll-ok': function () { return typeof window.srcPollOk === 'function' && !!window.srcPollOk(); },
    'pipeline-idle': function () {
      var a = doc.activeElement, rows = doc.getElementById('not-fit-rows');
      return (!a || a.id !== 'pipeline-q') &&
        !(doc.getElementById('not-fit-show') && rows && rows.childElementCount);
    },
    'not-view-name': function (ev) { return !(ev && ev.target && ev.target.name === 'view_name'); }
  };
  window.KStyle.when = WHEN;
  function whenAllows(el, ev) {
    var spec = el && el.getAttribute && el.getAttribute('data-k-when');
    if (!spec) return true;
    var kind = ev && ev.type ? ev.type : 'poll';
    return spec.split(';').every(function (pair) {
      var i = pair.indexOf('=');
      if (i < 0 || pair.slice(0, i).trim() !== kind) return true;
      var check = WHEN[pair.slice(i + 1).trim()];
      return check ? check(ev) : false;  // a name nobody defined never sends
    });
  }
  doc.addEventListener('htmx:confirm', function (e) {
    var d = e.detail || {};
    if (!whenAllows(d.elt, d.triggeringEvent)) e.preventDefault();
  });
  // data-k-vals="np-tab" replaces a js: hx-vals.
  var VALS = {
    'np-tab': function (p) {
      var np = doc.getElementById('sf-np');
      p.tab = (np && np.dataset.tab) || 'all';
    }
  };
  doc.addEventListener('htmx:configRequest', function (e) {
    var el = e.detail && e.detail.elt;
    var name = el && el.getAttribute && el.getAttribute('data-k-vals');
    if (name && VALS[name]) VALS[name](e.detail.parameters);
  });
  // data-k-after-request replaces hx-on::after-request: "close-dialog" closes the dialog it is
  // in, "reset-if-ok" resets the form after a successful request, and "remove-li-if-ok" removes
  // the list item it is in after a success that htmx did not retarget.
  var AFTER = ['close-dialog', 'reset-if-ok', 'remove-li-if-ok'];
  // Every name the attributes above accept, for tests/test_csp_browser.py (a name a template
  // uses that is missing here would silently never act).
  window.KStyle.names = function () {
    return { when: Object.keys(WHEN), vals: Object.keys(VALS), after: AFTER.slice(),
             init: Object.keys(inits) };
  };
  doc.addEventListener('htmx:afterRequest', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('[data-k-after-request]') : null;
    if (!t) return;
    var d = e.detail || {}, what = t.getAttribute('data-k-after-request');
    if (AFTER.indexOf(what) < 0) return;
    if (what === 'close-dialog') {
      var dlg = t.closest('dialog'); if (dlg) dlg.close();
    } else if (what === 'reset-if-ok') {
      if (d.successful && t.reset) t.reset();
    } else if (what === 'remove-li-if-ok') {
      if (d.successful && !(d.xhr && d.xhr.getResponseHeader('HX-Retarget'))) {
        var li = t.closest('li'); if (li) li.remove();
      }
    }
  });
})();
