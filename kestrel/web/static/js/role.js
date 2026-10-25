/* The Role page (D-UI-150): the small amount of behavior HTMX does not give it. The gliding tab
   underline, the slide of a tab or of Previous and Next, the match-map focus, the Add a story
   sheet, Your view's Adjust panel, the Not for me reasons, the More menu, the dialogs the actions
   load, the scaled document preview, and what Undo reloads. Everything is delegated on the
   document, so it keeps working after a swap. User-facing strings here are the copied-link toast
   and the focus line under the match map. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function reduced() { return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches); }
  function body() { return $('#rl-body'); }
  function oppId() { var p = $('#role'); return p ? p.getAttribute('data-opp') : ''; }
  function toast(msg, opts) { if (root.KUI) root.KUI.toast(msg, opts || {}); }
  function openModal(el) { if (el && root.KUI) root.KUI.openModal(el); }

  /* ------------------------------------------------------------------------ the tab underline */
  function placeInk(instant) {
    var tabs = $('#rl-tabs');
    if (!tabs) return;
    var on = $('a.is-on', tabs), ink = $('.rl-tabs__ink', tabs);
    if (!on || !ink) return;
    if (instant) ink.style.transition = 'none';
    ink.style.left = on.offsetLeft + 'px';
    ink.style.width = on.offsetWidth + 'px';
    if (instant) { void ink.offsetWidth; ink.style.transition = ''; }
  }
  root.addEventListener('resize', function () { placeInk(true); });
  if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(function () { placeInk(true); });

  /* ------------------------------------------------------------- sliding: tabs, Previous, Next */
  var pendingDir = 0;    // set by the click, used by the next swap that follows it
  var pendingKind = '';  // 'pane' | 'body'
  function tabOrder(name) {
    var names = $$('#rl-tabs a').map(function (a) { return a.getAttribute('data-tab'); });
    return names.indexOf(name);
  }
  doc.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('#rl-tabs a[data-tab], a[data-tab][hx-target="#rl-pane"]');
    if (t) {
      var name = t.getAttribute('data-tab'), b = body();
      var cur = b ? b.getAttribute('data-tab') : 'evaluation';
      if (name === cur) { e.preventDefault(); return; }
      pendingDir = tabOrder(name) > tabOrder(cur) ? 1 : -1;
      pendingKind = 'pane';
      if (b) b.setAttribute('data-tab', name);
      $$('#rl-tabs a').forEach(function (a) {
        var on = a.getAttribute('data-tab') === name;
        a.classList.toggle('is-on', on);
        a.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      placeInk(false);
      return;
    }
    var nav = e.target.closest && e.target.closest('[data-rl-dir]');
    if (nav) { pendingDir = parseInt(nav.getAttribute('data-rl-dir'), 10) || 1; pendingKind = 'body'; }
  }, true);

  function slideIn(el) {
    if (!el || reduced() || !el.animate || !pendingDir) { pendingDir = 0; return; }
    el.animate(
      [{ opacity: 0, transform: 'translateX(' + (50 * pendingDir) + 'px)' }, { opacity: 1, transform: 'none' }],
      { duration: 300, easing: 'cubic-bezier(.22,.8,.3,1)' }
    );
    pendingDir = 0;
  }
  function setTitle(el) { doc.title = (el.getAttribute('data-title') || 'Role') + ' · Kestrel'; }
  // The tab title follows the role in the same task as the swap that brings the new heading in.
  // htmx:load waits for the settle delay, so a title set only there lagged the heading by a beat.
  doc.addEventListener('htmx:afterSwap', function (e) {
    if (e.target && e.target.id === 'rl-body') setTitle(e.target);
  });
  doc.addEventListener('htmx:load', function (e) {
    var el = e.target;
    if (!el || !el.id && !el.parentNode) return;
    if (el.id === 'rl-body') {
      setTitle(el);
      var dlg = $('#rl-dialog'); if (dlg) dlg.innerHTML = '';
      if (root.KUI) root.KUI.closeAll();
      placeInk(true);
      if (pendingKind === 'body') slideIn(el);
      pendingKind = '';
      var main = $('.k-main, main'); if (main && main.scrollTo) main.scrollTo(0, 0);
    } else if (el.parentNode && el.parentNode.id === 'rl-pane') {
      if (pendingKind === 'pane') slideIn(el);
      pendingKind = '';
      placeInk(true);
    }
  });
  placeInk(true);

  /* ------------------------------------------------------------------ match map and its focus */
  var focusKey = null;
  function clearFocus() {
    focusKey = null;
    var main = $('.rl-main');
    if (main) main.classList.remove('is-focus');
    $$('.rl-fl, .rl-wseg, .rl-evi').forEach(function (n) { n.classList.remove('is-hit'); });
    $$('.rl-wchip').forEach(function (n) { n.classList.remove('is-on'); });
    var f = $('#rl-wfocus'); if (f) { f.classList.remove('is-on'); f.textContent = ''; }
  }
  var WORDS = { met: 'met', part: 'partly met', gap: 'not met' };
  function focusOn(kind, key) {
    if (focusKey === kind + key) { clearFocus(); return; }
    clearFocus();
    focusKey = kind + key;
    var main = $('.rl-main'); if (!main) return;
    main.classList.add('is-focus');
    var hits = [];
    $$('.rl-fl, .rl-wseg').forEach(function (n) {
      var hit = kind === 'f' ? n.getAttribute('data-c') === key : n.getAttribute('data-k') === key;
      if (hit) { n.classList.add('is-hit'); if (n.classList.contains('rl-fl')) hits.push(n); }
    });
    $$('.rl-evi').forEach(function (n) {
      var k = n.getAttribute('data-k');
      var row = $('.rl-fl[data-k="' + k + '"]');
      var hit = kind === 'f' ? !!(row && row.getAttribute('data-c') === key) : k === key;
      if (hit) n.classList.add('is-hit');
    });
    if (kind === 'f') $$('.rl-wchip[data-f="' + key + '"]').forEach(function (n) { n.classList.add('is-on'); });
    var f = $('#rl-wfocus');
    if (f) {
      f.textContent = '';
      hits.forEach(function (n) {
        var line = doc.createElement('div');
        var c = n.getAttribute('data-c');
        line.textContent = n.querySelector('span:last-child').firstChild.textContent.trim() + ' (' + WORDS[c] + ')';
        f.appendChild(line);
      });
      f.classList.toggle('is-on', hits.length > 0);
    }
  }
  doc.addEventListener('click', function (e) {
    var chip = e.target.closest && e.target.closest('.rl-wchip');
    if (chip) { e.stopPropagation(); focusOn('f', chip.getAttribute('data-f')); return; }
    var seg = e.target.closest && e.target.closest('.rl-wseg');
    if (seg) { e.stopPropagation(); focusOn('k', seg.getAttribute('data-k')); return; }
    if (focusKey && !(e.target.closest && e.target.closest('.rl-wfocus'))) clearFocus();
  });

  /* --------------------------------------------------------------------- Add a story sheet */
  doc.addEventListener('click', function (e) {
    var st = e.target.closest && e.target.closest('[data-rl-story]');
    if (st) {
      e.preventDefault();
      var req = st.getAttribute('data-rl-story') || '';
      var chip = $('#rl-story-for'), hid = $('#rl-story-fortext');
      if (chip) chip.textContent = req || 'Something else they ask for';
      if (hid) hid.value = req;
      $$('#rl-story-form textarea').forEach(function (t) { t.value = ''; });
      openModal($('#rl-story'));
      return;
    }
    var save = e.target.closest && e.target.closest('[data-rl-story-save]');
    if (save) {
      var form = $('#rl-story-form');
      if (!form || !root.htmx) return;
      var data = new FormData(form), vals = {};
      data.forEach(function (v, k) { vals[k] = v; });
      vals.rescore = save.getAttribute('data-rl-story-save');
      root.htmx.ajax('POST', '/opportunities/' + oppId() + '/role/story', { values: vals, swap: 'none', source: form });
    }
  });
  doc.addEventListener('rl-story-saved', function () {
    if (root.KUI) root.KUI.closeModal();
    $$('#rl-story-form textarea').forEach(function (t) { t.value = ''; });
  });

  /* ------------------------------------------------------------------------- Your view */
  function syncAdj() {
    var form = $('.rl-adjin'); if (!form) return;
    var picked = $('#rl-pick button.is-on');
    var save = $('#rl-adjsave'); if (save) save.disabled = !picked;
    var score = $('#rl-score'); if (score) score.value = picked ? picked.getAttribute('data-n') : '';
  }
  doc.addEventListener('click', function (e) {
    var t = e.target;
    var ad = t.closest && t.closest('[data-rl-adjust]');
    if (ad) {
      var panel = $('#rl-adj'); if (!panel) return;
      var open = !panel.classList.contains('is-on');
      panel.classList.toggle('is-on', open);
      ad.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }
    if (t.closest && t.closest('[data-rl-adjcancel]')) {
      var p2 = $('#rl-adj'); if (p2) p2.classList.remove('is-on');
      $$('[data-rl-adjust]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
      return;
    }
    var pick = t.closest && t.closest('#rl-pick button');
    if (pick) {
      $$('#rl-pick button').forEach(function (b) {
        var on = b === pick; b.classList.toggle('is-on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      syncAdj();
      return;
    }
    var nudge = t.closest && t.closest('#rl-nud [data-n]');
    if (nudge) {
      var row = nudge.closest('.rl-nr'); if (!row) return;
      var v = parseFloat(row.getAttribute('data-v')) + 0.5 * parseInt(nudge.getAttribute('data-n'), 10);
      v = Math.max(1, Math.min(5, v));
      row.setAttribute('data-v', String(v));
      row.querySelector('b').textContent = v.toFixed(1);
      row.querySelector('input').value = String(v);
      var bar = row.querySelector('u'); if (bar) bar.style.setProperty('--w', (v / 5 * 100) + '%');
      row.classList.add('is-changed');
    }
  });

  /* ------------------------------------------------- Not for me, the More menu, Why?, Copy link */
  doc.addEventListener('click', function (e) {
    var t = e.target;
    var nfm = t.closest && t.closest('[data-rl-nfm]');
    if (nfm) {
      var p = $('#rl-nfr'); if (!p) return;
      var open = !p.classList.contains('is-on');
      p.classList.toggle('is-on', open);
      nfm.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }
    if (t.closest && t.closest('[data-rl-nfm-close]')) {
      var p2 = $('#rl-nfr'); if (p2) p2.classList.remove('is-on');
      var b = $('[data-rl-nfm]'); if (b) b.setAttribute('aria-expanded', 'false');
      return;
    }
    var more = t.closest && t.closest('[data-rl-more] .rl-morebtn');
    var menus = $$('.rl-menu');
    if (more) {
      var menu = $('.rl-menu', more.parentNode);
      var show = menu.hidden;
      menus.forEach(function (m) { m.hidden = true; });
      menu.hidden = !show;
      more.setAttribute('aria-expanded', show ? 'true' : 'false');
      return;
    }
    if (!(t.closest && t.closest('.rl-menu')) || (t.closest && t.closest('.rl-menu [role="menuitem"]'))) {
      menus.forEach(function (m) { m.hidden = true; });
      $$('.rl-morebtn').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    }
    var why = t.closest && t.closest('[data-rl-open]');
    if (why) { e.preventDefault(); openModal($('#' + why.getAttribute('data-rl-open'))); return; }
    var href = t.closest && t.closest('[data-rl-href]');
    if (href) { root.location.href = href.getAttribute('data-rl-href'); return; }
    if (t.closest && t.closest('[data-rl-copy]')) {
      var url = root.location.origin + '/opportunities/' + oppId();
      var done = function () { toast('Link copied.'); };
      if (root.navigator.clipboard && root.navigator.clipboard.writeText) {
        root.navigator.clipboard.writeText(url).then(done, function () { toast(url); });
      } else { toast(url); }
    }
  });
  doc.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      $$('.rl-menu').forEach(function (m) { m.hidden = true; });
      if (focusKey) clearFocus();
    }
  });

  /* ------------------------------------------------------ dialogs the actions load, previews */
  function fitPreview(modal) {
    var stage = $('[data-rl-stage]', modal), page = $('[data-rl-page]', modal), frame = $('.rl-pvframe', modal);
    if (!stage || !page || !frame) return;
    var s = Math.min(1, Math.max(0.3, (stage.clientWidth - 48) / 816));
    page.style.setProperty('--s', String(s));
    var h = 1056;
    try { h = Math.max(1056, frame.contentDocument.documentElement.scrollHeight); } catch (err) { /* sandbox */ }
    frame.style.height = h + 'px';
    page.style.height = (h * s) + 'px';
  }
  function openLoaded() {
    var host = $('#rl-dialog'); if (!host) return;
    var m = $('.k-modal', host);
    if (m && !m.classList.contains('is-open')) {
      openModal(m);
      if (m.classList.contains('rl-preview')) {
        var frame = $('.rl-pvframe', m);
        if (frame) frame.addEventListener('load', function () { fitPreview(m); });
        fitPreview(m);
      }
    }
  }
  if (root.MutationObserver) {
    var watchDialog = function () {
      var host = $('#rl-dialog'); if (!host) return;
      new root.MutationObserver(function () { setTimeout(openLoaded, 0); }).observe(host, { childList: true });
    };
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', watchDialog); else watchDialog();
  }
  root.addEventListener('resize', function () {
    var m = $('#rl-dialog .rl-preview.is-open'); if (m) fitPreview(m);
  });
  doc.addEventListener('k-closed', function (e) {
    var el = e.target;
    if (el && el.closest && el.closest('#rl-dialog')) { setTimeout(function () { var h = $('#rl-dialog'); if (h && el.parentNode === h) h.innerHTML = ''; }, 350); }
  });

  /* --------------------------------------------------- a reload after an action, and after Undo */
  function reload(items) {
    if (!root.htmx) return;
    (items || []).forEach(function (it) {
      if (it && it.url && it.target) root.htmx.ajax('GET', it.url, { target: it.target, swap: it.swap || 'outerHTML' });
    });
  }
  doc.addEventListener('kestrel:undone', function (e) {
    var d = e.detail || {};
    reload(d.refresh);
  });
  doc.addEventListener('rl-refresh', function () {
    var b = body(); if (!b || !root.htmx) return;
    root.htmx.ajax('GET', '/opportunities/' + oppId() + '/tab/' + (b.getAttribute('data-tab') || 'evaluation'),
      { target: '#rl-pane', swap: 'innerHTML' });
  });
})(typeof window !== 'undefined' ? window : globalThis);
