/* Sources (D-UI-102): the behavior HTMX does not give the page. Search and filters over the rows
   already on the page, the collapsible groups, the switch that slides and folds a row out, opening
   the drawer and the Add sheet, and the Add sheet's small helpers. Everything is delegated on the
   document, so it keeps working after the groups or the whole panel are swapped. The page runs on
   the lifecycle (KUI.page, D-UI-210): this file only defines it, and mount starts every listener
   and timer through its context, so leaving stops all of them and coming back starts one set.
   All user-facing words come from the server's markup (data-why, toasts); none are written
   here. */
(function (root) {
  'use strict';
  function reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function smooth() { return reduced() ? 'auto' : 'smooth'; }
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  var FILTERS_KEY = 'kestrel.sources.filters';
  var COLLAPSED_KEY = 'kestrel.sources.collapsed';
  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var state = { q: '', type: '', origin: '', health: '' };
  var collapsed = null;       // group keys the operator folded; Paused starts folded
  var arrive = null;          // a row id to bring in with a glow after the next swap
  var drawerWasOpen = false;
  var sheetWasOpen = false;
  var cx = null;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function ss(key) { try { return root.sessionStorage.getItem(key); } catch (e) { return null; } }
  function ssSet(key, value) { try { root.sessionStorage.setItem(key, value); } catch (e) { /* private window */ } }
  function load() {
    try { var f = JSON.parse(ss(FILTERS_KEY) || '{}'); Object.keys(state).forEach(function (k) { if (typeof f[k] === 'string') state[k] = f[k]; }); } catch (e) { /* none */ }
    try { collapsed = new root.Set(JSON.parse(ss(COLLAPSED_KEY) || 'null') || ['paused']); } catch (e) { collapsed = new root.Set(['paused']); }
  }
  function save() { ssSet(FILTERS_KEY, JSON.stringify(state)); ssSet(COLLAPSED_KEY, JSON.stringify(Array.from(collapsed))); }
  function filtering() { return !!(state.q || state.type || state.origin || state.health); }

  /* ----------------------------------------------------------------- filters and groups */
  function matches(row) {
    var q = state.q.trim().toLowerCase();
    if (q && (row.getAttribute('data-search') || '').indexOf(q) < 0) return false;
    if (state.type && row.getAttribute('data-group') !== state.type) return false;
    if (state.origin && row.getAttribute('data-origin') !== state.origin) return false;
    if (state.health && row.getAttribute('data-health') !== state.health) return false;
    return true;
  }
  function syncControls() {
    var q = $('#src-q'); if (q && q.value !== state.q) q.value = state.q;
    [['#src-ftype', 'type'], ['#src-forig', 'origin'], ['#src-fhealth', 'health']].forEach(function (p) {
      var el = $(p[0]); if (el && el.value !== state[p[1]]) el.value = state[p[1]];
    });
    $$('[data-src-health]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-src-health') === state.health ? 'true' : 'false');
    });
  }
  function apply() {
    var list = $('#src-list');
    if (!list) return;
    var on = filtering();
    var seen = 0;
    $$('.src-grp', list).forEach(function (g) {
      var key = g.getAttribute('data-g');
      var rows = $$('.src-row', g);
      var shown = 0;
      rows.forEach(function (r) { var ok = matches(r); r.hidden = !ok; if (ok) shown += 1; });
      var cnt = $('.src-cnt', g);
      if (cnt && key !== 'sug') cnt.textContent = String(shown);
      // Suggestions step aside while filtering; an empty group hides, Paused hides under a health filter.
      var hide = (key === 'sug' && on) || (on && key !== 'sug' && shown === 0) ||
                 (key === 'paused' && !!state.health);
      g.hidden = hide;
      if (!hide && key !== 'sug') seen += shown;
      var open = !collapsed.has(key) || (on && shown > 0);
      g.classList.toggle('is-open', open);
      var tog = $('[data-src-collapse]', g); if (tog) tog.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    var none = $('#src-none');
    if (none) none.hidden = !(on && seen === 0);
    syncControls();
  }

  function onSearchInput(e) {
    if (e.target && e.target.id === 'src-q') { state.q = e.target.value; save(); apply(); }
  }
  function onChange(e) {
    var t = e.target; if (!t) return;
    if (t.id === 'src-ftype') { state.type = t.value; save(); apply(); }
    else if (t.id === 'src-forig') { state.origin = t.value; save(); apply(); }
    else if (t.id === 'src-fhealth') { state.health = t.value; save(); apply(); }
    else if (t.getAttribute && t.getAttribute('data-src-toggle')) { toggle(t); }
    else if (t.getAttribute && t.getAttribute('data-board')) { boardNote(); }
  }

  /* ------------------------------------------------------------------------------- clicks */
  function onClick(e) {
    var t = e.target; if (!t || !t.closest) return;
    var tile = t.closest('[data-src-health]');
    if (tile) { var k = tile.getAttribute('data-src-health'); state.health = state.health === k ? '' : k; save(); apply(); return; }
    if (t.closest('[data-src-clear]')) { state = { q: '', type: '', origin: '', health: '' }; save(); apply(); return; }
    var col = t.closest('[data-src-collapse]');
    if (col) {
      var key = col.getAttribute('data-src-collapse');
      if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
      save(); apply(); return;
    }
    var add = t.closest('[data-src-add]');
    if (add) { openSheet(add.getAttribute('data-src-add')); return; }
    var det = t.closest('[data-src-open]');
    if (det) { openDrawer(det.getAttribute('data-src-open')); return; }
    var row = t.closest('.src-row');
    if (row && !t.closest('button, a, input, label, select')) { openDrawer(row.getAttribute('data-id')); return; }
    var edit = t.closest('[data-src-edit]');
    if (edit) {
      var d = $('[data-testid="drawer-details"]'); if (d) d.open = true;
      var f = $('#ed-name'); if (f) { f.scrollIntoView({ block: 'center' }); f.focus(); }
    }
  }

  /* ---------------------------------------------------------- the switch, then the fold out */
  // A switch's save waits for its animation, but never for the page: leaving sends it at once.
  var pending = [];
  function toggle(box) {
    var id = box.getAttribute('data-src-toggle');
    var row = box.closest('.src-row');
    box.disabled = true;
    var sent = false;
    var url = '/sources/' + id + '/toggle';
    var go = function (leaving) {
      if (sent || !root.htmx) return;
      sent = true;
      var i = pending.indexOf(go);
      if (i >= 0) pending.splice(i, 1);
      var list = doc.getElementById('src-list');
      if (list && leaving !== true) {
        root.htmx.ajax('POST', url, { target: list, swap: 'outerHTML' });
      } else {
        // Leaving: each save is its own request (htmx would queue several from one element and
        // keep only the last), and keepalive lets it finish if the window loads a new document.
        root.fetch(url, { method: 'POST', credentials: 'same-origin', keepalive: true,
                          headers: { 'HX-Request': 'true' } }).catch(function () {});
      }
    };
    pending.push(go);
    // The switch slides first, then the row folds out, then the groups arrive from the server.
    cx.after(240, function () {
      if (row && root.KUI && root.KUI.foldOut) root.KUI.foldOut(row, go); else go();
    });
  }

  /* ------------------------------------------------------------- the drawer and the sheet */
  function openDrawer(id) {
    if (!id || !root.htmx) return;
    $$('.src-row.is-sel').forEach(function (r) { r.classList.remove('is-sel'); });
    var row = $('#src-row-' + id); if (row) row.classList.add('is-sel');
    if (root.KUI) root.KUI.closeFlyout();
    root.htmx.ajax('GET', '/sources/' + id + '/drawer', { target: '#src-drawer-slot', swap: 'innerHTML' });
  }
  function openSheet(kind) {
    if (!root.htmx) return;
    root.htmx.ajax('GET', '/sources/add' + (kind ? '/' + encodeURIComponent(kind) : ''),
                   { target: '#src-sheet-slot', swap: 'innerHTML' });
  }
  // A swap that replaces an overlay already open should not replay its slide-in.
  function reopen(el, kind, was) {
    if (!el || !root.KUI) return;
    if (was) el.style.transition = 'none';
    if (kind === 'slideover') root.KUI.openSlideOver(el); else root.KUI.openModal(el);
    if (was) { void el.offsetWidth; cx.after(60, function () { el.style.transition = ''; }); }
  }

  // The "Checking N sources" note re-reads the list every 6 seconds, for at most ten minutes
  // after the last Check all now, so a stuck task cannot poll forever.
  // The polling nodes ask srcPollOk() through their hx-trigger filter; mount publishes it and
  // unmount makes it answer no, so a poll still on its way after the page is left stops there.
  var polls = 0;
  function pollOk() { return ++polls <= 100; }
  function pollStopped() { return false; }
  function onCheckAll(e) {
    var p = e.detail && e.detail.pathInfo && e.detail.pathInfo.requestPath;
    if (p && /\/sources\/check-all$/.test(p)) polls = 0;
  }

  // The overlay opens after htmx has settled the new element: htmx re-applies the response's own
  // attributes during the settle step, which would drop the is-open class added any earlier.
  function onOverlaySettle(e) {
    var t = e.target; if (!t) return;
    if (t.id === 'sources-check-slot') {
      reopen($('#sources-check-dialog', t), 'modal', false);
    } else if (t.id === 'src-drawer-slot') {
      reopen($('#src-drawer', t), 'slideover', drawerWasOpen);
      drawerWasOpen = true;
    } else if (t.id === 'src-sheet-slot') {
      var sh = $('#src-sheet', t);
      reopen(sh, 'modal', sheetWasOpen);
      sheetWasOpen = true;
      sheetInit(sh);
    }
  }
  function onCheckSwap(e) {
    var t = e.target; if (!t) return;
    if (t.id === 'src-check') {
      var go = $('#src-add-go');
      if (go) go.disabled = !$('[name="pick"]', t);
    }
  }
  function onClosed(e) {
    var t = e.target; if (!t || !t.id) return;
    if (t.id === 'src-drawer') {
      drawerWasOpen = false;
      $$('.src-row.is-sel').forEach(function (r) { r.classList.remove('is-sel'); });
    } else if (t.id === 'src-sheet') { sheetWasOpen = false; }
  }

  function pulse(row) {
    if (!row || !root.KUI) return;
    root.KUI.arrive(row);
    var head = $('.src-grph', row.closest('.src-grp') || doc);
    if (head) { head.classList.remove('k-pulse-once'); void head.offsetWidth; head.classList.add('k-pulse-once'); }
  }
  // Events the server raises with HX-Trigger.
  function onDrawerClose() { if (root.KUI) root.KUI.closeSlideOver(); }
  function onSheetClose() { if (root.KUI) root.KUI.closeModal(); }
  function onMoved(e) {
    arrive = e.detail && e.detail.id;
    if (root.KUI) root.KUI.closeSlideOver();
  }
  function onArrived(e) { arrive = e.detail && e.detail.id; }

  function onListSettle(e) {
    var t = e.target; if (!t) return;
    if (t.id === 'src-list' || t.id === 'sources-panel') {
      apply();
      if (arrive) {
        var row = $('#src-row-' + arrive);
        if (row) {
          var g = row.closest('.src-grp');
          if (g && !g.classList.contains('is-open')) { collapsed.delete(g.getAttribute('data-g')); save(); apply(); }
          pulse(row);
          if (row.scrollIntoView) row.scrollIntoView({ block: 'nearest', behavior: smooth() });
        }
        arrive = null;
      }
    }
  }
  function onOobSwap(e) {
    var t = e.detail && e.detail.target;
    if (t && t.id === 'src-list') apply();
  }
  // A failed switch leaves the row folded: ask for the groups again.
  function reload() {
    if (root.htmx && $('#src-list')) root.htmx.ajax('GET', '/sources/list', { target: '#src-list', swap: 'outerHTML' });
  }
  function onResponseError(e) {
    var p = e.detail && e.detail.pathInfo && e.detail.pathInfo.requestPath;
    if (p && /\/sources\/\d+\/toggle$/.test(p)) reload();
  }

  /* --------------------------------------------------------------- the Add sheet's helpers */
  function boardNote() {
    var note = $('#src-bdmsg'); if (!note) return;
    var whys = $$('.src-bd.is-limited input:checked').map(function (i) { return i.closest('.src-bd').getAttribute('data-why'); }).filter(Boolean);
    note.textContent = '';
    if (whys.length) {
      var box = doc.createElement('div');
      box.className = 'src-msg is-warn';
      var p = doc.createElement('div');
      p.textContent = whys.join(' ') + ' You can still add it.';
      box.appendChild(p);
      note.appendChild(box);
    }
    $$('[data-src-key-panel]').forEach(function (box) {
      var panel = $(box.getAttribute('data-src-key-panel'));
      if (panel) panel.hidden = !box.checked;
    });
    var ind = $('#src-indeed'), cb = $('[data-board="indeed"]');
    if (ind && cb) ind.hidden = !cb.checked;
    // At least one board stays picked.
    if (!$('[data-board]:checked')) { if (cb) { cb.checked = true; boardNote(); } }
  }
  function sheetInit(sheet) {
    if (!sheet) return;
    if ($('[data-board]', sheet)) boardNote();
  }
  // Editing the link, name or feed clears the last answer and turns Add off again.
  function onCheckInput(e) {
    if (e.target && e.target.hasAttribute && e.target.hasAttribute('data-src-check-input')) {
      var out = $('#src-check'); if (out) out.textContent = '';
      var go = $('#src-add-go'); if (go) go.disabled = true;
    }
  }
  // Enter checks; it never adds something that has not been read.
  function onKeyDown(e) {
    if (e.key === 'Enter' && e.target && e.target.hasAttribute && e.target.hasAttribute('data-src-check-input')) {
      e.preventDefault();
      var b = $('#src-check-go'); if (b) b.click();
    }
  }

  // A good link answer lights the platform chip it names (mockup .plat.hit).
  function onCheckAnswer(e) {
    var t = e.detail && e.detail.target;
    if (!t || t.id !== 'src-check') return;
    var text = (t.textContent || '').toLowerCase();
    var good = !!t.querySelector('[data-testid="check-good"], [data-testid="check-match"], [data-testid="check-info"]');
    $$('.src-plat[data-p]').forEach(function (c) {
      c.classList.toggle('is-hit', good && text.indexOf(c.getAttribute('data-p').toLowerCase()) >= 0);
    });
  }
  function onCheckClear(e) {
    if (e.target && e.target.hasAttribute && e.target.hasAttribute('data-src-check-input')) {
      $$('.src-plat.is-hit').forEach(function (c) { c.classList.remove('is-hit'); });
    }
  }

  root.KUI.page.define('sources', {
    mount: function (main, ctx) {
      cx = ctx;
      state = { q: '', type: '', origin: '', health: '' };
      collapsed = null; arrive = null; drawerWasOpen = false; sheetWasOpen = false; polls = 0;
      pending = [];
      // A page change (in place: k-page-leave, before the next page is read; a full load:
      // pagehide) or the end of the page sends any switch save still waiting for its animation.
      var flush = function () { pending.slice().forEach(function (go) { go(true); }); };
      ctx.on(doc, 'k-page-leave', flush);
      ctx.on(root, 'pagehide', flush);
      ctx.onUnmount(flush);
      root.srcPollOk = pollOk;
      ctx.on(doc, 'input', onSearchInput);
      ctx.on(doc, 'change', onChange);
      ctx.on(doc, 'click', onClick);
      ctx.on(doc, 'htmx:beforeRequest', onCheckAll);
      ctx.on(doc, 'htmx:afterSettle', onOverlaySettle);
      ctx.on(doc, 'htmx:afterSwap', onCheckSwap);
      ctx.on(doc, 'k-closed', onClosed);
      ctx.on(doc, 'src-drawer-close', onDrawerClose);
      ctx.on(doc, 'src-sheet-close', onSheetClose);
      ctx.on(doc, 'src-moved', onMoved);
      ctx.on(doc, 'src-arrived', onArrived);
      ctx.on(doc, 'htmx:afterSettle', onListSettle);
      ctx.on(doc, 'htmx:oobAfterSwap', onOobSwap);
      ctx.on(doc, 'htmx:responseError', onResponseError);
      ctx.on(doc, 'htmx:sendError', reload);
      ctx.on(doc, 'kestrel:undone', reload);
      ctx.on(doc, 'input', onCheckInput);
      ctx.on(doc, 'keydown', onKeyDown);
      ctx.on(doc, 'htmx:afterSwap', onCheckAnswer);
      ctx.on(doc, 'input', onCheckClear);
      load();
      apply();
      ctx.onUnmount(function () { root.srcPollOk = pollStopped; });
    }
  });
})(window);
