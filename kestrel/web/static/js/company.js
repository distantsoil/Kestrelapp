/* Company page (D-UI-123). Small and delegated: source markers flash the row they point at,
   Copy buttons copy text for the operator to send themselves (nothing is ever sent), and an Undo
   from the Hide toast refreshes the header in place. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  function reduced() {
    return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function say(text) {
    if (root.KUI && root.KUI.toast) root.KUI.toast(text);
  }

  function copy(text, done) {
    var ok = function () { say(done || 'Copied.'); };
    if (root.navigator && root.navigator.clipboard && root.navigator.clipboard.writeText) {
      root.navigator.clipboard.writeText(text).then(ok, function () { say('Could not copy. Select the text and copy it yourself.'); });
    } else {
      say('Could not copy. Select the text and copy it yourself.');
    }
  }

  doc.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;

    var sup = t.closest('.co-sup');
    if (sup) {
      var row = doc.getElementById(sup.getAttribute('data-src'));
      if (row) {
        e.preventDefault();
        row.scrollIntoView({ block: 'center', behavior: reduced() ? 'auto' : 'smooth' });
        row.classList.remove('is-flash');
        void row.offsetWidth;
        row.classList.add('is-flash');
      }
      return;
    }

    var direct = t.closest('[data-copy]');
    if (direct) { copy(direct.getAttribute('data-copy'), direct.getAttribute('data-done')); return; }

    var from = t.closest('[data-copy-from]');
    if (from) {
      var src = doc.querySelector(from.getAttribute('data-copy-from'));
      if (src) copy(src.textContent, from.getAttribute('data-done'));
    }
  });

  // Undo on the Hide toast: the server already restored the company; bring the button and the
  // banner back in step without reloading the page.
  doc.addEventListener('kestrel:undone', function () {
    var page = doc.getElementById('co-page');
    if (!page || !root.htmx) return;
    root.htmx.ajax('GET', '/orgs/' + page.getAttribute('data-org') + '/state',
                   { target: '#co-actions', swap: 'outerHTML' });
  });
})(window);
