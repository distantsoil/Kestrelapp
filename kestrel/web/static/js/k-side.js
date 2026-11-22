/* The sidebar: repaint the counts and readout this window last showed, then the rail, pin
   and toggle behavior (moved out of base.html, D-INST-8). Loaded in <head>; it runs when the
   parser inserts the <i data-k-init="side"> marker at the end of the sidebar markup, where the
   inline script was, from k-head.js's observer, so before the first paint (an external script
   there would let the browser paint the page before it runs). */
window.KStyle.init('side', function () {
  try {
    var c = JSON.parse(sessionStorage.getItem('kestrel-side-counts') || '{}');
    Object.keys(c).forEach(function (id) {
      var el = /^nav-count-[a-z0-9-]+$/.test(id) && document.getElementById(id);
      if (el) el.textContent = String(c[id]);
    });
    var ring = document.getElementById('nav-ring');
    if (ring && c['nav-ring'] === '1') ring.hidden = false;
    var r = sessionStorage.getItem('kestrel-side-rail');
    var rail = document.getElementById('rail');
    if (r && rail) {
      var body = new DOMParser().parseFromString(r, 'text/html').body;
      if (!body.querySelector('script')) rail.replaceChildren.apply(rail, Array.prototype.slice.call(body.childNodes));
    }
  } catch (e) {}
  // The sidebar's forms (#126, D-UI-249). In the page it is the full sidebar (pinned) or the
  // 68 px rail. The rail is chosen (the kestrel-side cookie) or forced by the window width.
  // Opening the rail draws the full sidebar OVER the page; it closes after a short idle
  // time, on Esc, on a click outside and after choosing a page. The pushpin keeps it in the
  // page (kestrel-side-pin). The server reads both cookies for the first paint; every
  // storage access is guarded because either store may be blocked.
  var kwin = document.getElementById('kwin');
  var btn = document.querySelector('.k-side-toggle');
  var pin = document.querySelector('.k-side-pin');
  var side = document.getElementById('sidebar');
  if (kwin && btn && pin && side) {
    var root = document.documentElement;
    var mq1100 = window.matchMedia('(max-width: 1100px)');
    var mq900 = window.matchMedia('(max-width: 899px)');
    var mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
    var hint = mac ? ' (⌘B)' : ' (Ctrl B)';
    var cookieOf = function (n) {
      var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([01])'));
      return m ? m[1] : null;
    };
    var stored = function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } };
    var cs = cookieOf('kestrel-side'), cp = cookieOf('kestrel-side-pin');
    var collapsed = cs !== null ? cs === '1' : stored('kestrel-side-collapsed') === '1';
    var pinned = !collapsed && (cp !== null ? cp === '1' : stored('kestrel-side-pin') === '1');
    var open = false, closing = false, settling = false, hovering = false, idleTimer = null;
    var rail = function () { return collapsed || mq900.matches || (mq1100.matches && !pinned); };
    function render() {
      var r = rail();
      if (!r && !settling) { open = false; closing = false; }
      kwin.classList.toggle('is-rail', r);
      kwin.classList.toggle('is-side-open', (r && open) || settling);
      kwin.classList.toggle('is-side-closing', closing);
      kwin.classList.toggle('is-side-collapsed', collapsed);
      root.classList.toggle('k-side-collapsed', collapsed);
      root.classList.toggle('k-side-pinned', pinned);
      var expanded = !r || open;
      btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      btn.setAttribute('aria-label', expanded ? 'Collapse sidebar' : 'Expand sidebar');
      btn.title = (expanded ? 'Collapse sidebar' : 'Expand sidebar') + hint;
      // In the page the pin is "on"; pressing it returns the sidebar to the rail. On the
      // open rail it is "off"; pressing it keeps the sidebar in the page, if there is room.
      var inPage = !r;
      var tooSmall = r && mq900.matches;
      pin.setAttribute('aria-pressed', inPage ? 'true' : 'false');
      pin.setAttribute('aria-disabled', tooSmall ? 'true' : 'false');
      var pinLabel = tooSmall ? 'The window is too narrow to keep the sidebar open'
        : inPage ? 'Unpin sidebar (back to icons)' : 'Pin sidebar open';
      pin.setAttribute('aria-label', pinLabel);
      pin.title = pinLabel;
      armIdle();
    }
    function remember() {
      try {
        localStorage.setItem('kestrel-side-collapsed', collapsed ? '1' : '0');
        localStorage.setItem('kestrel-side-pin', pinned ? '1' : '0');
      } catch (e) {}
      var tail = '; path=/; max-age=31536000; samesite=lax';
      document.cookie = 'kestrel-side=' + (collapsed ? '1' : '0') + tail;
      document.cookie = 'kestrel-side-pin=' + (pinned ? '1' : '0') + tail;
    }
    // A browser with only the older localStorage copy moves it to the cookies once.
    if ((cs === null && collapsed) || (cp === null && pinned)) remember();
    // The grid glides on a press only (never on the restore at page load).
    function afterGlide(fn) {
      var done = false, t = null;
      function end() {
        if (done) return; done = true; clearTimeout(t);
        kwin.removeEventListener('transitionend', on); fn();
      }
      function on(e) { if (e.target === kwin && e.propertyName === 'grid-template-columns') end(); }
      kwin.addEventListener('transitionend', on);
      t = setTimeout(end, 700);
      if (!(parseFloat(getComputedStyle(kwin).transitionDuration) > 0)) end();
    }
    function collapseToRail() {
      collapsed = true; pinned = false; open = false; closing = false;
      kwin.classList.add('is-side-animating');
      root.classList.add('k-side-glide');
      remember(); render();
    }
    function openOverlay() {
      if (!rail() || (open && !closing)) return;
      open = true; closing = false; render();
    }
    function closeOverlay() {
      if (!open || closing) return;
      var a = getComputedStyle(side);
      if (a.animationName === 'none') { open = false; render(); return; }
      closing = true; render();
      var done = false;
      function end() {
        if (done) return; done = true; side.removeEventListener('animationend', on);
        if (closing) { open = false; closing = false; render(); }
      }
      function on(e) { if (e.target === side) end(); }
      side.addEventListener('animationend', on);
      setTimeout(end, 600);
    }
    function pinOpen() {
      if (mq900.matches) return;
      collapsed = false; pinned = true; settling = true;
      kwin.classList.add('is-side-animating');
      root.classList.add('k-side-glide');
      remember(); render();
      afterGlide(function () { settling = false; open = false; closing = false; render(); });
    }
    function armIdle() {
      clearTimeout(idleTimer);
      var f = document.activeElement;
      // Keyboard focus inside keeps it open; the focus a mouse press leaves on a button does not.
      if (!open || closing || hovering || (side.contains(f) && f.matches(':focus-visible'))) return;
      var ms = parseInt(side.dataset.idleMs, 10) || 5000;
      idleTimer = setTimeout(closeOverlay, ms);
    }
    function toggleSide() {
      if (!rail()) collapseToRail();
      else if (open && !closing) closeOverlay();
      else openOverlay();
    }
    render();
    btn.addEventListener('click', toggleSide);
    pin.addEventListener('click', function () {
      if (pin.getAttribute('aria-disabled') === 'true') return;
      if (!rail()) collapseToRail(); else pinOpen();
    });
    side.addEventListener('mouseenter', function () { hovering = true; armIdle(); });
    side.addEventListener('mouseleave', function () { hovering = false; armIdle(); });
    side.addEventListener('focusin', armIdle);
    side.addEventListener('focusout', function () { setTimeout(armIdle, 0); });
    // Choosing a page closes the overlay at once.
    side.addEventListener('click', function (e) {
      if (open && e.target.closest && e.target.closest('.mainnav a')) closeOverlay();
    });
    // A press anywhere outside the open sidebar closes it (mouse, pen and touch alike).
    document.addEventListener('pointerdown', function (e) {
      if (open && !closing && !side.contains(e.target)) closeOverlay();
    }, true);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && open && !closing && !e.defaultPrevented) {
        var had = side.contains(document.activeElement);
        closeOverlay();
        if (had) btn.focus();
      }
    });
    [mq1100, mq900].forEach(function (q) {
      if (q.addEventListener) q.addEventListener('change', render);
    });
    // Ctrl B (Cmd B on a Mac) toggles it. Not while the sidebar is hidden (phone width),
    // and never inside an editable field, where B belongs to the text.
    document.addEventListener('keydown', function (e) {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || String(e.key).toLowerCase() !== 'b') return;
      if (e.defaultPrevented || !btn.getClientRects().length) return;
      if (e.target && e.target.isContentEditable) return;
      e.preventDefault();
      toggleSide();
    });
  }
});
