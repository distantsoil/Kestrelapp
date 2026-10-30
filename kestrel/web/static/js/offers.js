/* Offers (D-UI-122): the little behavior HTMX does not give the page. The card menu, opening the
   Record an offer sheet the server returns, the weight words while a slider moves, and the draft
   panel: unlock on a walk-away, pick a situation, copy and edit. Everything is delegated on the
   document, so it keeps working after the body is swapped. Nothing here sends anything: Copy
   draft only puts the text on the clipboard. The page runs on the lifecycle (KUI.page, D-UI-210):
   this file only defines it, and mount starts every listener and timer through its context, so
   leaving stops all of them and coming back starts one set. User-facing strings here are the six
   weight words, the draft panel's toasts and the Copy label. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var cx = null;
  var justSaved = false, known = null;  // a save, and the cards seen before it
  var ready = null;                     // the draft panel's first content
  var inkFrom = null;                   // where the tab ink was before a tab change

  var WORDS = ['Ignore', 'A little', 'Some', 'Matters', 'A lot', 'Most'];

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function say(msg, tone) { if (root.KUI) root.KUI.toast(msg, tone ? { tone: tone } : undefined); }
  function form() { return $('#of-neg-form'); }
  function submit() {
    var f = form();
    if (!f) return;
    if (f.requestSubmit) f.requestSubmit(); else if (root.htmx) root.htmx.trigger(f, 'submit');
  }
  function drafted() { return !!$('#of-dbody'); }

  /* ----------------------------------------------------------------------------- card menu */
  function closeMenus() {
    $$('.of-menu').forEach(function (m) { m.hidden = true; });
    $$('[data-of-menu]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
  }
  function onMenuClick(e) {
    var btn = e.target.closest && e.target.closest('[data-of-menu]');
    if (btn) {
      var menu = doc.getElementById(btn.getAttribute('data-of-menu'));
      var open = menu && menu.hidden;
      closeMenus();
      if (menu && open) {
        menu.hidden = false;
        btn.setAttribute('aria-expanded', 'true');
        var first = $('button', menu);
        if (first) first.focus();
      }
      return;
    }
    if (!(e.target.closest && e.target.closest('.of-menu')) || (e.target.closest('.of-menu button'))) {
      closeMenus();
    }
  }
  function onEscape(e) { if (e.key === 'Escape') closeMenus(); }

  /* ------------------------------------------------------------------------- the sheet */
  function onSheetSwap(e) {
    var t = e.target;
    if (t && t.id === 'of-dialog') {
      var sheet = $('#of-sheet', t);
      if (sheet && root.KUI) {
        root.KUI.openModal(sheet);
        var first = $('select, input', sheet);
        if (first) first.focus();
      }
    }
  }
  // The server answers a saved sheet with this event: close it; the body was swapped already.
  function onSaved() { justSaved = true; if (root.KUI) root.KUI.closeModal(); }
  // A newly recorded offer's card arrives with a soft pulse (mockup .oc.arrive). The ids seen
  // before the save are compared with those after the swap; only a save triggers it, never a
  // tab change.
  function cardIds() {
    return Array.prototype.slice.call(doc.querySelectorAll('.of-card[data-offer]'));
  }
  function snapshot() { known = cardIds().map(function (c) { return c.getAttribute('data-offer'); }); }
  function onSettle() {
    if (justSaved && known) {
      cardIds().forEach(function (c) {
        if (known.indexOf(c.getAttribute('data-offer')) < 0) {
          c.classList.add('arrive');
          c.addEventListener('animationend', function () { c.classList.remove('arrive'); }, { once: true });
        }
      });
    }
    justSaved = false;
    snapshot();
  }
  // Undo of an outcome, or any change the page cannot patch: ask for the body again.
  function reload() {
    if (!root.htmx || !$('#of-body')) return;
    var pane = $('#of-pane');
    var tab = pane ? pane.getAttribute('data-tab') : 'compare';
    var f = form();
    var pursuit = f ? f.getAttribute('data-pursuit') : '';
    var url = '/offers?tab=' + encodeURIComponent(tab) + (pursuit && tab === 'negotiate' ? '&pursuit=' + pursuit : '');
    root.htmx.ajax('GET', url, { target: '#of-body', swap: 'innerHTML' });
  }

  /* ---------------------------------------------------- amounts: shown with separators, sent plain */
  // The visible box may read "140,000"; the hidden box named in data-plain carries what is
  // submitted: digits only when the text is a plain number, otherwise exactly what was typed.
  function plain(v) {
    var t = String(v || '').trim();
    return /^\$?\s*\d[\d,\s]*(\.\d+)?$/.test(t) ? t.replace(/[$,\s]/g, '') : t;
  }
  function syncPlain(box) {
    var id = box.getAttribute && box.getAttribute('data-plain');
    var hid = id && doc.getElementById(id);
    if (hid) hid.value = plain(box.value);
  }
  function onPlainInput(e) {
    if (e.target && e.target.getAttribute && e.target.getAttribute('data-plain')) syncPlain(e.target);
  }
  function onPlainChange(e) {
    var t = e.target;
    if (!t || !t.getAttribute || !t.getAttribute('data-plain')) return;
    syncPlain(t);
    var p = plain(t.value);
    if (/^\d+(\.\d+)?$/.test(p)) {
      var parts = p.split('.');
      t.value = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (parts[1] ? '.' + parts[1] : '');
    }
  }

  /* ------------------------------------------------------------------------ weight words */
  function onWeightInput(e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('of-w')) {
      var em = t.parentNode.querySelector('em');
      if (em) em.textContent = WORDS[parseInt(t.value, 10)] || '';
    } else if (t && t.id === 'of-walk') {
      lock(!t.value.trim());
    }
  }

  /* ------------------------------------------------------------------ the draft panel */
  // ready (above) is the panel's first content, so a cleared walk-away puts it back.
  function lock(on) {
    var w = $('#of-draft-wrap');
    if (!w) return;
    var was = w.getAttribute('data-locked') === '1';
    w.setAttribute('data-locked', on ? '1' : '0');
    // Typing the walk-away unlocks the panel with a small pulse (mockup .unlock).
    if (was && !on) {
      w.classList.remove('unlock'); void w.offsetWidth; w.classList.add('unlock');
      w.addEventListener('animationend', function () { w.classList.remove('unlock'); }, { once: true });
    }
  }
  function amount(v) {
    var m = /^(\d+\.?\d*|\.\d+)([km]?)$/.exec(String(v || '').toLowerCase().replace(/[$,\s]/g, ''));
    if (!m) return null;
    return parseFloat(m[1]) * ({ '': 1, k: 1e3, m: 1e6 }[m[2]]);
  }
  function onDraftChange(e) {
    var t = e.target;
    if (!t) return;
    if (t.id === 'of-walk') {
      var panel = $('#of-draft');
      if (!ready && panel && !drafted()) ready = panel.innerHTML;
      if (!t.value.trim()) {
        lock(true);
        if (panel && ready !== null) panel.innerHTML = ready;
      } else {
        lock(false);
        if (drafted()) submit();  // a new floor: the draft is written again within it
      }
      t.setAttribute('data-prev', t.value);
    } else if (t.id === 'of-ask') {
      var w = amount(($('#of-walk') || {}).value), a = amount(t.value);
      if (w !== null && a !== null && a < w) {
        t.value = t.getAttribute('data-prev') || '';
        say('Your ask is below your walk-away. Kestrel keeps drafts above it.', 'warn');
        return;
      }
      t.setAttribute('data-prev', t.value);
      if (drafted()) submit();
    }
  }
  function onDraftClick(e) {
    var t = e.target;
    var sit = t.closest && t.closest('[data-sit]');
    if (sit && !sit.disabled) {
      $$('[data-sit]').forEach(function (b) {
        var on = b === sit;
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      var hidden = $('#of-situation');
      if (hidden) hidden.value = sit.getAttribute('data-sit');
      var walk = $('#of-walk');
      if (walk && !walk.value.trim()) { walk.focus(); return; }
      submit();
      return;
    }
    if (t.closest && t.closest('[data-of-again]')) { submit(); return; }
    var edit = t.closest && t.closest('[data-of-edit]');
    if (edit) {
      var body = $('#of-dbody');
      if (!body) return;
      body.contentEditable = 'true';
      body.classList.add('is-editing');
      body.focus();
      say('Edit the draft right here. Your changes stay on this computer.');
      return;
    }
    var copy = t.closest && t.closest('[data-of-copy]');
    if (copy) {
      var text = ($('#of-dbody') || {}).innerText || '';
      var mine = cx;
      var done = function () {
        if (!mine || mine.signal.aborted) return;  // the page was left before the clipboard answered
        say('Copied. Paste it into your own email. Kestrel never sends it.');
        var was = copy.textContent;
        copy.textContent = 'Copied';
        mine.after(1600, function () { copy.textContent = was; });
      };
      if (root.navigator && root.navigator.clipboard) {
        root.navigator.clipboard.writeText(text).then(done, function () {
          say('Kestrel could not reach the clipboard. Select the draft and copy it by hand.', 'warn');
        });
      } else {
        say('Kestrel could not reach the clipboard. Select the draft and copy it by hand.', 'warn');
      }
    }
  }

  // A new draft arrived: the panel is no longer in editing mode.
  function onDraftSwap(e) {
    if (e.target && e.target.id === 'of-draft') {
      var body = $('#of-dbody');
      if (body) { body.contentEditable = 'false'; body.classList.remove('is-editing'); }
    }
  }
  /* ----------------------------------------------------------------------- tab ink and rise */
  function placeInk(from) {
    var tabs = $('.of-tabs');
    if (tabs && root.KUI) root.KUI.ink(tabs, from);
  }
  function onTabRequest(e) {
    var elt = e.detail && e.detail.elt;
    if (elt && elt.classList && elt.classList.contains('of-tab') && root.KUI) {
      inkFrom = root.KUI.inkPos($('.of-tabs'));
    }
  }
  function onInkSwap(e) {
    var elt = e.detail && e.detail.requestConfig && e.detail.requestConfig.elt;
    if (!elt || !elt.classList || !elt.classList.contains('of-tab')) { placeInk(null); return; }
    placeInk(inkFrom);
    inkFrom = null;
    if (root.KUI) root.KUI.paneIn($('#of-pane'));
  }
  function onResize() { placeInk(null); }

  /* ------------------------------------------------------------------------------ the page */
  root.KUI.page.define('offers', {
    mount: function (main, ctx) {
      cx = ctx;
      justSaved = false; known = null; ready = null; inkFrom = null;
      // The order the file added them in: a handler that stops the others must stay where it was.
      ctx.on(doc, 'click', onMenuClick);
      ctx.on(doc, 'keydown', onEscape);
      ctx.on(doc, 'htmx:afterSwap', onSheetSwap);
      ctx.on(doc, 'of-saved', onSaved);
      ctx.on(doc, 'htmx:afterSettle', onSettle);
      snapshot();
      ctx.on(doc, 'of-reload', reload);
      ctx.on(doc, 'kestrel:undone', reload);
      ctx.on(doc, 'input', onPlainInput);
      ctx.on(doc, 'change', onPlainChange, true);
      ctx.on(doc, 'input', onWeightInput);
      ctx.on(doc, 'change', onDraftChange);
      ctx.on(doc, 'click', onDraftClick);
      ctx.on(doc, 'htmx:afterSwap', onDraftSwap);
      ctx.on(doc, 'htmx:beforeRequest', onTabRequest);
      ctx.on(doc, 'htmx:afterSwap', onInkSwap);
      placeInk(null);
      ctx.on(root, 'resize', onResize);
      ctx.onUnmount(function () { if (cx === ctx) cx = null; });
    }
  });
})(window);
