/* Today (D-UI-92, D-UI-156): the small amount of behavior HTMX does not give the page.
   - The Top matches carousel: fixed-width cards, the next one peeking, arrows, keys, page dots and
     pointer dragging (a drag never presses the button under it).
   - Card actions: Save for later and Shortlist leave with a fade and a collapse; Review slides the
     role summary in and never decides; a row in a lower panel slides out.
   - Live: a new match during a scan opens a gap at its ranked position and rises in with a brief
     ring and a NEW tag (the view stays anchored); the status strip fades Last run into Next run.
   - The walkthrough: a spotlight tour over THIS page and its real sidebar.
   Everything is delegated on the document or re-run on htmx:load, so it survives a swapped
   section. The page runs on the lifecycle (KUI.page, D-UI-210): this file only defines it, and
   mount starts every listener and timer through its context, so leaving stops all of them and
   coming back starts one set. User-facing strings here are the tour steps and a few labels. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  var GAP = 16, PEEK = 60, CARD = 270;
  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var S = { idx: 0, perView: 4, cw: 0, fade: null, poll: null };
  var cx = null;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function reduced() { return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches); }
  function track() { var p = $('#today'); return p ? p.getAttribute('data-track') : ''; }
  function cards() { return $$('#td-track > .td-card'); }
  function count() { return cards().length; }
  function maxIdx() { return Math.max(0, count() - S.perView); }
  function later(fn, ms) { return cx ? cx.after(reduced() ? 0 : ms, fn) : 0; }

  /* ----------------------------------------------------------------------------- carousel */
  // The title and the "why it fits" share a fixed budget of five lines (a title may take two), so
  // every card is the same height.
  function balance(el) {
    (el ? [el] : cards()).forEach(function (c) {
      var t = $('.td-t', c);
      if (!t) return;
      c.style.setProperty('--wl', 3);
      var lh = parseFloat(root.getComputedStyle(t).lineHeight) || 19.5;
      t.style.minHeight = '0';  // measure the title's own lines, not its reserved two
      var lines = Math.round(t.scrollHeight / lh);
      t.style.minHeight = '';
      c.style.setProperty('--wl', lines > 1 ? 2 : 3);
    });
  }
  function offAt(i) { return i === 0 ? 0 : i * (S.cw + GAP) - PEEK - GAP; }
  function layout() {
    var vp = $('#td-vp');
    if (!vp) return;
    var vpw = vp.clientWidth;
    S.perView = Math.max(1, Math.floor((vpw - 2 * PEEK - GAP) / (CARD + GAP)));
    S.cw = (vpw - 2 * PEEK - GAP * (S.perView + 1)) / S.perView;
    cards().forEach(function (c) { if (!c.classList.contains('is-collapse')) c.style.width = S.cw + 'px'; });
    balance();
    var dots = $('#td-dots');
    if (dots) dots.innerHTML = maxIdx() ? Array.from({ length: maxIdx() + 1 }, function (_, n) { return '<i role="button" tabindex="0" aria-label="Page ' + (n + 1) + '"></i>'; }).join('') : '';
    go(Math.min(S.idx, maxIdx()));
  }
  function go(i) {
    S.idx = Math.max(0, Math.min(i, maxIdx()));
    var tr = $('#td-track');
    if (!tr) return;
    tr.style.transform = 'translateX(' + (-offAt(S.idx)) + 'px)';
    cards().forEach(function (c, k) {
      var off = k < S.idx || k >= S.idx + S.perView;
      c.classList.toggle('is-peek', off);
      if (off) c.setAttribute('inert', ''); else c.removeAttribute('inert');
    });
    var prev = $('#td-prev'), next = $('#td-next');
    if (prev) prev.disabled = S.idx === 0;
    if (next) next.disabled = S.idx >= maxIdx();
    $$('#td-dots i').forEach(function (d, k) { d.classList.toggle('is-on', k === S.idx); });
  }
  function initCarousel() {
    S.idx = S.keep || 0;
    S.keep = 0;
    var intro = !doc.body.hasAttribute('data-td-seen') && !reduced();
    doc.body.setAttribute('data-td-seen', '1');
    cards().forEach(function (c, i) {
      if (!intro) return;
      c.style.setProperty('--i', i);
      c.classList.add('is-in');
    });
    layout();
  }
  function onAnimationEnd(e) {
    var c = e.target && e.target.closest && e.target.closest('.td-card');
    if (!c || e.target !== c) return;
    if (e.animationName === 'td-in-up') c.classList.remove('is-in');
    if (e.animationName === 'k-glow') c.classList.remove('is-arrive');
  }

  // Dragging with touch, pen or mouse; vertical page scroll still works on touch.
  var drag = { x0: null, y0: 0, base: 0, moved: false, pid: null };
  function onPointerDown(e) {
    var vp = e.target.closest && e.target.closest('#td-vp');
    if (!vp || e.button !== 0 || e.target.closest('button, a')) return;
    drag.x0 = e.clientX; drag.y0 = e.clientY; drag.base = offAt(S.idx); drag.moved = false; drag.pid = e.pointerId;
  }
  function onPointerMove(e) {
    if (drag.x0 === null || e.pointerId !== drag.pid) return;
    var vp = $('#td-vp'), tr = $('#td-track');
    if (!vp || !tr) return;
    var dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (!drag.moved) {
      if (Math.abs(dx) < 6) return;
      if (Math.abs(dy) > Math.abs(dx)) { drag.x0 = null; return; }
      drag.moved = true; vp.classList.add('is-dragging');
      try { vp.setPointerCapture(drag.pid); } catch (err) { /* the pointer is gone */ }
    }
    var off = drag.base - dx, max = offAt(maxIdx());
    if (off < 0) off *= 0.35;
    if (off > max) off = max + (off - max) * 0.35;
    tr.style.transform = 'translateX(' + (-off) + 'px)';
  }
  function endDrag(e) {
    if (drag.x0 === null) return;
    var dx = (e.clientX !== undefined ? e.clientX : drag.x0) - drag.x0;
    drag.x0 = null;
    if (!drag.moved) return;
    var vp = $('#td-vp');
    if (vp) {
      vp.classList.remove('is-dragging');
      var kill = function (ev) { ev.stopPropagation(); ev.preventDefault(); };
      var unkill = cx.on(vp, 'click', kill, { capture: true, once: true });
      cx.after(50, unkill);
    }
    var step = Math.round(Math.abs(dx) / (S.cw + GAP)) || (Math.abs(dx) > 40 ? 1 : 0);
    go(dx < 0 ? S.idx + step : S.idx - step);
  }

  function onKeyDown(e) {
    if (tour.on) {
      if (e.key === 'Escape') stopTour();
      else if (e.key === 'ArrowRight') toStep(tour.i + 1);
      else if (e.key === 'ArrowLeft') toStep(tour.i - 1);
      return;
    }
    var t = e.target;
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (t && t.closest && t.closest('input, textarea, select, dialog, [role="dialog"], [role="tablist"], [contenteditable]')) return;
    if ($('.k-slideover.is-open, .k-modal.is-open, dialog[open]')) return;
    if (!$('#td-vp')) return;
    if (e.key === 'ArrowRight') go(S.idx + 1);
    else if (e.key === 'ArrowLeft') go(S.idx - 1);
  }
  function onClick(e) {
    var t = e.target;
    if (t.closest('#td-prev')) { go(S.idx - 1); return; }
    if (t.closest('#td-next')) { go(S.idx + 1); return; }
    var dot = t.closest('#td-dots i');
    if (dot) { go($$('#td-dots i').indexOf(dot)); return; }
    if (t.closest('#td-track') && !t.closest('button, a')) {
      var pk = cards().filter(function (c) {
        var r = c.getBoundingClientRect();
        return c.classList.contains('is-peek') && e.clientX >= r.left && e.clientX <= r.right;
      })[0];
      if (pk) { go(cards().indexOf(pk) < S.idx ? S.idx - 1 : S.idx + 1); return; }
    }
    var rv = t.closest('[data-td-review]');
    if (rv) { openRole(rv.getAttribute('data-td-review'), rv); return; }
    // D-UI-223: a tap or click on the active card's body opens its summary, as Review does. A
    // drag never gets here (endDrag swallows its click); its own controls do their own job.
    // Selecting text on the card is not a tap; the walkthrough owns clicks while it runs.
    var card = t.closest('#td-track > .td-card');
    if (card && !tour.on && !card.classList.contains('is-peek') &&
        !t.closest('button, a, input, select, textarea, .td-est')) {
      var sel = root.getSelection ? String(root.getSelection()) : '';
      var own = card.querySelector('[data-td-review]');
      if (own && !sel && $('#td-drawer')) {
        pressCard(card);
        openRole(own.getAttribute('data-td-review'), own);
      }
      return;
    }
    if (t.closest('[data-td-runnow]')) {
      var b = $('[data-testid="scan-now"]');
      if (b) b.click();
    }
  }
  function onDotKey(e) {
    var dot = e.target.closest && e.target.closest('#td-dots i');
    if (dot && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); go($$('#td-dots i').indexOf(dot)); }
  }
  function onResize() { layout(); if (tour.on) paintTour(false); }

  /* ------------------------------------------------------------------------ card actions */
  function collapse(c) {
    c.style.height = c.offsetHeight + 'px';
    c.style.alignSelf = 'flex-start';
    c.style.minWidth = '0';
    c.classList.add('is-collapse');
  }
  function doneCard() {
    var tpl = $('#td-done-tpl');
    return tpl ? tpl.innerHTML : '';
  }
  function removeCard(c, instant) {
    var finish = function () {
      c.remove();
      var tr = $('#td-track');
      if (tr && !tr.querySelector('.td-card')) {
        tr.innerHTML = doneCard();
        var sub = $('#td-sub');
        if (sub) sub.textContent = 'All caught up';
      }
      layout();
    };
    if (instant || reduced()) { finish(); return; }
    c.classList.add('is-leaving');
    later(function () { collapse(c); }, 300);
    later(finish, 750);
  }
  function cardOf(id) { return $('#td-track > .td-card[data-id="' + id + '"]'); }
  function refreshPanels() { doc.body.dispatchEvent(new CustomEvent('td-refresh')); }
  function warned(xhr) {
    try {
      var h = xhr && xhr.getResponseHeader && xhr.getResponseHeader('HX-Trigger');
      var t = h ? JSON.parse(h) : null;
      return !!(t && t['k-toast'] && t['k-toast'].tone === 'warn');
    } catch (err) { return false; }
  }
  function shortlisted(c) {
    if (!c) return;
    var b = $('.td-sl', c);
    if (b) { b.classList.add('is-added'); b.textContent = '✓ Added'; }
    enterNext = true;
    later(function () { removeCard(c); refreshPanels(); }, 550);
  }
  // The shortlisted role slides into the top of Decisions waiting (the mockup's .item.enter).
  var enterNext = false;
  function onAfterSwap(e) {
    var t = e.target;
    if (!enterNext || !t || t.id !== 'td-decisions') return;
    enterNext = false;
    var first = $('#td-decisions .td-item');
    if (first && first.animate && !reduced()) {
      first.animate([{ opacity: 0, transform: 'translateX(-24px)' }, { opacity: 1, transform: 'none' }],
        { duration: 450, easing: 'ease' });
    }
  }
  function onAfterRequest(e) {
    var d = e.detail, el = d && d.elt;
    if (!el || !el.matches) return;
    var url = (d.pathInfo && d.pathInfo.requestPath) || '';
    if (/^\/scan\/(start|cancel)/.test(url) || el.hasAttribute('data-td-refresh')) {
      doc.body.dispatchEvent(new CustomEvent('run-refresh'));
    }
    if (!d.successful || warned(d.xhr)) return;
    if (el.matches('.td-sl')) { shortlisted(el.closest('.td-card')); return; }
    if (el.matches('.td-save')) {
      el.classList.add('is-on'); el.title = 'Saved for later';
      var c = el.closest('.td-card');
      later(function () { removeCard(c); }, 600);
      return;
    }
    if (el.matches('[data-td-drawer-sl]')) {
      var id = el.getAttribute('data-td-drawer-sl');
      if (root.KUI) root.KUI.closeSlideOver();
      var card = cardOf(id);
      if (card) later(function () { shortlisted(card); }, 350); else refreshPanels();
      return;
    }
    if (el.hasAttribute('data-td-gone')) {
      var row = el.closest('.td-item');
      if (row) { row.classList.add('is-gone'); later(function () { row.remove(); refreshPanels(); }, 380); }
    }
  }
  // An Undo (any toast, kestrel:undone): the sections that show the change are asked again.
  function onUndone() {
    var m = $('#td-matches');
    refreshPanels();
    if (m && root.htmx) {
      S.keep = S.idx;
      root.htmx.ajax('GET', '/today/matches?track=' + encodeURIComponent(track()),
        { target: '#td-matches', swap: 'outerHTML' });
    }
  }

  /* -------------------------------------------------------------- the role summary (Review) */
  // A brief press on the card itself (D-UI-223); none under reduced motion.
  function pressCard(c) {
    if (reduced()) return;
    c.classList.add('is-pressed');
    cx.after(160, function () { c.classList.remove('is-pressed'); });
  }
  var opening = null;  // the role whose summary is on its way: a double tap asks once
  function openRole(id, trigger) {
    var dr = $('#td-drawer');
    if (!dr || !root.fetch || !cx || opening === id) return;
    opening = id;
    var signal = cx.signal;  // leaving the page drops an answer still on its way
    root.fetch('/today/roles/' + encodeURIComponent(id) + '/summary',
      { credentials: 'same-origin', signal: signal })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        if (signal.aborted) return;
        dr.innerHTML = html;
        if (root.htmx) root.htmx.process(dr);
        if (root.KUI) root.KUI.openSlideOver('#td-drawer', { trigger: trigger });
      })
      .catch(function () { /* offline, or the page was left: the summary stays closed */ })
      .then(function () { if (opening === id) opening = null; });
  }

  /* ------------------------------------------------------------------------ the status strip */
  function initStrip() {
    cx.clear(S.fade);
    S.fade = null;
    var f = $('#td-fader');
    if (!f || f.getAttribute('data-hold') === '1') return;
    var last = $('#td-face-last'), next = $('#td-face-next');
    if (!last || !next || reduced()) return;
    var a = $('.td-ico-a'), b = $('.td-ico-b');
    var showLast = !last.classList.contains('is-hide');
    S.fade = cx.every(4000, function () {
      showLast = !showLast;
      last.classList.toggle('is-hide', !showLast);
      next.classList.toggle('is-hide', showLast);
      last.setAttribute('aria-hidden', showLast ? 'false' : 'true');
      next.setAttribute('aria-hidden', showLast ? 'true' : 'false');
      if (a && b) { a.hidden = !showLast; b.hidden = showLast; }
    });
  }

  /* ------------------------------------------------------------------ live: a new match */
  function insertCard(item) {
    var tr = $('#td-track');
    if (!tr || cardOf(item.id)) return;
    // Hold the carousel's height while the horizontal gap opens. The first reviewed card
    // replaces an empty state, so glide to its height instead of jumping the lower panels.
    var heldHeight = tr.getBoundingClientRect().height;
    if (!tr._tdArrivals) {
      tr._tdTarget = count() ? heldHeight : 0;
      tr._tdAnchor = cards()[S.idx];
      tr.style.height = heldHeight + 'px';
      tr.getBoundingClientRect();
    }
    tr._tdArrivals = (tr._tdArrivals || 0) + 1;
    var done = $('.td-empty', tr);
    if (done) done.remove();
    var tmp = doc.createElement('div');
    tmp.innerHTML = item.html.trim();
    var el = tmp.firstElementChild;
    var t = $('.td-t', el);
    if (t) t.insertAdjacentHTML('beforeend', '<span class="td-new">NEW</span>');
    // A new match at or above the Strong match cutoff (the server decides) says so once.
    if (item.strong && root.KUI && item.score !== null && item.score !== undefined) {
      root.KUI.toast('New strong match: ' + Number(item.score).toFixed(1) + ' \u00b7 ' + item.title);
    }
    var before = item.before ? cardOf(item.before) : null;
    // Keep the view anchored: the viewport's first visible card stays where it is.
    el.classList.add('is-grow');
    el.style.width = '0px'; el.style.padding = '16px 0';
    el.style.alignSelf = 'flex-start'; el.style.minWidth = '0';
    if (before) tr.insertBefore(el, before); else tr.appendChild(el);
    if (root.htmx) root.htmx.process(el);
    var held = S.idx;
    layout();
    S.idx = Math.min(held, maxIdx());
    go(S.idx);  // the new card is still closed: hold the view, then move it as the gap opens
    // Measure at its real width, without interpolating the width transition from zero.
    el.style.transition = 'none';
    el.style.width = S.cw + 'px';
    el.style.padding = '';
    balance(el);
    var targetHeight = Math.max(tr._tdTarget, el.getBoundingClientRect().height);
    tr._tdTarget = targetHeight;
    el.style.padding = '16px 0';
    el.style.height = targetHeight + 'px';
    el.style.width = '0px';
    el.getBoundingClientRect();
    el.style.transition = '';
    tr.style.height = targetHeight + 'px';
    var open = function () { el.style.width = S.cw + 'px'; el.style.padding = ''; };
    if (reduced()) { open(); } else { requestAnimationFrame(function () { requestAnimationFrame(open); }); }
    later(function () {
      el.classList.remove('is-grow');
      el.style.height = ''; el.style.alignSelf = '';
      tr._tdArrivals -= 1;
      if (!tr._tdArrivals) tr.style.height = '';
      el.classList.add('is-arrive');
      var shift = tr._tdAnchor ? cards().indexOf(tr._tdAnchor) : S.idx;
      S.idx = Math.max(0, Math.min(shift, maxIdx()));
      go(S.idx);
    }, 460);
  }
  // A card whose full review finished: its content changes in place (a short fade), then the
  // cards slide into best-first order (owner round 13). No card jumps; reduced motion snaps.
  function finishCard(item) {
    var old = cardOf(item.id);
    if (!old) return;
    var tmp = doc.createElement('div');
    tmp.innerHTML = item.html.trim();
    var el = tmp.firstElementChild;
    if (!el) return;
    el.style.width = old.style.width;
    old.replaceWith(el);
    if (root.htmx) root.htmx.process(el);
    if (!reduced() && el.animate) el.animate([{ opacity: .4 }, { opacity: 1 }], { duration: 300 });
  }
  function reorder(ids) {
    var tr = $('#td-track');
    if (!tr || !ids.length) return;
    var list = cards();
    var before = {};
    list.forEach(function (c) { before[c.getAttribute('data-id')] = c.getBoundingClientRect().left; });
    var order = ids.map(String);
    var sorted = list.slice().sort(function (a, b) {
      var ia = order.indexOf(a.getAttribute('data-id')), ib = order.indexOf(b.getAttribute('data-id'));
      return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib);
    });
    if (sorted.every(function (c, i) { return c === list[i]; })) return;
    var tail = list[list.length - 1].nextSibling;
    sorted.forEach(function (c) { tr.insertBefore(c, tail); });
    layout();
    go(Math.min(S.idx, maxIdx()));
    if (reduced()) return;
    sorted.forEach(function (c) {
      var dx = before[c.getAttribute('data-id')] - c.getBoundingClientRect().left;
      if (dx && c.animate) {
        c.animate([{ transform: 'translateX(' + dx + 'px)' }, { transform: 'none' }],
                  { duration: 420, easing: 'cubic-bezier(.22,.8,.3,1)' });
      }
    });
  }
  function pollNew() {
    var tr = $('#td-track');
    if (!tr || doc.hidden || !root.fetch || !cx) return;
    var signal = cx.signal;
    var have = cards().map(function (c) { return c.getAttribute('data-id'); }).join(',');
    var pend = cards().filter(function (c) { return c.getAttribute('data-pending') === '1'; })
      .map(function (c) { return c.getAttribute('data-id'); }).join(',');
    root.fetch('/today/matches/new?track=' + encodeURIComponent(track()) + '&have=' + have +
               '&pending=' + pend,
      { credentials: 'same-origin', signal: signal })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || signal.aborted) return;
        (d.new || []).forEach(insertCard);
        (d.done || []).forEach(finishCard);
        if ((d.done || []).length) reorder(d.ids || []);
        var pending = $('#td-pending');
        var line = d.pending > 0 ? d.pending + ' more being reviewed' : '';
        if (pending && pending.textContent !== line) {
          pending.textContent = line;
          if (!reduced() && pending.animate) {
            pending.animate([{ opacity: .4 }, { opacity: 1 }], { duration: 300 });
          }
        }
        var sub = $('#td-sub');
        if (sub && d.found_line && (d.pending > 0 || !$('.td-empty', tr))) {
          sub.textContent = d.found_line;
        }
      })
      .catch(function () { /* offline or restarting: the next poll tries again */ });
  }
  // One timer for the page: the strip swaps itself every 5 s while a scan runs, and a timer
  // restarted by each swap would never fire. Only a change of period replaces it.
  function startPoll() {
    var top = $('#td-top');
    var busy = !!top && /active/.test(top.getAttribute('data-state') || '');
    var ms = busy ? 6000 : 30000;
    if (S.poll && S.pollMs === ms) return;
    cx.clear(S.poll);
    S.pollMs = ms;
    S.poll = cx.every(ms, pollNew);
  }

  /* --------------------------------------------------------------------- the walkthrough */
  var tour = { on: false, i: 0, from: null };
  function nav(href) { return $('nav.mainnav a[href="' + href + '"]') || $('#sidebar a[href="' + href + '"]'); }
  var STEPS = [
    { s: function () { return $('.td-hero .k-scene__stack') || $('.td-hero'); },
      h: 'Today is your daily start', p: 'Open it each morning. It shows your top matches and what needs you.' },
    { s: function () { return nav('/opportunities'); },
      h: 'Every role Kestrel finds', p: 'All roles found, scored for fit, newest first.' },
    { s: function () { return $('#td-track .td-card') || $('#td-track .td-empty') || $('#td-vp'); },
      h: 'Your top matches', p: 'The best fits show here first. New roles appear as the first search finishes.' },
    { s: function () { return $('#td-track [data-td-review]') || $('#td-track .td-empty') || $('#td-vp'); },
      h: 'See why a role fits', p: 'Review opens the reasons behind the score. From there you can tailor your resume with your own lines. You download it and apply yourself. Kestrel never applies for you.' },
    { s: function () { return nav('/calibrate'); },
      h: 'Teach Kestrel your taste', p: 'Rate a few roles in Calibrate. Too few to tell until you rate 5.' },
    { s: function () { return nav('/pursuits'); },
      h: 'Track what you applied to', p: 'Log each application and its outcome. Reminders show on Today.' },
    { s: null, h: "That's it", p: 'Your first roles arrive in a few minutes.' }
  ];
  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function button(label, cls, act) {
    var b = el('button', cls, label);
    b.type = 'button';
    b.setAttribute('data-td-tour', act);
    return b;
  }
  function paintTour(first) {
    var box = $('#td-tour'), spot = $('#td-spot'), tc = $('#td-tc');
    if (!box || !spot || !tc) return;
    var st = STEPS[tour.i], last = tour.i === STEPS.length - 1;
    var target = st.s && st.s();
    if (target && target.scrollIntoView) target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    tc.textContent = '';
    tc.appendChild(el('h4', '', st.h));
    tc.appendChild(el('p', '', st.p));
    var ft = el('div', 'td-tc__ft');
    var meta = el('div', 'td-tc__meta');
    if (!last) meta.appendChild(el('span', 'td-tc__cnt', (tour.i + 1) + ' of 6'));
    var dots = el('div', 'td-tdots');
    dots.setAttribute('aria-hidden', 'true');
    for (var k = 0; k < 6; k++) dots.appendChild(el('i', k === tour.i || (last && k === 5) ? 'is-on' : ''));
    meta.appendChild(dots);
    ft.appendChild(meta);
    var acts = el('div', 'td-tc__act');
    if (!last) acts.appendChild(button('Skip tour', 'k-btn k-btn--quiet k-btn--sm', 'skip'));
    if (tour.i > 0) acts.appendChild(button('Back', 'k-btn k-btn--sm', 'back'));
    acts.appendChild(button(last ? 'Close' : 'Next', 'k-btn k-btn--primary k-btn--sm', 'next'));
    ft.appendChild(acts);
    tc.appendChild(ft);
    var W = root.innerWidth, H = root.innerHeight, CW = tc.offsetWidth || 330, CH = tc.offsetHeight, x, y;
    if (target) {
      var r = target.getBoundingClientRect(), pad = 6;
      spot.classList.remove('is-none');
      spot.style.left = (r.left - pad) + 'px'; spot.style.top = (r.top - pad) + 'px';
      spot.style.width = (r.width + pad * 2) + 'px'; spot.style.height = (r.height + pad * 2) + 'px';
      x = r.right + pad + 16;
      if (x + CW > W - 12) x = r.left - pad - 16 - CW;
      y = r.top + r.height / 2 - CH / 2;
      if (x < 12) {
        x = Math.max(12, Math.min(r.left, W - CW - 12));
        y = r.bottom + pad + 14;
        if (y + CH > H - 12) y = r.top - pad - 14 - CH;
      }
      y = Math.max(12, Math.min(y, H - CH - 12));
    } else {
      spot.classList.add('is-none');
      spot.style.left = W / 2 + 'px'; spot.style.top = H / 2 + 'px';
      spot.style.width = '0px'; spot.style.height = '0px';
      x = (W - CW) / 2; y = (H - CH) / 2;
    }
    if (first) { tc.style.transition = 'none'; spot.style.transition = 'none'; }
    tc.style.left = x + 'px'; tc.style.top = y + 'px';
    if (first) { void tc.offsetHeight; tc.style.transition = ''; spot.style.transition = ''; }
  }
  function startTour(i) {
    var box = $('#td-tour');
    if (!box) return;
    tour.from = doc.activeElement;
    tour.i = i || 0; tour.on = true;
    box.hidden = false; box.classList.add('is-on');
    paintTour(true);
    var nx = $('[data-td-tour="next"]', box);
    if (nx) nx.focus();
  }
  function stopTour() {
    var box = $('#td-tour');
    tour.on = false;
    if (box) { box.classList.remove('is-on'); box.hidden = true; }
    if (tour.from && tour.from.focus) tour.from.focus();
  }
  function toStep(i) {
    tour.i = Math.max(0, Math.min(STEPS.length - 1, i));
    paintTour(false);
  }
  function onTourClick(e) {
    var b = e.target.closest && e.target.closest('[data-td-tour]');
    if (!b) return;
    var act = b.getAttribute('data-td-tour');
    if (act === 'next') { if (tour.i === STEPS.length - 1) stopTour(); else toStep(tour.i + 1); }
    else if (act === 'back') toStep(tour.i - 1);
    else stopTour();
  }
  function onTourStart(e) {
    if (e.target.closest && e.target.closest('[data-td-tour-start]')) startTour(0);
  }

  /* The strip refreshes in place (owner round 13: it vanished and came back on each poll). A
     poll that brings the same strip changes nothing; one that brings new counts keeps the face
     (Last run or Next run) that was showing, so only the numbers change. */
  var stripFace = null;
  function onBeforeSwap(e) {
    var tgt = e.detail && e.detail.target;
    if (!tgt || tgt.id !== 'td-top' || !e.detail.xhr) return;
    var box = doc.createElement('div');
    box.innerHTML = e.detail.xhr.responseText || '';
    var fresh = box.querySelector('#td-top');
    if (fresh && fresh.textContent.replace(/\s+/g, ' ') === tgt.textContent.replace(/\s+/g, ' ')) {
      e.detail.shouldSwap = false;
      return;
    }
    var last = $('#td-face-last');
    stripFace = last && last.classList.contains('is-hide') ? 'next' : 'last';
  }
  function keepFace() {
    if (stripFace !== 'next') return;
    var last = $('#td-face-last'), next = $('#td-face-next'), f = $('#td-fader');
    if (!last || !next || (f && f.getAttribute('data-hold') === '1')) return;
    last.classList.add('is-hide'); next.classList.remove('is-hide');
    last.setAttribute('aria-hidden', 'true'); next.setAttribute('aria-hidden', 'false');
    var a = $('.td-ico-a'), b = $('.td-ico-b');
    if (a && b) { a.hidden = true; b.hidden = false; }
  }

  // Updated in place (kestrel-ui.js data-k-morph): the face that was showing stays.
  function onMorphed(e) {
    if (e.target && e.target.id === 'td-top') { keepFace(); stripFace = null; }
  }

  /* ---------------------------------------------------------------------------------- init */
  function onLoad(e) {
    var n = e.detail && e.detail.elt;
    if (!n || !n.id) return;
    if (n.id === 'td-matches') { initCarousel(); }
    if (n.id === 'td-top') { keepFace(); initStrip(); startPoll(); stripFace = null; }
  }
  function boot() {
    if (!$('#today')) return;
    initCarousel();
    initStrip();
    startPoll();
    var host = $('#today');
    // The launch entrance (locked mockup): the page staggers in once. The class only gates the CSS
    // delays, so a panel refreshed later by htmx never replays it.
    if (!reduced()) { host.classList.add('td-launch'); cx.after(2200, function () { host.classList.remove('td-launch'); }); }
    if (host.getAttribute('data-tour') === '1') later(function () { startTour(0); }, 600);
  }

  root.KUI.page.define('today', {
    mount: function (main, ctx) {
      cx = ctx;
      S = { idx: 0, perView: 4, cw: 0, fade: null, poll: null };
      drag = { x0: null, y0: 0, base: 0, moved: false, pid: null };
      opening = null;
      tour = { on: false, i: 0, from: null };
      enterNext = false;
      stripFace = null;
      ctx.on(doc, 'animationend', onAnimationEnd);
      ctx.on(doc, 'pointerdown', onPointerDown);
      ctx.on(doc, 'pointermove', onPointerMove);
      ctx.on(doc, 'pointerup', endDrag);
      ctx.on(doc, 'pointercancel', endDrag);
      ctx.on(doc, 'keydown', onKeyDown);
      ctx.on(doc, 'click', onClick);
      ctx.on(doc, 'keydown', onDotKey);
      ctx.on(root, 'resize', onResize);
      ctx.on(doc, 'htmx:afterSwap', onAfterSwap);
      ctx.on(doc, 'htmx:afterRequest', onAfterRequest);
      ctx.on(doc, 'kestrel:undone', onUndone);
      ctx.on(doc, 'click', onTourClick);
      ctx.on(doc, 'click', onTourStart);
      ctx.on(doc.body, 'htmx:beforeSwap', onBeforeSwap);
      ctx.on(doc.body, 'k-morphed', onMorphed);
      ctx.on(doc.body, 'htmx:load', onLoad);
      boot();
      if (doc.fonts && doc.fonts.ready) {
        doc.fonts.ready.then(function () { if (!ctx.signal.aborted && $('#td-vp')) layout(); });
      }
      root.KTour = { start: startTour, stop: stopTour };
      root.TodayPage = { layout: layout, go: go, insertCard: insertCard, pollNew: pollNew };
      ctx.onUnmount(function () {
        if (tour.on) stopTour();
        var host = $('#today');
        if (host) host.classList.remove('td-launch');
        // The carousel's entrance plays on each arrival, as it did on each page load.
        doc.body.removeAttribute('data-td-seen');
        delete root.KTour;
        delete root.TodayPage;
      });
    }
  });
})(typeof window !== 'undefined' ? window : this);
