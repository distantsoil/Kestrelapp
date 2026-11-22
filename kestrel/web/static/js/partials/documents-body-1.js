/* Moved out of templates/partials/documents_body.html for the Content-Security-Policy (D-INST-8). Loaded where the
   inline script was, so it runs at the same point; inside an htmx swap it runs as the swap lands. */
// Import status (D-IMPORT-2). Step one reports Uploading with a percentage, then Reading once
// the bytes are sent; step two (partials/import_processing.html) counts the seconds it has run.
// The listeners sit on this section, not on the document (D-UI-210): a request from inside it
// bubbles up through it, and a swap that brings a new section leaves nothing of the old one
// listening. Running this again for the same section replaces its set. The seconds counter
// stops by itself once its line has left the page.
(function () {
  var form = document.getElementById('import-form');
  var host = form.closest('#pf-import') || form;
  var stage = form.querySelector('.import-stage');
  if (host.kImport) host.kImport.abort();
  var ac = host.kImport = new AbortController();
  var sig = { signal: ac.signal };
  // The chosen file's name shows bold and larger, so the choice is plain to see (owner).
  var picker = form.querySelector('#import-file');
  if (picker) picker.addEventListener('change', function () {
    picker.classList.toggle('is-chosen', !!(picker.files && picker.files.length));
  }, sig);
  form.addEventListener('htmx:beforeRequest', function () {
    var f = form.querySelector('input[type=file]');
    stage.textContent = (f && f.files && f.files.length) ? 'Uploading…' : 'Reading…';
  }, sig);
  form.addEventListener('htmx:xhr:progress', function (evt) {
    var d = evt.detail;
    if (!d || !d.lengthComputable || !d.total) return;
    stage.textContent = d.loaded < d.total
      ? 'Uploading… ' + Math.floor(100 * d.loaded / d.total) + '%'
      : 'Reading the file and extracting its text…';
  }, sig);
  var timers = new WeakMap();
  host.addEventListener('htmx:beforeRequest', function (evt) {
    var el = evt.detail && evt.detail.elt;
    if (!el || !el.classList || !el.classList.contains('import-processing')) return;
    // One import at a time: a second click would orphan a billed call still running.
    var btn = form.querySelector('button[type=submit]');
    if (btn) btn.disabled = true;
    var out = el.querySelector('.import-elapsed'), t0 = Date.now();
    var tick = setInterval(function () {
      if (!el.isConnected) { clearInterval(tick); timers.delete(el); return; }
      if (out) out.textContent = Math.round((Date.now() - t0) / 1000) + ' s elapsed.';
    }, 1000);
    timers.set(el, tick);
  }, sig);
  host.addEventListener('htmx:afterRequest', function (evt) {
    var el = evt.detail && evt.detail.elt;
    if (el && timers.has(el)) {
      clearInterval(timers.get(el)); timers.delete(el);
      var btn = form.querySelector('button[type=submit]');
      if (btn) btn.disabled = false;
    }
  }, sig);
})();
