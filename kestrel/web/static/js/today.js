/* Today (D-UI-92, D-UI-156): the small amount of behavior HTMX does not give the page.
   - The Top matches carousel: fixed-width cards, the next one peeking, arrows, keys, page dots and
     pointer dragging (a drag never presses the button under it).
   - Card actions: Save for later and Shortlist leave with a fade and a collapse; Review slides the
     role summary in and never decides; a row in a lower panel slides out.
   - Live: a new match during a scan opens a gap at its ranked position and rises in with a brief
     ring and a NEW tag (the view stays anchored); the status strip fades Last run into Next run.
   - The walkthrough: a spotlight tour over THIS page and its real sidebar.
   Everything is delegated on the document or re-run on htmx:load, so it survives a swapped
   section. User-facing strings here are the tour steps and a few labels. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  var GAP = 16, PEEK = 60, CARD = 270;
  var S = { idx: 0, perView: 4, cw: 0, fade: null, poll: null };

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function reduced() { return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches); }
  function track() { var p = $('#today'); return p ? p.getAttribute('data-track') : ''; }
  function cards() { return $$('#td-track > .td-card'); }
  function count() { return cards().length; }
  function maxIdx() { return Math.max(0, count() - S.perView); }
  function later(fn, ms) { return setTimeout(fn, reduced() ? 0 : ms); }

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
  doc.addEventListener('animationend', function (e) {
    var c = e.target && e.target.closest && e.target.closest('.td-card');
    if (!c || e.target !== c) return;
    if (e.animationName === 'td-in-up') c.classList.remove('is-in');
    if (e.animationName === 'k-glow') c.classList.remove('is-arrive');
  });

  // Dragging with touch, pen or mouse; vertical page scroll still works on touch.
  var drag = { x0: null, y0: 0, base: 0, moved: false, pid: null };
  doc.addEventListener('pointerdown', function (e) {
    var vp = e.target.closest && e.target.closest('#td-vp');
    if (!vp || e.button !== 0 || e.target.closest('button, a')) return;
    drag.x0 = e.clientX; drag.y0 = e.clientY; drag.base = offAt(S.idx); drag.moved = false; drag.pid = e.pointerId;
  });
  doc.addEventListener('pointermove', function (e) {
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
  });
  function endDrag(e) {
    if (drag.x0 === null) return;
    var dx = (e.clientX !== undefined ? e.clientX : drag.x0) - drag.x0;
    drag.x0 = null;
    if (!drag.moved) return;
    var vp = $('#td-vp');
    if (vp) {
      vp.classList.remove('is-dragging');
      var kill = function (ev) { ev.stopPropagation(); ev.preventDefault(); };
      vp.addEventListener('click', kill, { capture: true, once: true });
      setTimeout(function () { vp.removeEventListener('click', kill, { capture: true }); }, 50);
    }
    var step = Math.round(Math.abs(dx) / (S.cw + GAP)) || (Math.abs(dx) > 40 ? 1 : 0);
    go(dx < 0 ? S.idx + step : S.idx - step);
  }
  doc.addEventListener('pointerup', endDrag);
  doc.addEventListener('pointercancel', endDrag);

  doc.addEventListener('keydown', function (e) {
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
  });
  doc.addEventListener('click', function (e) {
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
    if (t.closest('[data-td-runnow]')) {
      var b = $('[data-testid="scan-now"]');
      if (b) b.click();
    }
  });
  doc.addEventListener('keydown', function (e) {
    var dot = e.target.closest && e.target.closest('#td-dots i');
    if (dot && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); go($$('#td-dots i').indexOf(dot)); }
  });
  root.addEventListener('resize', function () { layout(); if (tour.on) paintTour(false); });

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
    later(function () { removeCard(c); refreshPanels(); }, 550);
  }
  doc.addEventListener('htmx:afterRequest', function (e) {
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
  });
  // An Undo (any toast): the sections that show the change are asked again.
  doc.addEventListener('kestrel:undone', function () {
    var m = $('#td-matches');
    refreshPanels();
    if (m && root.htmx) {
      S.keep = S.idx;
      root.htmx.ajax('GET', '/today/matches?track=' + encodeURIComponent(track()),
        { target: '#td-matches', swap: 'outerHTML' });
    }
  });

  /* -------------------------------------------------------------- the role summary (Review) */
  function openRole(id, trigger) {
    var dr = $('#td-drawer');
    if (!dr || !root.fetch) return;
    root.fetch('/today/roles/' + encodeURIComponent(id) + '/summary', { credentials: 'same-origin' })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        dr.innerHTML = html;
        if (root.htmx) root.htmx.process(dr);
        if (root.KUI) root.KUI.openSlideOver('#td-drawer', { trigger: trigger });
      });
  }

  /* ------------------------------------------------------------------------ the status strip */
  function initStrip() {
    clearInterval(S.fade);
    var f = $('#td-fader');
    if (!f || f.getAttribute('data-hold') === '1') return;
    var last = $('#td-face-last'), next = $('#td-face-next');
    if (!last || !next || reduced()) return;
    var a = $('.td-ico-a'), b = $('.td-ico-b');
    var showLast = true;
    S.fade = setInterval(function () {
      showLast = !showLast;
      last.classList.toggle('is-hide', !showLast);
      next.classList.toggle('is-hide', showLast);
      last.setAttribute('aria-hidden', showLast ? 'false' : 'true');
      next.setAttribute('aria-hidden', showLast ? 'true' : 'false');
      if (a && b) { a.hidden = !showLast; b.hidden = showLast; }
    }, 4000);
  }

  /* ------------------------------------------------------------------ live: a new match */
  function insertCard(item) {
    var tr = $('#td-track');
    if (!tr || cardOf(item.id)) return;
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
    var ref = $('.td-card', tr);
    var hh = ref ? ref.offsetHeight : 300;
    // Keep the view anchored: the viewport's first visible card stays where it is.
    var anchor = cards()[S.idx];
    el.classList.add('is-grow');
    el.style.width = '0px'; el.style.padding = '16px 0'; el.style.height = hh + 'px';
    el.style.alignSelf = 'flex-start'; el.style.minWidth = '0';
    if (before) tr.insertBefore(el, before); else tr.appendChild(el);
    if (root.htmx) root.htmx.process(el);
    var shift = anchor ? cards().indexOf(anchor) : S.idx;
    var held = S.idx;
    layout();
    S.idx = Math.min(held, maxIdx());
    go(S.idx);  // the new card is still closed: hold the view, then move it as the gap opens
    el.style.width = S.cw + 'px'; balance(el); el.style.width = '0px';
    var open = function () { el.style.width = S.cw + 'px'; el.style.padding = ''; };
    if (reduced()) { open(); } else { requestAnimationFrame(function () { requestAnimationFrame(open); }); }
    later(function () {
      el.classList.remove('is-grow');
      el.style.height = ''; el.style.alignSelf = '';
      el.classList.add('is-arrive');
      S.idx = Math.min(shift, maxIdx());
      go(S.idx);
    }, 460);
  }
  function pollNew() {
    var tr = $('#td-track');
    if (!tr || doc.hidden || !root.fetch) return;
    var have = cards().map(function (c) { return c.getAttribute('data-id'); }).join(',');
    root.fetch('/today/matches/new?track=' + encodeURIComponent(track()) + '&have=' + have,
      { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        (d.new || []).forEach(insertCard);
        var sub = $('#td-sub');
        if (sub && d.found_line && !$('.td-empty', tr)) sub.textContent = d.found_line;
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
    clearInterval(S.poll);
    S.pollMs = ms;
    S.poll = setInterval(pollNew, ms);
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
    if (!last) ft.appendChild(el('span', 'td-tc__cnt', (tour.i + 1) + ' of 6'));
    var dots = el('div', 'td-tdots');
    dots.setAttribute('aria-hidden', 'true');
    for (var k = 0; k < 6; k++) dots.appendChild(el('i', k === tour.i || (last && k === 5) ? 'is-on' : ''));
    ft.appendChild(dots);
    ft.appendChild(el('span', 'td-tc__sp'));
    if (!last) ft.appendChild(button('Skip tour', 'k-btn k-btn--quiet k-btn--sm', 'skip'));
    if (tour.i > 0) ft.appendChild(button('Back', 'k-btn k-btn--sm', 'back'));
    ft.appendChild(button(last ? 'Close' : 'Next', 'k-btn k-btn--primary k-btn--sm', 'next'));
    tc.appendChild(ft);
    var W = root.innerWidth, H = root.innerHeight, CW = 330, CH = tc.offsetHeight, x, y;
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
  doc.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-td-tour]');
    if (!b) return;
    var act = b.getAttribute('data-td-tour');
    if (act === 'next') { if (tour.i === STEPS.length - 1) stopTour(); else toStep(tour.i + 1); }
    else if (act === 'back') toStep(tour.i - 1);
    else stopTour();
  });
  root.KTour = { start: startTour, stop: stopTour };
  doc.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('[data-td-tour-start]')) startTour(0);
  });

  /* ---------------------------------------------------------------------------------- init */
  doc.body.addEventListener('htmx:load', function (e) {
    var n = e.detail && e.detail.elt;
    if (!n || !n.id) return;
    if (n.id === 'td-matches') { initCarousel(); }
    if (n.id === 'td-top') { initStrip(); startPoll(); }
  });
  function boot() {
    if (!$('#today')) return;
    initCarousel();
    initStrip();
    startPoll();
    var host = $('#today');
    if (host.getAttribute('data-tour') === '1') later(function () { startTour(0); }, 600);
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot); else boot();
  if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(function () { if ($('#td-vp')) layout(); });
  root.TodayPage = { layout: layout, go: go, insertCard: insertCard, pollNew: pollNew };
})(typeof window !== 'undefined' ? window : this);
