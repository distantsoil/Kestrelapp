/* Kestrel UI runtime (UI overhaul, Phase 1). Plain script, no build step, no dependencies.
   Works beside HTMX and Alpine: behavior is bound once, by delegation on the document, so
   nothing needs re-binding after a swap; init(root) only re-runs the parts that need a fresh
   look at new markup (scene bands, overlay centering). It runs again on htmx:load (the swapped content).

   Public API: window.KUI
     setTheme('light'|'dark'|'auto')  setScheme('indigo'|'teal'|'plum'|'graphite')  applyStored()
     toast(message, {undo: fn, undoUrl, timeout, tone, href})   dismissToasts()
     openFlyout(el|selector, {trigger})  closeFlyout()    right side, vertically centered
     openSlideOver(el|selector, {trigger})  closeSlideOver()   modal: focus trapped, page inert
     openModal(el|selector, {trigger})  closeModal()      closeAll()
     center(el)  centerTop(windowHeight, elementHeight)   overlay-centering helpers
     setText(el, text)  foldOut(el, done)  arrive(el)  prepend(list, el)  moveTo(el, list, opts)
     saved(el, {text, tick})  fold(details, open)  ink(tabsEl)  paneIn(el)   motion primitives
     frame.{init, wire, state, closing}   the frameless window's controls and closing screen
     page.{define, start, mount, unmount, current, _stats}   the page lifecycle (D-UI-210)
     go(url, {guarded})  navigate(url, {pop, replace})   in-place page changes (D-UI-210)
   Declarative hooks (data attributes), all delegated:
     data-k-theme="dark"   data-k-scheme="teal"
     data-k-flyout="#id"   data-k-slideover="#id"   data-k-modal="#id"
     data-k-close          closes the overlay it sits in
     data-k-stepper        on .k-stepper with min, max, step; buttons carry data-k-step="-1|1"
   A server can raise a toast with the HX-Trigger header {"k-toast": {"message": "...",
   "undoUrl": "/..."}}.

   User-facing strings here are limited to the toast Undo label and the dismiss label. */
(function (root) {
  'use strict';

  var doc = root.document || null;
  var SCHEMES = ['indigo', 'teal', 'plum', 'graphite', 'contrast'];
  var THEMES = ['light', 'dark', 'auto'];
  var MARGIN = 12;

  /* ------------------------------------------------------------------ small helpers */
  function lsGet(key) { try { return root.localStorage.getItem(key); } catch (e) { return null; } }
  function lsSet(key, value) { try { root.localStorage.setItem(key, value); } catch (e) { /* private window */ } }
  function reduced() {
    return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function el$(ref) {
    if (!ref) return null;
    if (typeof ref === 'string') return doc ? doc.querySelector(ref) : null;
    return ref;
  }
  function focusable(container) {
    return Array.prototype.filter.call(
      container.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
        'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'),
      function (n) { return n.offsetParent !== null || n === doc.activeElement; });
  }

  /* ------------------------------------------------------------------- theme and scheme */
  function setTheme(value) {
    if (THEMES.indexOf(value) < 0) return;
    lsSet('kestrel-theme', value);
    // The desktop window keeps it too, so its next launch paints the right ground first.
    var pv = root.pywebview;
    if (pv && pv.api && typeof pv.api.window_set_theme === 'function') {
      try { pv.api.window_set_theme(value); } catch (e) { /* not the desktop window */ }
    }
    var h = doc.documentElement;
    if (value === 'auto') h.removeAttribute('data-theme'); else h.setAttribute('data-theme', value);
  }
  function setScheme(value) {
    if (SCHEMES.indexOf(value) < 0) return;
    lsSet('kestrel-scheme', value);
    doc.documentElement.setAttribute('data-scheme', value);
  }
  /* This browser's own choice wins over the server-rendered default; the theme is per browser. */
  function applyStored() {
    var t = lsGet('kestrel-theme');
    if (t === 'light' || t === 'dark') doc.documentElement.setAttribute('data-theme', t);
    var s = lsGet('kestrel-scheme');
    if (SCHEMES.indexOf(s) >= 0) doc.documentElement.setAttribute('data-scheme', s);
  }

  /* ------------------------------------------------------------------------- toasts */
  var MAX_TOASTS = 4;
  function toastRegion() {
    var r = doc.getElementById('k-toasts');
    if (!r) {
      r = doc.createElement('div');
      r.id = 'k-toasts';
      r.className = 'k-toasts';
      r.setAttribute('role', 'status');
      r.setAttribute('aria-live', 'polite');
      doc.body.appendChild(r);
    }
    return r;
  }
  function undoRefusal(text) {
    if (!text || text.indexOf('data-undo-msg') < 0 || typeof root.DOMParser !== 'function') return '';
    var m = new root.DOMParser().parseFromString(text, 'text/html').querySelector('[data-undo-msg]');
    return m && m.getAttribute('data-ok') === '0' ? (m.textContent || '').trim() : '';
  }
  function toast(message, opts) {
    opts = opts || {};
    var region = toastRegion();
    var own = Array.prototype.filter.call(region.children, function (c) { return c.classList.contains('k-toast'); });
    while (own.length >= MAX_TOASTS) { var old = own.shift(); old.parentNode.removeChild(old); }
    var node = doc.createElement('div');
    node.className = 'k-toast';
    if (opts.tone) node.setAttribute('data-tone', opts.tone);
    var text = doc.createElement(opts.href ? 'a' : 'span');
    text.textContent = String(message || '');
    if (opts.href) text.setAttribute('href', String(opts.href));
    node.appendChild(text);
    var timer = null;
    var gone = false;
    function dismiss() {
      if (gone) return;
      gone = true;
      clearTimeout(timer);
      node.classList.add('is-leaving');
      setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, reduced() ? 0 : 380);
    }
    var hasUndo = typeof opts.undo === 'function' || !!opts.undoUrl;
    if (hasUndo) {
      var b = doc.createElement('button');
      b.type = 'button';
      b.className = 'k-toast__undo';
      b.textContent = opts.undoLabel || 'Undo';
      b.addEventListener('click', function () {
        if (typeof opts.undo === 'function') opts.undo();
        else if (root.fetch) {
          // Read the answer once: a failed request changes nothing and says so (no kestrel:undone);
          // an answer carrying <span data-undo-msg data-ok="0"> is a refused Undo, said as such.
          root.fetch(opts.undoUrl, { method: 'POST', credentials: 'same-origin' })
            .then(function (r) {
              if (!r.ok) throw new Error('undo failed');
              return r.text();
            })
            .then(function (text) {
              var refused = undoRefusal(text);
              if (refused) toast(refused, { tone: 'warn' });
              doc.dispatchEvent(new CustomEvent('kestrel:undone', { detail: opts }));
            })
            .catch(function () { toast('Could not undo. Nothing changed.', { tone: 'warn' }); });
        }
        doc.dispatchEvent(new CustomEvent('kestrel:undo', { detail: opts }));
        dismiss();
      });
      node.appendChild(b);
    }
    function arm() {
      clearTimeout(timer);
      timer = setTimeout(dismiss, opts.timeout || (hasUndo ? 8000 : 2600));
    }
    node.addEventListener('mouseenter', function () { clearTimeout(timer); });
    node.addEventListener('mouseleave', arm);
    node.addEventListener('focusin', function () { clearTimeout(timer); });
    node.addEventListener('focusout', arm);
    region.appendChild(node);
    arm();
    return { el: node, dismiss: dismiss };
  }
  function dismissToasts() {
    var r = doc.getElementById('k-toasts');
    if (r) Array.prototype.slice.call(r.querySelectorAll('.k-toast')).forEach(function (t) { t.parentNode.removeChild(t); });
  }

  /* -------------------------------------------------------- overlay centering (the rule)
     Anything that slides in on the right opens at the middle of the right side of the window,
     never pinned to the top. .k-flyout does it in CSS (top 50% and translateY(-50%)). This
     covers elements that opt in with data-k-center and are positioned absolute or fixed, such
     as a panel inside a screen-specific container. Port of the mockups' observer, with the
     synchronous-reposition fix: the position is set in the mutation callback itself and by the
     open functions BEFORE the open class is added, so the panel never paints at the top and
     then jumps. A requestAnimationFrame pass follows only to catch late layout. */
  function centerTop(windowHeight, elementHeight) {
    var avail = windowHeight - 2 * MARGIN;
    if (elementHeight >= avail) return MARGIN;
    return Math.round((windowHeight - elementHeight) / 2);
  }
  function windowOf(el) {
    return el.closest ? el.closest('[data-k-window]') : null;
  }
  function center(el) {
    if (!el || !el.isConnected || el.getAttribute('data-k-center') === null) return false;
    var cs = root.getComputedStyle(el);
    if (cs.position !== 'absolute' && cs.position !== 'fixed') return false;
    var win = windowOf(el);
    var wr = win ? win.getBoundingClientRect()
                 : { top: 0, height: root.innerHeight, right: root.innerWidth };
    var h = el.getBoundingClientRect().height;
    if (h < 60) return false;
    var top = centerTop(wr.height, h);
    var off = 0;
    if (cs.position === 'absolute' && el.offsetParent && el.offsetParent !== win) {
      off = el.offsetParent.getBoundingClientRect().top - wr.top;
    }
    el.style.top = (top - off) + 'px';
    el.style.bottom = 'auto';
    return true;
  }
  function scanCenter(scope) {
    var list = (scope && scope.querySelectorAll ? scope : doc).querySelectorAll('[data-k-center]');
    for (var i = 0; i < list.length; i++) center(list[i]);
  }
  var centerWatch = null;
  function watchCenter() {
    if (centerWatch || !root.MutationObserver || !doc.body) return;
    centerWatch = new root.MutationObserver(function () {
      scanCenter(doc);  // synchronous: runs before the next paint
      if (root.requestAnimationFrame) root.requestAnimationFrame(function () { scanCenter(doc); });
    });
    centerWatch.observe(doc.body, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'hidden']
    });
    root.addEventListener('resize', function () { scanCenter(doc); });
  }

  /* --------------------------------------------------------------------- overlay stack */
  var stack = [];  // {el, kind, trigger, opener}
  function topmost() { return stack.length ? stack[stack.length - 1] : null; }

  function scrim() {
    var s = doc.getElementById('k-scrim');
    if (!s) {
      s = doc.createElement('div');
      s.id = 'k-scrim';
      s.className = 'k-scrim';
      s.setAttribute('aria-hidden', 'true');
      doc.body.appendChild(s);
    }
    return s;
  }
  function syncScrim() {
    var needs = stack.some(function (o) { return o.kind !== 'flyout'; });
    scrim().classList.toggle('is-on', needs);
  }
  function markTrigger(trigger, expanded) {
    if (trigger && trigger.setAttribute) trigger.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  }

  /* A slide-over or modal is modal: the page behind it is inert (no focus, no clicks, hidden
     from assistive tech) while it is open. Flyouts above it stay live. The overlay may sit
     deep inside the page, so every sibling along its path to <body> is made inert, except the
     toast region and the scrim. */
  var inerted = [];
  function syncInert() {
    inerted.forEach(function (n) { n.inert = false; n.removeAttribute('data-k-inert'); });
    inerted = [];
    var m = -1;
    for (var i = stack.length - 1; i >= 0; i--) { if (stack[i].kind !== 'flyout') { m = i; break; } }
    if (m < 0) return;
    var keep = stack.slice(m).map(function (o) { return o.el; });
    var path = [];
    keep.forEach(function (k) { for (var n = k; n && n !== doc.documentElement; n = n.parentNode) path.push(n); });
    path.forEach(function (n) {
      if (!n.parentNode || !n.parentNode.children) return;
      Array.prototype.forEach.call(n.parentNode.children, function (sib) {
        if (path.indexOf(sib) >= 0 || sib.id === 'k-toasts' || sib.id === 'k-scrim') return;
        if (/^(SCRIPT|STYLE|LINK|META)$/.test(sib.tagName)) return;
        if (sib.inert) return;
        sib.inert = true;
        sib.setAttribute('data-k-inert', '');
        inerted.push(sib);
      });
    });
  }

  // A page section still playing its staggered arrival has a transform, which makes it the
  // containing block of a fixed overlay inside it: a sheet opened in the first second landed
  // below the window and then jumped (2026-10-29). Finish those arrivals before opening; the
  // overlay's backdrop covers the last few pixels of travel. Every finite animation on an
  // ancestor counts, not a list of names: the launch entrance (k-inUp) and a page's own arrivals
  // (card rises, a role's slide) did the same (2026-10-30). Endless ones (a spinner) stay.
  function settleArrival(el) {
    for (var p = el.parentElement; p && p !== doc.body; p = p.parentElement) {
      if (!p.getAnimations) return;
      p.getAnimations().forEach(function (a) {
        var t = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : null;
        if (!t || !isFinite(t.endTime)) return;
        try { a.finish(); } catch (e) { /* already done or not finishable */ }
      });
    }
  }
  // A fixed overlay is placed against its containing block. Here that is not always the window:
  // a size container (.k-win, every page wrapper) or a transformed ancestor takes that role, so
  // on a long, scrolled page a "centered" sheet was centered on the whole page, off screen
  // (2026-10-29). anchor() measures that block and sets the overlay's offsets so it sits in the
  // window; it re-measures on scroll and resize while the overlay is open.
  function origin(el, side) {
    // Where the overlay's containing block puts top/left 0 (or right 0): measured, not guessed.
    var st = el.style, keep = [st.transition, st.transform, st.top, st.left, st.right];
    st.transition = 'none'; st.transform = 'none'; st.top = '0px';
    if (side === 'right') { st.left = 'auto'; st.right = '0px'; } else { st.left = '0px'; st.right = 'auto'; }
    var r = el.getBoundingClientRect();
    // Put everything back with transitions still off, settle styles, then restore transitions:
    // otherwise the overlay animates from the measuring position (it slid in off center).
    st.transform = keep[1]; st.top = keep[2]; st.left = keep[3]; st.right = keep[4];
    void el.offsetWidth;
    st.transition = keep[0];
    return r;
  }
  function anchor(el) {
    ['--k-ax', '--k-ay', '--k-ar'].forEach(function (v) { el.style.removeProperty(v); });
    var a = origin(el, 'left'), b = origin(el, 'right');
    var off = Math.abs(a.left) > 1 || Math.abs(a.top) > 1 || Math.abs(b.right - root.innerWidth) > 1;
    if (!off) return;  // the window is the containing block: the plain CSS is right
    el.style.setProperty('--k-ax', (root.innerWidth / 2 - a.left) + 'px');
    el.style.setProperty('--k-ay', (root.innerHeight / 2 - a.top) + 'px');
    el.style.setProperty('--k-ar', (b.right - root.innerWidth) + 'px');
  }
  // The page behind a modal or slide-over does not scroll (it is inert already); a page that
  // moved under an open sheet also moved the block the sheet is anchored to.
  function lockScroll() {
    var hold = stack.some(function (o) { return o.kind === 'modal' || o.kind === 'slideover'; });
    doc.documentElement.classList.toggle('k-scroll-held', hold);
  }
  var anchored = [];
  function reanchor() { anchored.forEach(anchor); }
  if (root.addEventListener) {
    root.addEventListener('scroll', reanchor, { passive: true });
    root.addEventListener('resize', reanchor);
  }
  function open(kind, ref, opts) {
    var el = el$(ref);
    opts = opts || {};
    if (!el) return null;
    // One overlay of a kind at a time. A flyout opens over a slide-over or a modal and leaves
    // them open; closing it returns to them.
    stack.slice().forEach(function (o) { if (o.kind === kind) close(o); });
    var opener = doc.activeElement;
    var trigger = opts.trigger || null;
    settleArrival(el);
    anchor(el);
    if (anchored.indexOf(el) < 0) anchored.push(el);
    if (el.getAttribute('data-k-center') !== null) center(el);  // before the class: no jump
    el.setAttribute('aria-hidden', 'false');
    el.classList.add('is-open');
    markTrigger(trigger, true);
    stack.push({ el: el, kind: kind, trigger: trigger, opener: opener });
    lockScroll();
    syncScrim();
    syncInert();
    var items = focusable(el);
    var first = el.querySelector('[data-k-autofocus]') || items[0];
    if (!first) { el.setAttribute('tabindex', '-1'); first = el; }
    try { first.focus({ preventScroll: true }); } catch (e) { /* element gone */ }
    return el;
  }
  function close(entry) {
    if (!entry) return;
    var i = stack.indexOf(entry);
    if (i >= 0) stack.splice(i, 1);
    entry.el.classList.remove('is-open');
    entry.el.setAttribute('aria-hidden', 'true');
    var ai = anchored.indexOf(entry.el);  // keeps its place while it fades out
    if (ai >= 0) anchored.splice(ai, 1);
    lockScroll();
    markTrigger(entry.trigger, false);
    syncScrim();
    syncInert();
    var back = entry.trigger || entry.opener;  // focus returns to what opened it
    if (back && back.isConnected && back.focus) {
      try { back.focus({ preventScroll: true }); } catch (e) { /* element gone */ }
    }
    entry.el.dispatchEvent(new CustomEvent('k-closed', { bubbles: true }));
  }
  function closeKind(kind) {
    stack.slice().forEach(function (o) { if (o.kind === kind) close(o); });
  }
  function closeAll() { stack.slice().reverse().forEach(close); }

  /* Escape closes the top overlay; a click outside closes a flyout or slide-over; a click on
     the scrim closes a modal unless it is marked data-k-static. Tab stays inside a modal. */
  function onKey(e) {
    var t = topmost();
    if (!t) return;
    if (e.key === 'Escape') { e.preventDefault(); close(t); return; }
    if (e.key === 'Tab' && t.kind !== 'flyout') {
      var items = focusable(t.el);
      if (!items.length) { e.preventDefault(); return; }
      var first = items[0], last = items[items.length - 1];
      if (e.shiftKey && doc.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && doc.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  function onOutside(e) {
    var t = topmost();
    if (!t) return;
    var target = e.target;
    if (t.el.contains(target)) return;
    if (t.trigger && t.trigger.contains && t.trigger.contains(target)) return;  // trigger toggles
    if (target.closest && target.closest('.k-toast')) return;
    if (t.kind === 'modal') {
      if (target.id === 'k-scrim' && t.el.getAttribute('data-k-static') === null) close(t);
      return;
    }
    close(t);
  }

  /* --------------------------------------------------------- in-place update helpers
     Rule: live updates never re-mount. Insert, update in place, fold out; anything moving
     between lists animates out, folds, and arrives. Reduced motion skips the animation. */
  function setText(node, text) {
    node = el$(node);
    if (!node) return false;
    text = String(text);
    if (node.textContent === text) return false;
    node.textContent = text;
    if (!reduced()) {
      node.classList.remove('k-pulse-once');
      void node.offsetWidth;  // restart the pop
      node.classList.add('k-pulse-once');
    }
    return true;
  }
  function arrive(node) {
    node = el$(node);
    if (!node || reduced()) return;
    node.classList.add('k-arrive');
    setTimeout(function () { node.classList.remove('k-arrive'); }, 3000);
  }
  function foldOut(node, done) {
    node = el$(node);
    if (!node) return;
    var finish = function () { if (done) done(node); };
    if (reduced()) { finish(); return; }
    node.style.maxHeight = node.offsetHeight + 'px';
    void node.offsetWidth;
    node.classList.add('k-item-out');
    setTimeout(finish, 480);
  }
  function prepend(list, node) {
    list = el$(list);
    if (!list || !node) return;
    list.insertBefore(node, list.firstChild);
    if (!reduced()) {
      node.classList.add('k-item-in');
      setTimeout(function () { node.classList.remove('k-item-in'); }, 600);
    }
  }
  function moveTo(node, dest, opts) {
    node = el$(node); dest = el$(dest); opts = opts || {};
    if (!node || !dest) return;
    foldOut(node, function () {
      node.classList.remove('k-item-out');
      node.style.maxHeight = '';
      dest.insertBefore(node, opts.before || dest.firstChild);
      arrive(node);
      var head = el$(opts.pulse);
      if (head && !reduced()) {
        head.classList.remove('k-pulse-once');
        void head.offsetWidth;
        head.classList.add('k-pulse-once');
      }
    });
  }

  /* ------------------------------------------------------------ delegated click hooks */
  function closest(target, attr) { return target.closest ? target.closest('[' + attr + ']') : null; }

  function onClick(e) {
    var t = e.target;
    var n;
    if ((n = closest(t, 'data-k-theme'))) { setTheme(n.getAttribute('data-k-theme')); return; }
    if ((n = closest(t, 'data-k-scheme'))) { setScheme(n.getAttribute('data-k-scheme')); return; }
    if ((n = closest(t, 'data-k-flyout'))) {
      var f = el$(n.getAttribute('data-k-flyout'));
      var cur = stack.filter(function (o) { return o.el === f; })[0];
      if (cur) close(cur); else open('flyout', f, { trigger: n });
      e.preventDefault();
      return;
    }
    if ((n = closest(t, 'data-k-slideover'))) {
      open('slideover', n.getAttribute('data-k-slideover'), { trigger: n });
      e.preventDefault();
      return;
    }
    if ((n = closest(t, 'data-k-modal'))) {
      open('modal', n.getAttribute('data-k-modal'), { trigger: n });
      e.preventDefault();
      return;
    }
    if ((n = closest(t, 'data-k-close'))) {
      var host = stack.filter(function (o) { return o.el.contains(n); })[0] || topmost();
      close(host);
      e.preventDefault();
      return;
    }
    if ((n = closest(t, 'data-k-step'))) { step(n); return; }
    if ((n = t.closest && t.closest('[data-k-seg] button'))) {
      var seg = n.closest('[data-k-seg]');
      Array.prototype.forEach.call(seg.querySelectorAll('button'), function (b) {
        b.classList.toggle('is-on', b === n);
        b.setAttribute('aria-pressed', b === n ? 'true' : 'false');
      });
      var hidden = seg.querySelector('input[type="hidden"]');
      if (hidden) { hidden.value = n.value; hidden.dispatchEvent(new Event('change', { bubbles: true })); }
      seg.dispatchEvent(new CustomEvent('k-seg-change', { bubbles: true, detail: { button: n } }));
    }
  }
  function step(btn) {
    var box = btn.closest('[data-k-stepper]');
    if (!box) return;
    var out = box.querySelector('output, input');
    if (!out) return;
    var min = parseFloat(box.getAttribute('min')); if (isNaN(min)) min = -Infinity;
    var max = parseFloat(box.getAttribute('max')); if (isNaN(max)) max = Infinity;
    var inc = parseFloat(box.getAttribute('step')) || 1;
    var cur = parseFloat(out.value !== undefined && out.tagName === 'INPUT' ? out.value : out.textContent);
    if (isNaN(cur)) cur = 0;
    var next = Math.min(max, Math.max(min, cur + inc * parseFloat(btn.getAttribute('data-k-step'))));
    if (out.tagName === 'INPUT') out.value = String(next); else out.textContent = String(next);
    Array.prototype.forEach.call(box.querySelectorAll('[data-k-step]'), function (b) {
      var d = parseFloat(b.getAttribute('data-k-step'));
      b.disabled = (d < 0 && next <= min) || (d > 0 && next >= max);
    });
    box.dispatchEvent(new CustomEvent('change', { bubbles: true, detail: { value: next } }));
  }

  /* ------------------------------------------------- the frameless desktop window (D-UI-88)
     The desktop shell exposes a small JS API on the window (window.pywebview.api, see
     kestrel/desktop/frame.py). Without it (a browser tab, `kestrel serve`) nothing here runs and
     the controls stay hidden. With it: the controls appear at the top of the sidebar (Windows and
     Linux; on macOS the real traffic lights sit there), the empty top of the sidebar and of the
     page's top row drag the window, a double click there toggles maximize, and the edge
     handles resize. Close asks the shell first: to the tray, or a real quit with the closing
     screen. */
  var frameApi = null, frameOS = '';
  function closingScreen() {
    if (!doc || doc.getElementById('k-closing')) return;
    var o = doc.createElement('div');
    o.id = 'k-closing';
    o.className = 'k-closing';
    o.setAttribute('role', 'status');
    o.setAttribute('aria-live', 'assertive');
    var box = doc.createElement('div');
    box.className = 'k-closing__in';
    var mark = doc.createElement('img');
    mark.className = 'k-closing__mark';
    var brand = doc.querySelector('.k-brand__mark img');  // its URL carries the build version
    mark.src = brand ? brand.getAttribute('src') : '/static/icon.svg';
    mark.alt = '';
    var name = doc.createElement('div');
    name.className = 'k-closing__name';
    name.textContent = 'Closing Kestrel';
    var line = doc.createElement('div');
    line.className = 'k-closing__status';
    line.textContent = 'Your work is saved and picks up where it left off next time.';
    box.appendChild(mark); box.appendChild(name); box.appendChild(line);
    o.appendChild(box);
    doc.body.appendChild(o);
    if (!reduced()) {
      setTimeout(function () { doc.documentElement.classList.add('k-leaving'); }, 450);
    }
  }
  function frameState(maximized) {
    if (!doc) return;
    doc.documentElement.classList.toggle('k-maximized', !!maximized);
    var w = doc.getElementById('k-wctl');
    if (w) w.classList.toggle('is-max', !!maximized);
    var b = w && w.querySelector('[data-wc="max"]');
    if (b) {
      var label = maximized ? 'Restore' : 'Maximize';
      b.setAttribute('aria-label', label);
      b.setAttribute('title', label);
    }
  }
  function call(name, args) {
    if (!frameApi || typeof frameApi[name] !== 'function') return Promise.resolve(null);
    try { return Promise.resolve(frameApi[name].apply(frameApi, args || [])); }
    catch (e) { return Promise.resolve(null); }
  }
  function wireFrame(api, info) {
    frameApi = api;
    info = info || {};
    frameOS = info.os === 'mac' || info.os === 'linux' ? info.os : 'win';
    var w = doc.getElementById('k-wctl');
    if (!w) return false;
    w.hidden = false;
    w.classList.toggle('is-mac', frameOS === 'mac');
    doc.documentElement.setAttribute('data-k-frame', frameOS);
    // The next page draws the controls before its first paint (window_controls.html), so they
    // never blink out and back on a page change (owner round 13).
    try { sessionStorage.setItem('kestrel-frame', frameOS); } catch (e) { /* storage off */ }
    // Windows resizes natively, from a strip the window keeps around the page (frame.py); the
    // page's edge handles are for Linux only.
    if (frameOS === 'linux') {
      Array.prototype.forEach.call(doc.querySelectorAll('[data-k-rsz]'), function (h) { h.hidden = false; });
    }
    frameState(!!info.maximized);
    if (frameOS === 'win') watchStrip();
    return true;
  }
  // D-UI-196: on Windows the window keeps a thin resize strip around the page in the sidebar's
  // color (--frame-strip), painted by the window itself, so the page tells it the color now
  // and again whenever the theme or scheme changes.
  var stripSent = null, stripWatched = false;
  function stripColor() {
    var probe = doc.createElement('span');
    probe.style.cssText = 'position:absolute;width:0;height:0;background-color:var(--frame-strip)';
    doc.body.appendChild(probe);
    var c = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return c;
  }
  function sendStrip() {
    var c = stripColor();
    if (c && c !== stripSent) { stripSent = c; call('window_set_frame_color', [c]); }
  }
  function watchStrip() {
    sendStrip();
    if (stripWatched) return;
    stripWatched = true;
    var later = function () { requestAnimationFrame(sendStrip); };
    new MutationObserver(later).observe(doc.documentElement,
      { attributes: true, attributeFilter: ['data-theme', 'data-scheme'] });
    if (root.matchMedia) {
      var mq = root.matchMedia('(prefers-color-scheme: dark)');
      if (mq.addEventListener) mq.addEventListener('change', later);
    }
  }
  // pywebview fills window.pywebview.api asynchronously and fires pywebviewready once; the page's
  // script can run either side of that. So look now, on the event, and briefly poll, and wire
  // once. The controls exist only when the desktop shell rendered them (#k-wctl).
  var frameWired = false, framePolls = 0;
  function frameInit() {
    if (frameWired || !doc || !doc.getElementById('k-wctl')) return;
    var pv = root.pywebview;
    if (!pv || !pv.api || typeof pv.api.window_info !== 'function') {
      if (framePolls++ < 100) setTimeout(frameInit, 100);  // up to 10 s
      return;
    }
    frameWired = true;
    frameApi = pv.api;
    call('window_info').then(function (info) {
      if (info) {
        wireFrame(pv.api, info);
        var t = lsGet('kestrel-theme');
        if (t && typeof pv.api.window_set_theme === 'function') call('window_set_theme', [t]);
      } else { frameWired = false; }
    });
  }
  function dragTarget(t) {
    // Only the empty part of a drag region: a control inside it stays a control.
    return t && t.hasAttribute && t.hasAttribute('data-k-drag') ? t : null;
  }
  // The resize band is measured from the window's own edges, not only from the handle
  // elements: the page's scrollbar sits in the right and bottom pixels, and a handle there was
  // never hit (hardware check, round 5). Corners take a larger square.
  var EDGE_PX = 6, CORNER_PX = 14;
  function edgeAt(x, y, w, h) {
    if (frameOS !== 'linux' || doc.documentElement.classList.contains('k-maximized')) return null;
    var onN = y < EDGE_PX, onS = y >= h - EDGE_PX, onW = x < EDGE_PX, onE = x >= w - EDGE_PX;
    if (!(onN || onS || onW || onE)) return null;
    var v = y < CORNER_PX ? 'n' : y >= h - CORNER_PX ? 's' : '';
    var z = x < CORNER_PX ? 'w' : x >= w - CORNER_PX ? 'e' : '';
    return (v + z) || (onN ? 'n' : onS ? 's' : onW ? 'w' : 'e');
  }
  function onFrameDown(e) {
    if (!frameApi || e.button !== 0) return;
    var t = e.target;
    var edge = (t && t.getAttribute && t.getAttribute('data-k-rsz')) ||
      edgeAt(e.clientX, e.clientY, root.innerWidth, root.innerHeight);
    if (edge) { e.preventDefault(); call('window_start_resize', [edge]); return; }
    // Windows drags natively (snap and Aero shake keep working); macOS and Linux use
    // pywebview's own drag region (.pywebview-drag-region), so nothing is sent from here.
    // The move starts only once the pointer has moved a few pixels with the button down:
    // Windows' move loop swallows the mouse-up, so a move started on the press meant the page
    // never saw a double click (hardware check, round 6). A press and release in place stays a
    // click, and two of them are the browser's own dblclick, which toggles maximize below.
    if (frameOS === 'win' && dragTarget(t)) {
      // The press point in physical screen pixels: the native move is anchored there.
      var r = root.devicePixelRatio || 1;
      pending = { x: e.clientX, y: e.clientY, sx: e.screenX * r, sy: e.screenY * r };
    }
  }
  var pending = null;
  var DRAG_PX = 3;
  function onFrameMove(e) {
    if (!pending) return;
    if (!(e.buttons & 1)) { pending = null; return; }
    if (Math.abs(e.clientX - pending.x) < DRAG_PX && Math.abs(e.clientY - pending.y) < DRAG_PX) return;
    var p = pending;
    pending = null;
    call('window_start_drag', [p.sx, p.sy]);
  }
  function onFrameUp() { pending = null; }
  function onFrameDbl(e) {
    if (!frameApi || !dragTarget(e.target)) return;
    pending = null;
    call('window_toggle_maximize').then(function (m) { if (m !== null) frameState(m); });
  }
  function onFrameClick(e) {
    var b = e.target.closest ? e.target.closest('[data-wc]') : null;
    if (!b || !frameApi) return false;
    var what = b.getAttribute('data-wc');
    if (what === 'min') call('window_minimize');
    else if (what === 'max') call('window_toggle_maximize').then(function (m) { if (m !== null) frameState(m); });
    else if (what === 'close') {
      call('window_close_intent').then(function (intent) {
        if (intent === 'quit') {
          closingScreen();
          setTimeout(function () { call('window_close'); }, reduced() ? 0 : 800);
        } else {
          call('window_close');
        }
      });
    }
    return true;
  }

  /* In-place update for a polled region marked data-k-morph (Today's status strip): the new
     markup is laid over the old node by node, so only the text and attributes that changed
     change, and nothing is removed and redrawn (owner round 16: the strip vanished and came back
     whenever its counts moved). A changed shape (another element where one was) replaces just
     that branch. The root's own hx-trigger changing (a scan starting or ending) is a real swap,
     so htmx picks up the new polling period. */
  function morphNode(cur, next) {
    if (cur.nodeType !== next.nodeType || cur.nodeName !== next.nodeName) {
      cur.parentNode.replaceChild(next.cloneNode(true), cur);
      return;
    }
    if (cur.nodeType === 3 || cur.nodeType === 8) {
      if (cur.nodeValue !== next.nodeValue) cur.nodeValue = next.nodeValue;
      return;
    }
    if (cur.nodeType !== 1) return;
    var i;
    for (i = cur.attributes.length - 1; i >= 0; i--) {
      var name = cur.attributes[i].name;
      if (!next.hasAttribute(name)) cur.removeAttribute(name);
    }
    for (i = 0; i < next.attributes.length; i++) {
      var at = next.attributes[i];
      if (cur.getAttribute(at.name) !== at.value) cur.setAttribute(at.name, at.value);
    }
    var a = Array.prototype.slice.call(cur.childNodes), b = Array.prototype.slice.call(next.childNodes);
    for (i = 0; i < b.length; i++) {
      if (i < a.length) morphNode(a[i], b[i]); else cur.appendChild(b[i].cloneNode(true));
    }
    for (i = b.length; i < a.length; i++) cur.removeChild(a[i]);
  }
  if (doc) {
    doc.addEventListener('htmx:beforeSwap', function (e) {
      var d = e.detail || {};
      var t = d.target;
      if (!t || !t.hasAttribute || !t.hasAttribute('data-k-morph') || !d.xhr || d.shouldSwap === false) return;
      if (d.xhr.status < 200 || d.xhr.status >= 300) return;
      var box = doc.createElement('div');
      box.innerHTML = d.xhr.responseText || '';
      var fresh = box.firstElementChild;
      if (!fresh || fresh.id !== t.id) return;
      if ((fresh.getAttribute('hx-trigger') || '') !== (t.getAttribute('hx-trigger') || '')) return;
      d.shouldSwap = false;
      morphNode(t, fresh);
      t.dispatchEvent(new CustomEvent('k-morphed', { bubbles: true }));
    });
  }

  /* A poll that brings back exactly what is on screen changes nothing: no swap, so no reflow and
     no flicker every few seconds (owner round 15: Profile's Save bar shook with the suggestions
     poll). Only polled swaps; anything a person clicked always swaps. */
  if (doc) {
    doc.addEventListener('htmx:beforeSwap', function (e) {
      var d = e.detail || {};
      var el = d.requestConfig && d.requestConfig.elt;
      var trig = el && el.getAttribute && el.getAttribute('hx-trigger');
      if (!trig || !/\bevery\b/.test(trig) || !d.xhr || !d.target) return;
      var norm = function (h) { return String(h || '').replace(/\s+/g, ' ').trim(); };
      var swap = (el.getAttribute('hx-swap') || 'innerHTML').split(' ')[0];
      var now = swap === 'outerHTML' ? d.target.outerHTML : d.target.innerHTML;
      if (norm(now) === norm(d.xhr.responseText)) d.shouldSwap = false;
    });
    // A request a person made that fails (an error answer, or no answer) always says so: htmx
    // swaps nothing on an error, so without this a press looked like it did nothing (issue #50).
    // Only a press, a submit, a change or a key counts: polls, lazy loads and background
    // htmx.ajax calls stay quiet. A page that explains its own failure sets detail.kHandled.
    var PERSON = /^(click|submit|change|input|search|keyup|keydown)$/;
    var failed = function (e) {
      var d = e.detail || {};
      var ev = d.requestConfig && d.requestConfig.triggeringEvent;
      if (!ev || !PERSON.test(ev.type)) return;
      setTimeout(function () {
        if (d.kHandled) return;
        toast(e.type === 'htmx:sendError'
          ? 'Kestrel did not answer, so nothing changed. Try again in a moment.'
          : 'That did not go through, so nothing changed. Try again; if it keeps happening, report it with a diagnostics file from Settings.',
          { tone: 'warn' });
      }, 0);
    };
    doc.addEventListener('htmx:responseError', failed);
    doc.addEventListener('htmx:sendError', failed);
    // A click on a page in the sidebar moves the highlight at once, so the click visibly lands
    // while the next page loads (owner round 15: a lag read as a missed click).
    doc.addEventListener('click', function (e) {
      var a = e.target && e.target.closest ? e.target.closest('.side .mainnav > a[href]') : null;
      if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
      Array.prototype.forEach.call(doc.querySelectorAll('.side .mainnav > a'), function (n) {
        n.classList.toggle('active', n === a);
        if (n === a) n.setAttribute('aria-current', 'page'); else n.removeAttribute('aria-current');
      });
      doc.documentElement.classList.add('k-going');
    });
    // Any other link to another page (a role's full details, a card, a breadcrumb): the link
    // shows it was pressed and the pointer shows progress until the page arrives.
    doc.addEventListener('click', function (e) {
      var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
      if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
      if (a.target === '_blank' || a.hasAttribute('download') || a.hasAttribute('hx-get') ||
          a.hasAttribute('hx-post') || a.hasAttribute('hx-boost')) return;
      var href = a.getAttribute('href') || '';
      if (!href || href.charAt(0) === '#' || /^(mailto|tel|javascript):/i.test(href)) return;
      var url;
      try { url = new URL(a.href, root.location.href); } catch (err) { return; }
      if (url.origin !== root.location.origin) return;
      if (url.pathname === root.location.pathname && url.search === root.location.search) return;
      a.classList.add('k-pressed-going');
      doc.documentElement.classList.add('k-going');
    });
    // Back to a page from the history cache: no navigation is on its way any more.
    root.addEventListener('pageshow', function () {
      doc.documentElement.classList.remove('k-going');
      Array.prototype.forEach.call(doc.querySelectorAll('.k-pressed-going'), function (n) {
        n.classList.remove('k-pressed-going');
      });
    });
  }

  /* Sidebar memory (owner report 2026-10-26): after each poll, keep the counts and the readout
     so the next page paints them at once (base.html restores them before first paint). */
  function keepSide() {
    try {
      var c = {};
      Array.prototype.forEach.call(doc.querySelectorAll('[id^="nav-count-"]'), function (el) {
        c[el.id] = el.textContent;
      });
      var ring = doc.getElementById('nav-ring');
      if (ring) c['nav-ring'] = ring.hidden ? '0' : '1';
      sessionStorage.setItem('kestrel-side-counts', JSON.stringify(c));
      var rail = doc.getElementById('rail');
      if (rail && rail.querySelector('[data-rail]')) sessionStorage.setItem('kestrel-side-rail', rail.innerHTML);
    } catch (e) {}
  }
  if (doc) {
    doc.addEventListener('htmx:afterSettle', keepSide);
    doc.addEventListener('htmx:oobAfterSwap', keepSide);
  }

  /* ------------------------------------------------------------ shell motion (mockups)
     <html> carries .k-intro (the one launch entrance) or .k-page-in (an in-app page change),
     decided before first paint by base.html's head script. Here each nav item and section
     label gets its stagger index, and the page's top-level blocks after the scene band get
     .k-rise with theirs. The classes come off once the motion is over. */
  /* The top row's native tooltips become in-page ones (components.css .k-tools [data-k-tip]),
     right-aligned so none is clipped at the window edge. */
  function topTips() {
    Array.prototype.forEach.call(doc.querySelectorAll('.k-tools [title]'), function (el) {
      el.setAttribute('data-k-tip', el.getAttribute('title'));
      if (!el.getAttribute('aria-label')) el.setAttribute('aria-label', el.getAttribute('title'));
      el.removeAttribute('title');
    });
  }
  var motionTimer = 0;
  function motionTargets() {
    var h = doc.documentElement;
    if (!h.classList.contains('k-intro') && !h.classList.contains('k-page-in')) return;
    var n = 0;
    Array.prototype.forEach.call(doc.querySelectorAll('.side .k-sec, .side .mainnav > a'), function (el) {
      el.style.setProperty('--i', String(n++));
    });
    var main = doc.querySelector('main.container');
    if (main) {
      var scene = main.querySelector('.k-scene');
      var block = scene;
      // The scene's own wrapper (Today's hero) counts as the band, not as a block that rises.
      while (block && block.parentElement && block.parentElement !== main &&
             block.parentElement.children.length === 1) block = block.parentElement;
      var host = block ? block.parentElement : main;
      var i = 0;
      Array.prototype.forEach.call(host ? host.children : [], function (el) {
        if (el === block || el.hidden || /^(SCRIPT|STYLE|TEMPLATE|DIALOG)$/.test(el.tagName)) return;
        if (el.classList.contains('k-slideover') || el.classList.contains('k-flyout') ||
            el.classList.contains('k-modal')) return;
        el.classList.add('k-rise');
        el.style.setProperty('--i', String(i++));
      });
    }
    // An in-place page change restarts the arrival: one clean-up, for the latest arrival.
    if (motionTimer) clearTimeout(motionTimer);
    motionTimer = setTimeout(function () {
      motionTimer = 0;
      h.classList.remove('k-intro', 'k-page-in', 'k-from-today');
      Array.prototype.forEach.call(doc.querySelectorAll('.k-rise'), function (el) {
        el.classList.remove('k-rise');
      });
    }, 2200);
  }

  /* Scroll cues (pipeline-mockup-locked.html :404-408, :922-943): a round arrow at the top or
     bottom centre of a scroller while there is more to see in that direction, hidden while it
     scrolls and at its ends; a click pages it. The page column gets them on every page
     (the document scrolls there); KUI.cue(el) adds them to any other scroller. */
  var CHEV = { down: '<path d="M6 9l6 6 6-6"/>', up: '<path d="M6 15l6-6 6 6"/>' };
  var cueSets = [];
  function cueEl(dir, fixed) {
    var d = doc.createElement('button');
    d.type = 'button';
    d.className = 'k-cue k-cue--' + dir + (fixed ? ' k-cue--fixed' : '');
    d.setAttribute('aria-label', dir === 'down' ? 'Scroll down' : 'Scroll up');
    d.setAttribute('tabindex', '-1');
    d.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + CHEV[dir] + '</svg>';
    return d;
  }
  /* The page cues are fixed over the viewport, so content can sit under one. Content must never
     be covered: when a cue would show, it looks for a spot along its edge with nothing under it
     (the page centre first, then outward) and stays hidden when there is none. "Nothing" means
     no text, and no control or media either: a link, button, field, label, image, icon, video,
     frame. The scan runs when the page comes to rest after a scroll, on a resize and when the
     content itself changes (a debounced observer), never on a timer; it samples a coarse grid
     and gives up after a few candidate spots. Its motion and the reduced-motion rule are
     unchanged. */
  var CUE_BLOCKING = 'a, button, input, select, textarea, label, img, svg, video, iframe, ' +
    '[role="button"]';
  var CUE_GRID = 3, CUE_STEP = 48, CUE_TRIES = 14;
  function cueTextUnder(l, t, w, h) {
    var seen = new Set(), rg = doc.createRange(), pad = 3;
    for (var i = 0; i < CUE_GRID; i++) for (var j = 0; j < CUE_GRID; j++) {
      var els = doc.elementsFromPoint(l + w * i / (CUE_GRID - 1), t + h * j / (CUE_GRID - 1));
      for (var k = 0; k < els.length; k++) seen.add(els[k]);
    }
    var found = false;
    seen.forEach(function (e) {
      if (found || e === doc.documentElement || e === doc.body) return;
      if (e.closest('.k-cue')) return;
      if (e.matches(CUE_BLOCKING)) { found = true; return; }  // a control or media (a frame's text too)
      for (var c = e.firstChild; c && !found; c = c.nextSibling) {
        if (c.nodeType !== 3 || !c.nodeValue.trim()) continue;
        rg.selectNodeContents(c);
        var rs = rg.getClientRects();
        for (var q = 0; q < rs.length; q++) {
          var b = rs[q];
          if (b.right > l - pad && b.left < l + w + pad && b.bottom > t - pad && b.top < t + h + pad) {
            found = true;
            break;
          }
        }
      }
    });
    return found;
  }
  function cueFreeX(dir, mid, lo, hi) {
    var size = 34, t = dir === 'up' ? 8 : root.innerHeight - 8 - size, tried = [mid];
    for (var d = CUE_STEP; tried.length < CUE_TRIES && (mid - d >= lo || mid + d <= hi); d += CUE_STEP) {
      if (mid - d >= lo) tried.push(mid - d);
      if (mid + d <= hi) tried.push(mid + d);
    }
    for (var i = 0; i < tried.length; i++) {
      if (!cueTextUnder(tried[i] - size / 2, t, size, size)) return tried[i];
    }
    return null;
  }
  function cueUpdate(cs) {
    var page = !cs.el;
    var top = page ? (root.scrollY || doc.documentElement.scrollTop) : cs.el.scrollTop;
    var more = page ? doc.documentElement.scrollHeight - root.innerHeight
                    : cs.el.scrollHeight - cs.el.clientHeight;
    if (page) {
      var main = doc.querySelector('.k-main');
      if (!main) return;
      var r = main.getBoundingClientRect();
      cs.up.style.left = cs.down.style.left = (r.left + r.width / 2) + 'px';
    } else {
      if (!cs.el.isConnected) { cs.up.remove(); cs.down.remove(); return; }
      var er = cs.el.getBoundingClientRect(), hr = cs.host.getBoundingClientRect();
      cs.up.style.left = cs.down.style.left = (er.left - hr.left + er.width / 2) + 'px';
      cs.up.style.top = (er.top - hr.top + 8) + 'px';
      cs.down.style.top = (er.bottom - hr.top - 42) + 'px';
    }
    var showUp = !cs.moving && more > 4 && top > 8;
    var showDown = !cs.moving && more > 4 && top < more - 8;
    if (page) {
      var m = doc.querySelector('.k-main').getBoundingClientRect();
      var lo = Math.max(m.left, 0) + 24, hi = Math.min(m.right, root.innerWidth) - 24;
      // The same page at the same place answers the same: the scan reruns only when this key
      // changes, or when the content changed (cueContentChanged clears the key).
      var key = [Math.round(top), Math.round(more), root.innerWidth, root.innerHeight,
                 Math.round(m.left), Math.round(m.width), showUp, showDown].join('|');
      if (cs.key !== key) {
        cs.key = key;
        cs.xs = {
          up: showUp ? cueFreeX('up', m.left + m.width / 2, lo, hi) : null,
          down: showDown ? cueFreeX('down', m.left + m.width / 2, lo, hi) : null
        };
      }
      if (cs.xs.up === null) showUp = false; else cs.up.style.left = cs.xs.up + 'px';
      if (cs.xs.down === null) showDown = false; else cs.down.style.left = cs.xs.down + 'px';
    }
    cs.up.classList.toggle('is-show', showUp);
    cs.down.classList.toggle('is-show', showDown);
  }
  function cue(el) {
    el = el ? el$(el) : null;
    if (el && el._kCues) return el._kCues;
    if (!el && cueSets.some(function (c) { return !c.el; })) return null;
    var host = el ? el.parentElement : doc.body;
    if (el && root.getComputedStyle(host).position === 'static') host.style.position = 'relative';
    var cs = { el: el, host: host, up: cueEl('up', !el), down: cueEl('down', !el), moving: false, t: 0 };
    host.appendChild(cs.up);
    host.appendChild(cs.down);
    var scroller = el || root;
    [['up', -1], ['down', 1]].forEach(function (p) {
      cs[p[0]].addEventListener('click', function () {
        var h = el ? el.clientHeight : root.innerHeight;
        scroller.scrollBy({ top: p[1] * h * 0.8, behavior: reduced() ? 'auto' : 'smooth' });
      });
    });
    scroller.addEventListener('scroll', function () {
      cs.moving = true; cueUpdate(cs); clearTimeout(cs.t);
      cs.t = setTimeout(function () { cs.moving = false; cueUpdate(cs); }, 650);
    }, { passive: true });
    if (el) el._kCues = cs;
    cueSets.push(cs);
    cueUpdate(cs);
    return cs;
  }
  function refreshCues() { cueSets.forEach(cueUpdate); }
  // The content changed (htmx swap, late load, something opened): scan again once it settles.
  var cueDebounce = 0;
  function cueContentChanged() {
    clearTimeout(cueDebounce);
    cueDebounce = setTimeout(function () {
      cueSets.forEach(function (cs) { cs.key = null; });
      refreshCues();
    }, 150);
  }
  function cueWatch() {
    root.addEventListener('resize', cueContentChanged);
    if (root.ResizeObserver) {
      var ro = new root.ResizeObserver(cueContentChanged);
      ro.observe(doc.body);
      var main = doc.querySelector('.k-main');
      if (main) ro.observe(main);
    }
    var main2 = doc.querySelector('.k-main');
    if (root.MutationObserver && main2) {  // the main content only: the side rail polls
      new root.MutationObserver(cueContentChanged).observe(main2, { childList: true, subtree: true });
    }
  }

  /* Shared motion primitives (components.css "Shared motion primitives"). */
  function saved(el, opts) {
    el = el$(el);
    if (!el || reduced()) return;
    el.classList.remove('k-saved-flash');
    void el.offsetWidth;
    el.classList.add('k-saved-flash');
    setTimeout(function () { el.classList.remove('k-saved-flash'); }, 1500);
    if (opts && opts.tick === false) return;
    var tick = doc.createElement('span');
    tick.className = 'k-saved-tick';
    tick.setAttribute('role', 'status');
    tick.textContent = (opts && opts.text) || 'Saved';
    el.insertAdjacentElement('afterend', tick);
    setTimeout(function () { tick.classList.add('is-leaving'); }, 1600);
    setTimeout(function () { if (tick.parentNode) tick.parentNode.removeChild(tick); }, 2100);
  }
  function fold(details, open) {
    details = el$(details);
    if (!details) return;
    var want = open === undefined ? !details.open : !!open;
    if (want || reduced()) { details.open = want; return; }
    details.classList.add('is-folding');
    setTimeout(function () { details.classList.remove('is-folding'); details.open = false; }, 200);
  }
  function ink(tabs, from) {
    tabs = el$(tabs);
    if (!tabs) return;
    var on = tabs.querySelector('[aria-selected="true"], .is-on, .active');
    var bar = tabs.querySelector('.k-tabink');
    if (!bar) {
      bar = doc.createElement('span');
      bar.className = 'k-tabink';
      bar.setAttribute('aria-hidden', 'true');
      if (root.getComputedStyle(tabs).position === 'static') tabs.style.position = 'relative';
      tabs.appendChild(bar);
    }
    if (!on) { bar.style.width = '0'; return; }
    // A swap that rebuilds the tabs passes where the bar was, so it slides from there.
    if (from && !reduced()) {
      bar.style.transition = 'none';
      bar.style.left = from.left + 'px';
      bar.style.width = from.width + 'px';
      void bar.offsetWidth;
      bar.style.transition = '';
    }
    bar.style.left = on.offsetLeft + 'px';
    bar.style.width = on.offsetWidth + 'px';
  }
  function inkPos(tabs) {
    tabs = el$(tabs);
    var bar = tabs && tabs.querySelector('.k-tabink');
    if (!bar || !bar.style.width) return null;
    return { left: parseFloat(bar.style.left) || 0, width: parseFloat(bar.style.width) || 0 };
  }
  function paneIn(el) {
    el = el$(el);
    if (!el || reduced()) return;
    el.classList.remove('k-pane-in');
    void el.offsetWidth;
    el.classList.add('k-pane-in');
    setTimeout(function () { el.classList.remove('k-pane-in'); }, 400);
  }
  function onFoldClick(e) {
    var sum = e.target.closest ? e.target.closest('details[data-k-fold] > summary') : null;
    if (!sum) return false;
    e.preventDefault();
    fold(sum.parentElement);
    return true;
  }

  /* ------------------------------------------------------------- page lifecycle (D-UI-210)
     A page script defines its page and starts nothing at top level:
       KUI.page.define('today', { mount: function (root, ctx) { ... }, unmount: function () {} });
     mount(root, ctx) gets the page root (<main data-k-page="today">) and a context that owns
     everything the page starts, so leaving ends all of it and coming back starts exactly one set:
       ctx.signal                         aborted at unmount (pass it to fetch, too)
       ctx.on(target, type, fn, options)  addEventListener tied to ctx.signal
       ctx.every(ms, fn) / ctx.after(ms, fn)   setInterval / setTimeout, cleared at unmount;
                                          both return the timer id, and ctx.clear(id) stops one
       ctx.observe(observer)              observer.disconnect() at unmount; returns the observer
                                          (a disconnect() before then lets it go early)
       ctx.onUnmount(fn)                  any other cleanup, run last to first
     base.html mounts the document's page once, after every deferred page script has run.
     KUI.page.mount() and unmount() are the same steps for the navigation controller and the
     leak tests; current() names the mounted page and _stats() counts what it holds. */
  var pageDefs = {};
  var mounted = null;

  function report(err) {  // a page's own error surfaces as before; it never stops the rest
    root.setTimeout(function () { throw err; }, 0);
  }
  function pageCtx(m) {
    var ac = new root.AbortController();
    var ls = m.listeners, ts = m.timers, os = m.observers, us = m.cleanups;
    function live() { return !ac.signal.aborted; }
    function drop(list, item) { var i = list.indexOf(item); if (i >= 0) list.splice(i, 1); }
    m.abort = function () { ac.abort(); ls.length = 0; };
    return {
      signal: ac.signal,
      on: function (target, type, fn, options) {
        if (!target || !target.addEventListener || !live()) return function () {};
        var obj = options && typeof options === 'object';
        var cap = obj ? !!options.capture : !!options;
        var once = obj && !!options.once;
        // The browser ignores a second identical registration, so it is counted once too.
        for (var i = 0; i < ls.length; i++) {
          var x = ls[i];
          if (x.t === target && x.type === type && x.fn === fn && x.cap === cap) return x.off;
        }
        var rec = { t: target, type: type, fn: fn, cap: cap };
        var h = once ? function (e) { drop(ls, rec); return fn.call(this, e); } : fn;
        var o = { capture: cap, signal: ac.signal };
        if (obj && options.passive !== undefined) o.passive = options.passive;
        if (once) o.once = true;
        rec.off = function () {
          if (ls.indexOf(rec) < 0) return;
          drop(ls, rec);
          target.removeEventListener(type, h, cap);
        };
        ls.push(rec);
        target.addEventListener(type, h, o);
        return rec.off;
      },
      every: function (ms, fn) {
        if (!live()) return 0;
        var id = root.setInterval(fn, ms);
        ts.push({ id: id, every: true });
        return id;
      },
      after: function (ms, fn) {
        if (!live()) return 0;
        var t = { id: null, every: false };
        t.id = root.setTimeout(function () { drop(ts, t); fn(); }, ms);
        ts.push(t);
        return t.id;
      },
      clear: function (id) {
        for (var i = 0; i < ts.length; i++) {
          if (ts[i].id !== id) continue;
          if (ts[i].every) root.clearInterval(id); else root.clearTimeout(id);
          ts.splice(i, 1);
          return;
        }
      },
      // A page that is done with an observer early calls its disconnect(), which lets it go here.
      observe: function (observer) {
        if (!observer) return observer;
        if (!live()) { observer.disconnect(); return observer; }
        if (os.indexOf(observer) >= 0) return observer;
        var stop = observer.disconnect;
        observer.disconnect = function () { drop(os, observer); return stop.apply(observer, arguments); };
        os.push(observer);
        return observer;
      },
      onUnmount: function (fn) {
        if (typeof fn !== 'function') return;
        if (live()) { us.push(fn); return; }
        try { fn(); } catch (e) { report(e); }
      }
    };
  }
  function pageRoot() { return doc ? doc.querySelector('[data-k-page]') : null; }
  function pageUnmount() {
    var m = mounted;
    if (!m) return false;
    mounted = null;
    try { if (m.def.unmount) m.def.unmount(m.root, m.ctx); } catch (e) { report(e); }
    m.abort();  // every ctx.on listener, and any fetch that was given ctx.signal
    m.timers.splice(0).forEach(function (t) {
      if (t.every) root.clearInterval(t.id); else root.clearTimeout(t.id);
    });
    m.observers.splice(0).forEach(function (o) { try { o.disconnect(); } catch (e) { report(e); } });
    m.cleanups.splice(0).reverse().forEach(function (fn) { try { fn(); } catch (e) { report(e); } });
    return true;
  }
  // mount() mounts the page the document names; mount(name, root) a given one. Whatever was
  // mounted is unmounted first, so there is only ever one page running.
  function pageMount(name, el) {
    if (name && typeof name !== 'string') { el = name; name = null; }
    el = el || pageRoot();
    name = name || (el && el.getAttribute ? el.getAttribute('data-k-page') : null);
    pageUnmount();
    var def = name ? pageDefs[name] : null;
    if (!def || !el) return false;
    var m = { name: name, def: def, root: el, listeners: [], timers: [], observers: [],
              cleanups: [] };
    m.ctx = pageCtx(m);
    mounted = m;
    try { def.mount(el, m.ctx); } catch (e) { report(e); }
    return true;
  }
  var page = {
    define: function (name, def) {
      if (!name || !def || typeof def.mount !== 'function') {
        throw new TypeError('KUI.page.define(name, { mount(root, ctx), unmount() })');
      }
      pageDefs[name] = def;
    },
    // base.html's ready path: the document's page, once. A page already mounted stays as is.
    start: function () { return mounted ? false : pageMount(); },
    mount: pageMount,
    unmount: pageUnmount,
    current: function () { return mounted ? mounted.name : null; },
    _stats: function () {
      var m = mounted;
      return { name: m ? m.name : null, listeners: m ? m.listeners.length : 0,
               timers: m ? m.timers.length : 0, observers: m ? m.observers.length : 0 };
    }
  };

  /* ---------------------------------------------------- in-place navigation (D-UI-210, D-UI-220)
     A link to another page changes the page in place: the window, the sidebar, the top row and
     the scene art stay, and only the content under them is swapped, so WebView2 never shows its
     bare background between two documents (the intermittent grey window, owner round 21). One
     transaction per page change:
       leave guard (root.kLeaveGuard) -> fetch the whole page (Accept text/html, no HX-Request,
       so every route answers with its full page) -> load its stylesheets without applying them
       and its page scripts (definitions only) -> commit in one frame: the old page's overlays,
       requests and lifecycle end, the stylesheets change, the content, title and sidebar
       highlight change, history and scroll move, the arrival motion starts, the page mounts.
     The old page stays on screen until all of that is ready; nothing is ever shown blank.
     Anything this cannot do safely the window does the classic way, a full page load: a link
     marked data-k-reload (or inside such a container), Settings > Preferences > Classic page
     loads (data-k-nav="0" on <html>), a reply that is not a Kestrel page, another build of the
     app, another set of sidebar pages (a track change), a failed fetch.
     History: every entry this document makes carries kNav {inst, idx, kind}. inst names the page
     instance that made it; kind is 'doc' (a page shown here), 'hx' (an htmx hx-push-url entry)
     or 'page' (the page's own pushState, Pipeline's ?open=). Back or Forward to an entry of the
     page on screen is that page's business; anything else is shown again, in place. */
  var nav = { inst: 0, idx: 0, seq: 0, ctl: null, shown: '', pushing: '', suppress: 0,
              hp: null, hr: null, at: {}, scripts: {}, armed: false, leaving: false, here: '',
              side: null, from: 0, scrollStop: null };
  var NAV_HX = ['hx-get', 'hx-post', 'hx-put', 'hx-patch', 'hx-delete', 'hx-boost'];
  function navOn() {
    return !!(nav.armed && doc.documentElement.getAttribute('data-k-nav') === '1' &&
      doc.querySelector('#main main.container[data-k-page]'));
  }
  function navKey(href) {
    try { var u = new URL(href, root.location.href); return u.pathname + u.search; } catch (e) { return ''; }
  }
  function navNow() { return root.performance && root.performance.now ? root.performance.now() : Date.now(); }
  function navFallback(href) { var e = new Error('classic page load'); e.kNavUrl = href; return e; }
  // Only the app's pages: never a file, an API answer or a download (a missed one still works:
  // a reply that is not a page falls back to the classic load).
  function navPage(url) {
    var p = url.pathname;
    if (/^\/(static|api)\//.test(p) || p === '/sw.js') return false;
    return !/\.[a-z0-9]{2,5}$/i.test(p) && !/\/(download|export)[^/]*(\/|$)/i.test(p);
  }

  /* History entries: stamped as they are made, by anyone (this controller, htmx, a page). */
  function navState(state, kind) {
    if (state !== null && state !== undefined && typeof state !== 'object') return state;
    var s = {};
    if (state) Object.keys(state).forEach(function (k) { s[k] = state[k]; });
    if (s.htmx) { delete s.htmx; kind = 'hx'; }  // htmx's own history restore stands down
    s.kNav = { inst: nav.inst, idx: nav.idx, kind: kind };
    return s;
  }
  function navStamp(state, kind) {
    var s = navState(state, kind);
    if (s && s.kNav && s.kNav.kind !== 'page') nav.shown = navKey(root.location.href);
    return s;
  }
  function navWrap() {
    var H = root.history;
    nav.hp = H.pushState; nav.hr = H.replaceState;
    H.pushState = function (state, title, url) {
      // Back or Forward already moved the address; the page still on screen is leaving and its
      // own history writes would land on the entry it is leaving for.
      if (nav.leaving && !nav.pushing) return;
      if (!nav.pushing) nav.at[nav.idx] = root.scrollY || 0;  // a commit saved it before the swap
      nav.idx += 1;
      var s = navState(state, nav.pushing || 'page');
      nav.hp.call(H, s, title || '', url);
      nav.here = navKey(root.location.href);
      if (s && s.kNav && s.kNav.kind !== 'page') nav.shown = nav.here;
    };
    H.replaceState = function (state, title, url) {
      if (nav.leaving && !nav.pushing) return;
      var cur = H.state && H.state.kNav;
      var s = navState(state, nav.pushing || (cur && cur.kind) || 'doc');
      nav.hr.call(H, s, title || '', url);
      nav.here = navKey(root.location.href);
      if (s && s.kNav && s.kNav.kind !== 'page') nav.shown = nav.here;
    };
    nav.hr.call(H, navState(H.state, 'doc'), '', root.location.href);
    nav.shown = nav.here = navKey(root.location.href);
    navSideSave();
    // Scroll positions are this controller's to restore, after the content is in place.
    try { H.scrollRestoration = 'manual'; } catch (e) { /* older engine */ }
    // htmx keeps no copies of pages in localStorage: Back never restores a stale snapshot.
    if (root.htmx && root.htmx.config) root.htmx.config.historyCacheSize = 0;
  }
  // A jump to #section within the page makes a browser entry of its own, with no state.
  function navHash() {
    var H = root.history;
    nav.here = navKey(root.location.href);
    if (H.state && H.state.kNav) return;
    nav.idx += 1;
    nav.hr.call(H, navState(H.state, 'page'), '', root.location.href);
  }
  function navPop(e) {
    if (nav.suppress) { nav.suppress -= 1; e.stopImmediatePropagation(); return; }
    var k = e.state && e.state.kNav;
    var here = navKey(root.location.href);
    var was = nav.here;
    nav.here = here;
    // A page change still loading is overtaken: Back or Forward has the last word.
    if (nav.ctl) {
      if (nav.leaving) nav.idx = nav.from;  // the cancelled entry was never shown
      nav.ctl.abort(); nav.ctl = null; nav.seq += 1; nav.leaving = false; navSettle(true);
    }
    if ((k && k.inst === nav.inst && (k.kind === 'page' || here === nav.shown)) ||
        (!k && here === nav.shown)) {
      nav.at[nav.idx] = root.scrollY || 0;
      if (k) nav.idx = k.idx;
      // Only the #section changed: back to where that entry was, as the browser would.
      if (was === here) navScrollEntry(k);
      return;  // the page on screen restores its own entry (its own popstate listener)
    }
    // The page being left must not act on an entry it no longer owns.
    e.stopImmediatePropagation();
    if (root.kLeaveGuard && !root.kLeaveGuard()) {
      // Unsaved changes: back to where this page was, and its Save bar says why.
      if (k && k.idx !== nav.idx) { nav.suppress += 1; root.history.go(nav.idx - k.idx); }
      return;
    }
    nav.at[nav.idx] = root.scrollY || 0;
    nav.from = nav.idx;
    if (k) nav.idx = k.idx;
    nav.leaving = true;
    navigate(root.location.href, { pop: true });
  }

  /* The fetched page: what it needs and whether this window can show it in place. */
  function navBuild(d) {
    var s = d.querySelector('script[src*="/static/js/kestrel-ui.js"]');
    return s ? s.getAttribute('src') : '';
  }
  function navLinks(d) {
    var out = [];
    Array.prototype.forEach.call(d.querySelectorAll('#sidebar .mainnav > a'), function (a) {
      out.push(a.getAttribute('href') + '|' + (a.getAttribute('title') || ''));
    });
    var ts = d.querySelector('#sidebar .track-switch');
    return out.join('\n') + '\n' + (ts ? ts.getAttribute('data-track') : '');
  }
  function navAbs(href) { return new URL(href, root.location.href).href; }
  function navSheets(d) {
    Array.prototype.forEach.call(doc.querySelectorAll('link[data-k-pending]'), function (l) { l.remove(); });
    var have = Array.prototype.slice.call(doc.head.querySelectorAll('link[rel="stylesheet"]'));
    var haveKeys = have.map(function (l) { return navAbs(l.getAttribute('href')); });
    var keep = [], add = [], waits = [], prev = null;
    // The head's sheets, then any a partial in the page content brings (the Score again page's
    // dialog): a page load applies those as it reaches them, so they are ready before the swap.
    var want = Array.prototype.slice.call(d.head.querySelectorAll('link[rel="stylesheet"][href]'))
      .concat(Array.prototype.slice.call(
        d.querySelectorAll('#main main.container link[rel="stylesheet"][href]')));
    want.forEach(function (w) {
      var href = navAbs(w.getAttribute('href'));
      var i = haveKeys.indexOf(href);
      if (i >= 0) { if (keep.indexOf(have[i]) < 0) keep.push(have[i]); prev = have[i]; return; }
      if (add.some(function (l) { return l.href === href; })) return;
      var l = doc.createElement('link');
      l.rel = 'stylesheet';
      l.setAttribute('data-k-pending', w.getAttribute('media') || '');
      l.media = 'print';  // fetched now, applied at the commit
      waits.push(new Promise(function (ok) { l.onload = ok; l.onerror = ok; }));
      l.href = href;
      // The cascade keeps its order: right after the sheet before it in the new page.
      if (prev) prev.parentNode.insertBefore(l, prev.nextSibling);
      else if (have[0]) have[0].parentNode.insertBefore(l, have[0]);
      else doc.head.appendChild(l);
      add.push(l);
      prev = l;
    });
    return { add: add, drop: have.filter(function (h) { return keep.indexOf(h) < 0; }),
             ready: Promise.all(waits) };
  }
  function navScripts(d, sel) {
    var out = [];
    Array.prototype.forEach.call(doc.querySelectorAll('script[src]'), function (s) {
      nav.scripts[navAbs(s.getAttribute('src'))] = true;
    });
    Array.prototype.forEach.call(d.querySelectorAll(sel), function (s) {
      var src = navAbs(s.getAttribute('src'));
      if (!nav.scripts[src] && out.indexOf(src) < 0) out.push(src);
    });
    return out;
  }
  function navLoad(list, dest) {
    return list.reduce(function (chain, src) {
      return chain.then(function () {
        return new Promise(function (ok, bad) {
          var s = doc.createElement('script');
          s.async = false;
          s.onload = function () { nav.scripts[src] = true; ok(); };
          s.onerror = function () { s.remove(); bad(navFallback(dest)); };
          s.src = src;
          doc.head.appendChild(s);
        });
      });
    }, Promise.resolve());
  }
  function navPrepare(res, opts) {
    var d = new DOMParser().parseFromString(res.text, 'text/html');
    var main = d.querySelector('#main main.container[data-k-page]');
    if (!main || !doc.querySelector('#main main.container[data-k-page]')) return null;
    if (navBuild(d) !== navBuild(doc) || navLinks(d) !== navLinks(doc)) return null;
    var name = main.getAttribute('data-k-page') || 'page';
    var js = navScripts(d, 'head script[src]');
    // A page script not on the lifecycle would start itself as it loads, over this page.
    if (js.length && name === 'page') return null;
    // A script a partial in the content loads (the model picker's) is loaded ahead as well, so
    // it is there when the content arrives; it runs again as it lands, as on a page load.
    var body = navScripts(d, '#main main.container script[src]').filter(function (src) {
      return js.indexOf(src) < 0;
    });
    return { d: d, main: main, name: name, url: res.url, title: d.title, js: js, body: body,
             css: navSheets(d), pop: !!opts.pop, replace: !!opts.replace };
  }

  /* The commit, in one frame. */
  function navScript(old) {
    var type = (old.getAttribute('type') || '').toLowerCase();
    if (type && !/^(text|application)\/javascript$|^module$/.test(type)) return old;  // data
    var s = doc.createElement('script');
    Array.prototype.forEach.call(old.attributes, function (a) { s.setAttribute(a.name, a.value); });
    if (s.src) s.async = false;
    s.text = old.text;
    return s;
  }
  // A parsed document's scripts never run; inline ones are made anew so they run as they land,
  // in order, as they would in a fresh document.
  function navAdopt(from) {
    return Array.prototype.map.call(from.childNodes, function (n) {
      var live = doc.importNode(n, true);
      if (live.nodeType !== 1) return live;
      if (live.tagName === 'SCRIPT') return navScript(live);
      Array.prototype.forEach.call(live.querySelectorAll('script'), function (o) {
        var s = navScript(o);
        if (s !== o) o.parentNode.replaceChild(s, o);
      });
      return live;
    });
  }
  function navShell(d) {
    // The sidebar, the phone bar and the More sheet: the highlight moves, nothing is redrawn.
    ['#sidebar .mainnav > a', '.bottombar a', 'nav[aria-label="All pages"] a'].forEach(function (sel) {
      var a = doc.querySelectorAll(sel), b = d.querySelectorAll(sel);
      if (a.length !== b.length) return;
      for (var i = 0; i < a.length; i++) {
        if (a[i].className !== b[i].className) a[i].className = b[i].className;
        var cur = b[i].getAttribute('aria-current');
        if (cur) a[i].setAttribute('aria-current', cur); else a[i].removeAttribute('aria-current');
      }
    });
    // Preferences the server renders on <html> (a save in Settings shows on the next page).
    var h = doc.documentElement, nh = d.documentElement;
    ['data-text-size', 'data-reduce-motion', 'data-k-nav'].forEach(function (n) {
      var v = nh.getAttribute(n);
      if (v !== null && h.getAttribute(n) !== v) h.setAttribute(n, v);
    });
    var own = null;
    try { own = root.localStorage.getItem('kestrel-scheme'); } catch (e) { /* storage off */ }
    var sc = nh.getAttribute('data-scheme');
    if (!own && sc && h.getAttribute('data-scheme') !== sc) h.setAttribute('data-scheme', sc);
  }
  // moveBefore keeps a node's running animations through the move; insertBefore restarts them.
  function navMove(parent, node, ref) {
    if (parent.moveBefore) {
      try { parent.moveBefore(node, ref); return; } catch (e) { /* not movable here */ }
    }
    parent.insertBefore(node, ref);
  }
  function navArtKey(el) { return (el.getAttribute('data-scene') || '') + '|' + (el.getAttribute('data-touch') || ''); }
  // The scene art stays when the next page draws the same scene and touch: the drawing (and its
  // running animation) moves into the next band instead of being redrawn.
  function navHoldArt(cur, next) {
    var a = cur.querySelector('.k-scene'), b = next.querySelector('.k-scene');
    if (!a || !b || navArtKey(a) !== navArtKey(b) || a.getAttribute('data-rendered') !== navArtKey(a)) return null;
    var art = Array.prototype.filter.call(a.children, function (n) { return n.classList.contains('k-scene__art'); });
    if (!art.length) return null;
    art.forEach(function (n) { navMove(cur.parentNode, n, cur); });  // out of the content, still live
    return { art: art, key: navArtKey(a) };
  }
  function navPlaceArt(held, cur) {
    if (!held) return;
    var b = cur.querySelector('.k-scene');
    if (!b || navArtKey(b) !== held.key) { held.art.forEach(function (n) { n.remove(); }); return; }
    var first = b.firstChild;
    held.art.forEach(function (n) { navMove(b, n, first); });
    b.setAttribute('data-rendered', held.key);
  }
  function navScrollEntry(k) {
    if (k && nav.at[k.idx] !== undefined) { root.scrollTo(0, nav.at[k.idx]); return; }
    var id = root.location.hash ? decodeURIComponent(root.location.hash.slice(1)) : '';
    var t = id ? doc.getElementById(id) : null;
    if (t && t.scrollIntoView) t.scrollIntoView();
  }
  // Back to a page whose sections load after it arrives (Today, About): the saved position is
  // put back again as they settle, until it is reached, the person scrolls, or a moment passes.
  function navScrollLater(y) {
    if (nav.scrollStop) nav.scrollStop();
    if (!y || (root.scrollY || 0) >= y - 2) return;
    var stop = function () {
      nav.scrollStop = null;
      doc.removeEventListener('htmx:afterSettle', again);
      ['wheel', 'touchstart', 'keydown', 'mousedown'].forEach(function (t) {
        root.removeEventListener(t, stop, true);
      });
      root.clearTimeout(timer);
    };
    var again = function () {
      root.scrollTo(0, y);
      if ((root.scrollY || 0) >= y - 2) stop();
    };
    var timer = root.setTimeout(stop, 2000);
    nav.scrollStop = stop;
    doc.addEventListener('htmx:afterSettle', again);
    ['wheel', 'touchstart', 'keydown', 'mousedown'].forEach(function (t) {
      root.addEventListener(t, stop, true);
    });
  }
  // The sidebar highlight moves on the click (a click visibly lands); a page change that does
  // not happen puts it back.
  function navSideSave() {
    nav.side = Array.prototype.map.call(doc.querySelectorAll('#sidebar .mainnav > a'), function (a) {
      return [a, a.className, a.getAttribute('aria-current')];
    });
  }
  function navScroll(p) {
    if (p.pop) {
      root.scrollTo(0, nav.at[nav.idx] || 0);
      navScrollLater(nav.at[nav.idx] || 0);
      return;
    }
    var id = p.url.hash ? decodeURIComponent(p.url.hash.slice(1)) : '';
    var t = id ? doc.getElementById(id) : null;
    if (t && t.scrollIntoView) t.scrollIntoView(); else root.scrollTo(0, 0);
  }
  function navSettle(undo) {
    if (undo && nav.side) {
      nav.side.forEach(function (x) {
        x[0].className = x[1];
        if (x[2]) x[0].setAttribute('aria-current', x[2]); else x[0].removeAttribute('aria-current');
      });
    }
    doc.documentElement.classList.remove('k-going');
    Array.prototype.forEach.call(doc.querySelectorAll('.k-pressed-going'), function (n) {
      n.classList.remove('k-pressed-going');
    });
  }
  function navApply(p) {
    if (p.seq !== nav.seq) return;
    nav.ctl = null;
    if (nav.scrollStop) nav.scrollStop();
    var cur = doc.querySelector('#main main.container[data-k-page]');
    var from = root.location.pathname;
    var href = p.url.pathname + p.url.search + p.url.hash;
    p.t2 = navNow();
    if (!p.pop) nav.at[nav.idx] = root.scrollY || 0;  // where the page being left was
    var moved = p.pop;  // the address is already the new page's
    try {
      // The page being left: its overlays (the page behind them is live again), its open
      // requests (their replies have nowhere to go), its lifecycle.
      closeAll();
      if (root.htmx) {
        Array.prototype.forEach.call(cur.querySelectorAll('[' + NAV_HX.join('],[') + ']'), function (el) {
          root.htmx.trigger(el, 'htmx:abort');
        });
      }
      pageUnmount();
      // The new page's stylesheets apply and the old page's go, in the same frame.
      p.css.add.forEach(function (l) {
        var m = l.getAttribute('data-k-pending');
        if (m) l.media = m; else l.removeAttribute('media');
        l.removeAttribute('data-k-pending');
      });
      p.css.drop.forEach(function (l) { l.remove(); });
      doc.title = p.title || doc.title;
      navShell(p.d);
      var held = navHoldArt(cur, p.main);
      // An element whose trigger listens "from:" the document or the window leaves through
      // htmx, which lets go of that listener; htmx drops nothing else outside the element.
      if (root.htmx && root.htmx.swap) {
        Array.prototype.forEach.call(
          cur.querySelectorAll('[hx-trigger*="from:"], [data-hx-trigger*="from:"]'), function (el) {
            if (el.isConnected) {
              root.htmx.swap(el, '', { swapStyle: 'delete', swapDelay: 0, settleDelay: 0,
                                       transition: false });
            }
          });
      }
      cur.setAttribute('data-k-page', p.name);
      cur.replaceChildren.apply(cur, navAdopt(p.main));
      navPlaceArt(held, cur);
      // History and position.
      nav.inst += 1;
      if (p.pop) {
        nav.hr.call(root.history, navState(root.history.state, 'doc'), '', href);
        nav.shown = nav.here = navKey(root.location.href);
      } else {
        nav.pushing = 'doc';
        try {
          if (p.replace) root.history.replaceState(null, '', href);
          else root.history.pushState(null, '', href);
          moved = true;
        } finally { nav.pushing = ''; }
      }
      nav.leaving = false;  // the page that was leaving has unmounted; history is the new page's
      navScroll(p);
      // The arrival (the same motion as a page load), then the page itself.
      var h = doc.documentElement;
      h.classList.remove('k-intro', 'k-from-today');
      h.classList.add('k-page-in');
      if (from === '/' && p.url.pathname !== '/') h.classList.add('k-from-today');
      motionTargets();
      if (root.KSceneBand) root.KSceneBand.renderAll(cur);
    } catch (err) {
      report(err);
      // Halfway is worse than a fresh document; the address already says where it goes.
      if (moved) root.location.replace(href); else root.location.assign(href);
      return null;
    }
    navSideSave();
    navSettle();
    // The rest in the order a page load has it: Alpine starts the new content's components
    // (its observer runs first in this microtask queue), then the page mounts, then htmx
    // processes the content and announces it.
    return Promise.resolve().then(function () {
      if (p.seq !== nav.seq) return;
      pageMount(p.name, cur);
      var h1 = cur.querySelector('h1');
      var ae = doc.activeElement;
      // Never from what the person is using (the palette, a toast, the bell) or from what the
      // page focused as it mounted; only from the link that was pressed, or from nothing.
      var free = !ae || ae === doc.body || ae === cur || !ae.isConnected ||
        !!(ae.closest && ae.closest('#sidebar, .bottombar, nav[aria-label="All pages"]'));
      if (h1 && free) {
        if (!h1.hasAttribute('tabindex')) h1.setAttribute('tabindex', '-1');
        h1.setAttribute('data-k-landing', '');
        try { h1.focus({ preventScroll: true }); } catch (e) { /* not focusable here */ }
      }
      if (root.htmx) {
        root.htmx.process(cur);
        // Announced before the new page is painted: whatever upgrades content on htmx:load
        // (the model picker) does so before anyone sees the plain version.
        root.htmx.trigger(cur, 'htmx:load');
      } else {
        init(cur);
      }
      navDiag(p, from);
    }).catch(report);
  }
  function navCommit(p) {
    var h = doc.documentElement;
    var still = h.getAttribute('data-reduce-motion') === '1' ||
      !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
    p.vt = false;
    if (doc.startViewTransition && !still && !doc.hidden) {
      try {
        // The same cross-fade as a page load (components.css): the sidebar and the art hold
        // still, the page column fades in over the old one.
        var vt = doc.startViewTransition(function () { return navApply(p); });
        p.vt = true;
        [vt.ready, vt.finished, vt.updateCallbackDone].forEach(function (pr) {
          if (pr && pr.catch) pr.catch(function () { /* a skipped transition still commits */ });
        });
        return;
      } catch (e) { p.vt = false; }
    }
    navApply(p);
  }
  // The page-change recorder (D-UI-204), desktop window only: one log line per change.
  function navDiag(p, from) {
    try {
      if (doc.documentElement.getAttribute('data-k-desktop') !== '1' || !root.navigator.sendBeacon) return;
      var rec = { to: root.location.pathname, came_from: from, vt: p.vt, inplace: true,
        fetch_ms: Math.round(p.t1 - p.t0), swap_ms: Math.round(navNow() - p.t2), hidden: doc.hidden };
      root.navigator.sendBeacon('/api/diag/page', new Blob([JSON.stringify(rec)], { type: 'application/json' }));
    } catch (e) { /* diagnostics never get in the way */ }
  }

  /* navigate(url, {pop, replace, guarded}): the one way to another page. */
  function navigate(href, opts) {
    opts = opts || {};
    var url;
    try { url = new URL(href, root.location.href); } catch (e) { return Promise.resolve(false); }
    var classic = function (to) {
      if (opts.pop) root.location.replace(to); else root.location.assign(to);
      return false;
    };
    if (!navOn() || url.origin !== root.location.origin) return Promise.resolve(classic(url.href));
    if (!opts.pop && !opts.guarded && root.kLeaveGuard && !root.kLeaveGuard()) {
      navSettle(true);
      return Promise.resolve(false);
    }
    if (nav.ctl) nav.ctl.abort();
    if (!opts.pop) nav.leaving = false;
    // KUI.go to the page on screen (a toast's link) shows it again in the same entry.
    if (opts.replace === undefined && !opts.pop) {
      opts.replace = navKey(url.href) === navKey(root.location.href);
    }
    var ctl = nav.ctl = new AbortController();
    var p = { seq: ++nav.seq, t0: navNow() };
    doc.documentElement.classList.add('k-going');
    // The page on screen is about to be left: a save it was holding back goes out now, before
    // the next page is read (Sources' switch).
    try { doc.dispatchEvent(new CustomEvent('k-page-leave', { detail: { url: url.href } })); }
    catch (e) { report(e); }
    return root.fetch(url.href, { headers: { Accept: 'text/html' }, credentials: 'same-origin',
                                  signal: ctl.signal })
      .then(function (r) {
        var final = new URL(r.url || url.href);
        final.hash = url.hash;  // a redirect keeps the link's #section
        if (final.origin !== root.location.origin ||
            !/text\/html/i.test(r.headers.get('content-type') || '')) {
          ctl.abort();  // a file or another site: not read here, the classic way gets it
          var f = navFallback(final.href);
          f.kNavFile = true;
          throw f;
        }
        return r.text().then(function (text) { return { text: text, url: final }; });
      })
      .then(function (res) {
        if (p.seq !== nav.seq) return false;
        p.t1 = navNow();
        var prep = navPrepare(res, opts);
        if (!prep) throw navFallback(res.url.href);
        Object.keys(prep).forEach(function (k) { p[k] = prep[k]; });
        var timer = 0;
        var late = new Promise(function (ok, bad) {
          timer = root.setTimeout(function () { bad(navFallback(res.url.href)); }, 8000);
        });
        return Promise.race([Promise.all([p.css.ready, navLoad(p.js.concat(p.body), res.url.href)]), late]).then(function () {
          root.clearTimeout(timer);
          if (p.seq !== nav.seq) return false;
          if (p.js.length && !pageDefs[p.name]) throw navFallback(res.url.href);
          // Edited while it loaded: the guard has the last word.
          if (!opts.pop && root.kLeaveGuard && !root.kLeaveGuard()) {
            nav.ctl = null;
            navSettle(true);
            return false;
          }
          navCommit(p);
          return true;
        }, function (err) { root.clearTimeout(timer); throw err; });
      })
      .catch(function (err) {
        if (p.seq !== nav.seq || (err && err.name === 'AbortError')) return false;
        nav.ctl = null;
        nav.leaving = false;
        if (err && err.kNavFile) navSettle(true);  // a download: this page stays
        else if (!opts.pop && root.kLeaveGuard && !root.kLeaveGuard()) { navSettle(true); return false; }
        return classic(err && err.kNavUrl ? err.kNavUrl : url.href);
      });
  }
  // KUI.go(url): what a page script calls instead of location.href = url.
  function navGo(href, opts) {
    if (navOn()) return navigate(href, opts);
    root.location.assign(href);
    return Promise.resolve(false);
  }
  function navClick(e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || !navOn()) return;
    if ((a.target && a.target !== '_self') || a.hasAttribute('download') || a.closest('[data-k-reload]')) return;
    for (var i = 0; i < NAV_HX.length; i++) {
      if (a.hasAttribute(NAV_HX[i]) || a.hasAttribute('data-' + NAV_HX[i])) return;
    }
    var raw = a.getAttribute('href') || '';
    if (!raw || raw === '#' || (/^[a-z][a-z0-9+.-]*:/i.test(raw) && !/^https?:/i.test(raw))) return;
    var url;
    try { url = new URL(a.href, root.location.href); } catch (err) { return; }
    if (url.origin !== root.location.origin || !navPage(url)) return;
    var same = navKey(url.href) === navKey(root.location.href);
    if (same && url.hash) {  // a jump within this page
      nav.at[nav.idx] = root.scrollY || 0;
      if (url.hash === root.location.hash) {
        // The browser would replace the entry with one that has no state; scroll instead.
        var t = doc.getElementById(decodeURIComponent(url.hash.slice(1)));
        if (t && t.scrollIntoView) { e.preventDefault(); t.scrollIntoView(); }
      }
      return;
    }
    e.preventDefault();
    navigate(url.href, { replace: same });
  }
  function navStart() {
    if (!doc || !root.fetch || !root.DOMParser || !root.history || !root.history.pushState ||
        !root.AbortController || !root.Promise) return;
    if (doc.documentElement.getAttribute('data-k-nav') !== '1') return;
    nav.armed = true;
    navWrap();
    doc.addEventListener('click', navClick);
    root.addEventListener('popstate', navPop);
    root.addEventListener('hashchange', navHash);
  }

  /* -------------------------------------------------------------------------- init */
  function init(scope) {
    if (!doc) return;
    topTips();  // an out-of-band swap (the avatar) can bring a native title back
    if (root.KSceneBand) root.KSceneBand.renderAll(scope || doc);
    scanCenter(scope || doc);
    // A swapped-out overlay must not stay in the stack.
    stack.slice().forEach(function (o) { if (!o.el.isConnected) { stack.splice(stack.indexOf(o), 1); } });
    syncScrim();
  }

  var KUI = {
    setTheme: setTheme, setScheme: setScheme, applyStored: applyStored,
    toast: toast, dismissToasts: dismissToasts,
    openFlyout: function (r, o) { return open('flyout', r, o); },
    closeFlyout: function () { closeKind('flyout'); },
    openSlideOver: function (r, o) { return open('slideover', r, o); },
    closeSlideOver: function () { closeKind('slideover'); },
    openModal: function (r, o) { return open('modal', r, o); },
    closeModal: function () { closeKind('modal'); },
    closeAll: closeAll,
    center: center, centerTop: centerTop,
    setText: setText, foldOut: foldOut, arrive: arrive, prepend: prepend, moveTo: moveTo,
    init: init, SCHEMES: SCHEMES.slice(), THEMES: THEMES.slice(),
    frame: { init: frameInit, wire: wireFrame, state: frameState, closing: closingScreen, edgeAt: edgeAt },
    saved: saved, fold: fold, ink: ink, inkPos: inkPos, paneIn: paneIn, cue: cue,
    page: page, go: navGo, navigate: navigate
  };
  root.KUI = KUI;
  if (typeof module !== 'undefined' && module.exports) module.exports = KUI;

  if (doc && doc.addEventListener) {
    doc.addEventListener('click', function (e) { if (!onFrameClick(e) && !onFoldClick(e)) onClick(e); });
    // In-place navigation (D-UI-210): its popstate listener goes in now, ahead of htmx's.
    navStart();
    doc.addEventListener('mousedown', onFrameDown, true);  // first, before any drag region
    doc.addEventListener('dblclick', onFrameDbl);
    doc.addEventListener('mousemove', onFrameMove);
    doc.addEventListener('mouseup', onFrameUp);
    root.addEventListener('pywebviewready', frameInit);
    doc.addEventListener('keydown', onKey);
    doc.addEventListener('pointerdown', onOutside, true);
    doc.addEventListener('k-toast', function (e) {
      var d = e.detail || {};
      toast(d.message || d.value || '', d);
    });
    // htmx swaps new markup in: look at it again (scene bands, centered panels).
    // htmx:load fires for the swapped content (and once for the page); afterSwap would run twice.
    doc.addEventListener('htmx:load', function (e) { init(e.target); });
    // Swapped-in content never replays the page's arrival (components.css, data-k-late). Set
    // synchronously after the swap, before the browser styles the new nodes.
    doc.addEventListener('htmx:afterSwap', function (e) {
      var t = e.detail && e.detail.target;
      if (!t || !t.querySelectorAll) return;
      if (t.classList && t.classList.contains('k-rise')) t.setAttribute('data-k-late', '');
      Array.prototype.forEach.call(t.querySelectorAll('.k-rise'), function (n) {
        n.setAttribute('data-k-late', '');
      });
    });
    var ready = function () {
      applyStored(); motionTargets(); topTips(); init(doc); watchCenter(); frameInit();
      if (doc.querySelector('.k-main')) {
        cue(null);
        var nav = doc.querySelector('.side .mainnav');
        if (nav) cue(nav);  // a short window scrolls the menu above the status readout
        cueWatch();  // content that loads in later changes the length: observed, not polled
      }
    };
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', ready); else ready();
  }
})(typeof window !== 'undefined' ? window : globalThis);
