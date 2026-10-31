/* Models (D-UI-105): the little behavior HTMX does not give the page. Column sort for the
   comparison table: each sortable cell carries its raw value in data-v, so "too few to judge"
   sorts below every real rate rather than being parsed as one. Delegated on the document, so it
   keeps working after the details block is opened or swapped. The page runs on the lifecycle
   (KUI.page, D-UI-210): this file only defines it, and mount starts every listener through its
   context, so leaving stops all of them and coming back starts one set. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  function onSortClick(e) {
    var th = e.target.closest && e.target.closest('#model-compare th[data-sort]');
    if (!th) return;
    var table = th.closest('table');
    var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
    var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
    Array.prototype.forEach.call(table.querySelectorAll('th'), function (h) { delete h.dataset.dir; });
    th.dataset.dir = dir;
    var body = table.tBodies[0];
    var rows = Array.prototype.filter.call(body.rows, function (r) { return r.cells.length > 1; });
    function key(r) {
      var c = r.cells[idx];
      var v = c.dataset.v !== undefined ? c.dataset.v : c.textContent.trim();
      return th.dataset.sort === 'num' ? parseFloat(v) : v.toLowerCase();
    }
    rows.sort(function (a, b) {
      var x = key(a), y = key(b);
      var c = x < y ? -1 : x > y ? 1 : 0;
      return dir === 'asc' ? c : -c;
    });
    rows.forEach(function (r) { body.appendChild(r); });
  }

  /* The row's more menu (models-mockup-locked.html .mmenu). Fixed to the viewport so the table's
     scroll box never clips it; closes on an outside click, Escape, scroll or a choice. */
  function closeMenus() {
    Array.prototype.forEach.call(doc.querySelectorAll('.mo-menu:not([hidden])'), function (m) {
      m.hidden = true;
      var b = m.parentNode.querySelector('[data-mo-more]');
      if (b) b.setAttribute('aria-expanded', 'false');
    });
  }
  function onMenuClick(e) {
    var btn = e.target.closest && e.target.closest('[data-mo-more]');
    var wasOpen = btn && !btn.parentNode.querySelector('.mo-menu').hidden;
    closeMenus();
    if (!btn || wasOpen) return;
    var menu = btn.parentNode.querySelector('.mo-menu');
    var r = btn.getBoundingClientRect();
    menu.hidden = false;
    menu.style.top = (r.bottom + 4) + 'px';
    menu.style.right = Math.max(8, root.innerWidth - r.right) + 'px';
    btn.setAttribute('aria-expanded', 'true');
  }
  function onKeyDown(e) { if (e.key === 'Escape') closeMenus(); }

  root.KUI.page.define('models', {
    mount: function (main, ctx) {
      ctx.on(doc, 'click', onSortClick);
      ctx.on(doc, 'click', onMenuClick);
      ctx.on(doc, 'keydown', onKeyDown);
      ctx.on(root, 'scroll', closeMenus, true);
      ctx.onUnmount(closeMenus);  // an open row menu is fixed to the viewport: it goes with the page
    }
  });
})(window);
