/* Pipeline (D-UI-96): the small amount of behavior HTMX does not give the page. Selecting a role
   slides its panel in (and gives the panel its own URL, ?open=<id>, so Back and a pasted link
   work), the hover checkboxes and the dark bulk bar, j / k / Enter / x / Esc, the menus, More
   filters, and folding rows out when an action takes them off the list. The server answers
   actions with HX-Trigger events (pl-gone, pl-close, pl-clear, pipelineRefresh, k-toast); this file
   listens for them. Everything is delegated on the document, so it keeps working after the list
   is swapped. The page runs on the lifecycle (KUI.page, D-UI-210): this file only defines it, and
   mount starts every listener and timer through its context, so leaving stops all of them and
   coming back starts one set. User-facing strings here are the selection count and two toast
   lines. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var state = { sel: null, picked: [], last: null, mf: false, slide: '', poll: false, known: null };
  var cx = null;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function reduced() {
    return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function rows() {
    return $$('#pipeline-list .pl-row[data-id]').filter(function (r) { return r.offsetParent !== null; });
  }
  function rowFor(id) { return $('#pipeline-list .pl-row[data-id="' + id + '"]'); }
  function panel() { return $('#pl-panel'); }
  function toast(msg, opts) { if (root.KUI) root.KUI.toast(msg, opts || {}); }
  // Another page, in place when the window can (D-UI-210).
  function go(url) {
    if (root.KUI && root.KUI.go) root.KUI.go(url); else root.location.href = url;
  }
  function ajax(method, url, opts) {
    return root.htmx ? root.htmx.ajax(method, url, opts) : Promise.resolve();
  }

  /* --------------------------------------------------------------------- panel position */
  // The panel is as tall as its content, top-aligned with the first group (not the search bar),
  // and follows the page as it scrolls, never past the bottom of the list.
  function fitPanel() {
    var p = panel(), stage = $('#pl-stage');
    if (!p || !stage) return;
    var anchor = $('#pipeline-list .pl-group, #pipeline-list .pl-signals, #pipeline-list .pl-empty, #pipeline-list .pl-nf');
    var sr = stage.getBoundingClientRect();
    var top = anchor ? anchor.getBoundingClientRect().top - sr.top : 0;
    top = Math.max(top, 12 - sr.top);
    // As tall as its content, but never past the bottom of the window: the footer stays in view
    // and the body scrolls inside the panel.
    p.style.maxHeight = Math.max(260, root.innerHeight - Math.max(sr.top + top, 12) - 12) + 'px';
    top = Math.min(top, Math.max(0, sr.height - p.offsetHeight - 12));
    p.style.setProperty('--pl-top', Math.max(0, Math.round(top)) + 'px');
  }
  function onScrollOrResize() { fitPanel(); placeBulk(); }

  /* ------------------------------------------------------------------------ selection */
  function openParam() {
    var id = new URL(root.location.href).searchParams.get('open');
    return id && /^[0-9]+$/.test(id) ? Number(id) : null;
  }
  function pushOpen(id) {
    var u = new URL(root.location.href);
    if (id) u.searchParams.set('open', String(id)); else u.searchParams.delete('open');
    if (u.href !== root.location.href) root.history.pushState({ pl: id || null }, '', u.href);
  }
  function markSel() {
    rows().forEach(function (r) {
      var on = Number(r.dataset.id) === state.sel;
      r.classList.toggle('is-sel', on);
      if (on) r.setAttribute('aria-current', 'true'); else r.removeAttribute('aria-current');
    });
  }
  function afterFill() {
    var p = panel();
    if (!p) return;
    p.classList.remove('is-out');
    p.classList.add('is-open');
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { p.classList.remove('is-in'); fitPanel(); });
    });
    fitPanel();
  }
  // Only the newest request may fill the panel: a slow answer for an earlier role is dropped.
  // Leaving the page drops an answer still on its way (the fetch carries the page's signal).
  var seq = 0;
  function load(id) {
    var mine = ++seq;
    var signal = cx.signal;
    return root.fetch('/opportunities/' + id + '/panel', { credentials: 'same-origin', signal: signal })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        if (mine !== seq || signal.aborted) return;
        var p = panel();
        if (!p) return;
        p.innerHTML = html;
        if (root.htmx) root.htmx.process(p);
        afterFill();
      }, function () { if (mine === seq && !signal.aborted) afterFill(); });
  }
  function select(id, opts) {
    opts = opts || {};
    var p = panel();
    if (!p || !id) return;
    var wasOpen = p.classList.contains('is-open');
    state.sel = id;
    markSel();
    if (!opts.silent) pushOpen(id);
    if (!wasOpen || opts.instant || reduced()) { load(id); return; }
    p.classList.add('is-out');
    cx.after(230, function () { p.classList.remove('is-out'); p.classList.add('is-in'); load(id); });
  }
  function close(opts) {
    var p = panel();
    var prev = state.sel;
    state.sel = null;
    markSel();
    if (p) p.classList.remove('is-open', 'is-out', 'is-in');
    if (!(opts && opts.silent)) pushOpen(null);
    var r = prev ? rowFor(prev) : null;
    if (r && !(opts && opts.noFocus)) r.focus({ preventScroll: true });
  }
  function onPopState() {
    var id = openParam();
    if (id) select(id, { silent: true }); else close({ silent: true, noFocus: true });
  }

  /* ------------------------------------------------------------------------ the bulk bar */
  function placeBulk() {
    var list = $('#pipeline-list');
    if (!list) return;
    var r = list.getBoundingClientRect();
    doc.documentElement.style.setProperty('--pl-bulk-left', Math.round(r.left + r.width / 2) + 'px');
  }
  function updateBulk() {
    var bar = $('#pl-bulk');
    if (!bar) return;
    var present = state.picked.filter(function (id) { return !!rowFor(id); });
    state.picked = present;
    rows().forEach(function (r) {
      var on = present.indexOf(Number(r.dataset.id)) >= 0;
      r.classList.toggle('is-picked', on);
      var ck = $('.pl-ck', r);
      if (ck) ck.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    $$('#pipeline-list .pl-rows').forEach(function (l) { l.classList.toggle('is-selecting', present.length > 0); });
    var count = $('#pl-bcount');
    if (count) count.textContent = present.length + ' selected';
    placeBulk();
    bar.classList.toggle('is-on', present.length > 0);
  }
  function pick(id, on) {
    var at = state.picked.indexOf(id);
    var want = on === undefined ? at < 0 : on;
    if (want && at < 0) state.picked.push(id);
    if (!want && at >= 0) state.picked.splice(at, 1);
    state.last = id;
    updateBulk();
  }
  function pickRange(id) {
    var ids = rows().map(function (r) { return Number(r.dataset.id); });
    var a = ids.indexOf(state.last === null ? id : state.last), b = ids.indexOf(id);
    if (a < 0 || b < 0) { pick(id); return; }
    ids.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(function (x) {
      if (state.picked.indexOf(x) < 0) state.picked.push(x);
    });
    state.last = id;
    updateBulk();
  }
  function clearPicks() { state.picked = []; state.last = null; updateBulk(); }

  /* ------------------------------------------------------------------------- clicks */
  // Registered in the capture phase (mount).
  function onClick(e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var bulk = t.closest('#pl-bulk [data-bulk]');
    if (bulk) { runBulk(bulk.getAttribute('data-bulk')); return; }
    if (t.closest('#pl-bclear')) { clearPicks(); return; }

    // The checkbox, or a modifier click, picks; a plain click on the row opens its panel.
    var rowEl = t.closest('#pipeline-list .pl-row[data-id]');
    var ck = t.closest('.pl-ck');
    if (rowEl && ck) { pick(Number(rowEl.dataset.id)); return; }
    if (rowEl && (e.shiftKey || e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (e.shiftKey) pickRange(Number(rowEl.dataset.id)); else pick(Number(rowEl.dataset.id));
      return;
    }
    if (rowEl && !t.closest('a, button, summary, details, input, select, textarea, .pl-more')) {
      select(Number(rowEl.dataset.id));
      return;
    }

    // The panel's own controls.
    if (t.closest('[data-pl-close]')) { close(); return; }
    if (t.closest('[data-pl-copy]')) {
      var url = root.location.origin + '/opportunities?open=' + (state.sel || '');
      var u = new URL(root.location.href);
      u.searchParams.set('open', String(state.sel || ''));
      var done = function () { toast('Link copied'); };
      if (root.navigator.clipboard && root.navigator.clipboard.writeText) {
        root.navigator.clipboard.writeText(u.href || url).then(done, done);
      } else { done(); }
      return;
    }
    var foot = t.closest('#pl-foot');
    if (foot) {
      if (t.closest('[data-pl-ask]')) { foot.classList.add('is-ask'); foot.classList.remove('is-lead'); return; }
      if (t.closest('[data-pl-lead]')) { foot.classList.add('is-lead'); foot.classList.remove('is-ask'); return; }
      if (t.closest('[data-pl-back]')) { foot.classList.remove('is-ask', 'is-lead'); return; }
      var due = t.closest('.pl-due button');
      if (due) {
        $$('.pl-due button', foot).forEach(function (b) { b.classList.toggle('is-on', b === due); });
        var hid = $('.pl-due input[name="due"]', foot);
        if (hid) hid.value = due.getAttribute('data-due');
        return;
      }
    }

    // Menus and More filters.
    if (t.closest('#pl-sort-more')) {
      var menu = $('#pl-sort-menu');
      var open = menu && menu.hidden;
      closeMenus();
      if (menu) { menu.hidden = !open; $('#pl-sort-more').setAttribute('aria-expanded', open ? 'true' : 'false'); }
      return;
    }
    if (t.closest('#pl-view-btn')) {
      var slot = $('#pl-view-slot');
      if (slot && slot.firstChild) { e.stopPropagation(); closeMenus(); }
      return;
    }
    if (t.closest('#pl-mf-btn')) { toggleMore(); return; }
    var tab = t.closest('#pl-tabs button[data-tab]');
    if (tab) { switchTab(tab); return; }
    if (!t.closest('.pl-drop, .pl-viewwrap, .pl-sortg')) closeMenus();
    if (!t.closest('.pl-more')) $$('details.pl-more[open]').forEach(function (d) { d.open = false; });

    // A click anywhere else on the page closes the panel (not on a menu, a toast or the bar).
    var p = panel();
    if (p && p.classList.contains('is-open') &&
        !t.closest('#pl-panel, .pl-row, .pl-drop, .pl-viewwrap, #pl-bulk, .k-toasts, .k-modal, .pl-more, a[href]')) {
      close({ noFocus: true });
    }
  }

  // The View menu closes on its second click, and a menu click inside it must not.
  function closeMenus() {
    var m = $('#pl-sort-menu');
    if (m) m.hidden = true;
    var b = $('#pl-sort-more');
    if (b) b.setAttribute('aria-expanded', 'false');
    var slot = $('#pl-view-slot');
    if (slot) slot.innerHTML = '';
    var vb = $('#pl-view-btn');
    if (vb) vb.setAttribute('aria-expanded', 'false');
  }
  function toggleMore(force, instant) {
    var mf = $('#pl-mf'), btn = $('#pl-mf-btn');
    if (!mf) return;
    state.mf = force === undefined ? !state.mf : force;
    mf.hidden = false;
    if (instant) mf.style.transition = 'none';
    mf.classList.toggle('is-open', state.mf);
    if (instant) { void mf.offsetHeight; mf.style.transition = ''; }
    if (btn) btn.setAttribute('aria-expanded', state.mf ? 'true' : 'false');
    cx.after(380, fitPanel);
  }

  /* ------------------------------------------------------------- Consulting tabs, the ink */
  function placeInk() {
    var tabs = $('#pl-tabs');
    if (!tabs) return;
    var on = $('button.is-on', tabs), ink = $('.pl-tabs__ink', tabs);
    if (!on || !ink) return;
    ink.style.left = on.offsetLeft + 'px';
    ink.style.width = on.offsetWidth + 'px';
  }
  function switchTab(btn) {
    var tabs = $('#pl-tabs');
    var order = $$('button[data-tab]', tabs);
    state.slide = order.indexOf(btn) > order.indexOf($('button.is-on', tabs)) ? 'r' : 'l';
    $$('button[data-tab]', tabs).forEach(function (b) {
      var on = b === btn;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    placeInk();
    clearPicks();
    close({ noFocus: true });
  }

  /* ------------------------------------------------------------------------- bulk actions */
  function runBulk(action) {
    var ids = state.picked.slice();
    if (!ids.length) return;
    if (action === 'score-again') {
      // The same model choice every Score again offers, as its own page.
      go('/rescore/choose?opp_ids=' + ids.join(',') + '&page=1&next=' +
        encodeURIComponent('/opportunities'));
      return;
    }
    ajax('POST', '/pipeline/bulk/' + action, {
      source: '#pl-bulk', target: '#pl-bulk', swap: 'none', values: { ids: ids.join(',') }
    });
  }

  /* ------------------------------------------------------------------------- keyboard */
  function onKeyDown(e) {
    var t = e.target;
    if (e.key === 'Escape') {
      var foot = $('#pl-foot');
      if (foot && (foot.classList.contains('is-ask') || foot.classList.contains('is-lead'))) {
        foot.classList.remove('is-ask', 'is-lead');
      } else if (state.picked.length) { clearPicks(); }
      else if (panel() && panel().classList.contains('is-open')) { close(); }
      closeMenus();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (!$('#pipeline-list')) return;
    var list = rows();
    var at = list.findIndex(function (r) { return Number(r.dataset.id) === state.sel; });
    if (e.key === 'j' || e.key === 'k') {
      if (!list.length) return;
      e.preventDefault();
      var next = e.key === 'j' ? Math.min(list.length - 1, at + 1) : Math.max(0, at < 0 ? 0 : at - 1);
      var r = list[next];
      select(Number(r.dataset.id));
      r.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'x' && state.sel) {
      e.preventDefault();
      pick(state.sel);
    } else if (e.key === 'Enter' && !/^(A|BUTTON|SUMMARY)$/.test(t ? t.tagName : '')) {
      var id = state.sel || (t && t.closest && t.closest('.pl-row') ? Number(t.closest('.pl-row').dataset.id) : null);
      if (id) { e.preventDefault(); go('/opportunities/' + id); }
    }
  }

  /* --------------------------------------------------------- what the server tells us */
  function ids(e) { return (e.detail && e.detail.ids) || []; }
  // A refresh asked for while the list is mid request or mid swap is held until it settles. The
  // event is otherwise lost: the old list is detached when its answer swaps in, and the new one is
  // not yet listening, so Undo's refresh could be swallowed by the stale answer of the delayed
  // refresh after a hide, leaving the restored role out of the list.
  var listBusy = false;
  var refreshHeld = false;
  var busyGuard = null;
  function fireRefresh() { if (root.htmx) root.htmx.trigger(doc.body, 'pipelineRefresh'); }
  function listIdle() {
    listBusy = false;
    cx.clear(busyGuard);
    if (refreshHeld) { refreshHeld = false; fireRefresh(); }
  }
  function refresh() {
    if (listBusy) refreshHeld = true; else fireRefresh();
  }
  function onBeforeRequest(e) {
    if (!e.target || e.target.id !== 'pipeline-list') return;
    listBusy = true;
    cx.clear(busyGuard);
    busyGuard = cx.after(15000, listIdle); // never wedge on a request that reports nothing
  }
  var LIST_DONE = ['htmx:afterSettle', 'htmx:responseError', 'htmx:sendError', 'htmx:timeout',
                   'htmx:swapError'];
  function onListDone(e) {
    if (e.target && e.target.id === 'pipeline-list') { listIdle(); syncRescoringPanel(); }
  }
  // Being scored again (issue #74): the list polls while work is live and its rows carry the
  // state. When the open role's row no longer matches what its panel shows (queued became
  // running, or the new score landed), the panel is asked again in place, with no slide.
  function syncRescoringPanel() {
    if (!state.sel) return;
    var row = rowFor(state.sel), inner = $('#pl-panel .pl-panel__in');
    if (!row || !inner || Number(inner.getAttribute('data-id')) !== Number(state.sel)) return;
    if ((row.getAttribute('data-rescoring') || '') !== (inner.getAttribute('data-rescoring') || '')) {
      select(state.sel, { silent: true, instant: true });
    }
  }

  function onGone(e) {
    var gone = ids(e);
    var n = 0;
    gone.forEach(function (id) {
      var r = rowFor(id);
      state.picked = state.picked.filter(function (x) { return x !== id; });
      if (state.sel === id) close({ noFocus: true });
      if (!r) return;
      n += 1;
      var drop = function () { if (r.parentNode) r.parentNode.removeChild(r); };
      if (root.KUI && root.KUI.foldOut) root.KUI.foldOut(r, drop); else drop();
    });
    updateBulk();
    cx.after(n && !reduced() ? 520 : 0, refresh);
  }
  function onClose(e) {
    var id = e.detail && (e.detail.value !== undefined ? e.detail.value : e.detail);
    if (!id || Number(id) === state.sel) close({ noFocus: true });
  }
  function onClear() { clearPicks(); }
  function onPanelRefresh() { if (state.sel) select(state.sel, { silent: true, instant: true }); }
  // Undo of any action: the list and the panel are asked again.
  function onUndone() {
    refresh();
    if (state.sel) select(state.sel, { silent: true, instant: true });
  }
  function onView() { closeMenus(); }

  /* ------------------------------------------------------------------- HTMX plumbing */
  // A request for the list carries the open panel, so the URL it pushes keeps it.
  function onConfigRequest(e) {
    var d = e.detail;
    // The list asking for itself (a scan is running) is a refresh, not a new filter.
    state.poll = !!(d && d.elt && d.elt.id === 'pipeline-list' && d.verb === 'get');
    if (d && d.target && d.target.id === 'pipeline-list' && d.verb === 'get' && state.sel) {
      d.parameters.open = String(state.sel);
    }
  }
  // A role that is gone still gets its panel's message, not a silent 404.
  function onPanel404(e) {
    var d = e.detail;
    if (d && d.xhr && d.xhr.status === 404 && d.target && d.target.id === 'pl-panel') {
      d.shouldSwap = true;
      d.isError = false;
    }
  }
  function onResponseError(e) {
    var d = e.detail;
    if (d) d.kHandled = true;
    if (d && d.xhr && d.xhr.status === 404 && d.target && d.target.id === 'pl-panel') return;
    toast('Could not do that. Try again.', { tone: 'warn' });
  }
  function onAfterSettle(e) {
    var t = e.target;
    if (t && t.id === 'pipeline-list') {
      if (state.slide) {
        t.setAttribute('data-slide', state.slide);
        var s = state.slide;
        state.slide = '';
        cx.after(400, function () { if (t.getAttribute('data-slide') === s) t.removeAttribute('data-slide'); });
      }
      if (state.mf) toggleMore(true, true);
      var mfBtn = $('#pl-mf-btn');
      if (mfBtn && state.mf) mfBtn.setAttribute('aria-expanded', 'true');
      markSel();
      updateBulk();
      placeInk();
      announceArrivals(t);
    }
    fitPanel();
  }
  // The list is replaced whole by every refresh: an action's own pipelineRefresh, the delayed one
  // after a role leaves, the scan poll. A row menu that is open when that happens would come back
  // closed, under the pointer, and the item being reached for would vanish. The open menu is
  // remembered by row and opened again in the new list.
  var menuRow = null;
  function onMenuBeforeSwap(e) {
    var t = e.target;
    if (!t || t.id !== 'pipeline-list') return;
    var d = t.querySelector('details.pl-more[open]');
    var row = d && d.closest('.pl-row');
    menuRow = row ? row.getAttribute('data-id') : null;
  }
  function onMenuAfterSwap(e) {
    var t = e.target;
    if (t && t.id === 'pipeline-list' && menuRow) {
      var d = t.querySelector('.pl-row[data-id="' + menuRow + '"] details.pl-more');
      if (d) d.open = true;
      menuRow = null;
    }
  }
  function onPanelAfterSwap(e) {
    var t = e.target;
    if (t && t.id === 'pl-panel') {
      // The panel was refilled by an action (Shortlist): keep it open.
      if (t.querySelector('.pl-panel__in')) afterFill();
    }
  }

  // A role a scan just found drops into its group with the fading ring (k-arrive).
  function rowIds(scope) {
    return $$('.pl-row[data-id]', scope).map(function (r) { return r.dataset.id; });
  }
  function announceArrivals(list) {
    var ids = rowIds(list);
    if (state.poll && state.known && !reduced()) {
      var seen = state.known;
      ids.forEach(function (id) {
        if (seen.indexOf(id) >= 0) return;
        var r = rowFor(Number(id));
        if (!r) return;
        r.classList.add('is-fresh-in');
        cx.after(2800, function () { r.classList.remove('is-fresh-in'); });
      });
    }
    state.known = ids;
    state.poll = false;
  }

  /* ------------------------------------------------------------------------ scroll cues
     The locked mockup's arrows: a round arrow at the top or bottom centre of a scrolling box that
     has more to see, hidden while it scrolls and at its ends. Clicking one scrolls by most of a
     page. The role panel body and the Not a fit box get one; each visit draws its own (cues). */
  var CUES = [['.pl-db', '#pl-panel'], ['.pl-nf__rows', '.pl-nf']];
  var cues = [];
  function cueFor(el, host) {
    for (var i = 0; i < cues.length; i++) if (cues[i].el === el) return cues[i];
    var cs = { el: el, host: host, t: 0, moving: false, offs: [] };
    ['up', 'down'].forEach(function (dir) {
      var d = doc.createElement('button');
      d.type = 'button'; d.className = 'pl-cue pl-cue--' + dir; d.tabIndex = -1;
      d.setAttribute('aria-label', dir === 'down' ? 'Scroll down' : 'Scroll up');
      d.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' +
        (dir === 'down' ? 'M6 9l6 6 6-6' : 'M6 15l6-6 6 6') + '"/></svg>';
      cs.offs.push(cx.on(d, 'click', function (ev) {
        ev.stopPropagation();
        el.scrollBy({ top: (dir === 'down' ? 1 : -1) * el.clientHeight * 0.8, behavior: 'smooth' });
      }));
      host.appendChild(d);
      cs[dir] = d;
    });
    cs.offs.push(cx.on(el, 'scroll', function () {
      cs.moving = true; updateCue(cs); cx.clear(cs.t);
      cs.t = cx.after(650, function () { cs.moving = false; updateCue(cs); });
    }, { passive: true }));
    cues.push(cs);
    return cs;
  }
  // A box that left the page takes its arrows and their listeners with it.
  function dropCue(cs) {
    cs.offs.forEach(function (off) { off(); });
    cx.clear(cs.t);
    cs.up.remove(); cs.down.remove();
    var i = cues.indexOf(cs);
    if (i >= 0) cues.splice(i, 1);
  }
  function updateCue(cs) {
    var el = cs.el, host = cs.host;
    if (!el.isConnected) { dropCue(cs); return; }
    var r = el.getBoundingClientRect(), h = host.getBoundingClientRect();
    var seen = Math.min(r.bottom, h.bottom) - Math.max(r.top, h.top);
    var vis = seen > 60 && root.getComputedStyle(el).visibility !== 'hidden';
    var x = r.left - h.left + r.width / 2;
    cs.up.style.left = cs.down.style.left = x + 'px';
    cs.up.style.top = (r.top - h.top + 8) + 'px';
    cs.down.style.top = (r.bottom - h.top - 42) + 'px';
    var more = el.scrollHeight - el.clientHeight;
    cs.up.classList.toggle('is-show', vis && !cs.moving && more > 4 && el.scrollTop > 8);
    cs.down.classList.toggle('is-show', vis && !cs.moving && more > 4 && el.scrollTop < more - 8);
  }
  function refreshCues() {
    CUES.forEach(function (pair) {
      $$(pair[0]).forEach(function (el) {
        var host = el.closest(pair[1]) || $(pair[1]);
        if (host) updateCue(cueFor(el, host));
      });
    });
  }

  function init() {
    // Arriving from Today: the content staggers in under the scene (the locked mockup's
    // .content.arrive). Any other way in shows the page as is. <html>.k-from-today says so on a
    // page load (base.html, from the referrer) and on an in-place change (kestrel-ui.js), where
    // the referrer is still the first document's.
    var pg = $('#pipeline');
    if (pg && doc.documentElement.classList.contains('k-from-today') && !reduced()) {
      pg.classList.add('pl-arrive');
      cx.after(1600, function () { pg.classList.remove('pl-arrive'); });
    }
    state.known = rowIds();
    var id = Number(($('#pipeline') || {}).dataset ? $('#pipeline').dataset.open : 0) || null;
    if (id) { state.sel = id; markSel(); }
    placeInk();
    fitPanel();
    placeBulk();
    if ($('#pl-mf') && $('#pl-mf-btn') && $('#pl-mf-btn').classList.contains('is-on')) toggleMore(true, true);
  }

  root.KUI.page.define('pipeline', {
    mount: function (main, ctx) {
      cx = ctx;
      state = { sel: null, picked: [], last: null, mf: false, slide: '', poll: false, known: null };
      listBusy = false; refreshHeld = false; busyGuard = null; menuRow = null; cues = [];
      ctx.on(doc, 'scroll', onScrollOrResize, true);
      ctx.on(root, 'resize', onScrollOrResize);
      // The panel's own history entries (?open=<id>, state {pl: id}): Back closes or reopens it.
      ctx.on(root, 'popstate', onPopState);
      ctx.on(doc, 'click', onClick, true);
      ctx.on(doc, 'keydown', onKeyDown);
      ctx.on(doc, 'htmx:beforeRequest', onBeforeRequest);
      LIST_DONE.forEach(function (n) { ctx.on(doc, n, onListDone); });
      ctx.on(doc, 'pl-gone', onGone);
      ctx.on(doc, 'pl-close', onClose);
      ctx.on(doc, 'pl-clear', onClear);
      ctx.on(doc, 'pl-panel-refresh', onPanelRefresh);
      ctx.on(doc, 'kestrel:undone', onUndone);
      ctx.on(doc, 'pl-view', onView);
      ctx.on(doc, 'htmx:configRequest', onConfigRequest);
      ctx.on(doc, 'htmx:beforeSwap', onPanel404);
      ctx.on(doc, 'htmx:responseError', onResponseError);
      ctx.on(doc, 'htmx:afterSettle', onAfterSettle);
      ctx.on(doc, 'htmx:beforeSwap', onMenuBeforeSwap);
      ctx.on(doc, 'htmx:afterSwap', onMenuAfterSwap);
      ctx.on(doc, 'htmx:afterSwap', onPanelAfterSwap);
      ctx.every(400, refreshCues);
      ctx.on(root, 'resize', refreshCues);
      init();
      ctx.onUnmount(function () {
        cues.slice().forEach(dropCue);
        doc.documentElement.style.removeProperty('--pl-bulk-left');
      });
    }
  });
})(window);
