/* Runs (D-UI-103): the small amount of behavior HTMX does not give the page.
   The poll (#runs-poll, every 5 s and on `run-refresh`) fetches a fragment of regions. A region
   whose signature is unchanged is patched in place: counts, bar widths and tile states update,
   and new events are prepended while the oldest fold out, so nothing re-mounts. A region whose
   signature changed (the run started, paused or finished) swaps its inside with a short fade, and
   the live card glows when a run finishes. Everything else is delegated on the document, so it
   survives a swap. User-facing strings here: none. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  var EVENTS_SHOWN = 10;
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
      setTimeout(gone, 600);
    });
  }

  function patch(oldRegion, newRegion) {
    $$('[data-live]', newRegion).forEach(function (n) {
      var o = $('[data-live="' + n.getAttribute('data-live') + '"]', oldRegion);
      if (!o) return;
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
      region.innerHTML = fresh.innerHTML;
      region.setAttribute('data-sig', fresh.getAttribute('data-sig'));
      if (root.htmx) root.htmx.process(region);
      if (finished) {
        region.classList.remove('k-arrive');
        void region.offsetWidth;
        region.classList.add('k-arrive');
      }
    };
    if (!fade || !inner) { apply(); return; }
    inner.classList.add('is-fading');
    setTimeout(apply, 160);
  }

  function apply(html) {
    var box = doc.createElement('div');
    box.innerHTML = html;
    $$('[data-region]', box).forEach(function (fresh) {
      var name = fresh.getAttribute('data-region');
      var region = $('[data-region="' + name + '"]', doc);
      if (!region) return;
      if (name === 'history' && $('#runs-tl [data-expanded="1"]', region)) return; // keep Show more open
      if (region.getAttribute('data-sig') === fresh.getAttribute('data-sig')) {
        patch(region, fresh);
        return;
      }
      var wasActive = !!$('[data-run="active"], [data-run="paused"]', region);
      var nowIdle = !!$('[data-run="idle"]', fresh);
      swapInside(region, fresh, name === 'live' && wasActive && nowIdle);
    });
  }

  doc.addEventListener('htmx:afterRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (!el || el.id !== 'runs-poll') return;
    var xhr = e.detail.xhr;
    if (!xhr || xhr.status !== 200 || !xhr.responseText) return;
    try { apply(xhr.responseText); } catch (err) { /* a bad fragment never breaks the page */ }
  });

  /* -------------------------------------------------------------------------- the controls */
  function closeMenu() {
    var m = $('#rn-menu');
    if (m) m.hidden = true;
    var b = $('[data-testid="rn-run-menu"]');
    if (b) b.setAttribute('aria-expanded', 'false');
  }
  doc.addEventListener('click', function (e) {
    var t = e.target;
    var caret = t.closest && t.closest('[data-testid="rn-run-menu"]');
    if (caret) {
      var m = $('#rn-menu');
      if (m) {
        m.hidden = !m.hidden;
        caret.setAttribute('aria-expanded', m.hidden ? 'false' : 'true');
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
      $('.rn-acts__main', acts).hidden = !!stop;
      $('.rn-acts__ask', acts).hidden = !stop;
      if (stop) { var no = $('[data-rn-keep]', acts); if (no) no.focus(); }
    }
  });
  doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenu(); });

  /* ------------------------------------------------------------------------ the schedule */
  doc.addEventListener('k-seg-change', function (e) {
    var seg = e.target;
    var form = seg.closest && seg.closest('.rn-sched__form');
    if (!form) return;
    var kind = $('input[name="kind"]', form);
    var wk = $('.rn-weekday', form);
    if (kind && wk) wk.hidden = kind.value !== 'weekly';
  });

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
  doc.addEventListener('change', function (e) {
    if (e.target && e.target.closest && e.target.closest('#rn-choose-dialog')) syncChoose();
  });
  doc.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (t && t.id === 'rn-dialog') {
      var dlg = $('.k-modal', t);
      if (dlg && root.KUI) root.KUI.openModal(dlg);
      var ok = $('#rn-ok', t);
      if (ok && ok.disabled) ok.setAttribute('data-held', '');
      syncChoose();
    }
  });
  // A Needs another try row folds out as soon as Try again is pressed; the poll then redraws.
  doc.addEventListener('htmx:beforeRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (el && el.matches && el.matches('[data-testid="rn-retry"]')) {
      var li = el.closest('li');
      if (li) li.classList.add('is-going');
    }
  });
  doc.addEventListener('htmx:afterRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (!el || !el.matches || !el.matches('[data-testid="rn-retry"]')) return;
    // The role may still be listed (queued but not yet scored): bring it back after a moment.
    setTimeout(function () {
      var li = el.closest && el.closest('li');
      if (li && li.classList.contains('is-going') && doc.contains(li)) li.classList.remove('is-going');
    }, 7000);
  });
  doc.addEventListener('htmx:responseError', function (e) {
    var el = e.detail && e.detail.elt;
    if (el && el.matches && el.matches('[data-testid="rn-retry"]')) {
      var li = el.closest('li');
      if (li) li.classList.remove('is-going');
    }
  });
})(window);
