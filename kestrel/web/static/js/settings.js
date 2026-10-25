/* Settings (D-UI-106): the behavior HTMX does not give the page. Section routing (with the old
   #system, #hosted, ... links), saving each control as it changes with a message and Undo, the
   online key entry, backup download and restore, the dialogs the actions load, the scene and
   seasonal-touch managers, the alert sound, and the search entries. Everything is delegated on
   the document, so it keeps working after a section or a card is swapped. The words the person
   reads come from the server's replies; the few strings here are the interface's own labels. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function toast(message, opts) {
    if (!message) return null;
    return root.KUI ? root.KUI.toast(message, opts || {}) : null;
  }

  /* --------------------------------------------------------------------- talking to the server */
  function post(url, body) {
    return root.fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); })
      .catch(function () { return { ok: false, message: 'Could not save that. Try again.' }; });
  }

  /* The pane that holds the control, so an Undo can load that section again in place. */
  var current = 'computer';

  function refreshPane(pane, then) {
    var el = $('#st-pane-' + pane);
    if (!el || !root.htmx) return;
    var url = '/settings/section/' + pane;
    var track = $('[data-testid^="st-track-"].is-on', el);
    if (track) url += '?track=' + track.getAttribute('data-testid').replace('st-track-', '');
    root.htmx.ajax('GET', url, { target: '#st-pane-' + pane, swap: 'outerHTML' }).then(function () {
      if (then) then();
    });
  }

  /* A message from a save, with an Undo that asks the server to put back exactly that change and
     says so when it cannot ("This can no longer be undone."). `refresh` names what to load again
     afterwards: a fragment and where it goes, else the section the control sits in. */
  function say(message, undoUrl, refresh) {
    if (!message) return;
    var opts = {};
    if (undoUrl) {
      opts.undo = function () {
        root.fetch(undoUrl, { method: 'POST', credentials: 'same-origin' })
          .then(function (r) { return r.json(); })
          .catch(function () { return { ok: false, message: 'This can no longer be undone.' }; })
          .then(function (res) {
            toast(res.ok ? (res.message || 'Put back.') : res.message);
            if (!res.ok) return;
            if (res.reload) { root.location.reload(); return; }
            if (refresh && refresh.refresh && root.htmx) {
              root.htmx.ajax('GET', refresh.refresh, { target: refresh.target, swap: 'outerHTML' });
            } else {
              refreshPane(current,
                          function () {
                            initPrefs();
                            if (res.key === 'ui.scheme') {
                              var selected = $('[data-st-scheme][aria-checked="true"]');
                              if (selected) paintScheme(selected.getAttribute('data-st-scheme'));
                            }
                          });
            }
          });
      };
    }
    toast(message, opts);
  }

  /* HX-Trigger messages from the cards that swap themselves arrive as a k-toast event. Take the
     ones that carry an Undo and show them through say(), so a refused Undo is explained. */
  doc.addEventListener('k-toast', function (e) {
    var d = e.detail || {};
    if (!d.undoUrl || !d.message) return;
    e.stopImmediatePropagation();
    say(d.message, d.undoUrl, d.refresh ? { refresh: d.refresh, target: d.target } : null);
  }, true);

  /* ---------------------------------------------------------------------------- the section */
  var SECTIONS = ['computer', 'roles', 'scoring', 'privacy', 'data', 'preferences'];
  // Old links keep working: #system, #hosted, #applied ... open the section that holds it now.
  var LEGACY = {
    system: ['computer'], hosted: ['computer', 'st-mode'], models: ['computer', 'st-chk'],
    updates: ['computer', 'updates'], 'desktop-window': ['computer', 'desktop-window'],
    applied: ['data', 'st-imp'], 'your-data': ['data', 'your-data'], backup: ['data', 'st-backups'],
    appearance: ['preferences', 'st-appearance'], core: ['roles', 'st-rules'],
    filters: ['roles', 'st-rules'], weights: ['scoring', 'st-w'], learning: ['scoring', 'st-learn'],
    lan: ['privacy', 'st-access']
  };

  function route() {
    var raw = (root.location.hash || '').replace(/^#/, '');
    var parts = raw.split('/');
    var head = decodeURIComponent(parts[0] || '');
    var find = parts[1] ? decodeURIComponent(parts[1]) : '';
    if (SECTIONS.indexOf(head) >= 0) return { sec: head, find: find };
    if (LEGACY[head]) return { sec: LEGACY[head][0], find: LEGACY[head][1] || '' };
    var el = head ? doc.getElementById(head) : null;       // any element id on the page
    var host = el && el.closest ? el.closest('[data-st-sec]') : null;
    if (host) return { sec: host.getAttribute('data-st-sec'), find: head };
    return { sec: 'computer', find: '' };
  }

  function glow(el) {
    if (!el) return;
    var target = el.closest('.st-row, .st-card, .k-dz') || el;
    target.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
    target.classList.remove('st-glow');
    void target.offsetWidth;
    target.classList.add('st-glow');
    root.setTimeout(function () { target.classList.remove('st-glow'); }, 1900);
  }
  function reducedMotion() {
    return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function show(sec, find) {
    current = sec;
    $$('[data-st-sec]').forEach(function (s) { s.hidden = s.getAttribute('data-st-sec') !== sec; });
    $$('[data-st-help]').forEach(function (h) { h.hidden = h.getAttribute('data-st-help') !== sec; });
    $$('[data-st-nav]').forEach(function (a) {
      var on = a.getAttribute('data-st-nav') === sec;
      a.classList.toggle('is-on', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    var host = $('#st-sec-' + sec);
    var land = function () {
      if (find) glow(doc.getElementById(find));
      else {
        var lay = $('#st-lay');   // a section opened from further down starts at the top of the list
        if (lay && lay.getBoundingClientRect().top < 0) lay.scrollIntoView({ block: 'start' });
      }
    };
    var lazy = host && host.getAttribute('data-st-lazy');
    if (lazy && !host.getAttribute('data-st-loaded') && !root.htmx) {
      // htmx loads after this script: try again once everything is in.
      root.addEventListener('load', function () { show(sec, find); }, { once: true });
      return;
    }
    if (lazy && !host.getAttribute('data-st-loaded') && root.htmx) {
      host.setAttribute('data-st-loaded', '1');
      host.innerHTML = '<p class="st-dsm" role="status">Loading…</p>';
      root.htmx.ajax('GET', lazy, { target: '#st-sec-' + sec, swap: 'innerHTML' }).then(land);
      return;
    }
    land();
  }
  function onHash() { var r = route(); show(r.sec, r.find); }

  /* ------------------------------------------------------------------- saving the controls */
  function bodyFor(el) {
    var url = el.getAttribute('data-st-url') || '/settings/ui';
    var key = el.getAttribute('data-st-key');
    if (url === '/settings/ui') return { url: url, body: { key: key, value: el.checked } };
    if (url === '/settings/prefs/tracks') {
      return { url: url, body: {
        employment: $('[data-st-track="employment"]').checked,
        consulting: $('[data-st-track="consulting"]').checked } };
    }
    if (url === '/settings/prefs/notify') return { url: url, body: { group: key, on: el.checked } };
    return { url: url, body: { key: key, on: el.checked } };
  }

  doc.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || !el.matches) return;
    if (el.matches('input[type="checkbox"][data-st-key]')) {
      if (el.hasAttribute('data-st-access')) return;
      var req = bodyFor(el);
      var tracks = el.hasAttribute('data-st-track');
      if (tracks && !req.body.employment && !req.body.consulting) {
        el.checked = true;
        var note = $('#st-trk-note');
        if (note) { note.textContent = 'Keep at least one on.'; root.setTimeout(function () { note.textContent = ''; }, 3000); }
        return;
      }
      post(req.url, req.body).then(function (res) {
        if (!res.ok) { el.checked = !el.checked; toast(res.message); return; }
        if (res.reload) {
          try { root.sessionStorage.setItem('stToast', res.message); } catch (err) { /* private window */ }
          root.location.reload();
          return;
        }
        applyKey(el.getAttribute('data-st-key'), el.checked);
        say(res.message, res.undoUrl);
      });
      return;
    }
    if (el.matches('select[data-st-ui-sel]')) {
      var key = el.getAttribute('data-st-ui-sel');
      post('/settings/ui', { key: key, value: el.value }).then(function (res) {
        if (!res.ok) { toast(res.message); return; }
        if (res.preview) { var p = $('#st-fmt-preview'); if (p) p.textContent = res.preview; }
        say(res.message, res.undoUrl);
        if (key.indexOf('region.') === 0) refreshPane('preferences');
      });
      return;
    }
    if (el.matches('[data-st-quiet], [data-st-quiet-toggle]')) { saveQuiet(); return; }
    if (el.id === 'st-vol') {
      post('/settings/ui', { key: 'notify.sound_volume', value: +el.value / 100, quiet: true });
      return;
    }
    if (el.id === 'st-photo' && el.files && el.files[0]) { uploadPhoto(el); return; }
    if (el.hasAttribute('data-st-autosubmit') && el.files && el.files[0]) {
      var form = el.closest('form');
      if (form && form.requestSubmit) form.requestSubmit();
      return;
    }
    if (el.id === 'st-restore-file' && el.files && el.files[0]) {
      var nm = $('#st-restore-name');
      if (nm) nm.textContent = el.files[0].name;
      if (root.KUI) root.KUI.openModal('#st-restore-dialog');
      return;
    }
    if (el.hasAttribute('data-st-match')) {
      var pill = $('#st-ctm-' + el.getAttribute('data-st-match'));
      if (pill) {
        var no = el.value === 'none';
        pill.textContent = no ? 'Not a match' : 'Possible match';
        pill.className = 'k-pill' + (no ? '' : ' k-pill--warn');
      }
    }
  });

  /* What a saved control changes on this page right away. */
  function applyKey(key, on) {
    if (key === 'ui.reduce_motion') {
      doc.documentElement.classList.toggle('st-rm', !!on);
      doc.documentElement.setAttribute('data-reduce-motion', on ? '1' : '0');
    }
    if (key === 'ui.scene_animate') { S.anim = !!on; paintBand(); }
    if (key === 'ui.touches_enabled') { S.season = !!on; syncSummary(); }
    if (key === 'notify.sound') {
      S.sound = !!on;
      var f = $('#st-snd-fold');
      if (f) { f.hidden = !on; f.classList.toggle('is-on', !!on); }
    }
  }

  /* Segmented controls and tiles that save as they are pressed. */
  doc.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var seg = t.closest('[data-st-ui-seg]');
    if (seg) {
      var b = t.closest('button');
      if (!b) return;
      var key = seg.getAttribute('data-st-ui-seg');
      post('/settings/ui', { key: key, value: b.value }).then(function (res) {
        if (!res.ok) { toast(res.message); return; }
        if (key === 'ui.text_size') applyTextSize(b.value);
        if (res.preview) { var p = $('#st-fmt-preview'); if (p) p.textContent = res.preview; }
        say(res.message, res.undoUrl);
        if (key.indexOf('region.') === 0) refreshPane('preferences');
      });
      return;
    }
    var th = t.closest('[data-st-theme] button');
    if (th) {
      if (root.KUI) root.KUI.setTheme(th.value);
      toast({ light: 'Light theme on.', dark: 'Dark theme on.', auto: 'Kestrel will match your computer.' }[th.value]);
      return;
    }
    var sc = t.closest('[data-st-scheme]');
    if (sc) {
      var id = sc.getAttribute('data-st-scheme');
      post('/settings/prefs/scheme', { scheme: id }).then(function (res) {
        if (!res.ok) { toast(res.message); return; }
        paintScheme(id);
        say(res.message, res.undoUrl);
      });
      return;
    }
    var av = t.closest('[data-st-avatar]');
    if (av) {
      var v = av.getAttribute('data-st-avatar');
      post('/settings/ui', { key: 'ui.avatar', value: v }).then(function (res) {
        if (!res.ok) { toast(res.message); return; }
        paintAvatar(v);
        say(res.message, res.undoUrl);
      });
      return;
    }
    if (t.closest('#st-photo-remove')) {
      post('/settings/prefs/avatar/photo/remove', {}).then(function (res) {
        toast(res.message); if (res.ok) refreshPane('preferences', initPrefs);
      });
      return;
    }
    var tile = t.closest('[data-st-svc]');
    if (tile) {
      var svc = tile.getAttribute('data-st-svc');
      $$('[data-st-svc]').forEach(function (x) {
        var on = x === tile; x.classList.toggle('is-on', on); x.setAttribute('aria-checked', on ? 'true' : 'false');
      });
      var hint = $('#st-svc-hint');
      if (hint) hint.hidden = svc !== 'other';
      var inp = $('#st-provider');
      if (inp) inp.value = svc;
      var fields = $('#st-other-fields');
      if (fields) fields.hidden = svc !== 'other';
      var test = $('[data-testid="st-key-test"]');
      if (test) test.textContent = svc === 'other' ? 'Test connection' : 'Test key';
      var save = $('[data-testid="st-key-save"]');
      if (save) save.textContent = svc === 'other' ? 'Save' : 'Save key';
      var key = $('#st-key');
      if (key) key.value = '';
      return;
    }
    if (t.closest('#st-key-show')) {
      var k = $('#st-key');
      var shown = k.type === 'text';
      k.type = shown ? 'password' : 'text';
      t.closest('#st-key-show').textContent = shown ? 'Show' : 'Hide';
      return;
    }
    var cp = t.closest('[data-st-copy]');
    if (cp) { copy(cp.getAttribute('data-st-copy')); return; }
    if (t.closest('[data-st-backup]')) { backup(t.closest('[data-st-backup]')); return; }
    if (t.closest('#st-restore-pick')) { var f = $('#st-restore-file'); if (f) f.click(); return; }
    if (t.closest('[data-st-restore-cancel]')) { var rf = $('#st-restore-file'); if (rf) rf.value = ''; return; }
    if (t.closest('[data-st-pick-contacts]')) {
      var pick = $('#st-ct-pick input[type="file"]');
      if (pick) pick.click();
      return;
    }
    if (t.closest('#st-play')) { playSound(); return; }
    if (t.closest('#st-forget-open')) { fillForget(); return; }
    var open = t.closest('[data-st-open]');
    if (open) { mgOpen(open.getAttribute('data-st-open')); return; }
  });

  doc.addEventListener('input', function (e) {
    var el = e.target;
    if (!el || !el.id) return;
    if (el.id === 'st-vol') { var o = $('#st-vol-t'); if (o) o.textContent = el.value + '%'; }
    if (el.id === 'st-clear-reset') { var g = $('#st-clear-go'); if (g) g.disabled = el.value !== 'RESET'; }
    if (el.id === 'st-forget-reset') { var f = $('#st-forget-go'); if (f) f.disabled = el.value !== 'RESET'; }
    if (el.id === 'st-access-ack') { var a = $('#st-access-go'); if (a) a.disabled = !el.checked; }
  });
  doc.addEventListener('change', function (e) {
    if (e.target && e.target.id === 'st-access-ack') { var a = $('#st-access-go'); if (a) a.disabled = !e.target.checked; }
    if (e.target && e.target.matches && e.target.matches('[data-st-access]')) {
      var sw = e.target;
      if (sw.checked) {
        sw.checked = false;           // it turns on only after the confirm
        var ack = $('#st-access-ack'); if (ack) ack.checked = false;
        var go = $('#st-access-go'); if (go) go.disabled = true;
        if (root.KUI) root.KUI.openModal('#st-access-dialog');
      } else if (root.htmx) {
        root.htmx.ajax('POST', '/settings/privacy/access', {
          target: '#st-access', swap: 'outerHTML', values: { on: '0' } });
      }
    }
  });

  function copy(text) {
    try {
      var p = root.navigator.clipboard.writeText(text);
      if (p && p.then) p.then(function () { toast('Address copied.'); });
      else toast('Address copied.');
    } catch (e) { toast('Could not copy. Select the address and copy it.'); }
  }

  function applyTextSize(v) {
    /* The shell scales every page from this attribute (components.css). */
    doc.documentElement.setAttribute('data-text-size', (v === 'small' || v === 'large') ? v : 'default');
  }

  /* ---------------------------------------------------------------------- quiet hours, sound */
  function saveQuiet() {
    var on = $('[data-st-quiet-toggle]');
    var from = $('[data-st-quiet="start"]');
    var to = $('[data-st-quiet="end"]');
    if (!on || !from || !to) return;
    from.disabled = to.disabled = !on.checked;
    post('/settings/ui', { key: 'notify.quiet_hours',
      value: { enabled: on.checked, start: from.value, end: to.value } })
      .then(function (res) { if (!res.ok) toast(res.message); else say(res.message, res.undoUrl); });
  }

  var AC = null;
  function playSound() {
    var name = ($('[data-testid="st-sound-name"]') || {}).value || 'chime';
    var vol = +(($('#st-vol') || { value: 60 }).value) / 100;
    var btn = $('#st-play');
    try { AC = AC || new (root.AudioContext || root.webkitAudioContext)(); } catch (e) { return; }
    var t0 = AC.currentTime;
    var out = AC.createGain();
    out.gain.value = Math.max(0, Math.min(1, vol)) * 0.5;
    out.connect(AC.destination);
    function tone(f, start, dur, type, peak) {
      var o = AC.createOscillator(), g = AC.createGain();
      o.type = type; o.frequency.value = f;
      g.gain.setValueAtTime(0, t0 + start);
      g.gain.linearRampToValueAtTime(peak, t0 + start + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + start + dur);
      o.connect(g); g.connect(out); o.start(t0 + start); o.stop(t0 + start + dur + 0.05);
    }
    if (name === 'soft_click') { tone(1800, 0, 0.05, 'triangle', 0.6); tone(900, 0.012, 0.06, 'sine', 0.4); }
    else if (name === 'bell') { [[880, 1], [1760, 0.35], [2640, 0.18], [1318.5, 0.25]].forEach(function (p) { tone(p[0], 0, 1.6, 'sine', p[1] * 0.7); }); }
    else { tone(1046.5, 0, 0.5, 'sine', 0.6); tone(1568, 0.13, 0.7, 'sine', 0.5); }
    if (btn) { btn.disabled = true; root.setTimeout(function () { btn.disabled = false; }, 1100); }
  }

  /* ---------------------------------------------------------------------- the picture */
  function paintAvatar(kind) {
    var big = $('#st-avbig');
    var src = $('[data-st-avatar="' + kind + '"]');
    if (big) {
      big.className = 'st-av st-big st-a-' + kind;
      if (src) big.innerHTML = src.innerHTML;
    }
    $$('[data-st-avatar]').forEach(function (b) {
      var on = b.getAttribute('data-st-avatar') === kind;
      b.classList.toggle('is-on', on); b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }
  function uploadPhoto(input) {
    var fd = new root.FormData();
    fd.append('photo', input.files[0]);
    root.fetch('/settings/prefs/avatar/photo', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .catch(function () { return { ok: false, message: 'Could not save the photo. Try again.' }; })
      .then(function (res) {
        input.value = '';
        if (!res.ok) { toast(res.message); return; }
        say(res.message, res.undoUrl);
        refreshPane('preferences', initPrefs);
      });
  }

  /* ----------------------------------------------------------------------- backup and restore */
  function backup(btn) {
    var label = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Preparing…';
    root.fetch('/settings/data/backup', { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('backup ' + r.status);
      var cd = r.headers.get('Content-Disposition') || '';
      var m = /filename="?([^";]+)"?/.exec(cd);
      return r.blob().then(function (b) { return { blob: b, name: m ? m[1] : 'kestrel-backup.zip' }; });
    }).then(function (f) {
      var a = doc.createElement('a');
      a.href = root.URL.createObjectURL(f.blob);
      a.download = f.name;
      doc.body.appendChild(a);
      a.click();
      root.setTimeout(function () { root.URL.revokeObjectURL(a.href); a.remove(); }, 4000);
      var last = $('#st-lastbk'); if (last) last.textContent = 'Last backup: just now';
      toast('Backup saved: ' + f.name);
    }).catch(function () {
      toast('Could not make the backup. Try again.');
    }).then(function () { btn.disabled = false; btn.innerHTML = label; });
  }

  /* -------------------------------------------------------------------------- forget dialog */
  function fillForget() {
    var names = { ratings: 'Your ratings', model: 'Your own sorting model', weights: 'Suggested weight changes' };
    var list = $('#st-forget-list');
    var chosen = [];
    $$('[data-st-forget]').forEach(function (c) {
      var k = c.getAttribute('data-st-forget');
      var hid = $('[data-st-fg="' + k + '"]');
      if (hid) hid.value = c.checked ? 'on' : 'off';
      if (c.checked) chosen.push(names[k]);
    });
    if (list) list.innerHTML = chosen.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('');
    var res = $('#st-forget-res'); if (res) res.innerHTML = '';
    var inp = $('#st-forget-reset'); if (inp) inp.value = '';
    var go = $('#st-forget-go'); if (go) go.disabled = true;
  }
  doc.addEventListener('change', function (e) {
    if (e.target && e.target.hasAttribute && e.target.hasAttribute('data-st-forget')) {
      var open = $('#st-forget-open');
      var any = $$('[data-st-forget]').some(function (c) { return c.checked; });
      if (open) open.disabled = !any;
    }
  });

  /* ------------------------------------------------------------------- htmx lifecycle hooks */
  var lastFocus = '';
  doc.addEventListener('htmx:beforeRequest', function (e) {
    var a = doc.activeElement;
    lastFocus = a && a.id ? a.id : (a && a.getAttribute ? (a.getAttribute('data-testid') || '') : '');
    var btn = e.detail && e.detail.elt;
    if (btn && btn.getAttribute && btn.getAttribute('data-busy-text') && btn.tagName === 'BUTTON') {
      btn.setAttribute('data-label', btn.innerHTML);
      btn.textContent = btn.getAttribute('data-busy-text');
      btn.disabled = true;
    }
    if (btn && btn.id === 'st-keyform') root.__stKeyOp = e.detail.requestConfig && e.detail.requestConfig.triggeringEvent
      && e.detail.requestConfig.triggeringEvent.submitter && e.detail.requestConfig.triggeringEvent.submitter.value;
  });
  doc.addEventListener('htmx:afterSwap', function (e) {
    var t = e.detail && e.detail.target;
    if (!t) return;
    if (t.id === 'st-dialog') {
      var dlg = $('.k-modal', t);
      if (dlg && root.KUI) root.KUI.openModal('#' + dlg.id);
    }
    if (lastFocus) {
      var again = doc.getElementById(lastFocus) || $('[data-testid="' + lastFocus + '"]');
      if (again && again.focus && !t.contains(doc.activeElement)) { try { again.focus({ preventScroll: true }); } catch (err) { /* gone */ } }
    }
  });
  doc.addEventListener('htmx:afterRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (el && el.getAttribute && el.getAttribute('data-label') && el.tagName === 'BUTTON') {
      el.innerHTML = el.getAttribute('data-label'); el.disabled = false; el.removeAttribute('data-label');
    }
    if (el && el.id === 'st-keyform' && e.detail.successful && root.__stKeyOp === 'save') {
      var k = $('#st-key'); if (k && /saved/i.test(($('#st-keyres') || {}).textContent || '')) k.value = '';
    }
  });
  doc.addEventListener('htmx:load', function () { initPrefs(); initFlyouts(); });

  /* The one-time messages that survive a reload (a track switch reloads so the sidebar follows). */
  function replayToast() {
    try {
      var m = root.sessionStorage.getItem('stToast');
      if (m) { root.sessionStorage.removeItem('stToast'); toast(m); }
    } catch (e) { /* private window */ }
  }

  /* ------------------------------------------------------------ scenes and seasonal touches */
  var S = { scene: 'mountains', daily: true, rot: [], anim: true, season: true, off: [], order: [],
            sound: true };
  var D = null;
  var SXDATE = {};
  var GROUPS = [['Seasons and nature', 'nature'], ['Civic', 'civic'], ['Lights and lanterns', 'lights']];
  // Server touch ids and the ids the art registers under, both ways.
  var TOUCH_JS = { spring: 'spring', earth_day: 'earthday', longest_day: 'longday', shooting_stars: 'meteors',
    autumn: 'autumn', harvest: 'harvest', longest_night: 'longnight', new_year: 'newyear', fourth_of_july: 'july4',
    halloween: 'halloween', lunar_new_year: 'lunarny', labor_day: 'laborday', colors_of_spring: 'colors',
    crescent_evenings: 'crescent', lantern_night: 'lanterns', winter_lights: 'winterlights' };
  var JS_TOUCH = {};
  Object.keys(TOUCH_JS).forEach(function (k) { JS_TOUCH[TOUCH_JS[k]] = k; });

  function scenes() { return root.SCENES || []; }
  function seasons() { return root.SEASONS || []; }
  function sceneById(id) { return scenes().filter(function (s) { return s.id === id; })[0] || null; }
  function seasonById(id) { return seasons().filter(function (s) { return s.id === id; })[0] || null; }
  function rotation() {
    var order = S.order.length ? S.order : scenes().map(function (s) { return s.id; });
    return order.filter(function (id) { return S.rot.indexOf(id) >= 0; });
  }
  function ordinal() {
    var d = new Date();
    return Math.floor(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 864e5) + 719163;
  }
  function effective() {
    if (!S.daily) return S.scene;
    var r = rotation();
    return r.length ? r[ordinal() % r.length] : S.scene;
  }
  function paintScheme(id) {
    if (root.KUI) root.KUI.setScheme(id);
    $$('[data-st-scheme]').forEach(function (x) {
      var on = x.getAttribute('data-st-scheme') === id;
      x.classList.toggle('is-on', on); x.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }
  function initPrefs() {
    var tag = $('#st-prefs-data');
    if (!tag) return;
    try { D = JSON.parse(tag.textContent); } catch (e) { return; }
    S.scene = D.scene; S.daily = D.daily; S.rot = D.rotation.slice(); S.anim = D.animate;
    S.season = D.touches; S.order = D.order.slice(); S.sound = D.sound;
    S.off = D.touchesOff.slice();
    S.todayLabel = D.todayLabel; S.touchLabels = D.touchLabels || {}; SXDATE = D.touchDays || {};
    $$('[data-st-theme] button').forEach(function (b) {
      var cur = 'auto';
      try { cur = root.localStorage.getItem('kestrel-theme') || 'auto'; } catch (e) { /* no storage */ }
      var on = b.value === cur;
      b.classList.toggle('is-on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    applyTextSize(D.textSize);
    doc.documentElement.classList.toggle('st-rm', !!D.reduceMotion);
    doc.documentElement.setAttribute('data-reduce-motion', D.reduceMotion ? '1' : '0');
    syncSummary();
  }
  function sceneName(id) { var s = sceneById(id); return s ? s.name : id; }
  function nextTouch() {
    if (!S.season) return 'Off. Your choices are saved.';
    var now = new Date(); now.setHours(0, 0, 0, 0);
    var best = null;
    seasons().forEach(function (s) {
      var d = SXDATE[s.id];
      if (!d || S.off.indexOf(s.id) >= 0) return;
      var dt = new Date(now.getFullYear(), d[0] - 1, d[1]);
      if (dt < now) dt = new Date(now.getFullYear() + 1, d[0] - 1, d[1]);
      if (!best || dt < best.dt) best = { dt: dt, s: s, d: d };
    });
    return best ? 'Next: ' + best.s.name + ' · around ' + (S.touchLabels[best.s.id] || '') : 'All touches are off.';
  }
  function syncSummary() {
    var line = $('#st-scene-line');
    if (line && scenes().length) {
      line.textContent = S.daily
        ? sceneName(effective()) + ' today. Changes daily, ' + rotation().length + ' of ' + scenes().length + ' in rotation.'
        : (S.scene === 'none' ? 'None' : sceneName(S.scene));
    }
    var nx = $('#st-next-touch'); if (nx) nx.textContent = nextTouch();
    var cnt = $('#st-touch-count');
    if (cnt) { var tot = seasons().length; cnt.textContent = (tot - S.off.length) + ' of ' + tot + ' on'; }
    paintBand();
  }
  function paintBand() {
    var band = $('.st-page .k-scene');
    if (!band || !root.KSceneBand) return;
    var want = effective();
    root.KSceneBand.setAnimate(band, S.anim);
    if (band.getAttribute('data-scene') !== want) {
      band.setAttribute('data-scene', want);
      root.KSceneBand.render(band);
    }
  }

  var mg = { kind: null, tab: 'nature', sel: null, msg: '', msgT: 0 };
  function mgOpen(kind) {
    mg.kind = kind;
    mg.tab = 'nature';
    mg.msg = '';
    var items = kind === 'scenes' ? [{ id: S.daily ? effective() : S.scene }] : seasons();
    mg.sel = kind === 'scenes' ? items[0].id : (items[0] || {}).id;
    mgDraw();
  }
  function mgItems() {
    if (mg.kind === 'scenes') {
      var list = scenes().map(function (s) { return { id: s.id, name: s.name, sub: s.moves || '' }; });
      if (!S.daily) list.push({ id: 'none', name: 'None', sub: 'Plain sky, no artwork' });
      return list;
    }
    var g = GROUPS.filter(function (x) { return x[1] === mg.tab; })[0][0];
    return seasons().filter(function (s) { return s.group === g; })
      .map(function (s) { return { id: s.id, name: s.name, sub: s.when || '' }; });
  }
  function mgDraw() {
    var body = $('#st-mg-' + mg.kind + '-body');
    if (!body) return;
    var sc = mg.kind === 'scenes';
    var items = mgItems();
    if (!items.some(function (x) { return x.id === mg.sel; })) mg.sel = items[0] && items[0].id;
    var rot = rotation();
    var master = sc ? S.daily : S.season;
    var radio = sc && !S.daily;
    var cur = items.filter(function (x) { return x.id === mg.sel; })[0] || items[0] || { name: '', sub: '' };
    var onN = sc ? items.filter(function (x) { return rot.indexOf(x.id) >= 0; }).length
      : items.filter(function (x) { return S.off.indexOf(x.id) < 0; }).length;
    var title = sc ? 'Scenes' : 'Seasonal touches';
    var desc = sc ? (S.daily ? 'Changes daily. Today: ' + sceneName(effective()) + '.' : 'Shows one scene all the time.')
      : 'Small extras over your scene on a few dates a year.';
    var tabs = sc ? '' : '<div class="st-mgtabs" role="group">' + GROUPS.map(function (g) {
      var n = seasons().filter(function (s) { return s.group === g[0]; }).length;
      return '<button type="button" data-mgtab="' + g[1] + '" class="' + (g[1] === mg.tab ? 'is-on' : '') +
        '" aria-pressed="' + (g[1] === mg.tab) + '">' + esc(g[0]) + '<i>' + n + '</i></button>';
    }).join('') + '</div>';
    var rows = items.map(function (x) {
      var checked = sc ? rot.indexOf(x.id) >= 0 : S.off.indexOf(x.id) < 0;
      var ctl = radio
        ? '<input type="radio" name="st-mg-r" data-mgrad="' + esc(x.id) + '"' + (S.scene === x.id ? ' checked' : '') + ' aria-label="Use ' + esc(x.name) + '">'
        : '<input type="checkbox" data-mgcb="' + esc(x.id) + '"' + (checked ? ' checked' : '') + ' aria-label="' + (sc ? 'Include ' + esc(x.name) + ' in rotation' : 'Show ' + esc(x.name)) + '">';
      return '<div class="st-mgrow' + (x.id === mg.sel ? ' is-on' : '') + '">' + ctl +
        '<button type="button" class="st-mgname" data-mgrow="' + esc(x.id) + '" title="' + esc(x.name) + '"><b>' + esc(x.name) + '</b><span>' + esc(x.sub) + '</span></button></div>';
    }).join('');
    var count = mg.msg || (radio ? 'Showing ' + (S.scene === 'none' ? 'None' : sceneName(S.scene)) : onN + ' of ' + items.length + (sc ? ' in rotation' : ' on'));
    var acts = radio ? '' : '<span class="st-lnks"><button type="button" class="k-link" data-mgall>Select all</button><span aria-hidden="true">/</span><button type="button" class="k-link" data-mgclr>Clear</button></span>';
    var hint = sc ? (S.daily ? 'Checkboxes include. Names preview.' : 'Choose one scene. Names preview.') : 'Checkboxes turn on. Names preview.';
    var useBtn = radio
      ? '<button type="button" class="k-btn k-btn--sm k-btn--primary" data-mguse' + (S.scene === mg.sel ? ' disabled' : '') + '>' + (S.scene === mg.sel ? 'In use' : 'Use this scene') + '</button>'
      : '<button type="button" class="k-btn k-btn--sm" data-mgreplay>Replay preview</button>';
    var touchArt = !sc && !seasonById(cur.id) ? ' (art coming)' : '';
    body.innerHTML = '<div class="st-mgh"><div class="k-grow"><h3>' + title + '</h3><p>' + esc(desc) + '</p></div>' +
      '<label class="st-mgon"><span>' + (master ? 'On' : 'Off') + '</span><input type="checkbox" class="st-check" data-mgmaster' + (master ? ' checked' : '') + ' aria-label="' + (sc ? 'Change scene daily' : 'Seasonal touches') + '"></label></div>' +
      tabs + '<div class="st-mggrid"><div class="st-mgcard' + (!sc && !S.season ? ' is-dim' : '') + '"><p class="st-mghint">' + hint + (!sc && !S.season ? ' Off. Your choices are saved.' : '') + '</p>' +
      '<div class="st-mgct"><span>' + esc(count) + '</span>' + acts + '</div><div class="st-mglist">' + rows + '</div></div>' +
      '<div class="st-mgcard st-mgpv"><h4>' + esc(cur.name) + '</h4><p class="st-mgsub">' + esc(cur.sub + touchArt) + '</p>' +
      '<div class="st-mgart"><div class="st-mgttl" aria-hidden="true">Today<small>' + esc(S.todayLabel || '') + '</small></div>' +
      '<div class="k-scene" id="st-mg-band" data-scene="' + (sc ? esc(cur.id) : esc(effective())) + '" data-touch="' + (sc ? '' : esc(cur.id)) + '" data-animate="' + (S.anim ? 1 : 0) + '"></div></div>' +
      '<div class="st-mgfoot"><span class="k-pill k-pill--info">Preview only</span><small>Previewing does not change your choices.</small></div><div class="st-mgfoot">' + useBtn + '</div></div></div>';
    var band = $('#st-mg-band');
    if (band && root.KSceneBand) root.KSceneBand.render(band);
  }
  function mgMsg(text) {
    mg.msg = text; root.clearTimeout(mg.msgT);
    mg.msgT = root.setTimeout(function () { mg.msg = ''; if (mg.kind) mgDraw(); }, 2600);
    mgDraw();
  }
  function saveUi(key, value, quiet) {
    return post('/settings/ui', { key: key, value: value, quiet: !!quiet }).then(function (res) {
      if (!res.ok) { toast(res.message); return res; }
      say(res.message, res.undoUrl);
      return res;
    });
  }
  function offServer() { return S.off.map(function (id) { return JS_TOUCH[id]; }).filter(Boolean); }

  doc.addEventListener('click', function (e) {
    var t = e.target;
    if (!mg.kind || !t || !t.closest || !t.closest('.st-mgbody')) return;
    var tab = t.closest('[data-mgtab]');
    if (tab) { mg.tab = tab.getAttribute('data-mgtab'); mg.sel = null; mgDraw(); return; }
    var row = t.closest('[data-mgrow]');
    if (row) { mg.sel = row.getAttribute('data-mgrow'); mgDraw(); return; }
    if (t.closest('[data-mgreplay]')) { var b = $('#st-mg-band'); if (b) { b.removeAttribute('data-rendered'); root.KSceneBand.render(b); } return; }
    if (t.closest('[data-mguse]')) { useScene(mg.sel); return; }
    var all = t.closest('[data-mgall]'), clr = t.closest('[data-mgclr]');
    if (all || clr) {
      if (mg.kind === 'scenes') {
        if (all) { S.rot = (S.order.length ? S.order : scenes().map(function (s) { return s.id; })).slice(); }
        else { var keep = rotation().indexOf(mg.sel) >= 0 ? mg.sel : rotation()[0]; S.rot = [keep]; mgMsg('Kept ' + sceneName(keep) + '. Tick more to add.'); }
        saveUi('ui.scene_rotation', rotation()).then(function () { syncSummary(); mgDraw(); });
      } else {
        var ids = mgItems().map(function (x) { return x.id; });
        S.off = all ? S.off.filter(function (x) { return ids.indexOf(x) < 0; })
                    : S.off.filter(function (x) { return ids.indexOf(x) < 0; }).concat(ids);
        saveUi('ui.touches_off', offServer(), true).then(function () { syncSummary(); mgDraw(); });
      }
      return;
    }
  });
  doc.addEventListener('change', function (e) {
    var t = e.target;
    if (!mg.kind || !t || !t.closest || !t.closest('.st-mgbody')) return;
    if (t.hasAttribute('data-mgmaster')) {
      if (mg.kind === 'scenes') { S.daily = t.checked; saveUi('ui.scene_daily', S.daily).then(function () { syncSummary(); mgDraw(); }); }
      else { S.season = t.checked; saveUi('ui.touches_enabled', S.season).then(function () { syncSummary(); mgDraw(); }); }
      var pt = $('[data-st-key="ui.touches_enabled"]'); if (pt && mg.kind !== 'scenes') pt.checked = S.season;
      return;
    }
    if (t.hasAttribute('data-mgrad')) { useScene(t.getAttribute('data-mgrad')); return; }
    if (t.hasAttribute('data-mgcb')) {
      var id = t.getAttribute('data-mgcb');
      if (mg.kind === 'scenes') {
        var has = S.rot.indexOf(id) >= 0;
        var next = has ? S.rot.filter(function (x) { return x !== id; }) : S.rot.concat([id]);
        if (!next.length) { t.checked = true; mgMsg('Keep at least one scene.'); return; }
        S.rot = next;
        saveUi('ui.scene_rotation', rotation()).then(function () { syncSummary(); mgDraw(); });
      } else {
        S.off = t.checked ? S.off.filter(function (x) { return x !== id; }) : S.off.concat([id]);
        saveUi('ui.touches_off', offServer(), true).then(function () { syncSummary(); mgDraw(); });
      }
    }
  });
  function useScene(id) {
    S.scene = id;
    saveUi('ui.scene', id).then(function () { syncSummary(); mgDraw(); });
  }
  doc.addEventListener('k-closed', function (e) {
    if (e.target && e.target.id && /^st-mg-/.test(e.target.id)) mg.kind = null;
  });
  // Re-drawn after an Undo reloads the section: the managers read the fresh data.
  doc.addEventListener('htmx:afterSettle', function () { if (mg.kind) { initPrefs(); mgDraw(); } });

  /* ------------------------------------------------------------------------ search entries */
  // The palette (Ctrl K) lists these under "Settings: ...", each with plain synonyms, so a person
  // can type "sleep" or "phone" and land on the control. The target is #section/element.
  var INDEX = [
    ['How should Kestrel run (online model, local, both)', 'computer', 'st-mode'],
    ['Setup checklist (Ollama, download models)', 'computer', 'st-chk'],
    ['Keep this computer awake (sleep)', 'computer', 'st-everyday'],
    ['Start with my computer (login, tray)', 'computer', 'st-everyday'],
    ['Check for updates (version)', 'computer', 'updates'],
    ['Rules that hide roles (filters)', 'roles', 'st-rules'],
    ['Pay below your minimum (salary)', 'roles', 'st-rules'],
    ['Score cutoffs (strong match, worth a look)', 'roles', 'st-cut'],
    ['Hidden companies (unhide)', 'roles', 'st-hidden'],
    ['What matters most (weights)', 'scoring', 'st-w'],
    ['What Kestrel has learned (ratings)', 'scoring', 'st-learn'],
    ['Export my ratings', 'scoring', 'st-ratings'],
    ['Forget my ratings (reset learning)', 'scoring', 'st-forget-dz'],
    ['Confidential mode (hide employer)', 'privacy', 'st-conf'],
    ['Access from other devices (phone, wifi, QR code)', 'privacy', 'st-access'],
    ['Connect an AI assistant (MCP, Claude Desktop)', 'privacy', 'st-mcp'],
    ['Backups (download, restore)', 'data', 'st-backups'],
    ['Import past applications (csv, spreadsheet)', 'data', 'st-imp'],
    ['Your contacts (LinkedIn connections)', 'data', 'st-ct'],
    ['Diagnostics (logs, troubleshooting)', 'data', 'st-diag'],
    ['Restart setup or Clear everything (reset)', 'data', 'st-danger'],
    ["What you're looking for (employment, consulting)", 'preferences', 'st-tracks'],
    ['Theme and color scheme (dark, light, accent)', 'preferences', 'st-appearance'],
    ['Your picture (avatar, photo)', 'preferences', 'st-picture'],
    ['Scene and seasonal touches (holiday, artwork)', 'preferences', 'st-appearance'],
    ['Text size and reduce motion (font, animation)', 'preferences', 'st-appearance'],
    ['Region (date format, time, currency)', 'preferences', 'st-region'],
    ['Notifications (alerts, sound, quiet hours)', 'preferences', 'st-notify']
  ];
  function registerSearch() {
    var A = root.Alpine;
    var host = doc.documentElement;
    var data = null;
    try { data = A && A.$data ? A.$data(host) : null; } catch (e) { data = null; }
    if (!data || !data.pages || data.__stIndexed) return false;
    INDEX.forEach(function (r) { data.pages.push({ label: 'Settings: ' + r[0], href: '/settings#' + r[1] + '/' + r[2] }); });
    data.__stIndexed = true;
    return true;
  }

  function initFlyouts() { /* the shared script wires the flyouts; nothing to do here */ }

  /* --------------------------------------------------------------------------------- start */
  function start() {
    initPrefs();
    onHash();
    replayToast();
    if (!registerSearch()) {
      doc.addEventListener('alpine:initialized', registerSearch, { once: true });
      root.setTimeout(registerSearch, 600);
    }
  }
  root.addEventListener('hashchange', onHash);
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start); else start();
})(typeof window !== 'undefined' ? window : globalThis);
