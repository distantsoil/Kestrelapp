/* Models (D-UI-105): the little behavior HTMX does not give the page. Column sort for the
   comparison table: each sortable cell carries its raw value in data-v, so "too few to judge"
   sorts below every real rate rather than being parsed as one. Delegated on the document, so it
   keeps working after the details block is opened or swapped. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  doc.addEventListener('click', function (e) {
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
  });
})(window);
