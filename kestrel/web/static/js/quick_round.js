/* The quick round (issue 171, D-UI-272). One card at a time: Left Not for me, Right Yes, Up Top
   pick, Down Not sure, or drag the card (left, right or up). Every answer but Not sure is saved
   at once as a local label (POST /calibrate/quick/answer); Undo (the button, Ctrl or Cmd+Z) removes
   the last saved one. Nothing here reads or shows Kestrel's score, and nothing leaves the
   computer. Under Reduce motion (the app's switch or the OS setting) the cards do not fly or follow
   the pointer; the buttons and keys work the same. The page runs on the lifecycle (KUI.page, D-UI-210). */
(function () {
  'use strict';
  if (!window.KUI || !window.KUI.page) return;
  var doc = document;
  var FLY_MS = 260;
  var THRESHOLD = 110;
  var WORDS = { yes: 'Yes', no: 'Not for me', top: 'Top pick', unsure: 'Not sure' };

  var cx = null;
  var S = null;  // per visit: cards, idx, hist, seen, busy

  /* The app's own Reduce motion switch (Settings, data-reduce-motion="1" on <html>) as well as
     the OS setting (D-UI-321). */
  function reduced() {
    var h = doc.documentElement;
    if (h && h.getAttribute('data-reduce-motion') === '1') return true;
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function $(sel, root) { return (root || doc).querySelector(sel); }
  function page() { return $('#qr'); }
  function track() { var p = page(); return p ? p.getAttribute('data-track') : ''; }
  function q(url) { return url + (url.indexOf('?') < 0 ? '?' : '&') + 'track=' + encodeURIComponent(track()); }
  function toast(msg, opts) { if (window.KUI && window.KUI.toast) window.KUI.toast(msg, opts); }
  function roundOn() { var r = $('#qr-round'); return !!r && !r.hidden; }

  function cards() { return Array.prototype.slice.call($('#qr-stage').querySelectorAll('.qr-card')); }

  /* ------------------------------------------------------------------ drawing the deck */
  function layout() {
    S.cards.forEach(function (c, k) {
      c.classList.remove('is-front', 'is-n1', 'is-n2', 'is-gone', 'is-drag');
      c.style.transform = ''; c.style.opacity = '';
      c.removeAttribute('aria-hidden');
      c.setAttribute('tabindex', '-1');
      var s = c.querySelectorAll('.qr-stamp');
      for (var i = 0; i < s.length; i++) s[i].style.opacity = '';
      if (k < S.idx) { c.classList.add('is-gone'); c.setAttribute('aria-hidden', 'true'); }
      else if (k === S.idx) c.classList.add('is-front');
      else if (k === S.idx + 1) { c.classList.add('is-n1'); c.setAttribute('aria-hidden', 'true'); }
      else if (k === S.idx + 2) { c.classList.add('is-n2'); c.setAttribute('aria-hidden', 'true'); }
      else c.setAttribute('aria-hidden', 'true');
    });
    var front = S.cards[S.idx];
    if (front) front.setAttribute('tabindex', '0');
    drawPips();
  }

  /* The pips and the count only: safe while a card is flying out. */
  function drawPips() {
    var N = S.cards.length;
    var pips = $('#qr-pips');
    pips.style.setProperty('--n', String(N));
    pips.innerHTML = '';
    for (var k = 0; k < N; k++) {
      var i = doc.createElement('i');
      var h = S.hist[k];
      if (h && !h.failed) i.className = 'is-' + h.answer;
      else if (k === S.idx) i.className = 'is-cur';
      pips.appendChild(i);
    }
    $('#qr-count').textContent = Math.min(S.idx + 1, N) + ' of ' + N;
    $('#qr-undo').disabled = S.hist.length === 0;
  }

  function stamps(c, x, y) {
    var t = c.querySelector('.qr-stamp--t'), yy = c.querySelector('.qr-stamp--y'), n = c.querySelector('.qr-stamp--n');
    if (t) t.style.opacity = Math.max(0, Math.min(1, -y / 110));
    if (yy) yy.style.opacity = Math.max(0, Math.min(1, x / 110));
    if (n) n.style.opacity = Math.max(0, Math.min(1, -x / 110));
  }

  /* ------------------------------------------------------------------------ the server */
  function post(url, params) {
    return window.fetch(q(url), {
      method: 'POST', credentials: 'same-origin', signal: cx && cx.signal,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new window.URLSearchParams(params || {}).toString()
    }).then(function (r) {
      if (!r.ok) throw new Error('request failed');
      return r.json();
    });
  }
  function saveAnswer(opp, answer) {
    return post('/calibrate/quick/answer', { opportunity_id: opp, answer: answer });
  }
  function fromHtml(text) {
    return new window.DOMParser().parseFromString(text, 'text/html');
  }
  function fetchPartial(url) {
    return window.fetch(q(url), { credentials: 'same-origin', signal: cx && cx.signal })
      .then(function (r) { if (!r.ok) throw new Error('request failed'); return r.text(); })
      .then(fromHtml);
  }

  /* ------------------------------------------------------------------------- answering */
  function say(msg) { var l = $('#qr-live'); if (l) l.textContent = msg; }

  function commit(answer) {
    if (!S || S.busy || !roundOn() || S.idx >= S.cards.length) return;
    var c = S.cards[S.idx];
    var opp = c.getAttribute('data-opp');
    S.busy = true;
    var rec = { opp: opp, answer: answer, card: c, promise: null, failed: false, flying: false };
    rec.promise = saveAnswer(opp, answer).then(function (res) {
      rec.labelId = res.label_id; rec.offer = res.offer; return res;
    }).catch(function () {
      rec.failed = true;
      if (!S || (cx && cx.signal.aborted)) return null;
      toast('That answer was not saved, so nothing changed. Try again.', { tone: 'warn' });
      var seenAt = S.seen.lastIndexOf(rec.opp);
      if (seenAt >= 0) S.seen.splice(seenAt, 1);
      if (rec.flying) {
        /* Still flying out: the card has not been passed yet. Cancel that step and bring the
           same card back; busy stays set until it is in place, so no answer lands meanwhile. */
        rec.flying = false;
        S.hist.pop();
        settle(S.cards[S.idx]);
      } else if (S.hist[S.hist.length - 1] === rec) stepBack();
      /* An earlier card failed after later ones were answered: its record stays where it is,
         marked failed, so history position k is still card k (pips, Undo). Undo over it only
         steps back to that card; nothing on the server is touched (D-UI-321). */
      else drawPips();
      return null;
    });
    S.hist.push(rec);
    S.seen.push(opp);
    say(WORDS[answer] + '. ' + Math.min(S.idx + 1, S.cards.length) + ' of ' + S.cards.length + '.');
    if (reduced()) { c.classList.add('is-gone'); advance(); return; }
    c.classList.remove('is-drag');
    if (answer === 'top') {
      stamps(c, 0, -120); c.style.transform = 'translateY(-560px) rotate(-4deg)';
    } else if (answer === 'yes' || answer === 'no') {
      var x = answer === 'yes' ? 1 : -1;
      stamps(c, x * 120, 0); c.style.transform = 'translate(' + (x * 640) + 'px,40px) rotate(' + (x * 22) + 'deg)';
    } else {
      c.style.transform = 'translateY(70px) scale(.9)';
    }
    c.style.opacity = '0';
    rec.flying = true;
    cx.after(FLY_MS, function () {
      if (!rec.flying) return;  // the save failed mid-flight and the card came back
      rec.flying = false;
      advance();
    });
  }

  function advance() {
    if (!S) return;
    S.idx += 1;
    S.busy = false;
    if (S.idx >= S.cards.length) { finish(); return; }
    layout();
  }

  /* Draw the deck with card c in front again, without animating it in from where it flew. */
  function settle(c) {
    if (c) { c.style.transition = 'none'; }
    layout();
    if (c) { void c.offsetWidth; c.style.transition = ''; }
    S.busy = false;
  }

  /* One step back, after a failed save or an Undo. */
  function stepBack() {
    S.hist.pop();
    S.idx = Math.max(0, S.idx - 1);
    settle(S.cards[S.idx]);
  }

  /* The offer as it was before this round: sent with the Undo of the round's only remaining
     saved answer, so the strip is not left snoozed by answers that are all gone. */
  function offerToRestore(rec) {
    var others = S.hist.some(function (h) { return h !== rec && h.labelId && !h.failed; });
    return others ? null : rec.offer;
  }

  function undo() {
    if (!S || S.busy || !roundOn() || !S.hist.length) return;
    var rec = S.hist[S.hist.length - 1];
    S.busy = true;
    Promise.resolve(rec.promise).then(function () {
      if (rec.failed) {  // never saved: step back to its card, nothing to remove
        stepBack();
        say('Back to that role. ' + (S.idx + 1) + ' of ' + S.cards.length + '.');
        return;
      }
      var gone = rec.labelId
        ? post('/calibrate/quick/undo/' + rec.labelId, offerToRestore(rec) || {})
        : Promise.resolve(null);
      return gone.then(function () {
        var at = S.seen.lastIndexOf(rec.opp);
        if (at >= 0) S.seen.splice(at, 1);
        stepBack();
        say('Undone. ' + (S.idx + 1) + ' of ' + S.cards.length + '.');
      });
    }).catch(function () {
      S.busy = false;
      toast('Could not undo. Nothing changed.', { tone: 'warn' });
    });
  }

  /* ----------------------------------------------------------------------- the done screen */
  function tally() {
    var t = { yes: 0, top: 0, no: 0, unsure: 0 };
    S.hist.forEach(function (h) { if (!h.failed) t[h.answer] += 1; });
    return t;
  }
  function show(el, on) { if (el) el.hidden = !on; }

  function finish() {
    if (!S) return;
    S.busy = true;
    var t = tally();
    var all = S.hist.map(function (h) { return h.promise; });
    Promise.all(all).then(function () {
      var qs = 'yes=' + t.yes + '&top=' + t.top + '&no=' + t.no + '&unsure=' + t.unsure +
        '&seen=' + S.seen.join(',');
      return fetchPartial('/calibrate/quick/done?' + qs);
    }).then(function (d) {
      var box = $('#qr-done');
      box.innerHTML = '';
      var node = d.body.firstElementChild;
      if (node) box.appendChild(doc.importNode(node, true));
      show($('#qr-round'), false);
      show(box, true);
      var h = $('#qr-done h2');
      if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); }
    }).catch(function () {
      if (cx && cx.signal.aborted) return;
      S.busy = false;
      toast('Could not show the summary. Your answers are saved.', { tone: 'warn' });
    });
  }

  function keepGoing() {
    if (!S) return;
    var btn = $('#qr-more');
    if (btn) btn.disabled = true;
    fetchPartial('/calibrate/quick/cards?seen=' + S.seen.join(',')).then(function (d) {
      var fresh = Array.prototype.slice.call(d.body.querySelectorAll('.qr-card'));
      if (!fresh.length) {
        if (btn) btn.disabled = false;
        toast('Nothing more to sort right now.');
        return;
      }
      var stage = $('#qr-stage');
      stage.innerHTML = '';
      fresh.forEach(function (c) { stage.appendChild(doc.importNode(c, true)); });
      S.cards = cards(); S.idx = 0; S.hist = []; S.busy = false;
      layout();
      show($('#qr-done'), false);
      show($('#qr-round'), true);
      if (S.cards[0]) S.cards[0].focus({ preventScroll: true });
    }).catch(function () {
      if (btn) btn.disabled = false;
      if (!(cx && cx.signal.aborted)) toast('Could not load more. Try again.', { tone: 'warn' });
    });
  }

  /* --------------------------------------------------------------------------------- events */
  function onClick(e) {
    var t = e.target;
    if (!t.closest) return;
    var map = { 'qr-no': 'no', 'qr-yes': 'yes', 'qr-top': 'top', 'qr-unsure': 'unsure' };
    var b = t.closest('.qr-act');
    if (b && map[b.id]) { commit(map[b.id]); return; }
    if (t.closest('#qr-undo')) { undo(); return; }
    if (t.closest('#qr-finish')) { if (S && !S.busy) finish(); return; }
    if (t.closest('#qr-more')) { keepGoing(); }
  }

  function onKeyDown(e) {
    if (!S || e.defaultPrevented) return;
    var tag = e.target && e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'z' || e.key === 'Z')) {
      if (roundOn()) { e.preventDefault(); undo(); }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || !roundOn()) return;
    var kind = { ArrowLeft: 'no', ArrowRight: 'yes', ArrowUp: 'top', ArrowDown: 'unsure' }[e.key];
    if (!kind) return;
    e.preventDefault();
    if (!e.repeat) commit(kind);
  }

  var drag = null;
  function onPointerDown(e) {
    if (!S || S.busy || reduced() || !roundOn() || (e.button !== undefined && e.button > 0)) return;
    var c = e.target.closest && e.target.closest('.qr-card.is-front');
    if (!c || e.target.closest('a')) return;
    drag = { c: c, sx: e.clientX, sy: e.clientY, dx: 0, dy: 0, id: e.pointerId };
    try { c.setPointerCapture(e.pointerId); } catch (err) { /* a synthetic pointer */ }
    c.classList.add('is-drag');
  }
  function onPointerMove(e) {
    if (!drag || e.pointerId !== drag.id) return;
    drag.dx = e.clientX - drag.sx;
    drag.dy = Math.min(0, e.clientY - drag.sy);
    drag.c.style.transform = 'translate(' + drag.dx + 'px,' + drag.dy + 'px) rotate(' + (drag.dx / 18) + 'deg)';
    stamps(drag.c, drag.dx, Math.abs(drag.dy) > Math.abs(drag.dx) ? drag.dy : 0);
  }
  function onPointerEnd(e) {
    if (!drag || e.pointerId !== drag.id) return;
    var d = drag; drag = null;
    d.c.classList.remove('is-drag');
    if (-d.dy > THRESHOLD && -d.dy > Math.abs(d.dx)) { commit('top'); }
    else if (Math.abs(d.dx) > THRESHOLD) { commit(d.dx > 0 ? 'yes' : 'no'); }
    else { d.c.style.transform = ''; stamps(d.c, 0, 0); }
  }

  window.KUI.page.define('quick-round', {
    mount: function (main, ctx) {
      cx = ctx; drag = null;
      var stage = $('#qr-stage');
      if (!stage) { S = null; return; }
      S = { cards: cards(), idx: 0, hist: [], seen: [], busy: false };
      if (S.cards.length) layout();
      ctx.on(doc, 'click', onClick);
      ctx.on(doc, 'keydown', onKeyDown);
      ctx.on(doc, 'pointerdown', onPointerDown);
      ctx.on(doc, 'pointermove', onPointerMove);
      ctx.on(doc, 'pointerup', onPointerEnd);
      ctx.on(doc, 'pointercancel', onPointerEnd);
      ctx.onUnmount(function () { if (cx === ctx) { cx = null; S = null; drag = null; } });
    }
  });
})();
