/* Kestrel UI runtime (UI overhaul, Phase 1). Plain script, no build step, no dependencies.
   Works beside HTMX and Alpine: behavior is bound once, by delegation on the document, so
   nothing needs re-binding after a swap; init(root) only re-runs the parts that need a fresh
   look at new markup (scene bands, overlay centering). It runs again on htmx:load (the swapped content).

   Public API: window.KUI
     setTheme('light'|'dark'|'auto')  setScheme('indigo'|'teal'|'plum'|'graphite')  applyStored()
     toast(message, {undo: fn, undoUrl, timeout, tone})   dismissToasts()
     openFlyout(el|selector, {trigger})  closeFlyout()    right side, vertically centered
     openSlideOver(el|selector, {trigger})  closeSlideOver()   modal: focus trapped, page inert
     openModal(el|selector, {trigger})  closeModal()      closeAll()
     center(el)  centerTop(windowHeight, elementHeight)   overlay-centering helpers
     setText(el, text)  foldOut(el, done)  arrive(el)  prepend(list, el)  moveTo(el, list, opts)
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
    while (region.children.length >= MAX_TOASTS) region.removeChild(region.firstChild);
    var node = doc.createElement('div');
    node.className = 'k-toast';
    if (opts.tone) node.setAttribute('data-tone', opts.tone);
    var text = doc.createElement('span');
    text.textContent = String(message || '');
    node.appendChild(text);
    var timer = null;
    var gone = false;
    function dismiss() {
      if (gone) return;
      gone = true;
      clearTimeout(timer);
      node.classList.add('is-leaving');
      setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, reduced() ? 0 : 260);
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
      timer = setTimeout(dismiss, opts.timeout || (hasUndo ? 8000 : 5000));
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
    if (r) while (r.firstChild) r.removeChild(r.firstChild);
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

  function open(kind, ref, opts) {
    var el = el$(ref);
    opts = opts || {};
    if (!el) return null;
    // One overlay of a kind at a time. A flyout opens over a slide-over or a modal and leaves
    // them open; closing it returns to them.
    stack.slice().forEach(function (o) { if (o.kind === kind) close(o); });
    var opener = doc.activeElement;
    var trigger = opts.trigger || null;
    if (el.getAttribute('data-k-center') !== null) center(el);  // before the class: no jump
    el.setAttribute('aria-hidden', 'false');
    el.classList.add('is-open');
    markTrigger(trigger, true);
    stack.push({ el: el, kind: kind, trigger: trigger, opener: opener });
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

  /* -------------------------------------------------------------------------- init */
  function init(scope) {
    if (!doc) return;
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
    init: init, SCHEMES: SCHEMES.slice(), THEMES: THEMES.slice()
  };
  root.KUI = KUI;
  if (typeof module !== 'undefined' && module.exports) module.exports = KUI;

  if (doc && doc.addEventListener) {
    doc.addEventListener('click', onClick);
    doc.addEventListener('keydown', onKey);
    doc.addEventListener('pointerdown', onOutside, true);
    doc.addEventListener('k-toast', function (e) {
      var d = e.detail || {};
      toast(d.message || d.value || '', d);
    });
    // htmx swaps new markup in: look at it again (scene bands, centered panels).
    // htmx:load fires for the swapped content (and once for the page); afterSwap would run twice.
    doc.addEventListener('htmx:load', function (e) { init(e.target); });
    var ready = function () { applyStored(); init(doc); watchCenter(); };
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', ready); else ready();
  }
})(typeof window !== 'undefined' ? window : globalThis);
