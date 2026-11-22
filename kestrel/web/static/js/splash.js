/* The loading page's behavior (moved out of splash.html). It talks to the shell only over HTTP
   to the server that served it (kestrel/desktop/splash_server.py): it long-polls events?after=N
   for the shell's updates and runs each one named in KSplash, and it posts call/<name> for a
   button. No eval and no pywebview bridge: the app's policy refuses both on WKWebView (D-SEC-61). */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var noteTimer = null;
  // A call to the shell: resolves to its answer, or null when the shell is not there.
  function api(name, args) {
    return fetch('call/' + name, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ args: args || [] })
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { return j ? j.value : null; })
      .catch(function () { return null; });
  }
  function bar(pct) {
    var b = $('bar');
    if (pct === null || pct === undefined) { b.classList.add('is-indet'); b.firstElementChild.style.width = ''; }
    else { b.classList.remove('is-indet'); b.firstElementChild.style.width = pct + '%'; }
  }
  function note(text) { $('note').textContent = text || ''; $('note').classList.toggle('is-on', !!text); }
  // Cancel appears only while the start is waiting (the shell drives it); a new attempt re-arms it.
  function cancelBtn(show) { $('cancelRow').style.display = show ? 'flex' : 'none'; if (show) $('cancel').disabled = false; }
  function show(which) {
    $('sp').style.display = which === 'prob' ? 'none' : '';
    $('prob').classList.toggle('is-on', which === 'prob');
  }
  // One status line that cross-fades; pct null is the indeterminate bar. A later note (the
  // "this can take a minute" line) shows after `noteAfterMs` unless the status moves on.
  function say(text, pct, later, noteAfterMs) {
    show('sp');
    var s = $('status');
    clearTimeout(noteTimer);
    note('');
    if (s.textContent !== text) {
      s.classList.add('is-swap');
      setTimeout(function () { s.textContent = text; s.classList.remove('is-swap'); }, 220);
    }
    bar(pct);
    if (later) noteTimer = setTimeout(function () { note(later); }, noteAfterMs || 0);
  }
  function fail(reason, details, code, errorCode) {
    clearTimeout(noteTimer);
    cancelBtn(false);
    var dl = $('download');
    var is_newer = code === 'newer_database';
    dl.style.display = is_newer ? 'inline-block' : 'none';
    $('retry').style.display = is_newer ? 'none' : '';
    $('retry').disabled = false;
    var offer = code === 'safe_mode_offer';
    $('safe').style.display = offer ? 'inline-block' : 'none';
    $('safe').disabled = false;
    $('retry').classList.toggle('p', !offer);
    $('reason').textContent = reason || '';
    // The code sits under the reason and leads Show details (SPEC-INTERFACES 2.7). Text only.
    var ec = $('ecode');
    ec.textContent = errorCode ? 'Code ' + errorCode : '';
    ec.hidden = !errorCode;
    $('det').textContent = (errorCode ? 'Code ' + errorCode + '\n' : '') + (details || '');
    $('det').classList.remove('is-on');
    $('detBtn').textContent = 'Show details';
    $('detBtn').setAttribute('aria-expanded', 'false');
    show('prob');
    (is_newer ? dl : $('retry')).focus();
  }
  function ready() { cancelBtn(false); say('Ready', 100); setTimeout(function () { $('splash').classList.add('is-out'); }, 250); }
  // One start at a time: the button is disabled until this attempt ends on the card again.
  $('retry').addEventListener('click', function () {
    if ($('retry').disabled) return;
    $('retry').disabled = true;
    say('Opening your data', 10);
    api('splash_retry');
  });
  $('safe').addEventListener('click', function () {
    if ($('safe').disabled) return;
    $('safe').disabled = true;
    $('retry').disabled = true;
    say('Opening your data', 10);
    api('splash_safe_start');
  });
  $('download').addEventListener('click', function () { api('splash_download'); });
  $('cancel').addEventListener('click', function () {
    if ($('cancel').disabled) return;
    $('cancel').disabled = true;
    api('splash_cancel');
  });
  $('logs').addEventListener('click', function () { api('splash_open_logs'); });
  $('quit').addEventListener('click', function () { api('splash_quit'); });
  $('detBtn').addEventListener('click', function () {
    var on = !$('det').classList.contains('is-on');
    $('det').classList.toggle('is-on', on);
    $('detBtn').textContent = on ? 'Hide details' : 'Show details';
    $('detBtn').setAttribute('aria-expanded', on ? 'true' : 'false');
  });
  // The window controls, as on every page (Windows and Linux drawn; macOS keeps its own).
  var os = '';
  function frame() {
    api('window_info').then(function (info) {
      if (!info) return;
      os = info.os || 'win';
      if (os !== 'mac') $('wctl').hidden = false;
    });
  }
  $('wctl').addEventListener('click', function (e) {
    var b = e.target.closest('[data-wc]');
    if (!b) return;
    var w = b.getAttribute('data-wc');
    api(w === 'min' ? 'window_minimize' : w === 'max' ? 'window_toggle_maximize' : 'window_close');
  });
  // Windows hands the drag to its own move loop, which swallows the mouse-up: a second press on
  // the same spot within the double-click time maximizes instead (as in kestrel-ui.js).
  var lastDown = null;
  document.addEventListener('mousedown', function (e) {
    if (os !== 'win' || e.button !== 0 || !e.target.hasAttribute('data-k-drag')) return;
    var now = Date.now();
    if (lastDown && now - lastDown.t <= 500 && Math.abs(e.clientX - lastDown.x) <= 4 &&
        Math.abs(e.clientY - lastDown.y) <= 4) {
      lastDown = null; api('window_toggle_maximize'); return;
    }
    lastDown = { t: now, x: e.clientX, y: e.clientY };
    api('window_start_drag');
  });
  document.addEventListener('dblclick', function (e) {
    if (os !== 'win' && e.target.hasAttribute('data-k-drag')) api('window_toggle_maximize');
  });
  window.KSplash = { say: say, fail: fail, ready: ready, note: note, cancelBtn: cancelBtn };
  bar(8);
  frame();
  // The shell's updates, in order: every event since the last one seen, replayed from the first
  // on a reload. A name not in KSplash is ignored. A failed fetch waits and asks again; the
  // shell stops answering once the app page has replaced this one.
  var seq = 0, misses = 0;
  function poll() {
    fetch('events?after=' + seq, { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('status ' + r.status); return r.json(); })
      .then(function (j) {
        misses = 0;
        (j.events || []).forEach(function (ev) {
          var fn = window.KSplash[ev[0]];
          if (typeof fn === 'function' && Object.prototype.hasOwnProperty.call(window.KSplash, ev[0])) {
            fn.apply(null, Array.isArray(ev[1]) ? ev[1] : []);
          }
        });
        seq = typeof j.seq === 'number' ? j.seq : seq;
        document.documentElement.setAttribute('data-splash-seq', String(seq));  // applied so far
        poll();
      })
      .catch(function () { misses += 1; setTimeout(poll, Math.min(2000, 100 * misses)); });
  }
  poll();
})();
