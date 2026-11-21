/* The first-run notice's behavior (D-UI-249). A static file, not an inline script, so a strict
   content policy can run it (D-STORY-702). Loaded after first-run-art.js by
   partials/first_run_break.html; does nothing when the dialog is not on the page. */
(function () {
  var d = document.getElementById('td-break');
  if (!d) return;
  var art = window.KestrelFirstRunArt ? window.KestrelFirstRunArt.mount(d.querySelector('.sf-break__art')) : null;
  d.__art = art;
  var btn = d.querySelector('[data-testid="first-run-break-dismiss"]');
  var sent = false;
  function still() {
    return document.documentElement.getAttribute('data-reduce-motion') === '1' ||
      (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function remember() {
    if (sent) return;
    sent = true;
    fetch('/onboarding/first-run-break/dismiss', { method: 'POST', headers: { 'HX-Request': 'true' } })
      .catch(function () { sent = false; });  // still pending: it shows again next time
  }
  function finish() { d.classList.remove('is-closing'); if (d.open) d.close(); }
  function shut() {
    remember();
    if (!d.open || d.classList.contains('is-closing')) return;
    if (still()) { finish(); return; }
    d.classList.add('is-closing');
    var t = setTimeout(finish, 400);
    d.addEventListener('animationend', function done(e) {
      if (e.target !== d) return;
      d.removeEventListener('animationend', done);
      clearTimeout(t);
      finish();
    });
  }
  btn.addEventListener('click', shut);
  // Escape takes the same path. Where the browser will not let the cancel be stopped (no user
  // activation yet), the dialog closes at once; the close handler still remembers it.
  d.addEventListener('cancel', function (e) { e.preventDefault(); shut(); });
  d.addEventListener('close', function () { d.classList.remove('is-closing'); remember(); if (art) art.stop(); });
  if (typeof d.showModal === 'function') d.showModal(); else d.setAttribute('open', '');
})();
