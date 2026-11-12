/* Calibrate (D-UI-99): the keyboard and the small bits of card state. The page is server
   rendered and swaps in place with HTMX; this file only (1) picks a score and enables Save,
   (2) opens "Tell Kestrel why" and counts nudges, (3) keys 1 to 5, O, S and Enter, (4) reloads
   the card after an Undo, and (5) scrolls to a role loaded for a change only if it is out of
   view, then back only if it moved. Behavior is delegated on the document, so nothing needs
   re-binding after a swap. The page runs on the lifecycle (KUI.page, D-UI-210): this file only
   defines it, and mount starts every listener through its context, so leaving stops all of
   them and coming back starts one set. Mount also syncs the Save button, which the old
   DOMContentLoaded-only start missed whenever the page was not a fresh document load. */
(function () {
  'use strict';
  if (!window.KUI || !window.KUI.page) return;
  function reduced() {
    // The OS setting or the app's own Reduce motion switch (as role.js and profile.js read it).
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) ||
      document.documentElement.getAttribute('data-reduce-motion') === '1';
  }
  function smooth() { return reduced() ? 'auto' : 'smooth'; }
  var doc = document;
  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var returnTo = null;  // scroll position before a change scrolled the page, or null
  var oldStrips = null;
  var cx = null;

  function $(sel, root) { return (root || doc).querySelector(sel); }
  function form() { return $('#cal-form'); }

  function scroller(el) {
    for (var n = el.parentElement; n; n = n.parentElement) {
      var o = getComputedStyle(n).overflowY;
      if ((o === 'auto' || o === 'scroll') && n.scrollHeight > n.clientHeight) return n;
    }
    return doc.scrollingElement || doc.documentElement;
  }

  /* Save reads "Save", or "Save with reasons" once a reason or a nudge is chosen. On a change it
     stays off until something differs from the earlier rating. */
  function syncSave() {
    var f = form();
    if (!f) return;
    var score = f.elements.score.value;
    var extra = !!f.querySelector('input[name=reason]:checked') ||
      Array.prototype.some.call(f.querySelectorAll('input[name^=nudge_]'), function (i) { return i.value !== '0'; });
    var label = $('#cal-savel'), pn = $('#cal-why-panel');
    /* As drawn: "Save with reasons" once any is set, "Save" while the panel is open, else
       "Save, nothing to add". */
    if (label) label.textContent = extra ? 'Save with reasons'
      : (pn && pn.classList.contains('is-open') ? 'Save' : 'Save, nothing to add');
    var save = $('#cal-save');
    if (!save) return;
    var prev = f.getAttribute('data-previous');
    save.disabled = !score || (!!prev && score === prev && !extra);
  }

  function pick(n) {
    var f = form();
    if (!f) return;
    f.elements.score.value = String(n);
    f.querySelectorAll('.cal-pickbtn').forEach(function (b) {
      var on = b.getAttribute('data-n') === String(n);
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    var why = $('#cal-whybtn');
    if (why) why.hidden = false;
    syncSave();
  }

  function toggleWhy(open) {
    var panel = $('#cal-why-panel'), btn = $('#cal-whybtn');
    if (!panel || !btn || btn.hidden) return;
    var next = typeof open === 'boolean' ? open : !panel.classList.contains('is-open');
    panel.classList.toggle('is-open', next);
    panel.setAttribute('aria-hidden', next ? 'false' : 'true');
    btn.setAttribute('aria-expanded', next ? 'true' : 'false');
    syncSave();
  }

  function onClick(e) {
    var t = e.target;
    var pk = t.closest && t.closest('.cal-pickbtn');
    if (pk) { pick(+pk.getAttribute('data-n')); return; }
    if (t.closest && t.closest('#cal-whybtn')) { toggleWhy(); return; }
    var nd = t.closest && t.closest('.cal-nud button');
    if (nd) {
      var row = nd.closest('.cal-nud');
      var input = row.querySelector('input'), out = row.querySelector('b');
      var base = +row.getAttribute('data-base');
      var step = Math.max(-4, Math.min(4, +input.value + +nd.getAttribute('data-nd')));
      var shown = base + step;
      if (shown < 1 || shown > 5) return;
      input.value = String(step);
      out.textContent = String(shown);
      out.classList.toggle('is-changed', step !== 0);
      syncSave();
      return;
    }
    var sc = t.closest && t.closest('[data-cal-scroll]');
    if (sc) {
      var target = $(sc.getAttribute('data-cal-scroll'));
      if (target) target.scrollIntoView({ block: 'nearest', behavior: smooth() });
    }
  }

  function onChange(e) {
    if (e.target.matches && e.target.matches('input[name=reason]')) syncSave();
  }

  function onKeyDown(e) {
    if (e.ctrlKey || e.metaKey || e.altKey || !form()) return;
    var dp = doc.getElementById('cal-panel-detailed');
    if (dp && dp.hidden) return;  // the Quick tab is showing: the rating keys belong to Detailed
    var t = e.target, tag = t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    var k = e.key.toLowerCase();
    if (k.length === 1 && '12345'.indexOf(k) >= 0) {
      e.preventDefault();
      var b = form().querySelector('.cal-pickbtn[data-n="' + k + '"]');
      if (b) b.click();
    } else if (k === 'o') { e.preventDefault(); toggleWhy(); }
    else if (k === 's') { var s = $('#cal-skip'); if (s) { e.preventDefault(); s.click(); } }
    else if (k === 'enter' && tag !== 'BUTTON' && tag !== 'A') {
      var save = $('#cal-save');
      if (save && !save.disabled) { e.preventDefault(); save.click(); }
    }
  }

  /* After an Undo the toast names where to reload the card from (HX-Trigger k-toast `refresh`).
     Heard only while this page is mounted, so an Undo from another page never reloads a card. */
  function onUndone(e) {
    var url = e.detail && e.detail.refresh;
    if (url && window.htmx) window.htmx.ajax('GET', url, { target: '#cal-card', swap: 'outerHTML' });
  }

  /* A role loaded for a change: scroll only if the card is out of view, and remember where the
     page was. When the card goes back to the queue, return only if the page moved. */
  function onAfterSwap(e) {
    /* An outerHTML swap leaves e.target as the replaced node; the new card has the same id. */
    if (!(e.target && e.target.id === 'cal-card')) return;
    var card = doc.getElementById('cal-card');
    if (!card) return;
    var sc = scroller(card);
    if (card.getAttribute('data-change') === '1') {
      var before = sc.scrollTop;
      var r = card.getBoundingClientRect(), box = sc === doc.scrollingElement || sc === doc.documentElement
        ? { top: 0, bottom: window.innerHeight } : sc.getBoundingClientRect();
      if (r.top < box.top || r.bottom > box.bottom) {
        card.scrollIntoView({ block: 'nearest', behavior: smooth() });
        returnTo = before;
      }
    } else if (returnTo !== null) {
      var to = returnTo;
      returnTo = null;
      if (Math.abs(sc.scrollTop - to) > 4) sc.scrollTo({ top: to, behavior: smooth() });
    }
    syncSave();
  }

  /* The taste strip opens the sorting-model flyout; it is a div with role=button, so Enter and
     Space open it as a button would. */
  function onStripKey(e) {
    var t = e.target;
    if ((e.key === 'Enter' || e.key === ' ') && t.matches && t.matches('.cal-seg--open')) {
      e.preventDefault();
      t.click();
    }
  }

  /* Strips swap out of band after each save. The ring, the progress bar and the session dots
     start from what the old strip showed, then move, as the mockup does (animProg, dotpop). */
  function stripState(root) {
    var ring = root.querySelector('.cal-ring'), bar = root.querySelector('.cal-track u');
    return {
      p: ring ? ring.style.getPropertyValue('--p') : null,
      w: bar ? bar.style.width : null,
      on: root.querySelectorAll('.cal-dots i.is-on').length
    };
  }
  function onOobBeforeSwap(e) {
    if (e.target && e.target.id === 'cal-top') oldStrips = stripState(e.target);
  }
  function onOobAfterSwap(e) {
    var c = cx;
    var top = doc.getElementById('cal-top');
    if (!oldStrips || !top || !(e.target === top || top.contains(e.target))) return;
    var old = oldStrips;
    oldStrips = null;
    if (reduced()) return;
    var ring = top.querySelector('.cal-ring'), bar = top.querySelector('.cal-track u');
    var toP = ring && ring.style.getPropertyValue('--p'), toW = bar && bar.style.width;
    if (ring && old.p !== null && old.p !== toP) ring.style.setProperty('--p', old.p);
    if (bar && old.w && old.w !== toW) bar.style.width = old.w;
    var dots = top.querySelectorAll('.cal-dots i');
    var now = top.querySelectorAll('.cal-dots i.is-on').length;
    if (now > old.on && dots[now - 1]) dots[now - 1].classList.add('just');
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (!c || c.signal.aborted) return;  // the page was left before the strips settled
        if (ring && toP !== null) ring.style.setProperty('--p', toP);
        if (bar && toW) bar.style.width = toW;
      });
    });
  }

  /* Quick and Detailed tabs (issue 171). Both panels are in the page; the choice only shows one.
     The thumb slides under the selected tab, the leaving panel fades and the arriving one rises
     in (all off under reduced motion). Arrow keys move between the tabs. */
  function tabs() { return Array.prototype.slice.call(doc.querySelectorAll('.cal-tabs [data-cal-tab]')); }
  function panel(name) { return doc.getElementById('cal-panel-' + name); }
  function placeThumb(animate) {
    var bar = $('.cal-tabs'), sel = $('.cal-tabs [aria-selected="true"]'), th = $('.cal-tabs__thumb');
    if (!bar || !sel || !th || !sel.offsetWidth) return;
    if (!animate) bar.classList.remove('is-placed');
    th.style.width = sel.offsetWidth + 'px';
    th.style.transform = 'translateX(' + (sel.offsetLeft - 3) + 'px)';
    if (!animate) requestAnimationFrame(function () { bar.classList.add('is-placed'); });
  }
  /* The chosen tab goes in the address (no new history entry, through the page context), so a
     reload or a copied link opens the same tab. Quick is the default, so it is the bare address. */
  function keepInUrl(name) {
    if (!cx || !window.URL) return;
    var url = new window.URL(window.location.href);
    if (name === 'detailed') url.searchParams.set('tab', 'detailed');
    else url.searchParams.delete('tab');
    cx.replaceUrl(url.href);
  }
  function selectTab(name, focus) {
    var all = tabs();
    var cur = all.filter(function (b) { return b.getAttribute('aria-selected') === 'true'; })[0];
    if (!cur || cur.getAttribute('data-cal-tab') === name) { if (focus && cur) cur.focus(); return; }
    all.forEach(function (b) {
      var on = b.getAttribute('data-cal-tab') === name;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.setAttribute('tabindex', on ? '0' : '-1');
      if (on && focus) b.focus();
    });
    placeThumb(true);
    keepInUrl(name);
    var from = panel(cur.getAttribute('data-cal-tab')), to = panel(name);
    if (!from || !to) return;
    var show = function () {
      from.hidden = true; from.classList.remove('is-out');
      to.hidden = false;
      if (!reduced()) { to.classList.add('is-in'); cx.after(360, function () { to.classList.remove('is-in'); }); }
    };
    if (reduced()) { show(); return; }
    from.classList.add('is-out');
    cx.after(140, show);
  }
  function onTabClick(e) {
    var t = e.target;
    var b = t.closest && t.closest('[data-cal-tab]');
    if (b) { selectTab(b.getAttribute('data-cal-tab'), false); return; }
    var go = t.closest && t.closest('[data-cal-tab-go]');
    if (go) selectTab(go.getAttribute('data-cal-tab-go'), true);
  }
  function onTabKey(e) {
    var b = e.target && e.target.closest && e.target.closest('.cal-tabs [data-cal-tab]');
    if (!b || e.ctrlKey || e.metaKey || e.altKey) return;
    var all = tabs(), at = all.indexOf(b), next = -1;
    if (e.key === 'ArrowRight') next = (at + 1) % all.length;
    else if (e.key === 'ArrowLeft') next = (at + all.length - 1) % all.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = all.length - 1;
    if (next < 0) return;
    e.preventDefault();
    selectTab(all[next].getAttribute('data-cal-tab'), true);
  }

  window.KUI.page.define('calibrate', {
    mount: function (main, ctx) {
      cx = ctx; returnTo = null; oldStrips = null;
      ctx.on(doc, 'click', onTabClick);
      ctx.on(doc, 'keydown', onTabKey);
      ctx.on(window, 'resize', function () { placeThumb(false); });
      placeThumb(false);
      if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(function () { if (!ctx.signal.aborted) placeThumb(false); });
      ctx.on(doc, 'click', onClick);
      ctx.on(doc, 'change', onChange);
      ctx.on(doc, 'keydown', onKeyDown);
      ctx.on(doc, 'kestrel:undone', onUndone);
      ctx.on(doc, 'htmx:afterSwap', onAfterSwap);
      ctx.on(doc, 'keydown', onStripKey);
      ctx.on(doc, 'htmx:oobBeforeSwap', onOobBeforeSwap);
      ctx.on(doc, 'htmx:oobAfterSwap', onOobAfterSwap);
      syncSave();
      ctx.onUnmount(function () { if (cx === ctx) cx = null; });
    }
  });
})();
