/* Interview prep (D-UI-121): the little behavior HTMX does not give the page. Menus, a story
   opening to its words, the mode picker, the answer box (word count, hint, Next question), opening
   the sheets the server returns, the paper preview scaled to fit, the PDF download, and reading an
   Undo that can no longer be done. Everything is delegated on the document, so it keeps working
   after the body is swapped. Nothing here sends anything: the server decides and checks every
   send. The page runs on the lifecycle (KUI.page, D-UI-210): this file only defines it, and
   mount starts every listener and timer through its context, so leaving stops all of them and
   coming back starts one set. The Stories page renders the same template and uses this page. */
(function (root) {
  'use strict';
  function reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function smooth() { return reduced() ? 'auto' : 'smooth'; }
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var cx = null;
  var inkFrom = null;  // where the tab ink was before a tab change

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
  function onClick(e) {
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
  }
  function onEscape(e) { if (e.key === 'Escape') closeMenus(null); }
  function onInput(e) {
    if (e.target && e.target.id === 'pp-ans') {
      var v = e.target.value.trim();
      var wc = $('#pp-wc');
      if (wc) {
        var n = v ? v.split(/\s+/).length : 0;
        wc.textContent = n + (n === 1 ? ' word' : ' words');
      }
    }
  }

  /* A send to an online model the server has not been told to allow yet: ask first. The server
     refuses without the confirmation too; this only saves the trip. */
  function onSubmit(e) {
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
  }

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
  // Runs on htmx:afterSettle, not afterSwap: settle copies class and style from a same-id element
  // onto the new one and would strip the open state (#94).
  function onSheetSwap(e) {
    var t = e.target;
    if (t && t.id === 'pp-dialog' && root.KUI) {
      var modal = $('.k-modal', t);
      if (modal) {
        root.KUI.openModal(modal);
        var ta = $('textarea', modal);
        if (ta) { try { ta.focus({ preventScroll: true }); } catch (err) { /* gone */ } }
        scalePages();
        cx.after(60, scalePages);
        cx.after(420, scalePages);
      }
    }
    if (t && t.id === 'pp-saved') cx.after(1600, function () {
      var s = $('#pp-saved'); if (s) s.classList.remove('is-on');
    });
  }
  function onPagesScroll(e) {
    if (e.target && e.target.id === 'pp-pages') pageNumber();
  }
  // The server answers a saved sheet with this event: close it; the cards were swapped already.
  function onCloseSheet() { if (root.KUI) root.KUI.closeModal(); }
  // The PDF is made: hand the file to the browser, which asks where to save it.
  function onDownload(e) {
    var d = e.detail || {};
    if (!d.url) return;
    var a = doc.createElement('a');
    a.href = d.url;
    a.download = d.name || '';
    doc.body.appendChild(a);
    a.click();
    a.remove();
    if (root.KUI) root.KUI.closeModal();
  }

  /* ------------------------------------------------------------ after the answer comes back */
  function onChatSwap(e) {
    var t = e.target;
    if (t && t.id === 'pp-chat') {
      var crits = $$('.pp-crit', t);
      var last = crits[crits.length - 1];
      var ask = $('#pp-ans', t);
      if (last && !ask) last.scrollIntoView({ block: 'nearest', behavior: smooth() });
      else if (ask && !crits.length) ask.focus({ preventScroll: true });
    }
  }

  /* -------------------------------------------------------------------------- Undo */
  // Undo reloads what is on screen, tab and all, from the server.
  function onUndone() {
    var layout = $('#pp-layout');
    if (!layout || !root.htmx) return;
    var tab = layout.getAttribute('data-tab') || 'pack';
    var opp = layout.getAttribute('data-opp');
    var url = (opp ? '/prep/' + opp : '/prep') + '?tab=' + encodeURIComponent(tab);
    root.htmx.ajax('GET', url, { target: '#pp-body', swap: 'innerHTML' });
  }
  function onResponseError(e) {
    var xhr = e.detail && e.detail.xhr;
    if (xhr && xhr.status === 404) e.detail.kHandled = true;
    if (xhr && xhr.status === 404) say('That is no longer here. Reload the page to see what changed.', 'warn');
  }

  /* ----------------------------------------------------------------------- tab ink and rise */
  function placeInk(from) {
    var tabs = $('.pp-tabs');
    if (tabs && root.KUI) root.KUI.ink(tabs, from);
  }
  function onTabRequest(e) {
    var elt = e.detail && e.detail.elt;
    if (elt && elt.classList && elt.classList.contains('pp-tab') && root.KUI) {
      inkFrom = root.KUI.inkPos($('.pp-tabs'));
    }
  }
  function onInkSwap(e) {
    var elt = e.detail && e.detail.requestConfig && e.detail.requestConfig.elt;
    if (!elt || !elt.classList || !elt.classList.contains('pp-tab')) { placeInk(null); return; }
    placeInk(inkFrom);
    inkFrom = null;
    if (root.KUI) root.KUI.paneIn($('#pp-pane'));
  }
  function onTabsSwap(e) {
    if (e.target && e.target.id === 'pp-tabs') placeInk(null);
  }
  function onInkResize() { placeInk(null); }

  /* ------------------------------------------------------------------------------ the page */
  root.KUI.page.define('prep', {
    mount: function (main, ctx) {
      cx = ctx;
      inkFrom = null;
      // The order the file added them in.
      ctx.on(doc, 'click', onClick);
      ctx.on(doc, 'keydown', onEscape);
      ctx.on(doc, 'input', onInput);
      ctx.on(doc, 'submit', onSubmit, true);
      ctx.on(doc, 'htmx:afterSettle', onSheetSwap);
      ctx.on(doc, 'scroll', onPagesScroll, true);
      ctx.on(root, 'resize', scalePages);
      ctx.on(doc, 'pp-close-sheet', onCloseSheet);
      ctx.on(doc, 'pp-download', onDownload);
      ctx.on(doc, 'htmx:oobAfterSwap', onChatSwap);
      ctx.on(doc, 'kestrel:undone', onUndone);
      ctx.on(doc, 'htmx:responseError', onResponseError);
      ctx.on(doc, 'htmx:beforeRequest', onTabRequest);
      ctx.on(doc, 'htmx:afterSwap', onInkSwap);
      ctx.on(doc, 'htmx:oobAfterSwap', onTabsSwap);
      placeInk(null);
      ctx.on(root, 'resize', onInkResize);
      ctx.onUnmount(function () { if (cx === ctx) cx = null; });
    }
  });
})(typeof window !== 'undefined' ? window : globalThis);
