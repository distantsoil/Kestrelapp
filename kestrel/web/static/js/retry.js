/* Needs another try (D-UI-104): the small amount of behavior HTMX does not give the page.
   Selection and the bulk button label, the empty state, the model menu, opening the dialogs the
   actions load, and folding a row out when it leaves the list. Everything is delegated on the
   document, so it keeps working after a row or the whole card is swapped. User-facing strings
   here are the bulk button's two labels and the selection count. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function liveRows() { return $$('#rt-list .rt-row').filter(function (r) { return !r.hidden; }); }
  function stepNow() { var s = $('#rt-step'); return s ? s.value : 'all'; }

  /* ------------------------------------------------------------- selection and the empty state */
  function sync() {
    var rows = liveRows();
    var boxes = rows.map(function (r) { return $('.rt-cb', r); }).filter(Boolean);
    var picked = boxes.filter(function (b) { return b.checked; });
    rows.forEach(function (r) {
      var b = $('.rt-cb', r);
      r.classList.toggle('is-sel', !!(b && b.checked));
    });
    var all = $('#rt-all');
    if (all) {
      all.checked = boxes.length > 0 && picked.length === boxes.length;
      all.indeterminate = picked.length > 0 && picked.length < boxes.length;
      all.disabled = boxes.length === 0;
    }
    var label = $('#rt-all-text');
    if (label) label.textContent = picked.length ? picked.length + ' selected' : 'Select all';
    var bulk = $('#rt-bulk');
    if (bulk) {
      bulk.textContent = picked.length ? 'Try selected again (' + picked.length + ')' : 'Try all again';
      bulk.disabled = boxes.length === 0;
    }
    var empty = $('#rt-empty');
    if (empty) empty.hidden = rows.length > 0;
  }

  doc.addEventListener('change', function (e) {
    var t = e.target;
    if (t && t.id === 'rt-all') {
      liveRows().forEach(function (r) { var b = $('.rt-cb', r); if (b) b.checked = t.checked; });
      sync();
    } else if (t && t.classList && t.classList.contains('rt-cb')) {
      sync();
    }
  });

  /* ---------------------------------------------------------------------------- the model menu */
  function closeMenus() {
    $$('.rt-menu-slot').forEach(function (s) { s.innerHTML = ''; });
    $$('[data-testid="rt-model"]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
  }
  // A second click on the button closes the menu instead of asking for it again.
  doc.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-testid="rt-model"]');
    if (btn) {
      var slot = $('.rt-menu-slot', btn.parentNode);
      if (slot && slot.firstChild) {
        e.stopPropagation();
        closeMenus();
      }
      return;
    }
    if (!(e.target.closest && e.target.closest('.rt-menu'))) closeMenus();
  }, true);
  doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenus(); });

  /* ---------------------------------------------------------- what HTMX swapped in, and events */
  doc.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (!t) return;
    if (t.classList && t.classList.contains('rt-menu-slot')) {
      var menu = $('.rt-menu', t);
      var btn = $('[data-testid="rt-model"]', t.parentNode);
      if (btn) btn.setAttribute('aria-expanded', 'true');
      if (menu) {
        var r = menu.getBoundingClientRect();
        if (r.bottom > root.innerHeight - 8) menu.classList.add('is-up');
        var first = $('button', menu);
        if (first) first.focus();
      }
    } else if (t.id === 'rt-dialog') {
      var dlg = $('.k-modal', t);
      if (dlg && root.KUI) root.KUI.openModal(dlg);
    }
    sync();
  });
  doc.addEventListener('htmx:afterSettle', sync);

  // A row left the list: Remove, or a try that scored. It folds out; the empty state follows.
  doc.addEventListener('rt-gone', function (e) {
    var id = e.detail && e.detail.id;
    var row = id ? doc.getElementById(id) : null;
    if (!row) { sync(); return; }
    closeMenus();
    var done = function () { if (row.parentNode) row.parentNode.removeChild(row); sync(); };
    if (root.KUI && root.KUI.foldOut) root.KUI.foldOut(row, done); else done();
  });

  // Undo of Remove, or any change the page cannot patch row by row: ask for the card again.
  function reload() {
    if (!root.htmx || !$('#rt-card')) return;
    root.htmx.ajax('GET', '/quarantine?step=' + encodeURIComponent(stepNow()),
                   { target: '#rt-card', swap: 'outerHTML' });
  }
  doc.addEventListener('rt-reload', reload);
  doc.addEventListener('kestrel:undone', reload);

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', sync); else sync();
})(window);
