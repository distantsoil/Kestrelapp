/* Interview prep (D-UI-121): the little behavior HTMX does not give the page. Menus, a story
   opening to its words, the mode picker, the answer box (word count, hint, Next question), opening
   the sheets the server returns, the paper preview scaled to fit, the PDF download, and reading an
   Undo that can no longer be done. Everything is delegated on the document, so it keeps working
   after the body is swapped. Nothing here sends anything: the server decides and checks every
   send. */
(function (root) {
  'use strict';
  function reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function smooth() { return reduced() ? 'auto' : 'smooth'; }
  var doc = root.document;
  if (!doc) return;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function say(msg, tone) { if (root.KUI) root.KUI.toast(msg, tone ? { tone: tone } : undefined); }

  /* ------------------------------------------------------------------------------- menus */
  function closeMenus(except) {
    $$('.pp-menu').forEach(function (m) { if (m !== except) m.hidden = true; });
    $$('[data-pp-menu]').forEach(function (b) {
      var m = b.parentNode && $('.pp-menu', b.parentNode);
      if (m !== except) b.setAttribute('aria-expanded', 'false');
    });
  }
  doc.addEventListener('click', function (e) {
    var t = e.target;
    var btn = t.closest && t.closest('[data-pp-menu]');
    if (btn) {
      var menu = $('.pp-menu', btn.parentNode);
      var open = menu && menu.hidden;
      closeMenus(open ? menu : null);
      if (menu) {
        menu.hidden = !open;
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        var first = open && $('a, button', menu);
        if (first) first.focus();
      }
      return;
    }
    if (!(t.closest && t.closest('.pp-menu')) || (t.closest('.pp-menu a, .pp-menu button'))) closeMenus(null);

    /* A story opens to the operator's own words. */
    var chip = t.closest && t.closest('[data-pp-open]');
    if (chip) {
      var star = chip.parentNode && $('.pp-star', chip.parentNode);
      if (star) {
        star.hidden = !star.hidden;
        chip.setAttribute('aria-expanded', star.hidden ? 'false' : 'true');
      }
      return;
    }
    /* The mode picker. */
    var mode = t.closest && t.closest('[data-pp-mode]');
    if (mode && !mode.disabled) {
      $$('[data-pp-mode]').forEach(function (b) {
        var on = b === mode;
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
      });
      var field = $('#pp-mode');
      if (field) field.value = mode.getAttribute('data-pp-mode');
      return;
    }
    /* The answer box. */
    if (t.closest && t.closest('[data-pp-hint]')) {
      var hint = $('#pp-hint');
      var b = t.closest('[data-pp-hint]');
      if (hint) {
        hint.hidden = !hint.hidden;
        b.textContent = hint.hidden ? 'Show a hint' : 'Hide hint';
      }
      return;
    }
    if (t.closest && t.closest('[data-pp-next]')) {
      var acts = $('#pp-acts');
      var block = $('#pp-ask-block');
      if (acts) acts.remove();
      if (block) {
        block.hidden = false;
        var ta = $('#pp-ans');
        if (ta) { ta.focus({ preventScroll: true }); }
        block.scrollIntoView({ block: 'nearest', behavior: smooth() });
      }
    }
  });
  doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenus(null); });
  doc.addEventListener('input', function (e) {
    if (e.target && e.target.id === 'pp-ans') {
      var v = e.target.value.trim();
      var wc = $('#pp-wc');
      if (wc) {
        var n = v ? v.split(/\s+/).length : 0;
        wc.textContent = n + (n === 1 ? ' word' : ' words');
      }
    }
  });

  /* A send to an online model the server has not been told to allow yet: ask first. The server
     refuses without the confirmation too; this only saves the trip. */
  doc.addEventListener('submit', function (e) {
    var route = $('#pp-route');
    var form = e.target;
    if (!route || !route.hasAttribute('data-needs-confirm') || !form.getAttribute) return;
    var posts = form.getAttribute('hx-post') || '';
    if (posts.indexOf('/mock/') !== 0 || posts.indexOf('/skip') >= 0 || posts.indexOf('/end') >= 0) return;
    e.preventDefault();
    e.stopPropagation();
    var layout = $('#pp-layout');
    var opp = layout ? layout.getAttribute('data-opp') : '';
    if (root.htmx && opp) root.htmx.ajax('GET', '/mock/' + opp + '/online', { target: '#pp-dialog', swap: 'innerHTML' });
  }, true);

  /* ------------------------------------------------------------- sheets and the preview */
  function scalePages() {
    var sc = $('#pp-pages');
    if (!sc) return;
    var s = Math.min(0.95, (sc.clientWidth - 48) / 816);
    if (!(s > 0)) return;
    $$('.pp-sheet-wrap', sc).forEach(function (w) {
      w.style.width = (816 * s) + 'px';
      w.style.height = (1056 * s) + 'px';
      if (w.firstElementChild) w.firstElementChild.style.transform = 'scale(' + s + ')';
    });
  }
  function pageNumber() {
    var sc = $('#pp-pages');
    var out = $('#pp-pgn');
    if (!sc || !out) return;
    var wraps = $$('.pp-sheet-wrap', sc);
    var n = 1;
    wraps.forEach(function (w, i) {
      if (sc.scrollTop + sc.clientHeight / 2 > w.offsetTop - sc.offsetTop) n = i + 1;
    });
    out.textContent = String(n);
  }
  doc.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (t && t.id === 'pp-dialog' && root.KUI) {
      var modal = $('.k-modal', t);
      if (modal) {
        root.KUI.openModal(modal);
        var ta = $('textarea', modal);
        if (ta) ta.focus();
        scalePages();
        setTimeout(scalePages, 60);
        setTimeout(scalePages, 420);
      }
    }
    if (t && t.id === 'pp-saved') setTimeout(function () {
      var s = $('#pp-saved'); if (s) s.classList.remove('is-on');
    }, 1600);
  });
  doc.addEventListener('scroll', function (e) {
    if (e.target && e.target.id === 'pp-pages') pageNumber();
  }, true);
  root.addEventListener('resize', scalePages);
  // The server answers a saved sheet with this event: close it; the cards were swapped already.
  doc.addEventListener('pp-close-sheet', function () { if (root.KUI) root.KUI.closeModal(); });
  // The PDF is made: hand the file to the browser, which asks where to save it.
  doc.addEventListener('pp-download', function (e) {
    var d = e.detail || {};
    if (!d.url) return;
    var a = doc.createElement('a');
    a.href = d.url;
    a.download = d.name || '';
    doc.body.appendChild(a);
    a.click();
    a.remove();
    if (root.KUI) root.KUI.closeModal();
  });

  /* ------------------------------------------------------------ after the answer comes back */
  doc.addEventListener('htmx:oobAfterSwap', function (e) {
    var t = e.target;
    if (t && t.id === 'pp-chat') {
      var crits = $$('.pp-crit', t);
      var last = crits[crits.length - 1];
      var ask = $('#pp-ans', t);
      if (last && !ask) last.scrollIntoView({ block: 'nearest', behavior: smooth() });
      else if (ask && !crits.length) ask.focus({ preventScroll: true });
    }
  });

  /* -------------------------------------------------------------------------- Undo */
  // Undo reloads what is on screen, tab and all, from the server.
  doc.addEventListener('kestrel:undone', function () {
    var layout = $('#pp-layout');
    if (!layout || !root.htmx) return;
    var tab = layout.getAttribute('data-tab') || 'pack';
    var opp = layout.getAttribute('data-opp');
    var url = (opp ? '/prep/' + opp : '/prep') + '?tab=' + encodeURIComponent(tab);
    root.htmx.ajax('GET', url, { target: '#pp-body', swap: 'innerHTML' });
  });
  doc.addEventListener('htmx:responseError', function (e) {
    var xhr = e.detail && e.detail.xhr;
    if (xhr && xhr.status === 404) say('That is no longer here. Reload the page to see what changed.', 'warn');
  });
})(typeof window !== 'undefined' ? window : globalThis);
