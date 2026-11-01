/* Runs (D-UI-103): the small amount of behavior HTMX does not give the page.
   The poll (#runs-poll, every 5 s and on `run-refresh`) fetches a fragment of regions. A region
   whose signature is unchanged is patched in place: counts, bar widths and tile states update,
   and new events are prepended while the oldest fold out, so nothing re-mounts. A region whose
   signature changed (the run started, paused or finished) swaps its inside with a short fade, and
   the live card glows when a run finishes. Everything else is delegated on the document, so it
   survives a swap. The page runs on the lifecycle (KUI.page, D-UI-210): this file only defines
   it, and mount starts every listener and timer through its context, so leaving stops all of
   them and coming back starts one set. User-facing strings here: none. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  var EVENTS_SHOWN = 6;
  var stopPending = false;  // the Stop request is out and unanswered
  var cx = null;  // the mounted page's context (KUI.page), set in mount
  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function reduced() {
    return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  /* ------------------------------------------------------------------ patching in place */
  function patchFeed(oldList, newList) {
    var have = {};
    $$('li', oldList).forEach(function (li) { have[li.getAttribute('data-k')] = li; });
    var fresh = $$('li', newList).filter(function (li) { return !have[li.getAttribute('data-k')]; });
    // The placeholder row goes once real events exist.
    if (have.none && $$('li', newList).some(function (li) { return li.getAttribute('data-k') !== 'none'; })) {
      have.none.remove();
    }
    // Newest first: the fresh rows are already in that order, so insert them in reverse.
    fresh.reverse().forEach(function (li) {
      var node = doc.importNode(li, true);
      if (!reduced()) {
        node.classList.add('is-in');
        node.addEventListener('animationend', function () { node.classList.remove('is-in'); }, { once: true });
      }
      oldList.insertBefore(node, oldList.firstChild);
    });
    var rows = $$('li', oldList).filter(function (li) { return !li.classList.contains('is-out'); });
    rows.slice(EVENTS_SHOWN).forEach(function (old) {
      var gone = function () { if (old.parentNode) old.parentNode.removeChild(old); };
      if (reduced()) { gone(); return; }
      old.classList.add('is-out');
      old.addEventListener('animationend', gone, { once: true });
      cx.after(600, gone);
    });
  }

  function patch(oldRegion, newRegion) {
    $$('[data-live]', newRegion).forEach(function (n) {
      var o = $('[data-live="' + n.getAttribute('data-live') + '"]', oldRegion);
      if (!o) return;
      // Stop was pressed and the server has not said so yet: the card keeps its own words.
      if (n.getAttribute('data-live') === 'step' && o.closest('[data-stopping]') &&
          !$('[data-stopping]', newRegion)) return;
      if (o.innerHTML !== n.innerHTML) o.innerHTML = n.innerHTML;
      if (o.className !== n.className) o.className = n.className;
      var tid = n.getAttribute('data-testid');
      if (tid && o.getAttribute('data-testid') !== tid) o.setAttribute('data-testid', tid);
    });
    $$('[data-live-key]', newRegion).forEach(function (n) {
      var o = $('[data-live-key="' + n.getAttribute('data-live-key') + '"]', oldRegion);
      if (!o) return;
      if (o.getAttribute('data-state') !== n.getAttribute('data-state')) {
        o.setAttribute('data-state', n.getAttribute('data-state'));
      }
      var ob = $$('.rn-bar i', o), nb = $$('.rn-bar i', n);
      nb.forEach(function (bar, i) {
        if (ob[i] && ob[i].style.width !== bar.style.width) ob[i].style.width = bar.style.width;
      });
      var ol = $('.rn-bar', o), nl = $('.rn-bar', n);
      if (ol && nl) ol.setAttribute('aria-label', nl.getAttribute('aria-label'));
    });
    var of = $('[data-feed]', oldRegion), nf = $('[data-feed]', newRegion);
    if (of && nf) patchFeed(of, nf);
  }

  function swapInside(region, fresh, finished) {
    var fade = !reduced();
    var inner = region.firstElementChild;
    var apply = function () {
      // The card eases to its new height instead of jumping, so the cards below it glide rather
      // than snap down on every refresh (owner round 11, D-UI-201).
      var from = region.offsetHeight;
      region.innerHTML = fresh.innerHTML;
      region.setAttribute('data-sig', fresh.getAttribute('data-sig'));
      if (root.htmx) root.htmx.process(region);
      if (fade && region.animate) {
        var to = region.offsetHeight;
        if (Math.abs(to - from) > 2) {
          region.animate([{ height: from + 'px', overflow: 'hidden' }, { height: to + 'px', overflow: 'hidden' }],
                         { duration: 260, easing: 'cubic-bezier(.22,.8,.3,1)' });
        }
      }
      if (finished) {
        region.classList.remove('k-arrive');
        void region.offsetWidth;
        region.classList.add('k-arrive');
      }
    };
    // A live refresh swaps in place (a fade on every refresh read as the page reloading); only a
    // run that just ended fades over to its result.
    if (!fade || !inner || !finished) { apply(); return; }
    inner.classList.add('is-fading');
    cx.after(160, apply);
  }

  function apply(html) {
    var box = doc.createElement('div');
    box.innerHTML = html;
    $$('[data-region]', box).forEach(function (fresh) {
      var name = fresh.getAttribute('data-region');
      var region = $('[data-region="' + name + '"]', doc);
      if (!region) return;
      if (name === 'history' && $('#runs-tl [data-expanded="1"]', region)) return; // keep Show more open
      // Stop was pressed and its request is still out: the card keeps saying Stopping, and the
      // answer (or the failure) decides what it says next.
      if (name === 'live' && stopPending) return;
      if (region.getAttribute('data-sig') === fresh.getAttribute('data-sig')) {
        // A step line that wraps differently changes the card's height: it eases, never jumps.
        easeHeight(region, function () { patch(region, fresh); });
        return;
      }
      var wasActive = !!$('[data-run="active"], [data-run="paused"], [data-run="stopping"]', region);
      var nowIdle = !!$('[data-run="idle"]', fresh);
      swapInside(region, fresh, name === 'live' && wasActive && nowIdle);
    });
  }

  function onPollDone(e) {
    var el = e.detail && e.detail.elt;
    if (!el || el.id !== 'runs-poll') return;
    var xhr = e.detail.xhr;
    if (!xhr || xhr.status !== 200 || !xhr.responseText) return;
    try { apply(xhr.responseText); } catch (err) { /* a bad fragment never breaks the page */ }
  }

  /* -------------------------------------------------------------------------- the controls */
  function closeMenu() {
    var m = $('#rn-menu');
    if (m) m.hidden = true;
    var b = $('[data-testid="rn-run-menu"]');
    if (b) b.setAttribute('aria-expanded', 'false');
  }
  function onClick(e) {
    var t = e.target;
    var caret = t.closest && t.closest('[data-testid="rn-run-menu"]');
    if (caret) {
      var m = $('#rn-menu');
      if (m) {
        m.hidden = !m.hidden;
        caret.setAttribute('aria-expanded', m.hidden ? 'false' : 'true');
        // The header band clips what hangs below it (it keeps the scene art inside), so the open
        // menu is placed against the window under the split button (owner report 2026-10-26).
        if (!m.hidden) {
          var r = caret.closest('.rn-split').getBoundingClientRect();
          m.style.position = 'fixed';
          m.style.left = r.left + 'px';
          m.style.top = (r.bottom + 6) + 'px';
        }
        if (!m.hidden) { var first = $('button', m); if (first) first.focus(); }
      }
      return;
    }
    if (t.closest && t.closest('#rn-menu')) { closeMenu(); return; }
    closeMenu();
    // Stop confirms inline: the buttons swap for the question, and back on Keep running.
    var stop = t.closest && t.closest('[data-rn-stop]');
    var keep = t.closest && t.closest('[data-rn-keep]');
    if (stop || keep) {
      var acts = (stop || keep).closest('.rn-acts');
      // The question is taller than the buttons it replaces: the card eases, never jumps.
      easeHeight((stop || keep).closest('.rn-live-in') || acts, function () {
        $('.rn-acts__main', acts).hidden = !!stop;
        $('.rn-acts__ask', acts).hidden = !stop;
      });
      if (stop) { var no = $('[data-rn-keep]', acts); if (no) no.focus(); }
    }
  }
  /* Stop (#91): the press changes the card at once, before the server answers, and a second
     press is swallowed here, so a slow or busy server never looks like a press that did nothing.
     The words come from data attributes on the card. A failed send puts everything back. */
  function stopCard(el) { return el && el.closest ? el.closest('.rn-live-in') : null; }
  // The card eases to its new height when the confirm question folds away, as it does on a swap.
  function easeHeight(card, change) {
    var from = card.offsetHeight;
    change();
    var to = card.offsetHeight;
    if (!reduced() && card.animate && Math.abs(to - from) > 2) {
      card.animate([{ height: from + 'px', overflow: 'hidden' }, { height: to + 'px', overflow: 'hidden' }],
                   { duration: 260, easing: 'cubic-bezier(.22,.8,.3,1)' });
    }
  }
  function enterStopping(yes) {
    var card = stopCard(yes);
    if (!card || card.hasAttribute('data-stopping')) return;
    easeHeight(card, function () { setStopping(yes, card); });
  }
  function setStopping(yes, card) {
    var acts = $('.rn-acts', card), title = $('h3', card), step = $('[data-live="step"]', card);
    card.setAttribute('data-stopping', '');
    stopPending = true;
    card.__kback = { title: title && title.textContent, step: step && step.textContent };
    $('.rn-acts__ask', acts).hidden = true;
    $('.rn-acts__main', acts).hidden = true;
    var span = $('.rn-acts__stop', acts), btn = $('.is-stopping', span);
    span.hidden = false;
    if (btn && !reduced()) {
      btn.classList.add('is-pressing');
      btn.addEventListener('animationend', function () { btn.classList.remove('is-pressing'); }, { once: true });
    }
    yes.disabled = true;
    if (title) title.textContent = card.getAttribute('data-stopping-title');
    if (step) step.textContent = card.getAttribute('data-stopping-step');
  }
  function leaveStopping(yes) {
    var card = stopCard(yes);
    if (!card || !card.hasAttribute('data-stopping') || !card.__kback) return;
    easeHeight(card, function () { unsetStopping(yes, card); });
  }
  function unsetStopping(yes, card) {
    var acts = $('.rn-acts', card), title = $('h3', card), step = $('[data-live="step"]', card);
    card.removeAttribute('data-stopping');
    yes.disabled = false;
    $('.rn-acts__stop', acts).hidden = true;
    $('.rn-acts__main', acts).hidden = false;
    if (title) title.textContent = card.__kback.title;
    if (step) step.textContent = card.__kback.step;
    card.__kback = null;
  }
  function onStopClickCapture(e) {
    var yes = e.target.closest && e.target.closest('[data-testid="run-stop-yes"]');
    var card = stopCard(yes);
    if (card && card.hasAttribute('data-stopping')) { e.preventDefault(); e.stopImmediatePropagation(); }
  }
  function onStopDone(e) {
    var el = e.detail && e.detail.elt;
    if (!el || !el.matches || !el.matches('[data-testid="run-stop-yes"]')) return;
    stopPending = false;
    if (e.detail.successful === false) {
      // The stop did not go through: show what is true now (Paused, Stop pressable again).
      leaveStopping(el);
      if (root.htmx) root.htmx.trigger(doc.body, 'run-refresh');
    }
  }
  function onKeyDown(e) { if (e.key === 'Escape') closeMenu(); }

  /* ------------------------------------------------------------------------ the schedule */
  function onSegChange(e) {
    var seg = e.target;
    var form = seg.closest && seg.closest('.rn-sched__form');
    if (!form) return;
    var kind = $('input[name="kind"]', form);
    var wk = $('.rn-weekday', form);
    if (kind && wk) wk.hidden = kind.value !== 'weekly';
  }

  /* ------------------------------------------------------- the "this run only" sheet */
  function syncChoose() {
    var dlg = $('#rn-choose-dialog');
    if (!dlg) return;
    var online = $('input[name="mode"]:checked', dlg);
    var isOnline = !!online && (online.value === 'hosted' || online.value === 'both');
    $$('[data-sends]', dlg).forEach(function (ul) {
      ul.hidden = !(online && ul.getAttribute('data-sends') === online.value);
    });
    var ask = $('#rn-ask', dlg), ok = $('#rn-ok', dlg), go = $('#rn-start', dlg);
    if (ask) ask.hidden = !isOnline;
    if (ok) ok.disabled = !isOnline || ok.hasAttribute('data-held');
    if (go) go.disabled = isOnline && !(ok && ok.checked);
  }
  function onChange(e) {
    if (e.target && e.target.closest && e.target.closest('#rn-choose-dialog')) syncChoose();
  }
  function onAfterSwap(e) {
    var t = e.target;
    if (t && t.id === 'rn-dialog') {
      var dlg = $('.k-modal', t);
      if (dlg && root.KUI) root.KUI.openModal(dlg);
      var ok = $('#rn-ok', t);
      if (ok && ok.disabled) ok.setAttribute('data-held', '');
      syncChoose();
    }
  }
  // A Needs another try row folds out as soon as Try again is pressed; the poll then redraws.
  function onBeforeRequest(e) {
    var el = e.detail && e.detail.elt;
    if (el && el.matches && el.matches('[data-testid="run-stop-yes"]')) enterStopping(el);
    if (el && el.matches && el.matches('[data-testid="rn-retry"]')) {
      var li = el.closest('li');
      if (li) li.classList.add('is-going');
    }
  }
  function onRetryDone(e) {
    var el = e.detail && e.detail.elt;
    if (!el || !el.matches || !el.matches('[data-testid="rn-retry"]')) return;
    // The role may still be listed (queued but not yet scored): bring it back after a moment.
    cx.after(7000, function () {
      var li = el.closest && el.closest('li');
      if (li && li.classList.contains('is-going') && doc.contains(li)) li.classList.remove('is-going');
    });
  }
  function onResponseError(e) {
    var el = e.detail && e.detail.elt;
    if (el && el.matches && el.matches('[data-testid="rn-retry"]')) {
      var li = el.closest('li');
      if (li) li.classList.remove('is-going');
    }
  }

  root.KUI.page.define('runs', {
    mount: function (main, ctx) {
      cx = ctx;
      stopPending = false;
      ctx.on(doc, 'htmx:afterRequest', onPollDone);
      ctx.on(root, 'scroll', closeMenu, true);
      ctx.on(root, 'resize', closeMenu);
      ctx.on(doc, 'click', onClick);
      ctx.on(doc, 'keydown', onKeyDown);
      ctx.on(doc, 'k-seg-change', onSegChange);
      ctx.on(doc, 'change', onChange);
      ctx.on(doc, 'htmx:afterSwap', onAfterSwap);
      ctx.on(doc, 'htmx:beforeRequest', onBeforeRequest);
      ctx.on(doc, 'htmx:afterRequest', onRetryDone);
      ctx.on(doc, 'htmx:afterRequest', onStopDone);
      ctx.on(doc, 'click', onStopClickCapture, true);
      ctx.on(doc, 'htmx:responseError', onResponseError);
    },
    // Runs before the listeners are dropped: the split button's menu closes with the page.
    unmount: closeMenu
  });
})(window);
