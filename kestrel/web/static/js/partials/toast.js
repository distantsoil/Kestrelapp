/* Moved out of templates/partials/toast.html for the Content-Security-Policy (D-INST-8). Loaded where the
   inline script was, so it runs at the same point; inside an htmx swap it runs as the swap lands. */
(function () {
  var KEY = 'kestrel-scan-event';            // the last scan event any tab has shown
  var DISMISSED = 'kestrel-scan-dismissed';  // the scan event a tab dismissed
  var NEW_HREF = '/opportunities?new_today=1';  // the Pipeline's New today chip
  var MAIN = 'kestrel-toast', NOTE = 'kestrel-toast-note';
  var memo = {};  // when localStorage is unavailable, remember for this page only
  var timers = {};
  var HOLD_SCAN = 9000;  // the scan toast has a destination, so it stays longer
  var HOLD_NOTE = 6000;  // a plain note here (KUI.toast, when present, times its own)
  var queued = [];  // plain toasts waiting while the scan toast holds the only box
  var shownEvent = null;  // the scan event the persistent toast is showing, if any
  var fresh = false;  // set when the rail has just swapped in a new #scan-signal
  // One toast system: the scan toast lives in the shared bottom region with the others.
  (function () {
    var region = document.getElementById('k-toasts'), t = document.getElementById(MAIN);
    if (region && t && t.parentNode !== region && region.appendChild) region.appendChild(t);
  })();
  function load(k) {
    try { var v = localStorage.getItem(k); if (v !== null) return v; } catch (e) {}
    return k in memo ? memo[k] : null;
  }
  function store(k, v) {
    memo[k] = v;
    try { localStorage.setItem(k, v); } catch (e) {}
  }
  function getSeen() { return load(KEY); }
  function setSeen(v) { store(KEY, v); }
  function box(id) { return document.getElementById(id); }
  function hide(id) {
    id = id || MAIN;
    var t = box(id);
    clearTimeout(timers[id]); timers[id] = null;
    if (!t) return;
    t.removeAttribute('data-show');
    setTimeout(function () { if (!t.hasAttribute('data-show')) t.hidden = true; }, 300);
    if (id === MAIN) {
      shownEvent = null;
      var next = queued.shift();
      if (next) show(next[0], next[1]);
    }
  }
  function dismissScan() {
    if (shownEvent !== null) store(DISMISSED, shownEvent);  // clears it in the other tabs
    hide(MAIN);
  }
  // opts: {href: where a click goes, main: use the scan toast's box, tone: 'warn'}.
  // The scan toast owns #kestrel-toast; any other goes to #kestrel-toast-note, so a plain
  // toast can never replace it or start a timer on it.
  function show(text, opts) {
    opts = opts || {};
    var id = opts.main ? MAIN : NOTE;
    var t = box(id);
    if (!t && !opts.main) {
      if (shownEvent !== null) { queued.push([text, opts]); return; }  // no second box
      id = MAIN; t = box(MAIN);
    }
    if (!t) return;
    var link = t.querySelector('[data-toast-text]');
    link.textContent = text;
    if (link.setAttribute && link.removeAttribute) {
      if (opts.href) link.setAttribute('href', opts.href); else link.removeAttribute('href');
    }
    if (opts.tone) t.setAttribute('data-tone', opts.tone); else t.removeAttribute('data-tone');
    t.hidden = false;
    requestAnimationFrame(function () { t.setAttribute('data-show', ''); });
    arm(id);
  }
  // A toast clears after its hold (9 s for the scan toast, 6 s for a plain note here); a pointer
  // or focus on it holds it, and leaving restarts the clock, as the shared toasts do.
  function arm(id) {
    clearTimeout(timers[id]);
    timers[id] = setTimeout(function () { hide(id); }, id === MAIN ? HOLD_SCAN : HOLD_NOTE);
  }
  function hold(id) { clearTimeout(timers[id]); timers[id] = null; }
  function release(e) {
    var id = ownerOf(e.target), to = e.relatedTarget;
    if (!id || (to && ownerOf(to) === id)) return;
    var t = box(id);
    if (t && t.hasAttribute('data-show')) arm(id);
  }
  function grab(e) { var id = ownerOf(e.target); if (id) hold(id); }
  document.addEventListener('mouseover', grab);
  document.addEventListener('focusin', grab);
  document.addEventListener('mouseout', release);
  document.addEventListener('focusout', release);
  // Plain notes, with or without Undo or a destination, go through the shared toast region
  // (kestrel-ui.js). The scan toast above stays here: it is cross-tab and clickable, which the
  // shared region does not model; it sits in that same region.
  window.kestrelToast = function (text, opts) {
    opts = opts || {};
    if (window.KUI) {
      // Undo rides along (D-UI-152): the shared region owns the button and its longer timeout.
      var undoable = typeof opts.undo === 'function' || !!opts.undoUrl;
      return window.KUI.toast(text, {tone: opts.tone, timeout: undoable ? undefined : 6000,
        undo: opts.undo, undoUrl: opts.undoUrl, undoLabel: opts.undoLabel, href: opts.href});
    }
    return show(text, Object.assign({}, opts, {main: false}));
  };
  // The end of the search step is not the end of the run (D-UI-63): scoring may continue, so
  // this never says "finished". The run panel says "Last run ... finished" when it closes.
  function searchDoneText(n, errs, scoring) {
    var text = 'Search done: ' + n + ' new';
    if (errs) text += ', ' + errs + (errs === 1 ? ' source had a problem' : ' sources had a problem');
    text += '.';
    if (scoring) text += ' Still scoring ' + scoring + '.';
    return text;
  }
  window.kestrelSearchDoneText = searchDoneText;
  function check() {
    var sig = box('scan-signal');
    if (!sig || !sig.hasAttribute('data-ready')) return;
    var ev = sig.getAttribute('data-event') || 'none';
    var seen = getSeen();
    if (seen === null) {
      setSeen(ev);  // first look in this browser: a baseline, never a toast for an old scan
    } else if (ev !== seen && ev !== 'none' && sig.getAttribute('data-scanning') !== '1') {
      // Marked seen only when shown: an event that lands while another scan is still running
      // stays unseen, and toasts once scanning clears (the poll after the last search ends).
      setSeen(ev);
      var n = parseInt(sig.getAttribute('data-new') || '0', 10) || 0;
      var errs = parseInt(sig.getAttribute('data-errors') || '0', 10) || 0;
      var scoring = parseInt(sig.getAttribute('data-scoring') || '0', 10) || 0;
      show(searchDoneText(n, errs, scoring), {href: NEW_HREF, main: true, tone: errs ? 'warn' : ''});
      shownEvent = ev;
    }
    // At most one button refresh per fresh signal, so a state that flips between the two
    // responses can never turn into a request loop.
    var btn = box('scan-btn');
    if (fresh && btn && window.htmx
        && btn.getAttribute('data-scanning') !== sig.getAttribute('data-scanning')) {
      htmx.ajax('GET', '/scan/button', {target: '#scan-btn', swap: 'outerHTML', headers: {'X-Kestrel-Background': '1'}});
    }
    fresh = false;
  }
  function ownerOf(el) {
    if (!el || !el.closest) return null;
    var t = el.closest('#' + NOTE);
    if (t) return NOTE;
    return el.closest('#' + MAIN) ? MAIN : null;
  }
  document.addEventListener('click', function (e) {
    if (!e.target || !e.target.closest) return;
    var id = ownerOf(e.target);
    if (e.target.closest('[data-toast-close]')) {
      if ((id || MAIN) === MAIN) dismissScan(); else hide(id);
      return;
    }
    // A click anywhere on a toast that has a destination follows it (the link itself
    // navigates on its own; the rest of the box does the same).
    if (!id) return;
    var link = box(id) && box(id).querySelector('a[data-toast-text][href]');
    if (!link) return;
    if (id === MAIN) dismissScan(); else hide(id);
    if (e.target.closest('a[href]')) return;
    var to = link.getAttribute('href');
    if (window.KUI && window.KUI.go) window.KUI.go(to); else window.location.assign(to);
  });
  // Escape dismisses the toast that has focus (its link or its x).
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var id = ownerOf(document.activeElement);
    if (!id) return;
    if (id === MAIN) dismissScan(); else hide(id);
  });
  // Another tab dismissed the scan toast this tab is showing: clear it here too.
  if (window.addEventListener) {
    window.addEventListener('storage', function (e) {
      if (e.key === DISMISSED && shownEvent !== null && e.newValue === shownEvent) hide(MAIN);
    });
  }
  document.addEventListener('htmx:oobAfterSwap', function () { fresh = true; setTimeout(check, 0); });
  document.addEventListener('htmx:afterSettle', function () { setTimeout(check, 0); });
})();
