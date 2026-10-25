/* Calibrate (D-UI-99): the keyboard and the small bits of card state. The page is server
   rendered and swaps in place with HTMX; this file only (1) picks a score and enables Save,
   (2) opens "Tell Kestrel why" and counts nudges, (3) keys 1 to 5, O, S and Enter, (4) reloads
   the card after an Undo, and (5) scrolls to a role loaded for a change only if it is out of
   view, then back only if it moved. Behavior is delegated on the document, so nothing needs
   re-binding after a swap. */
(function () {
  'use strict';
  function reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function smooth() { return reduced() ? 'auto' : 'smooth'; }
  var doc = document;
  var returnTo = null;  // scroll position before a change scrolled the page, or null

  function $(sel, root) { return (root || doc).querySelector(sel); }
  function form() { return $('#cal-form'); }

  function scroller(el) {
    for (var n = el.parentElement; n; n = n.parentElement) {
      var o = getComputedStyle(n).overflowY;
      if ((o === 'auto' || o === 'scroll') && n.scrollHeight > n.clientHeight) return n;
    }
    return doc.scrollingElement || doc.documentElement;
  }

  /* Save reads "Save", or "Save with reasons" once a reason or a nudge is chosen. On a change it
     stays off until something differs from the earlier rating. */
  function syncSave() {
    var f = form();
    if (!f) return;
    var score = f.elements.score.value;
    var extra = !!f.querySelector('input[name=reason]:checked') ||
      Array.prototype.some.call(f.querySelectorAll('input[name^=nudge_]'), function (i) { return i.value !== '0'; });
    var label = $('#cal-savel');
    if (label) label.textContent = extra ? 'Save with reasons' : 'Save';
    var save = $('#cal-save');
    if (!save) return;
    var prev = f.getAttribute('data-previous');
    save.disabled = !score || (!!prev && score === prev && !extra);
  }

  function pick(n) {
    var f = form();
    if (!f) return;
    f.elements.score.value = String(n);
    f.querySelectorAll('.cal-pickbtn').forEach(function (b) {
      var on = b.getAttribute('data-n') === String(n);
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    var why = $('#cal-whybtn');
    if (why) why.hidden = false;
    syncSave();
  }

  function toggleWhy(open) {
    var panel = $('#cal-why-panel'), btn = $('#cal-whybtn');
    if (!panel || !btn || btn.hidden) return;
    var next = typeof open === 'boolean' ? open : panel.hidden;
    panel.hidden = !next;
    btn.setAttribute('aria-expanded', next ? 'true' : 'false');
  }

  doc.addEventListener('click', function (e) {
    var t = e.target;
    var pk = t.closest && t.closest('.cal-pickbtn');
    if (pk) { pick(+pk.getAttribute('data-n')); return; }
    if (t.closest && t.closest('#cal-whybtn')) { toggleWhy(); return; }
    var nd = t.closest && t.closest('.cal-nud button');
    if (nd) {
      var row = nd.closest('.cal-nud');
      var input = row.querySelector('input'), out = row.querySelector('b');
      var base = +row.getAttribute('data-base');
      var step = Math.max(-4, Math.min(4, +input.value + +nd.getAttribute('data-nd')));
      var shown = base + step;
      if (shown < 1 || shown > 5) return;
      input.value = String(step);
      out.textContent = String(shown);
      out.classList.toggle('is-changed', step !== 0);
      syncSave();
      return;
    }
    var sc = t.closest && t.closest('[data-cal-scroll]');
    if (sc) {
      var target = $(sc.getAttribute('data-cal-scroll'));
      if (target) target.scrollIntoView({ block: 'nearest', behavior: smooth() });
    }
  });

  doc.addEventListener('change', function (e) {
    if (e.target.matches && e.target.matches('input[name=reason]')) syncSave();
  });

  doc.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey || !form()) return;
    var t = e.target, tag = t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    var k = e.key.toLowerCase();
    if (k.length === 1 && '12345'.indexOf(k) >= 0) {
      e.preventDefault();
      var b = form().querySelector('.cal-pickbtn[data-n="' + k + '"]');
      if (b) b.click();
    } else if (k === 'o') { e.preventDefault(); toggleWhy(); }
    else if (k === 's') { var s = $('#cal-skip'); if (s) { e.preventDefault(); s.click(); } }
    else if (k === 'enter' && tag !== 'BUTTON' && tag !== 'A') {
      var save = $('#cal-save');
      if (save && !save.disabled) { e.preventDefault(); save.click(); }
    }
  });

  /* After an Undo the toast names where to reload the card from (HX-Trigger k-toast `refresh`). */
  doc.addEventListener('kestrel:undone', function (e) {
    var url = e.detail && e.detail.refresh;
    if (url && window.htmx) window.htmx.ajax('GET', url, { target: '#cal-card', swap: 'outerHTML' });
  });

  /* A role loaded for a change: scroll only if the card is out of view, and remember where the
     page was. When the card goes back to the queue, return only if the page moved. */
  doc.addEventListener('htmx:afterSwap', function (e) {
    /* An outerHTML swap leaves e.target as the replaced node; the new card has the same id. */
    if (!(e.target && e.target.id === 'cal-card')) return;
    var card = doc.getElementById('cal-card');
    if (!card) return;
    var sc = scroller(card);
    if (card.getAttribute('data-change') === '1') {
      var before = sc.scrollTop;
      var r = card.getBoundingClientRect(), box = sc === doc.scrollingElement || sc === doc.documentElement
        ? { top: 0, bottom: window.innerHeight } : sc.getBoundingClientRect();
      if (r.top < box.top || r.bottom > box.bottom) {
        card.scrollIntoView({ block: 'nearest', behavior: smooth() });
        returnTo = before;
      }
    } else if (returnTo !== null) {
      var to = returnTo;
      returnTo = null;
      if (Math.abs(sc.scrollTop - to) > 4) sc.scrollTo({ top: to, behavior: smooth() });
    }
    syncSave();
  });

  doc.addEventListener('DOMContentLoaded', syncSave);
})();
