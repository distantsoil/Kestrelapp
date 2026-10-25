/* About (D-UI-125): the small amount of behavior the page needs. Copy for the paths and the
   versions, the third-party notices loaded on first open (as text, never as markup), and the
   logo easter egg. Everything is delegated on the document, so it survives HTMX swaps. The
   version text comes from the page, never from this file. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }

  function copy(text, button, label) {
    var done = function () {
      if (!button) return;
      button.textContent = 'Copied';
      root.setTimeout(function () { button.textContent = label; }, 1400);
    };
    try {
      var p = root.navigator.clipboard.writeText(text);
      if (p && p.then) p.then(done, function () { /* clipboard refused: the button stays as it was */ });
      else done();
    } catch (e) { /* no clipboard in this window */ }
  }

  /* "Kestrel X.Y.Z" then one "label value" line per component row, exactly what the page shows. */
  function versionsText() {
    var tag = $('#about-version');
    var head = tag ? tag.textContent.replace(/^Version\s+/, '') : '';
    var lines = ['Kestrel ' + head];
    $$('#ab-comp tr').forEach(function (tr) {
      var cells = tr.querySelectorAll('td');
      if (cells.length === 2 && !/Checking/.test(cells[1].textContent)) lines.push(cells[0].textContent.trim() + ' ' + cells[1].textContent.trim());
    });
    return lines.join('\n');
  }

  /* /about#diagnostics and /about#versions land on a closed toggle: open it (and any toggle
     around it) and scroll to it. Runs on load and when the hash changes on the page itself. */
  function openHash() {
    var hash = root.location && root.location.hash.slice(1);
    var target = hash && doc.getElementById(hash);
    if (!target) return;
    for (var n = target; n; n = n.parentElement) if (n.tagName === 'DETAILS') n.open = true;
    target.scrollIntoView();
  }
  root.addEventListener('hashchange', openHash);
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', openHash); else openHash();

  var taps = 0;
  var tapTimer = 0;

  doc.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var path = t.closest('[data-copy-target]');
    if (path) {
      var src = doc.getElementById(path.getAttribute('data-copy-target'));
      if (src) copy(src.textContent.trim(), path, 'Copy');
      return;
    }
    var vers = t.closest('[data-copy-versions]');
    if (vers) {
      if (!vers.getAttribute('data-label')) vers.setAttribute('data-label', vers.textContent);
      copy(versionsText(), vers, vers.getAttribute('data-label'));
      if (root.KUI) root.KUI.toast('Copied. No personal data is included.');
      return;
    }
    if (t.closest('#ab-mark')) {
      var mark = $('#ab-mark');
      mark.classList.remove('is-tap');
      void mark.offsetWidth;
      mark.classList.add('is-tap');
      root.clearTimeout(tapTimer);
      taps += 1;
      tapTimer = root.setTimeout(function () { taps = 0; }, 1500);
      if (taps >= 5) {
        taps = 0;
        var bird = $('#ab-glider');
        if (bird) { bird.classList.remove('is-go'); void bird.offsetWidth; bird.classList.add('is-go'); }
        if (root.KUI) root.KUI.toast('A kestrel can hover in place while it hunts. So can you.');
      }
    }
  });

  /* The licenses are long: fetch them the first time the toggle opens, and show them as text. */
  var notices = doc.getElementById('ab-notices');
  if (notices) {
    notices.addEventListener('toggle', function () {
      if (!notices.open || notices.getAttribute('data-loaded')) return;
      notices.setAttribute('data-loaded', '1');
      var box = $('#ab-notes-text');
      root.fetch('/about/notices').then(function (r) {
        if (!r.ok) throw new Error('status ' + r.status);
        return r.text();
      }).then(function (text) {
        box.textContent = text;
      }).catch(function () {
        notices.removeAttribute('data-loaded');
        box.textContent = 'The notices could not be loaded. Use the link above to open them as a page.';
      });
    });
  }
})(typeof window !== 'undefined' ? window : globalThis);
