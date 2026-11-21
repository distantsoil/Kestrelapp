/* Moved out of templates/partials/documents_body.html for the Content-Security-Policy (D-INST-8). Loaded where the
   inline script was, so it runs at the same point; inside an htmx swap it runs as the swap lands. */
// The test render uses the layout, font and alignment chosen here, so the review
// matches the preview. Each control holds one set of listeners (D-UI-210): if this runs
// again for the same control, the earlier set ends first, so a swap never doubles them.
(function () {
  var a = document.getElementById('test-render');
  var ids = ['layout', 'font', 'header_align', 'accent', 'accent_custom'];
  if (!a) return;
  var base = a.getAttribute('href');
  function sync() {
    var href = base;
    ids.forEach(function (id) {
      var el = document.getElementById(id);
      if (el && el.value) href += '&' + id + '=' + encodeURIComponent(el.value);
    });
    a.setAttribute('href', href);
  }
  ids.forEach(function (id) {
    var el = document.getElementById(id);
    if (!el) return;
    if (el.kSync) el.kSync.abort();
    var ac = el.kSync = new AbortController();
    el.addEventListener('change', sync, { signal: ac.signal });
    el.addEventListener('input', sync, { signal: ac.signal });
  });
  sync();
})();
