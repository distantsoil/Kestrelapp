/* About (D-UI-125): the small amount of behavior the page needs. Copy for the paths and the
   versions, the third-party notices loaded on first open (as text, never as markup), and the
   logo easter egg. Everything is delegated on the document, so it survives HTMX swaps. The
   version text comes from the page, never from this file. The page runs on the lifecycle
   (KUI.page, D-UI-210): this file only defines it, and mount starts every listener and timer
   through its context, so leaving stops all of them and coming back starts one set. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var cx = null;
  var taps = 0;
  var tapTimer = 0;
  var notices = null;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }

  function copy(text, button, label) {
    var c = cx;  // a late clipboard answer after the page is gone starts nothing (an ended context)
    var done = function () {
      if (!button || !c) return;
      button.textContent = 'Copied';
      c.after(1400, function () { button.textContent = label; });
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
     around it) and scroll to it. Runs at mount and when the hash changes on the page itself. */
  function openHash() {
    var hash = root.location && root.location.hash.slice(1);
    var target = hash && doc.getElementById(hash);
    if (!target) return;
    for (var n = target; n; n = n.parentElement) if (n.tagName === 'DETAILS') n.open = true;
    // The diagnostics save lives in the Something wrong? flyout: open that, not scroll to it.
    var fly = target.closest && target.closest('.k-flyout');
    if (fly) { root.KUI.openFlyout(fly, { trigger: null }); return; }
    target.scrollIntoView();
  }

  function onClick(e) {
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
      root.KUI.toast('Copied. No personal data is included.');
      return;
    }
    if (t.closest('#ab-mark')) {
      var mark = $('#ab-mark');
      mark.classList.remove('is-tap');
      void mark.offsetWidth;
      mark.classList.add('is-tap');
      cx.clear(tapTimer);
      taps += 1;
      tapTimer = cx.after(1500, function () { taps = 0; });
      if (taps >= 5) {
        taps = 0;
        var bird = $('#ab-glider');
        if (bird) { bird.classList.remove('is-go'); void bird.offsetWidth; bird.classList.add('is-go'); }
        root.KUI.toast('A kestrel can hover in place while it hunts. So can you.');
      }
    }
  }

  /* The licenses are long: fetch them the first time the toggle opens, and show them as text.
     data-loaded marks a box that has its text; leaving mid-fetch lifts it, so the next visit asks
     again. */
  function onNoticesToggle() {
    if (!notices || !notices.open || notices.getAttribute('data-loaded')) return;
    var node = notices;
    var signal = cx.signal;
    node.setAttribute('data-loaded', '1');
    var box = $('#ab-notes-text');
    root.fetch('/about/notices', { signal: signal }).then(function (r) {
      if (!r.ok) throw new Error('status ' + r.status);
      return r.text();
    }).then(function (text) {
      if (signal.aborted) return;
      box.textContent = text;
    }).catch(function () {
      node.removeAttribute('data-loaded');
      if (signal.aborted) return;
      box.textContent = 'The notices could not be loaded. Use the link above to open them as a page.';
    });
  }

  root.KUI.page.define('about', {
    mount: function (main, ctx) {
      cx = ctx; taps = 0; tapTimer = 0;
      notices = doc.getElementById('ab-notices');
      ctx.on(root, 'hashchange', openHash);
      openHash();
      ctx.on(doc, 'click', onClick);
      if (notices) {
        ctx.on(notices, 'toggle', onNoticesToggle);
        onNoticesToggle();  // left open mid-fetch on an earlier visit: finish the load
      }
      ctx.onUnmount(function () {
        if (cx === ctx) { cx = null; notices = null; }
      });
    }
  });
})(typeof window !== 'undefined' ? window : globalThis);
