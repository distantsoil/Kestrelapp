/* Profile (D-UI-21, D-UI-119): the Save bar and its tab-switch nudge, Undo, title chips, the
   document Look, and the document preview as one true-size page that is scaled and never
   re-flowed (D-UI-126). Plain script,
   delegated, so nothing needs re-binding after an htmx swap. The page runs on the lifecycle
   (KUI.page, D-UI-210): this file defines the page "profile" and KProfile.mount, which onboarding
   (the same chips, Save bar and leave guard) mounts inside its own page. Every listener, timer and
   observer starts in mount through the page's context, so leaving stops all of them. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;
  var PAGE_W = 816;      // a Letter page at 96 dpi: the width the document is laid out at
  var PAGE_H = 1056;
  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var cx = null;
  var folding = null;    // Save bars whose saved confirmation is folding away
  var watched = null;    // preview boxes the ResizeObserver below already watches
  var ro = null;
  var frames = [];       // preview frames whose load this visit listens for (bindFrame)

  /* ------------------------------------------------------------ the near-floor hint
     The hint under the margin control is rendered for the saved stop; it follows the control. */
  function onSegChange(e) {
    var box = e.target && e.target.closest ? e.target.closest('.pf-margin') : null;
    if (!box) return;
    var hint = box.querySelector('[data-pf-margin-hint]');
    var text = box.querySelector('[data-pf-margin-text]');
    var hidden = box.querySelector('input[type="hidden"]');
    if (!hint || !text || !hidden) return;
    var what = hint.getAttribute('data-what') || 'pay floor';
    text.textContent = hidden.value === '0'
      ? 'Off: a role whose posted pay tops out below your ' + what + ' is set aside.'
      : 'Roles within ' + hidden.value + '% below your ' + what + ' stay in, marked "Just under your pay floor".';
  }

  // The switch: off posts Exact (0) and folds the stops away; on opens them at 5.
  function onMarginSwitch(e) {
    var sw = e.target && e.target.closest ? e.target.closest('[data-pf-margin-on]') : null;
    if (!sw) return;
    var box = sw.closest('[data-pf-margin]');
    var hidden = box && box.querySelector('[data-pf-margin-value]');
    var fold = box && box.querySelector('[data-pf-margin-fold]');
    if (!hidden || !fold) return;
    var stops = box.querySelectorAll('[data-k-seg] button');
    var pick = sw.checked ? (hidden.dataset.last || '5') : '0';
    if (!sw.checked && hidden.value !== '0') hidden.dataset.last = hidden.value;
    hidden.value = pick;
    Array.prototype.forEach.call(stops, function (b) {
      b.classList.toggle('is-on', b.value === pick);
      b.setAttribute('aria-pressed', b.value === pick ? 'true' : 'false');
      if (sw.checked) b.removeAttribute('tabindex'); else b.setAttribute('tabindex', '-1');
    });
    fold.classList.toggle('is-open', sw.checked);
    hidden.dispatchEvent(new Event('change', { bubbles: true }));
    box.querySelector('[data-k-seg]').dispatchEvent(new CustomEvent('k-seg-change', { bubbles: true }));
  }

  /* ------------------------------------------------------------------ the Save bar */
  function dirtyForms() { return doc.querySelectorAll('form[data-pf-form][data-dirty]'); }

  // A saved confirmation folds away by itself; an edit before then turns it back into the bar.
  function foldSaved(scope) {
    Array.prototype.forEach.call((scope || doc).querySelectorAll('[data-pf-savebar].is-saved'),
      function (bar) {
        if (folding.has(bar)) return;
        folding.add(bar);
        cx.after(3500, function () {
          folding.delete(bar);
          if (!bar.classList.contains('is-saved')) return;
          bar.removeAttribute('data-shown');
          cx.after(400, function () { bar.classList.remove('is-saved'); });
        });
      });
  }
  function onSettleFold(e) { foldSaved(e.target); }

  function markDirty(form) {
    var sb = form.querySelector('[data-pf-savebar].is-saved');
    if (sb) { sb.classList.remove('is-saved'); sb.removeAttribute('data-shown'); }
    if (form.hasAttribute('data-dirty')) return;
    form.setAttribute('data-dirty', '');
    var msg = form.querySelector('[data-pf-msg]');
    if (msg) msg.textContent = 'Unsaved changes';
  }

  // A control that saves on its own (the photo, the privacy card) or only helps type a chip
  // never makes the profile form look unsaved.
  function formOf(el) {
    if (!el || !el.closest || el.closest('[data-pf-nodirty], [data-pf-chip-input]')) return null;
    return el.closest('form[data-pf-form]');
  }
  function onEdit(e) {  // input and change
    var form = formOf(e.target);
    if (form) markDirty(form);
  }

  function nudge(form, leaving) {
    var bar = form.querySelector('[data-pf-savebar]');
    var msg = form.querySelector('[data-pf-msg]');
    if (msg) {
      msg.textContent = leaving ? 'Save or discard your changes before you leave this page.'
        : 'Save or discard your changes before you switch tabs.';
    }
    if (!bar) return;
    bar.classList.remove('is-nudge');
    void bar.offsetWidth;   // restart the animation
    bar.classList.add('is-nudge');
    cx.after(700, function () { bar.classList.remove('is-nudge'); });
  }

  function onSaveRefused(e) {
    var form = e.target.closest && e.target.closest('form[data-pf-form]');
    if (!form) return;
    markDirty(form);
    var msg = form.querySelector('[data-pf-msg]');
    if (!msg) return;
    var problem = doc.createElement('span');
    problem.className = 'pf-problem';
    problem.setAttribute('role', 'alert');
    problem.setAttribute('data-testid', 'profile-problem');
    problem.textContent = 'Not saved: ' + e.detail.message;
    msg.replaceChildren(problem);
  }

  function onTitleAccepted(e) {
    var box = doc.querySelector('[data-pf-chips="target_titles"]');
    if (!box || !e.detail.title) return;
    var form = box.closest('form[data-pf-form]');
    var dirty = form && form.hasAttribute('data-dirty');
    addChip(box, e.detail.title);
    // Accept already persisted this title. Other draft edits still need Save.
    if (form && !dirty) form.removeAttribute('data-dirty');
  }

  /* ----------------------------------------------------- the Suggest titles box
     The button opens the suggestions inside the Titles you want card. The open state lives on
     the wrapper, which htmx never swaps, so the 5 second refresh cannot close it. */
  function onSuggToggle(e) {
    var btn = e.target.closest ? e.target.closest('[data-pf-sugg-toggle]') : null;
    if (!btn) return;
    var wrap = btn.closest('[data-pf-sugg]');
    if (!wrap) return;
    var open = !wrap.classList.contains('is-open');
    setSugg(wrap, open);
    if (!open) wrap.setAttribute('data-pf-sugg-closed', '');  // the person closed it
  }
  function setSugg(wrap, open) {
    wrap.classList.toggle('is-open', open);
    var b = wrap.querySelector('[data-pf-sugg-toggle]');
    if (b) b.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  // Suggestions waiting for an answer (or a request in flight, or the ask-first panel) open the
  // box on their own, so a finished background request is seen, unless the person closed it.
  function onSettleSugg() {
    var wrap = doc.querySelector('[data-pf-sugg]');
    if (!wrap || wrap.hasAttribute('data-pf-sugg-closed') || wrap.classList.contains('is-open')) return;
    if (wrap.querySelector('[data-testid="title-accept"]')) {
      setSugg(wrap, true);
    }
  }

  /* ------------------------------------------------------------- the document preview */
  /* Pagination. The document is laid out as one tall column; the PDF breaks it into Letter pages
     with a margin on each. Here the same breaks are drawn: a unit that would cross the bottom
     margin (or leave a heading alone) moves to the top of the next page, using a spacer, and a
     gap is drawn between sheets. Only spacing changes; no text is touched. document.html asks
     the PDF for the same breaks (break-inside: avoid on a bullet, break-after: avoid on a
     heading and a role header). */
  var UNITS = 'header.doc, h2.section-heading, .role .row, .role .context, ul.bullets > li, ' +
              '.summary, .skills .group, .entry';
  var KEEP_NEXT = 'h2.section-heading, .role .row';

  // A unit still crossing a page edge after a pass (a font or image that changed the layout
  // mid-pass): paginate again, at most twice (owner round 13: a bullet split across pages).
  function straddles(frame) {
    var d = frame.contentDocument, win = frame.contentWindow;
    var m = parseFloat(win.getComputedStyle(d.body).paddingTop) || 48;
    return Array.prototype.some.call(d.querySelectorAll(UNITS), function (el) {
      var r = el.getBoundingClientRect();
      var pg = Math.floor(Math.max(0, r.top) / PAGE_H);
      return r.bottom > (pg + 1) * PAGE_H - m + 1 && r.bottom - r.top <= PAGE_H - 2 * m;
    });
  }
  function paginate(page, frame, again) {
    var d = null;
    try { d = frame.contentDocument; } catch (err) { d = null; }
    if (!d || !d.body) return;
    var win = frame.contentWindow;
    // The frame is one page tall while it measures; a scrollbar inside it (Windows draws a solid
    // one) would narrow the text, so the lines would re-wrap wider once the frame grows and the
    // breaks would land mid-bullet (owner round 13). The page never scrolls inside the frame.
    d.documentElement.style.overflow = 'hidden';
    Array.prototype.forEach.call(d.querySelectorAll('[data-pf-spacer]'), function (n) { n.remove(); });
    frame.style.height = PAGE_H + 'px';
    var m = parseFloat(win.getComputedStyle(d.body).paddingTop) || 48;
    var units = Array.prototype.slice.call(d.querySelectorAll(UNITS));
    var gap = function (i) { return units[i].getBoundingClientRect(); };
    var end = 0;
    for (var i = 0; i < units.length; i++) {
      var el = units[i];
      var r = gap(i);
      var top = r.top, bottom = r.bottom;
      var j = i;
      while (j + 1 < units.length && units[j].matches(KEEP_NEXT)) {   // keep with the next unit
        j++;
        bottom = Math.max(bottom, gap(j).bottom);
      }
      var pg = Math.floor(Math.max(0, top) / PAGE_H);
      var limit = (pg + 1) * PAGE_H - m;
      if (bottom > limit && bottom - top <= PAGE_H - 2 * m) {
        var target = (pg + 1) * PAGE_H + m;
        var sp = d.createElement('div');
        sp.setAttribute('data-pf-spacer', '');
        sp.style.height = Math.max(0, target - top) + 'px';
        el.parentNode.insertBefore(sp, el);
        var off = el.getBoundingClientRect().top - target;   // a margin the spacer did not absorb
        if (Math.abs(off) > 0.5) sp.style.height = Math.max(0, target - top - off) + 'px';
      }
      end = Math.max(end, gap(i).bottom);
    }
    var pages = Math.max(1, Math.ceil((end + m) / PAGE_H));
    frame.style.height = pages * PAGE_H + 'px';
    frame.__pfPages = pages;
    var scale = page.querySelector('[data-pf-scale]');
    Array.prototype.forEach.call(scale.querySelectorAll('.pf-page__gap'), function (n) { n.remove(); });
    for (var k = 1; k < pages; k++) {
      var band = doc.createElement('div');
      band.className = 'pf-page__gap';
      band.setAttribute('aria-hidden', 'true');
      band.style.top = k * PAGE_H + 'px';
      band.textContent = 'PAGE ' + (k + 1);
      scale.appendChild(band);
    }
    page.setAttribute('data-pf-pages', String(pages));
    if ((again || 0) < 2 && straddles(frame)) paginate(page, frame, (again || 0) + 1);
  }

  function fit(page) {
    if (!page) return;
    var scale = page.querySelector('[data-pf-scale]');
    var frame = page.querySelector('iframe');
    if (!scale || !frame) return;
    var width = page.clientWidth || PAGE_W;
    var k = Math.min(1, width / PAGE_W);
    var h = frame.__pfPages ? frame.__pfPages * PAGE_H : PAGE_H;
    if (!frame.__pfPages) {
      try {
        var d = frame.contentDocument;
        if (d && d.documentElement) h = Math.max(PAGE_H, d.documentElement.scrollHeight);
      } catch (err) { /* a frame that is not readable keeps one page */ }
    }
    frame.style.height = h + 'px';
    var off = Math.max(0, (width - PAGE_W * k) / 2);   // centered when there is room
    scale.style.transform = 'translateX(' + off + 'px) scale(' + k + ')';
    page.style.height = Math.ceil(h * k) + 'px';
    page.setAttribute('data-pf-scale-k', k.toFixed(4));
  }

  // Each preview frame this visit listens to, once; a frame that left the page (a refreshed
  // preview, a closed Enlarge) is let go, listener and all.
  function bindFrame(p, frame) {
    frames = frames.filter(function (f) {
      if (f.frame.isConnected) return true;
      f.off();
      return false;
    });
    for (var i = 0; i < frames.length; i++) if (frames[i].frame === frame) return;
    var signal = cx.signal;
    frames.push({ frame: frame, off: cx.on(frame, 'load', function () {
      frame.__pfPages = 0;
      paginate(p, frame);
      fit(p);
      var fonts = null;
      try { fonts = frame.contentDocument.fonts; } catch (err) { fonts = null; }
      if (fonts && fonts.ready) {
        fonts.ready.then(function () { if (!signal.aborted) { paginate(p, frame); fit(p); } });
      }
    }) });
  }
  function fitAll(scope) {
    Array.prototype.forEach.call((scope || doc).querySelectorAll('[data-pf-page]'), function (p) {
      var frame = p.querySelector('iframe');
      if (frame) bindFrame(p, frame);
      if (frame && !frame.__pfPages) {
        try {
          if (frame.contentDocument && frame.contentDocument.readyState === 'complete' &&
              frame.contentDocument.body) paginate(p, frame);
        } catch (err) { /* not readable yet: the load event paginates */ }
      }
      fit(p);
    });
  }

  function enlarge() {
    var src = doc.querySelector('#preview [data-pf-page]');
    var body = doc.getElementById('pf-enlarge-body');
    if (!body) return;
    // The same markup, so the same frame at the same layout width: only the scale differs.
    body.innerHTML = src ? src.outerHTML
      : '<p class="pf-hint">Nothing to show yet. Refresh the preview first.</p>';
    if (root.KUI && root.KUI.openModal) root.KUI.openModal('#pf-enlarge');
    root.requestAnimationFrame(function () { fitAll(body); });
    // The modal is still easing in: fit again once it has its final width.
    cx.after(400, function () { fitAll(body); });
    if (root.ResizeObserver && !watched.has(body)) {
      watched.add(body);
      cx.observe(new root.ResizeObserver(function () { fitAll(body); })).observe(body);
    }
  }

  /* ------------------------------------------------------------------------ title chips
     The chips are the display; a hidden textarea carries one phrase per line, which is what the
     save route reads. Enter, the Add button or a semicolon adds what was typed (commas stay inside a chip, so "Austin, TX" is one place); the x removes a chip. */
  var X_ICON = '<svg class="k-ic k-ic--sm" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  function chipValues(box) {
    return Array.prototype.map.call(box.querySelectorAll('[data-pf-chip] > span'), function (n) {
      return n.textContent;
    });
  }
  function syncChips(box) {
    var area = box.querySelector('[data-pf-chips-value]');
    if (!area) return;
    area.value = chipValues(box).join('\n');
    var form = box.closest('form[data-pf-form]');
    if (form) markDirty(form);
  }
  function addChip(box, raw) {
    var text = String(raw || '').replace(/\s+/g, ' ').trim();
    if (!text) return false;
    var have = chipValues(box).map(function (v) { return v.toLowerCase(); });
    if (have.indexOf(text.toLowerCase()) >= 0) return true;   // already there: nothing to add
    var li = doc.createElement('li');
    li.className = 'pf-chip' + (box.querySelector('.pf-chip--skip') ? ' pf-chip--skip' : '');
    li.setAttribute('data-pf-chip', '');
    var span = doc.createElement('span');
    span.textContent = text;
    var x = doc.createElement('button');
    x.type = 'button';
    x.className = 'pf-chip__x';
    x.setAttribute('data-pf-chip-remove', '');
    x.setAttribute('aria-label', 'Remove ' + text);
    x.innerHTML = X_ICON;
    li.appendChild(span);
    li.appendChild(x);
    var add = box.querySelector('.pf-chipadd');
    add.parentNode.insertBefore(li, add);
    syncChips(box);
    return true;
  }
  // One or more chips from typed or pasted text: a semicolon or a line break ends a chip.
  function addChips(box, raw) {
    var ok = false;
    String(raw || '').split(/[;\n]+/).forEach(function (part) { if (addChip(box, part)) ok = true; });
    return ok;
  }
  function onChipRemove(e) {
    var remove = e.target.closest && e.target.closest('form[data-pf-form] [data-pf-chip-remove]');
    if (!remove) return;
    var box = remove.closest('[data-pf-chips]');
    var chip = remove.closest('[data-pf-chip]');
    if (box && chip) { chip.remove(); syncChips(box); }
  }
  function onChipKey(e) {
    var input = e.target.closest && e.target.closest('[data-pf-chip-input]');
    if (!input) return;
    if (e.key === 'Enter' || e.key === ';') {
      e.preventDefault();   // Enter must not submit the profile form
      var box = input.closest('[data-pf-chips]');
      if (box && addChips(box, input.value)) input.value = '';
    } else if (e.key === 'Backspace' && !input.value) {
      var box2 = input.closest('[data-pf-chips]');
      var last = box2 && box2.querySelectorAll('[data-pf-chip]');
      if (last && last.length) { last[last.length - 1].remove(); syncChips(box2); }
    }
  }
  function onChipBlur(e) {
    var input = e.target.closest && e.target.closest('[data-pf-chip-input]');
    if (!input || !input.value.trim()) return;
    var box = input.closest('[data-pf-chips]');
    if (box && addChips(box, input.value)) input.value = '';
  }
  function onChipPaste(e) {
    var input = e.target.closest && e.target.closest('[data-pf-chip-input]');
    var text = input && e.clipboardData && e.clipboardData.getData('text');
    if (!text || !/[;\n]/.test(text)) return;   // plain text pastes into the field as usual
    var box = input.closest('[data-pf-chips]');
    if (!box) return;
    e.preventDefault();
    addChips(box, input.value + text);
    input.value = '';
  }

  /* ---------------------------------------------------------------------- the document Look
     Swatches set the hidden accent (a preset color, or "custom"); the color picker is the
     custom one. The form around them saves on change (POST /documents/{id}/look). */
  function pickAccent(box, value, which) {
    var hidden = box.querySelector('#accent');
    if (!hidden) return;
    hidden.value = value;
    Array.prototype.forEach.call(box.querySelectorAll('.pf-swatch'), function (b) {
      var on = b === which;
      b.classList.toggle('is-on', on);
      if (b.tagName === 'BUTTON') b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    hidden.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function onSwatch(e) {
    var sw = e.target.closest && e.target.closest('[data-pf-swatches] button.pf-swatch');
    if (sw) pickAccent(sw.closest('[data-pf-swatches]'), sw.getAttribute('data-accent') || '', sw);
  }
  // Registered in the capture phase (mount).
  function onCustomAccent(e) {
    var t = e.target;
    if (t && t.id === 'accent_custom') {
      var box = t.closest('[data-pf-swatches]');
      if (box) {
        e.stopPropagation();   // the accent change below saves and redraws once
        pickAccent(box, 'custom', t.closest('.pf-swatch'));
      }
    }
  }

  /* --------------------------------------------------------------------------------- Undo
     A save raises "pf-saved" with the stamp of the file it wrote. Undo sends that stamp back,
     so a toast from an earlier save cannot restore the wrong version, and any failure says so in
     plain words instead of silently reloading. Undo goes back one save. The toast outlives the
     page, so its Undo is never tied to the page's signal. */
  var UNDO_FAILED = 'Could not undo. Your saved profile is unchanged.';
  var STALE = 'Undo is no longer available';

  function say(message) {
    if (root.KUI && root.KUI.toast) root.KUI.toast(message, { tone: 'bad' });
  }
  function undo(detail) {
    var body = new root.URLSearchParams();
    body.set('token', detail.token || '');
    root.fetch(detail.undoUrl || '/profile/undo', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: body.toString()
    }).then(function (r) {
      if (r.ok) { root.location.reload(); return null; }
      return r.text().then(function (t) {
        say(r.status === 409 && t.indexOf(STALE) === 0 ? t : UNDO_FAILED);
      });
    }).catch(function () { say(UNDO_FAILED); });
  }
  function onSaved(e) {
    var d = e.detail || {};
    if (!root.KUI || !root.KUI.toast) return;
    root.KUI.toast(d.message || 'Saved', { undo: function () { undo(d); } });
  }

  /* ------------------------------------------------------------------------------ stories */
  function onStorySaved(e) {
    var el = e.detail && e.detail.elt;
    if (!el || el.id !== 'pf-story-form' || !e.detail.successful) return;
    el.reset();
    if (root.KUI && root.KUI.closeModal) root.KUI.closeModal();
  }

  /* Leaving the page with unsaved changes is held like a tab switch (owner round 13): any link
     to another page (the sidebar, a card, the command palette) nudges the Save bar instead. The
     guard is window.kLeaveGuard while the page is mounted, and released at unmount. */
  function leaveGuard() {
    var dirty = dirtyForms();
    if (!dirty.length) return true;
    nudge(dirty[0], true);
    return false;
  }
  // Registered in the capture phase (mount), ahead of the link's own click.
  function onLeaveClick(e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    if (a.target === '_blank' || a.hasAttribute('download') || a.closest('.pf-tabs, .pf-track')) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || /^(mailto|tel|javascript):/i.test(href)) return;
    var url;
    try { url = new URL(a.href, root.location.href); } catch (err) { return; }
    if (url.origin !== root.location.origin) return;
    if (url.pathname === root.location.pathname && url.search === root.location.search) return;
    if (!leaveGuard()) { e.preventDefault(); e.stopPropagation(); }
  }

  /* ----------------------------------------------------------------------- the clicks */
  // Registered in the capture phase (mount).
  function onClick(e) {
    var t = e.target;
    if (!t.closest) return;
    var tab = t.closest('.pf-tabs a, .pf-track a');
    if (tab) {
      var dirty = dirtyForms();
      if (dirty.length) { e.preventDefault(); e.stopPropagation(); nudge(dirty[0]); }
      return;
    }
    if (t.closest('[data-pf-discard]')) { root.location.reload(); return; }
    if (t.closest('[data-pf-enlarge]')) { enlarge(); }
  }

  function onPreviewSwap(e) {
    var el = e.detail && e.detail.target;
    if (el && (el.id === 'preview' || (el.querySelector && el.querySelector('[data-pf-page]')))) {
      fitAll(el);
    }
  }
  // An edit to the document (a variant, a role, a bullet, the details) refreshes the preview.
  var refreshTimer = null;
  function onDocumentEdit(e) {
    var cfg = e.detail && e.detail.requestConfig;
    var path = (cfg && cfg.path) || '';
    if (!e.detail.successful || !cfg || cfg.verb !== 'post' || path.indexOf('/documents/') !== 0) return;
    if (/\/(preview|accent|look|assess)/.test(path)) return;
    var pv = doc.getElementById('preview');
    if (!pv || !root.htmx) return;
    cx.clear(refreshTimer);
    refreshTimer = cx.after(350, function () { root.htmx.trigger(pv, 'pf-refresh'); });
  }
  // One ResizeObserver per visit fits every preview box it is given (mount makes it).
  function watch() {
    if (!ro) return;
    Array.prototype.forEach.call(doc.querySelectorAll('.pf-preview'), function (n) {
      if (!watched.has(n)) { watched.add(n); ro.observe(n); }
    });
  }
  function onResizeFit() { fitAll(doc); }

  /* ------------------------------------------------------------------ tab ink and pane entry
     Each tab is its own page, so the ink under the active tab starts where it was on the page the
     person left (kept for a moment in sessionStorage) and slides to its own tab, as the mockup's
     ink does. The tab's body rises in with the shared pane entrance. */
  var INK_KEY = 'pfInk';
  function tabsInk() {
    var tabs = doc.querySelector('.pf-tabs');
    var KUI = root.KUI;
    if (!tabs || !KUI) return;
    var on = tabs.querySelector('a.active');
    if (!on) return;
    var prev = null;
    try {
      prev = JSON.parse(root.sessionStorage.getItem(INK_KEY) || 'null');
      root.sessionStorage.removeItem(INK_KEY);
    } catch (e) { prev = null; }
    KUI.ink(tabs);
    var bar = tabs.querySelector('.k-tabink');
    if (bar && prev && Date.now() - prev.t < 4000 && prev.left !== on.offsetLeft) {
      var left = on.offsetLeft, width = on.offsetWidth;
      bar.style.transition = 'none';
      bar.style.left = prev.left + 'px';
      bar.style.width = prev.width + 'px';
      void bar.offsetWidth;
      bar.style.transition = '';
      root.requestAnimationFrame(function () {
        bar.style.left = left + 'px';
        bar.style.width = width + 'px';
      });
    }
    KUI.paneIn(doc.querySelector('.k-layout__main'));
  }
  function onPageHide() {
    var on = doc.querySelector('.pf-tabs a.active');
    if (!on) return;
    try {
      root.sessionStorage.setItem(INK_KEY, JSON.stringify(
        { left: on.offsetLeft, width: on.offsetWidth, t: Date.now() }));
    } catch (e) { /* private window: the ink just appears under the new tab */ }
  }
  function onResizeInk() {
    var tabs = doc.querySelector('.pf-tabs');
    if (tabs && root.KUI) root.KUI.ink(tabs);
  }

  /* The pay card's floor and target marks follow the two numbers, as the mockup's pay bar does. */
  function payviz() {
    var box = doc.querySelector('[data-pf-payviz]');
    var fl = doc.getElementById('pe-cfloor'), tg = doc.getElementById('pe-ctarget');
    if (!box || !fl || !tg) return;
    var lo = 40000, hi = 220000;
    function pc(v) { return Math.max(2, Math.min(98, (v - lo) / (hi - lo) * 100)); }
    function num(el) { return Number(String(el.value).replace(/[^0-9.]/g, '')) || 0; }
    function set(which, v) {
      box.querySelectorAll('[data-pz="' + which + '"]').forEach(function (n) {
        n.style.left = (v ? pc(v) : 2) + '%';
      });
      box.querySelectorAll('[data-pz="' + which + '"]').forEach(function (n) { n.hidden = !v; });
    }
    // Labels never run off the bar's ends or into each other (owner report 2026-10-26): near
    // an end a label aligns inward, and two close labels align away from each other.
    function place() {
      var f = num(fl) ? pc(num(fl)) : null, t = num(tg) ? pc(num(tg)) : null;
      function shift(which, p, side) {
        var n = box.querySelector('.pf-payviz__lbl[data-pz="' + which + '"]');
        if (!n || p === null) return;
        if (!side) side = p > 88 ? 'left' : (p < 12 ? 'right' : '');
        n.style.transform = side === 'left' ? 'translateX(calc(-100% + 2px))'
          : side === 'right' ? 'translateX(-2px)' : '';
      }
      var close = f !== null && t !== null && Math.abs(t - f) < 14;
      shift('floor', f, close ? (f <= t ? 'left' : 'right') : '');
      shift('target', t, close ? (t >= f ? 'right' : 'left') : '');
    }
    function draw() { set('floor', num(fl)); set('target', num(tg)); place(); }
    cx.on(fl, 'input', draw); cx.on(tg, 'input', draw);
    draw();
  }

  /* ---------------------------------------------------------------------------- the mount
     The Profile page's own start, and the shared one onboarding runs inside its page (it loads
     this file first). The order is the order the listeners were added in before D-UI-210. */
  function mount(main, ctx) {
    cx = ctx;
    folding = new root.WeakSet();
    watched = new root.WeakSet();
    frames = [];
    refreshTimer = null;
    ctx.on(doc, 'k-seg-change', onSegChange);
    ctx.on(doc, 'change', onMarginSwitch);
    ctx.on(doc, 'htmx:afterSettle', onSettleFold);
    foldSaved();
    ctx.on(doc, 'input', onEdit);
    ctx.on(doc, 'change', onEdit);
    ctx.on(doc, 'pf-save-refused', onSaveRefused);
    ctx.on(doc, 'pf-title-accepted', onTitleAccepted);
    ctx.on(doc, 'click', onSuggToggle);
    ctx.on(doc, 'htmx:afterSettle', onSettleSugg);
    ctx.on(doc, 'click', onChipRemove);
    ctx.on(doc, 'keydown', onChipKey);
    ctx.on(doc, 'focusout', onChipBlur);
    ctx.on(doc, 'paste', onChipPaste);
    ctx.on(doc, 'click', onSwatch);
    ctx.on(doc, 'change', onCustomAccent, true);
    ctx.on(doc, 'pf-saved', onSaved);
    ctx.on(doc, 'htmx:afterRequest', onStorySaved);
    root.kLeaveGuard = leaveGuard;
    ctx.onUnmount(function () { if (root.kLeaveGuard === leaveGuard) delete root.kLeaveGuard; });
    ctx.on(doc, 'click', onLeaveClick, true);
    ctx.on(doc, 'click', onClick, true);
    ctx.on(doc, 'htmx:afterSwap', onPreviewSwap);
    ctx.on(doc, 'htmx:afterRequest', onDocumentEdit);
    ro = root.ResizeObserver ? ctx.observe(new root.ResizeObserver(onResizeFit)) : null;
    if (ro) { ctx.on(doc, 'htmx:load', watch); watch(); }
    ctx.on(root, 'resize', onResizeFit);
    ctx.on(root, 'pagehide', onPageHide);
    ctx.on(root, 'resize', onResizeInk);
    // The ink under the active tab is there from the start, as the locked mockup draws it
    // (before D-UI-210 the start ran ahead of kestrel-ui.js and missed it until a resize).
    tabsInk();
    // Measured again once the interface font is in (font-display: block on a page load).
    if (doc.fonts && doc.fonts.ready) {
      doc.fonts.ready.then(function () {
        var tabs = doc.querySelector('.pf-tabs');
        if (!ctx.signal.aborted && tabs && root.KUI) root.KUI.ink(tabs);
      });
    }
    payviz();
  }
  root.KProfile = { mount: mount };
  root.KUI.page.define('profile', { mount: mount });
})(window);
