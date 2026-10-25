/* Applications / Leads page script (D-UI-100). Plain script, delegated on the document, so a swap
   of the board, the rail or the panel never needs re-binding. The server does every write (HTMX
   posts) and answers with the board plus, out of band, the rail, the count line and the open
   panel. This file only filters what is on screen, places menus, opens the panel, drags cards and
   animates the move. Nothing here sends anything anywhere. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  var st = {
    q: '', chip: 'all', fit: '', pri: '', work: '', company: '', added: '', due: '',
    range: '0', cap: '0', open: false, expanded: {}, panel: null, menuFor: null, before: null,
    inited: false
  };

  function $(sel, ctx) { return (ctx || doc).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }
  function page() { return $('#apps-page'); }
  /* Follow the card's own server-rendered link for a menu destination. */
  function follow(card, which) {
    var a = card.querySelector('a[data-go="' + which + '"]');
    if (a) a.click();
  }
  function board() { return $('#apps-board'); }
  function reduced() {
    return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function fold(t) {
    return String(t || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  }
  function track() { var p = page(); return p ? p.getAttribute('data-track') : 'employment'; }
  function toast(msg, opts) { if (root.KUI) root.KUI.toast(msg, opts); }

  /* ------------------------------------------------------------------ filters and the fold */
  function readState() {
    var p = page();
    if (!p) return;
    var q = new root.URLSearchParams(root.location.search);
    st.q = q.get('q') || '';
    st.chip = q.get('stage') || 'all';
    st.range = p.getAttribute('data-range') || '0';
    st.cap = p.getAttribute('data-cap') || '0';
    st.open = q.get('open') === 'closed' || p.getAttribute('data-open-closed') === '1';
    var box = $('#ap-q');
    if (box) box.value = st.q;
    ['fit', 'pri', 'work', 'company', 'added', 'due'].forEach(function (k) {
      st[k] = q.get(k) || '';
    });
    $$('[data-f]').forEach(function (el) {
      var k = el.getAttribute('data-f');
      if (k === 'range' || k === 'cap') { st[k] = el.value; return; }
      el.value = st[k];
    });
    var mf = $('#ap-mf'), any = st.fit || st.pri || st.work || st.company || st.added || st.due;
    if (mf && any) { mf.classList.add('is-open'); $('#ap-mf-btn').setAttribute('aria-expanded', 'true'); }
  }

  function dayDiff(iso) {
    var d = Date.parse((iso || '') + 'T00:00:00Z');
    var t = Date.parse(((page() || {}).getAttribute ? page().getAttribute('data-today') : '') + 'T00:00:00Z');
    return isNaN(d) || isNaN(t) ? null : Math.round((t - d) / 86400000);
  }

  function matches(el, words) {
    var ds = el.dataset;
    if (!words.every(function (w) { return (ds.search || '').indexOf(w) >= 0; })) return false;
    // An unscored card is kept, never dropped (hard rule 4).
    if (st.fit && ds.fit !== '' && !(parseFloat(ds.fit) >= parseFloat(st.fit))) return false;
    if (st.pri && ds.pri !== '1') return false;
    if (st.work && ds.work !== st.work) return false;
    if (st.company && (ds.company || '').indexOf(fold(st.company)) < 0 &&
        (ds.company || '').indexOf(st.company.toLowerCase()) < 0) return false;
    if (st.added) {
      var a = dayDiff(ds.added);
      if (a !== null && a > Number(st.added)) return false;
    }
    if (st.due && ds.quiet !== '1') return false;
    var limit = Number(st.range) || 0;
    if (limit) {  // a card with no date is never hidden by the range
      var u = dayDiff(ds.updated);
      if (u !== null && u > limit) return false;
    }
    return true;
  }

  function active() {
    return !!(st.q.trim() || st.chip !== 'all' || st.fit || st.pri || st.work || st.company ||
              st.added || st.due || st.range !== '0');
  }

  function apply() {
    var b = board(), p = page();
    if (!b || !p) return;
    var words = fold(st.q).split(' ').filter(Boolean);
    var items = $$('[data-search]', b);
    var perChip = { all: 0 }, shown = 0, ok = 0;
    var lastCol = p.getAttribute('data-last-col');
    var foldedOpen = st.open || !!st.q.trim() || st.chip === lastCol;
    var colOk = {}, colFolded = {};
    items.forEach(function (el) {
      var pass = matches(el, words);
      var col = el.dataset.col;
      if (pass) {
        perChip.all++; perChip[col] = (perChip[col] || 0) + 1;
      }
      var chipOk = pass && (st.chip === 'all' || st.chip === col);
      el.hidden = !chipOk;
      if (chipOk) {
        ok++;
        colOk[col] = (colOk[col] || 0) + 1;
        if (el.dataset.folded === '1') colFolded[col] = (colFolded[col] || 0) + 1;
      }
    });
    // Per-column cap on the open cards; the folded group is not capped.
    var cap = Number(st.cap) || 0;
    var capped = {};
    $$('.ap-col', b).forEach(function (col) {
      var key = col.getAttribute('data-col');
      col.hidden = st.chip !== 'all' && st.chip !== key;
      var seen = 0, held = 0;
      $$('.ap-card', col).forEach(function (el) {
        if (el.hidden || el.dataset.folded === '1') return;
        seen++;
        if (cap && seen > cap && !st.expanded[key]) { el.hidden = true; held++; }
      });
      capped[key] = held;
      var n = $('.ap-cn', col);
      if (n) n.textContent = colOk[key] || 0;
      var empty = $('.ap-cempty', col);
      if (empty) empty.hidden = (seen - held) > 0;
      var more = $('[data-colmore]', col);
      if (more) {
        var open = st.expanded[key] && cap && seen > cap;
        more.hidden = !(held || open);
        more.textContent = held ? 'Show all ' + seen : 'Show fewer';
      }
      var fg = $('.ap-closedg', col);
      if (fg) {
        fg.classList.toggle('is-open', foldedOpen);
        var btn = $('.ap-cgh', fg);
        if (btn) {
          btn.setAttribute('aria-expanded', foldedOpen ? 'true' : 'false');
          var label = $('span', btn);
          if (label) label.textContent = 'Closed (' + (colFolded[key] || 0) + ')';
        }
      }
    });
    var grid = $('.ap-board', b);
    if (grid) grid.style.setProperty('--n', st.chip === 'all' ? '4' : '1');
    // What is on screen: matches, less the capped ones and a folded group nobody opened.
    shown = ok;
    Object.keys(capped).forEach(function (k) { shown -= capped[k]; });
    if (!foldedOpen && b.getAttribute('data-view') !== 'table') Object.keys(colFolded).forEach(function (k) { shown -= colFolded[k]; });
    var total = items.length;
    var count = $('[data-testid="filter-count"]');
    if (count) count.textContent = 'Showing ' + Math.max(shown, 0) + ' of ' + total;
    var nomatch = $('#apps-nomatch');
    if (nomatch) nomatch.hidden = !(total > 0 && ok === 0);
    $$('#ap-chips [data-chip]').forEach(function (chip) {
      var k = chip.getAttribute('data-chip');
      var n = $('.k-chip__n', chip);
      if (n) n.textContent = perChip[k] || 0;
      var on = k === st.chip;
      chip.classList.toggle('is-on', on);
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    $$('[data-clear-filters]').forEach(function (btn) {
      if (btn.id === 'apps-clear' || btn.getAttribute('data-testid') === 'clear-filters-inline') {
        btn.hidden = !active();
      }
    });
    $$('[data-view-btn]').forEach(function (btn) {
      var on = btn.getAttribute('data-view-btn') === b.getAttribute('data-view');
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    syncUrl();
  }

  function syncUrl() {
    var q = new root.URLSearchParams(root.location.search);
    function set(k, v) { if (v) q.set(k, v); else q.delete(k); }
    set('q', st.q.trim());
    set('stage', st.chip === 'all' ? '' : st.chip);
    ['fit', 'pri', 'work', 'company', 'added', 'due'].forEach(function (k) { set(k, st[k]); });
    set('range', st.range === '0' ? '' : st.range);
    set('cap', st.cap === '0' ? '' : st.cap);
    set('open', st.open ? 'closed' : '');
    var b = board();
    set('view', b && b.getAttribute('data-view') === 'table' ? 'table' : '');
    var s = q.toString();
    try { root.history.replaceState(null, '', root.location.pathname + (s ? '?' + s : '')); }
    catch (e) { /* a sandboxed frame */ }
  }

  function clearFilters() {
    st.q = ''; st.chip = 'all'; st.fit = st.pri = st.work = st.company = st.added = st.due = '';
    st.range = '0';
    var box = $('#ap-q');
    if (box) box.value = '';
    $$('[data-f]').forEach(function (el) {
      var k = el.getAttribute('data-f');
      el.value = k === 'range' ? '0' : k === 'cap' ? st.cap : '';
    });
    savePref('range', '0');
    apply();
  }

  function savePref(k, v) {
    var body = new root.URLSearchParams();
    body.set(k, v);
    if (root.fetch) root.fetch('/pursuits/prefs', { method: 'POST', body: body }).catch(function () {});
  }

  /* ------------------------------------------------------------------------------ menus */
  function closeMenus() {
    $$('.ap-menu').forEach(function (m) { m.hidden = true; });
    st.menuFor = null;
  }
  function placeMenu(menu, anchor) {
    closeMenus();
    menu.hidden = false;
    var r = anchor.getBoundingClientRect();
    var w = menu.offsetWidth, h = menu.offsetHeight;
    var left = Math.max(8, Math.min(r.left, root.innerWidth - w - 8));
    var top = r.bottom + 6;
    if (top + h > root.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
    var first = $('button', menu);
    if (first) first.focus({ preventScroll: true });
  }
  function ownerId(el) {
    var card = el.closest('[data-id]');
    if (card) return card.getAttribute('data-id');
    var pb = el.closest('[data-panel-id]');
    return pb ? pb.getAttribute('data-panel-id') : null;
  }
  function post(url, values) {
    return root.htmx.ajax('POST', url, { target: '#apps-board', swap: 'outerHTML', values: values || {} });
  }
  function openStageMenu(anchor) {
    var id = ownerId(anchor), menu = $('#ap-menu-stage');
    if (!id || !menu) return;
    var label = (anchor.textContent || '').trim();
    $$('button', menu).forEach(function (b) {
      b.classList.toggle('is-on', b.textContent.trim() === label);
    });
    placeMenu(menu, anchor);
    st.menuFor = { id: id, kind: 'stage' };
  }
  function openStepMenu(anchor) {
    var id = ownerId(anchor), menu = $('#ap-menu-' + anchor.getAttribute('data-step-key'));
    if (!id || !menu) return;
    placeMenu(menu, anchor);
    st.menuFor = { id: id, kind: 'step' };
  }
  function openMoreMenu(anchor) {
    var card = anchor.closest('[data-id]');
    var menu = $('#ap-menu-more');
    if (!card || !menu) return;
    var pri = $('[data-more-pick="priority"]', menu);
    if (pri) pri.textContent = card.getAttribute('data-pri') === '1' ? 'Remove high priority' : 'Mark high priority';
    placeMenu(menu, anchor);
    st.menuFor = { id: card.getAttribute('data-id'), kind: 'more', card: card };
  }

  function removeCard(id, needsConfirm) {
    if (needsConfirm && !root.confirm(
      'An offer was entered for this one. Removing it deletes that offer, and Undo cannot bring it back. Remove anyway?')) return;
    post('/pursuits/' + id + '/remove', {});
  }

  function onMenuClick(e) {
    var t = e.target;
    var btn = t.closest('button');
    if (!btn) return;
    var ctx = st.menuFor;
    closeMenus();
    if (!ctx) return;
    var id = ctx.id;
    if (btn.hasAttribute('data-stage-pick')) {
      post('/pursuits/' + id + '/status', { to_status: btn.getAttribute('data-stage-pick') });
    } else if (btn.hasAttribute('data-pick')) {
      var key = btn.getAttribute('data-pick');
      if (key.indexOf('form:') === 0) openPanel(id, key.slice(5));
      else post('/pursuits/' + id + '/act/' + key, {});
    } else if (btn.hasAttribute('data-more-pick')) {
      var card = ctx.card, what = btn.getAttribute('data-more-pick');
      if (what === 'tailor') follow(card, 'tailor');
      else if (what === 'full') follow(card, 'full');
      else if (what === 'priority') post('/pursuits/' + id + '/priority', {});
      else if (what === 'link') copyLink(card.getAttribute('data-full'));
      else if (what === 'remove') removeCard(id, !!card.getAttribute('data-confirm'));
    }
  }
  function copyLink(path) {
    var url = root.location.origin + path;
    var done = function () { toast('Link copied'); };
    if (root.navigator && root.navigator.clipboard && root.navigator.clipboard.writeText) {
      root.navigator.clipboard.writeText(url).then(done, function () { toast('Could not copy the link.'); });
    } else toast('Could not copy the link.');
  }

  /* ----------------------------------------------------------------------------- the panel */
  function markSelected() {
    $$('.ap-card.is-sel, .ap-row.is-sel').forEach(function (el) { el.classList.remove('is-sel'); });
    if (st.panel === null) return;
    $$('.ap-card[data-id="' + st.panel + '"], .ap-row[data-id="' + st.panel + '"]').forEach(function (el) {
      el.classList.add('is-sel');
    });
  }
  function openPanel(id, form) {
    var url = '/pursuits/' + id + '/panel' + (form ? '?form=' + encodeURIComponent(form) : '');
    var panel = $('#ap-panel');
    if (!panel || !root.htmx) return;
    if (String(st.panel) !== String(id)) panelScroll = 0;
    root.htmx.ajax('GET', url, { target: '#ap-panel-in', swap: 'innerHTML' }).then(function () {
      st.panel = id;
      markSelected();
      // The menus count as the opener, so picking from one is not an outside click.
      if (!panel.classList.contains('is-open') && root.KUI) {
        root.KUI.openFlyout(panel, { trigger: $('#ap-menus') });
      }
    });
  }
  function closePanel() {
    var panel = $('#ap-panel');
    if (panel && panel.classList.contains('is-open') && root.KUI) root.KUI.closeFlyout();
  }

  /* --------------------------------------------------------------------- in-place movement */
  function snapRects() {
    var map = {};
    $$('#apps-board .ap-card[data-id]').forEach(function (el) {
      map[el.getAttribute('data-id')] = el.getBoundingClientRect();
    });
    st.before = map;
  }
  function flip() {
    var before = st.before;
    st.before = null;
    if (!before || reduced()) return;
    $$('#apps-board .ap-card[data-id]').forEach(function (el) {
      var b = before[el.getAttribute('data-id')];
      if (!b || !el.animate) return;
      var n = el.getBoundingClientRect();
      var dx = b.left - n.left, dy = b.top - n.top;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
        el.animate([{ transform: 'translate(' + dx + 'px,' + dy + 'px)' }, { transform: 'none' }],
          { duration: 420, easing: 'cubic-bezier(.22,.8,.3,1)' });
      }
    });
  }

  var pending = false, panelScroll = 0;
  function afterSwap() {
    if (pending) return;
    pending = true;
    root.requestAnimationFrame(function () {
      pending = false;
      $$('.ap-closedg').forEach(function (g) { g.classList.add('is-still'); });
      apply();
      root.requestAnimationFrame(function () {
        $$('.ap-closedg').forEach(function (g) { g.classList.remove('is-still'); });
      });
      markSelected();
      flip();
      var body = $('#ap-panel .k-flyout__body');
      if (body && panelScroll) { body.scrollTop = panelScroll; }
      panelScroll = 0;
      var fresh = $('#apps-board .ap-card[data-new="1"]');
      if (fresh && root.KUI) root.KUI.arrive(fresh);
      // A rail that was open stays as the swap left it; the "later" rows stay folded.
    });
  }

  /* ---------------------------------------------------------------------- the click router */
  function onClick(e) {
    if (!page()) return;
    var t = e.target;
    if (t.closest('.ap-menu')) { onMenuClick(e); return; }
    var trigger = t.closest('[data-stage-menu], [data-more], [data-step-kind]');
    if (!trigger) closeMenus();

    var stageBtn = t.closest('[data-stage-menu]');
    if (stageBtn) { e.preventDefault(); openStageMenu(stageBtn); return; }
    var more = t.closest('[data-more]');
    if (more) { e.preventDefault(); openMoreMenu(more); return; }
    var step = t.closest('[data-step-kind]');
    if (step) {
      e.preventDefault();
      var id = ownerId(step), kind = step.getAttribute('data-step-kind'), key = step.getAttribute('data-step-key');
      if (kind === 'menu') openStepMenu(step);
      else if (kind === 'form') openPanel(id, key);
      else post('/pursuits/' + id + '/act/' + key, {});
      return;
    }
    var cg = t.closest('.ap-cgh');
    if (cg) { st.open = !st.open; apply(); return; }
    var colmore = t.closest('[data-colmore]');
    if (colmore) {
      var c = colmore.getAttribute('data-col');
      st.expanded[c] = !st.expanded[c];
      apply();
      return;
    }
    if (t.closest('[data-clear-filters]')) { clearFilters(); return; }
    var mfBtn = t.closest('#ap-mf-btn');
    if (mfBtn) {
      var mf = $('#ap-mf'), openNow = !mf.classList.contains('is-open');
      mf.classList.toggle('is-open', openNow);
      mfBtn.setAttribute('aria-expanded', openNow ? 'true' : 'false');
      return;
    }
    var chip = t.closest('#ap-chips [data-chip]');
    if (chip) { st.chip = chip.getAttribute('data-chip'); apply(); return; }
    var fuMore = t.closest('[data-fu-more]');
    if (fuMore) {
      var showing = fuMore.getAttribute('aria-expanded') === 'true';
      $$('#apps-rail [data-fu="later"]').forEach(function (r) { r.hidden = showing; });
      fuMore.setAttribute('aria-expanded', showing ? 'false' : 'true');
      fuMore.textContent = showing ? fuMore.getAttribute('data-n') + ' more later this week' : 'Show only what is due now';
      var scope = $('[data-fu-scope]');
      if (scope) scope.textContent = showing ? 'due now' : 'this week';
      return;
    }
    var rem = t.closest('[data-remind-open]');
    if (rem) {
      $('[data-remf]').classList.add('is-open');
      rem.hidden = true;
      return;
    }
    if (t.closest('[data-remind-close]')) {
      $('[data-remf]').classList.remove('is-open');
      var again = $('[data-remind-open]');
      if (again) again.hidden = false;
      return;
    }
    var open = t.closest('[data-open]');
    if (open) { openPanel(open.getAttribute('data-open')); return; }
    if (t.closest('[data-quiet-link]')) {
      var late = $('#apps-board .ap-card[data-quiet="1"]:not([hidden])') || $('#apps-board [data-quiet="1"]');
      if (late) openPanel(late.getAttribute('data-id'));
      return;
    }
    var rm = t.closest('[data-panel-remove]');
    if (rm) { removeCard(rm.getAttribute('data-id'), !!rm.getAttribute('data-confirm')); return; }
    if (t.closest('button, a, select, summary, input, label, textarea')) return;
    var card = t.closest('.ap-card, .ap-row');
    if (card) openPanel(card.getAttribute('data-id'));
  }

  function onKey(e) {
    if (!page()) return;
    var t = e.target;
    var tag = t && t.tagName;
    var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable);
    if (e.key === 'Escape' && $$('.ap-menu').some(function (m) { return !m.hidden; })) {
      e.stopPropagation(); closeMenus(); return;
    }
    if (e.key === 'Escape' && t && t.id === 'ap-q') {
      e.preventDefault(); t.value = ''; st.q = ''; apply(); return;
    }
    if (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      var box = $('#ap-q');
      if (box) { e.preventDefault(); box.focus(); box.select(); }
      return;
    }
    if ((e.key === 'Enter' || e.key === ' ') && t && t.matches && t.matches('.ap-card, .ap-row')) {
      e.preventDefault();
      openPanel(t.getAttribute('data-id'));
      return;
    }
    var menu = t && t.closest && t.closest('.ap-menu');
    if (menu && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      var items = $$('button', menu), i = items.indexOf(doc.activeElement);
      e.preventDefault();
      items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
    }
  }

  function onInput(e) {
    var t = e.target;
    if (!page()) return;
    if (t.id === 'ap-q') { st.q = t.value; apply(); return; }
    var f = t.getAttribute && t.getAttribute('data-f');
    if (!f) return;
    st[f] = t.value;
    if (f === 'range' || f === 'cap') savePref(f, t.value);
    apply();
  }

  /* ----------------------------------------------------------------------- drag and drop */
  var drag = { id: null, col: null, el: null };
  function colOf(e) { return e.target && e.target.closest ? e.target.closest('.ap-col') : null; }
  function clearOver() { $$('.ap-col.is-over').forEach(function (c) { c.classList.remove('is-over'); }); }
  function onDragStart(e) {
    var t = e.target;
    if (!t || !t.classList || !t.classList.contains('ap-card')) { drag.id = null; return; }
    drag.id = t.getAttribute('data-id'); drag.col = t.getAttribute('data-col'); drag.el = t;
    t.classList.add('is-dragging');
    closeMenus();
    if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', drag.id); }
  }
  function onDragEnd() {
    if (drag.el) drag.el.classList.remove('is-dragging');
    drag.id = drag.col = drag.el = null;
    clearOver();
  }
  function onDragOver(e) {
    var col = colOf(e);
    if (!col || drag.id === null) return;
    e.preventDefault(); clearOver(); col.classList.add('is-over');
  }
  function onDragLeave(e) {
    var col = colOf(e);
    if (col && !col.contains(e.relatedTarget)) col.classList.remove('is-over');
  }
  function onDrop(e) {
    var col = colOf(e);
    if (!col || drag.id === null) return;
    e.preventDefault(); clearOver();
    var id = drag.id, from = drag.col;
    drag.id = drag.col = null;
    if (from === col.getAttribute('data-col')) return;
    post('/pursuits/' + id + '/status', { to_status: col.getAttribute('data-status') });
  }

  /* ----------------------------------------------------------------------- add a role sheet */
  function onAddDone(e) {
    var form = e.target;
    if (!form || form.id !== 'ap-add-form') return;
    var xhr = e.detail && e.detail.xhr;
    var err = $('#ap-add-err');
    var hadToast = xhr && /k-toast/.test(xhr.getResponseHeader('HX-Trigger') || '');
    root.setTimeout(function () {
      var note = $('#apps-board [data-testid="board-notice"]');
      if (hadToast) {
        if (err) err.textContent = '';
        form.reset();
        if (root.KUI) root.KUI.closeModal();
      } else if (note && note.classList.contains('ap-notice')) {
        if (err) err.textContent = note.textContent;
      } else {
        if (err) err.textContent = '';
        if (note) toast(note.textContent);
        form.reset();
        if (root.KUI) root.KUI.closeModal();
      }
    }, 30);
  }

  function init() {
    if (!page() || st.inited) return;
    st.inited = true;
    readState();
    apply();
    function openDraft(id) {
      root.htmx.ajax('GET', '/pursuits/' + id + '/follow-up', { target: '#ap-follow-up-body', swap: 'innerHTML' });
      root.KUI.openModal('#ap-follow-up');
    }
    doc.addEventListener('click', function (event) {
      var button = event.target.closest('[data-follow-up]');
      if (button) openDraft(button.getAttribute('data-follow-up'));
      if (event.target.closest('[data-follow-up-copy]')) {
        var text = $('#ap-draft-subject').value + '\n\n' + $('#ap-draft-text').value;
        var status = $('#ap-copy-status');
        if (!root.navigator.clipboard) {
          status.textContent = 'Select the draft and copy it with your keyboard.';
          return;
        }
        root.navigator.clipboard.writeText(text).then(function () {
          status.textContent = 'Copied. Review it before you use it.';
        }).catch(function () {
          status.textContent = 'Select the draft and copy it with your keyboard.';
        });
      }
    });
    var autoDraft = $('[data-auto-draft]');
    if (autoDraft) openDraft(autoDraft.getAttribute('data-auto-draft'));
    doc.addEventListener('click', onClick);
    doc.addEventListener('keydown', onKey, true);  // before the overlay's own Escape
    doc.addEventListener('input', onInput);
    doc.addEventListener('change', onInput);
    doc.addEventListener('dragstart', onDragStart);
    doc.addEventListener('dragend', onDragEnd);
    doc.addEventListener('dragover', onDragOver);
    doc.addEventListener('dragleave', onDragLeave);
    doc.addEventListener('drop', onDrop);
    doc.addEventListener('htmx:beforeSwap', function (e) {
      var tgt = e.detail && e.detail.target;
      if (tgt && tgt.id === 'apps-board') {
        snapRects();
        var body = $('#ap-panel .k-flyout__body');
        if (body && st.panel !== null) panelScroll = body.scrollTop;
      }
    });
    doc.addEventListener('htmx:load', afterSwap);
    doc.addEventListener('htmx:configRequest', function (e) {
      if (st.panel === null || !e.detail) return;
      var path = String(e.detail.path || '');
      if (path.indexOf('/pursuits') !== 0 || /\/panel(\?|$)/.test(path)) return;
      e.detail.parameters.panel = st.panel;
    });
    doc.addEventListener('htmx:afterRequest', onAddDone);
    doc.addEventListener('k-closed', function (e) {
      if (e.target && e.target.id === 'ap-panel') { st.panel = null; markSelected(); }
    });
    doc.addEventListener('apps-close-panel', closePanel);
    // A card that is gone (removed elsewhere): say so and show the board as it is.
    doc.addEventListener('htmx:responseError', function (e) {
      var path = String((e.detail && e.detail.pathInfo && e.detail.pathInfo.requestPath) || '');
      if (path.indexOf('/pursuits') !== 0) return;
      toast('That card is no longer on the board.');
      closePanel();
      var b = board();
      var view = b && b.getAttribute('data-view') === 'table' ? 'table' : 'kanban';
      root.htmx.ajax('GET', '/pursuits?view=' + view + '&track=' + track(), { target: '#apps-board', swap: 'outerHTML' });
    });
    // Undo from a toast: ask for the board again, and the panel when one is open.
    doc.addEventListener('kestrel:undone', function () {
      root.htmx.ajax('GET', '/status/counts', { swap: 'none' });
      var b = board();
      var view = b && b.getAttribute('data-view') === 'table' ? 'table' : 'kanban';
      root.htmx.ajax('GET', '/pursuits?view=' + view + '&track=' + track(), { target: '#apps-board', swap: 'outerHTML' });
      if (st.panel !== null) {
        root.htmx.ajax('GET', '/pursuits/' + st.panel + '/panel', { target: '#ap-panel-in', swap: 'innerHTML' });
      }
    });
    root.addEventListener('scroll', closeMenus, true);
    root.addEventListener('resize', closeMenus);
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init); else init();
})(typeof window !== 'undefined' ? window : globalThis);
