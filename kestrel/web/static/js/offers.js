/* Offers (D-UI-122): the little behavior HTMX does not give the page. The card menu, opening the
   Record an offer sheet the server returns, the weight words while a slider moves, and the draft
   panel: unlock on a walk-away, pick a situation, copy and edit. Everything is delegated on the
   document, so it keeps working after the body is swapped. Nothing here sends anything: Copy
   draft only puts the text on the clipboard. User-facing strings here are the six weight words,
   the draft panel's toasts and the Copy label. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

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
  doc.addEventListener('click', function (e) {
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
  });
  doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenus(); });

  /* ------------------------------------------------------------------------- the sheet */
  doc.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (t && t.id === 'of-dialog') {
      var sheet = $('#of-sheet', t);
      if (sheet && root.KUI) {
        root.KUI.openModal(sheet);
        var first = $('select, input', sheet);
        if (first) first.focus();
      }
    }
  });
  // The server answers a saved sheet with this event: close it; the body was swapped already.
  doc.addEventListener('of-saved', function () { if (root.KUI) root.KUI.closeModal(); });
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
  doc.addEventListener('of-reload', reload);
  doc.addEventListener('kestrel:undone', reload);

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
  doc.addEventListener('input', function (e) {
    if (e.target && e.target.getAttribute && e.target.getAttribute('data-plain')) syncPlain(e.target);
  });
  doc.addEventListener('change', function (e) {
    var t = e.target;
    if (!t || !t.getAttribute || !t.getAttribute('data-plain')) return;
    syncPlain(t);
    var p = plain(t.value);
    if (/^\d+(\.\d+)?$/.test(p)) {
      var parts = p.split('.');
      t.value = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (parts[1] ? '.' + parts[1] : '');
    }
  }, true);

  /* ------------------------------------------------------------------------ weight words */
  doc.addEventListener('input', function (e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('of-w')) {
      var em = t.parentNode.querySelector('em');
      if (em) em.textContent = WORDS[parseInt(t.value, 10)] || '';
    } else if (t && t.id === 'of-walk') {
      lock(!t.value.trim());
    }
  });

  /* ------------------------------------------------------------------ the draft panel */
  var ready = null;  // the panel's first content, so a cleared walk-away puts it back
  function lock(on) {
    var w = $('#of-draft-wrap');
    if (w) w.setAttribute('data-locked', on ? '1' : '0');
  }
  function amount(v) {
    var m = /^(\d+\.?\d*|\.\d+)([km]?)$/.exec(String(v || '').toLowerCase().replace(/[$,\s]/g, ''));
    if (!m) return null;
    return parseFloat(m[1]) * ({ '': 1, k: 1e3, m: 1e6 }[m[2]]);
  }
  doc.addEventListener('change', function (e) {
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
  });
  doc.addEventListener('click', function (e) {
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
      var done = function () {
        say('Copied. Paste it into your own email. Kestrel never sends it.');
        var was = copy.textContent;
        copy.textContent = 'Copied';
        setTimeout(function () { copy.textContent = was; }, 1600);
      };
      if (root.navigator && root.navigator.clipboard) {
        root.navigator.clipboard.writeText(text).then(done, function () {
          say('Kestrel could not reach the clipboard. Select the draft and copy it by hand.', 'warn');
        });
      } else {
        say('Kestrel could not reach the clipboard. Select the draft and copy it by hand.', 'warn');
      }
    }
  });

  // A new draft arrived: the panel is no longer in editing mode.
  doc.addEventListener('htmx:afterSwap', function (e) {
    if (e.target && e.target.id === 'of-draft') {
      var body = $('#of-dbody');
      if (body) { body.contentEditable = 'false'; body.classList.remove('is-editing'); }
    }
  });
})(window);
