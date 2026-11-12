/* The Role page (D-UI-150): the small amount of behavior HTMX does not give it. The gliding tab
   underline, the slide of a tab or of Previous and Next, the match-map focus, the Add a story
   sheet, Your view's Adjust panel, the Not for me reasons, the More menu, the dialogs the actions
   load, the scaled document preview, and what Undo reloads. Everything is delegated on the
   document, so it keeps working after a swap. The page runs on the lifecycle (KUI.page,
   D-UI-210): this file only defines it, and mount starts every listener, timer and observer
   through its context, so leaving stops all of them and coming back starts one set. The history
   a tab, Previous or Next writes is htmx's own (hx-push-url on the links); nothing here writes
   history. User-facing strings here are the copied-link toast and the focus line under the match
   map. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var cx = null;
  var pendingDir = 0;    // set by the click, used by the next swap that follows it
  var pendingKind = '';  // 'pane' | 'body'
  var focusKey = null;   // the match map's focus
  var dialogWatch = null;  // the observer on the dialog host

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  // The OS setting or the app's own Reduce motion switch (data-reduce-motion="1", as profile.js reads it).
  function reduced() {
    return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches) ||
      doc.documentElement.getAttribute('data-reduce-motion') === '1';
  }
  function body() { return $('#rl-body'); }
  function oppId() { var p = $('#role'); return p ? p.getAttribute('data-opp') : ''; }
  function toast(msg, opts) { if (root.KUI) root.KUI.toast(msg, opts || {}); }
  function openModal(el) { if (el && root.KUI) root.KUI.openModal(el); }

  /* ------------------------------------------------------------------------ the tab underline */
  function placeInk(instant) {
    var tabs = $('#rl-tabs');
    if (!tabs) return;
    var on = $('a.is-on', tabs), ink = $('.rl-tabs__ink', tabs);
    if (!on || !ink) return;
    if (instant) ink.style.transition = 'none';
    ink.style.left = on.offsetLeft + 'px';
    ink.style.width = on.offsetWidth + 'px';
    if (instant) { void ink.offsetWidth; ink.style.transition = ''; }
  }
  function onInkResize() { placeInk(true); }

  /* ------------------------------------------------------------- sliding: tabs, Previous, Next */
  function tabOrder(name) {
    var names = $$('#rl-tabs a').map(function (a) { return a.getAttribute('data-tab'); });
    return names.indexOf(name);
  }
  function onTabClick(e) {
    var t = e.target.closest && e.target.closest('#rl-tabs a[data-tab], a[data-tab][hx-target="#rl-pane"]');
    if (t) {
      var name = t.getAttribute('data-tab'), b = body();
      var cur = b ? b.getAttribute('data-tab') : 'evaluation';
      if (name === cur) { e.preventDefault(); return; }
      pendingDir = tabOrder(name) > tabOrder(cur) ? 1 : -1;
      pendingKind = 'pane';
      if (b) b.setAttribute('data-tab', name);
      $$('#rl-tabs a').forEach(function (a) {
        var on = a.getAttribute('data-tab') === name;
        a.classList.toggle('is-on', on);
        a.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      placeInk(false);
      return;
    }
    var nav = e.target.closest && e.target.closest('[data-rl-dir]');
    if (nav) { pendingDir = parseInt(nav.getAttribute('data-rl-dir'), 10) || 1; pendingKind = 'body'; }
  }

  // A tab change fades the new pane in (the locked mockup's inFade .3s); moving to another role
  // slides the whole body in from the side it came from.
  function slideIn(el, fadeOnly) {
    if (!el || reduced() || !el.animate || !pendingDir) { pendingDir = 0; return; }
    el.animate(
      fadeOnly
        ? [{ opacity: 0 }, { opacity: 1 }]
        : [{ opacity: 0, transform: 'translateX(' + (50 * pendingDir) + 'px)' }, { opacity: 1, transform: 'none' }],
      { duration: 300, easing: 'cubic-bezier(.22,.8,.3,1)' }
    );
    pendingDir = 0;
  }
  function setTitle(el) { doc.title = (el.getAttribute('data-title') || 'Role') + ' · Kestrel'; }
  // The tab title follows the role in the same task as the swap that brings the new heading in.
  // htmx:load waits for the settle delay, so a title set only there lagged the heading by a beat.
  // The fade and the slide start here too, in the swap's own task, before the browser paints the
  // new content: started on htmx:load (after the settle delay) the new pane was drawn at full
  // strength for a frame or two, then dropped to nothing and faded in (owner, 2026-10-30: "flash
  // and flicker" on a tab change).
  function onBodySwap(e) {
    var t = e.target;
    if (!t) return;
    if (t.id === 'rl-body') {
      setTitle(t);
      if (pendingKind === 'body') { slideIn(t); pendingKind = 'body-done'; }
    } else if (t.id === 'rl-pane' && pendingKind === 'pane') {
      slideIn(t, true);
      pendingKind = '';
    }
  }
  function onLoad(e) {
    var el = e.target;
    if (!el || !el.id && !el.parentNode) return;
    if (el.id === 'rl-body') {
      setTitle(el);
      var dlg = $('#rl-dialog'); if (dlg) dlg.innerHTML = '';
      if (root.KUI) root.KUI.closeAll();
      placeInk(true);
      if (pendingKind === 'body') slideIn(el);  // only if the swap itself did not start it
      pendingKind = '';
      var main = $('.k-main, main'); if (main && main.scrollTo) main.scrollTo(0, 0);
    } else if (el.parentNode && el.parentNode.id === 'rl-pane') {
      placeInk(true);  // the fade started at the swap (onBodySwap)
    }
  }

  /* ------------------------------------------------------------------ match map and its focus */
  function clearFocus() {
    focusKey = null;
    var main = $('.rl-main');
    if (main) main.classList.remove('is-focus');
    $$('.rl-fl, .rl-wseg, .rl-evi').forEach(function (n) { n.classList.remove('is-hit'); });
    $$('.rl-wchip').forEach(function (n) { n.classList.remove('is-on'); });
    var f = $('#rl-wfocus'); if (f) { f.classList.remove('is-on'); f.textContent = ''; }
  }
  var WORDS = { met: 'met', part: 'partly met', gap: 'not met' };
  function focusOn(kind, key) {
    if (focusKey === kind + key) { clearFocus(); return; }
    clearFocus();
    focusKey = kind + key;
    var main = $('.rl-main'); if (!main) return;
    main.classList.add('is-focus');
    var hits = [];
    $$('.rl-fl, .rl-wseg').forEach(function (n) {
      var hit = kind === 'f' ? n.getAttribute('data-c') === key : n.getAttribute('data-k') === key;
      if (hit) { n.classList.add('is-hit'); if (n.classList.contains('rl-fl')) hits.push(n); }
    });
    $$('.rl-evi').forEach(function (n) {
      var k = n.getAttribute('data-k');
      var row = $('.rl-fl[data-k="' + k + '"]');
      var hit = kind === 'f' ? !!(row && row.getAttribute('data-c') === key) : k === key;
      if (hit) n.classList.add('is-hit');
    });
    if (kind === 'f') $$('.rl-wchip[data-f="' + key + '"]').forEach(function (n) { n.classList.add('is-on'); });
    var f = $('#rl-wfocus');
    if (f) {
      f.textContent = '';
      hits.forEach(function (n) {
        var line = doc.createElement('div');
        var c = n.getAttribute('data-c');
        line.textContent = (n.querySelector('.rl-mt') || n).textContent.trim() + ' (' + WORDS[c] + ')';
        f.appendChild(line);
      });
      f.classList.toggle('is-on', hits.length > 0);
    }
  }
  function onFocusClick(e) {
    var chip = e.target.closest && e.target.closest('.rl-wchip');
    if (chip) { e.stopPropagation(); focusOn('f', chip.getAttribute('data-f')); return; }
    var seg = e.target.closest && e.target.closest('.rl-wseg');
    if (seg) { e.stopPropagation(); focusOn('k', seg.getAttribute('data-k')); return; }
    if (focusKey && !(e.target.closest && e.target.closest('.rl-wfocus'))) clearFocus();
  }

  /* --------------------------------------------------------------------- Add a story sheet */
  var pendingRow = '';  // the row (data-id) whose correction waits for the story being written
  function openStorySheet(req) {
    var chip = $('#rl-story-for'), hid = $('#rl-story-fortext');
    if (chip) chip.textContent = req || 'Something else they ask for';
    if (hid) hid.value = req || '';
    $$('#rl-story-form textarea').forEach(function (t) { t.value = ''; });
    // Opened from a requirement's correction panel, the correction is applied right after the
    // story is saved and the page then offers Score again with my changes, which is where a
    // re-score is confirmed. A re-score queued here would be stamped before that correction
    // exists and be held, so the sheet does not offer one in that case.
    var rs = $('[data-rl-story-save="1"]');
    if (rs) rs.hidden = !!pendingRow;
    openModal($('#rl-story'));
  }
  function onStoryClick(e) {
    var st = e.target.closest && e.target.closest('[data-rl-story]');
    if (st) {
      e.preventDefault();
      pendingRow = '';
      openStorySheet(st.getAttribute('data-rl-story') || '');
      return;
    }
    var save = e.target.closest && e.target.closest('[data-rl-story-save]');
    if (save) {
      var form = $('#rl-story-form');
      if (!form || !root.htmx) return;
      var data = new FormData(form), vals = {};
      data.forEach(function (v, k) { vals[k] = v; });
      vals.rescore = pendingRow ? '0' : save.getAttribute('data-rl-story-save');
      root.htmx.ajax('POST', '/opportunities/' + oppId() + '/role/story', { values: vals, swap: 'none', source: form });
    }
  }
  function onStorySaved(e) {
    if (root.KUI) root.KUI.closeModal();
    $$('#rl-story-form textarea').forEach(function (t) { t.value = ''; });
    var d = (e && e.detail) || {};
    var id = pendingRow; pendingRow = '';
    var row = id ? $('.rl-mrow[data-id="' + id + '"]') : null;
    var form = row ? $('[data-rl-mform]', row) : null;
    if (!form || !d.id) return;
    // The story the person just wrote is the proof: put it in the panel as the picked story and
    // apply the correction with it (the correction is a local row; nothing is sent).
    var box = $('[data-rl-storychips]', form);
    if (box) {
      var chip = doc.createElement('button');
      chip.type = 'button'; chip.className = 'rl-mchip';
      chip.setAttribute('data-rl-story-pick', String(d.id));
      chip.textContent = d.title || 'Your story';
      box.insertBefore(chip, box.firstChild);
    }
    if (!$('[data-rl-reason]', form).value) setPick(row, 'all');
    pickStory(row, String(d.id));
    if (!$('[data-rl-msave]', form).disabled && form.requestSubmit) form.requestSubmit();
  }


  /* ----------------------------------------------- the match rows: basis, corrections, Evidence
     (issue 170, D-UI-290). Everything here is delegated, so it survives each swap of #rl-match.
     The panel in a row is a form the server owns; this file only opens and closes it, reads the
     picks into its hidden fields, writes the one effect line and keeps Save honest. Nothing here
     sends anything: a Save posts a local correction, and Score again is the existing dialog. */
  var flip = null;      // what #rl-match looked like before a swap: row places, open parts
  var cxWant = null;    // whether the Your view strip should end up open after its swap
  function instant(fn) {
    var m = $('#rl-match'); if (!m) { fn(); return; }
    m.classList.add('rl-noanim'); fn(); void m.offsetHeight;
    if (cx) cx.after(30, function () { m.classList.remove('rl-noanim'); }); else m.classList.remove('rl-noanim');
  }
  function rowOf(el) { return el.closest && el.closest('.rl-mrow'); }
  function formOf(row) { return row ? $('[data-rl-mform]', row) : null; }
  function setStep(form, name, on) {
    var s = $('[data-rl-step="' + name + '"]', form);
    if (s) s.classList.toggle('on', !!on);
  }
  function syncSave(form) {
    var reason = $('[data-rl-reason]', form).value;
    var chip = reason ? $('[data-rl-pick="' + reason + '"]', form) : null;
    var ok = !!chip;
    if (ok && chip.getAttribute('data-story') === '1' && !$('[data-rl-storyid]', form).value) ok = false;
    if (ok && chip.getAttribute('data-note') === 'needed' && !$('[data-rl-note]', form).value.trim()) ok = false;
    $('[data-rl-msave]', form).disabled = !ok;
  }
  function fillStories(form) {
    var box = $('[data-rl-storychips]', form), tpl = $('#rl-story-tpl');
    if (!box || !tpl || box.children.length) return;
    box.appendChild(tpl.content.cloneNode(true));
  }
  function pickStory(row, id) {
    var form = formOf(row); if (!form) return;
    $('[data-rl-storyid]', form).value = id;
    $$('[data-rl-story-pick]', form).forEach(function (c) {
      c.setAttribute('aria-pressed', c.getAttribute('data-rl-story-pick') === id ? 'true' : 'false');
    });
    syncSave(form);
  }
  function setPick(row, reason) {
    var form = formOf(row); if (!form) return;
    var chip = $('[data-rl-pick="' + reason + '"]', form); if (!chip) return;
    $('[data-rl-reason]', form).value = reason;
    $$('[data-rl-pick]', form).forEach(function (c) {
      c.setAttribute('aria-pressed', c === chip ? 'true' : 'false');
    });
    var needs = chip.getAttribute('data-story') === '1';
    if (needs) fillStories(form);
    else { $('[data-rl-storyid]', form).value = ''; pickStory(row, ''); }
    setStep(form, 'story', needs);
    var route = $('[data-rl-step="route"]', form);
    setStep(form, 'route', reason === 'not_me' && !!route && route.textContent.trim() !== '');
    setStep(form, 'note', true);
    var note = $('[data-rl-note]', form);
    note.setAttribute('placeholder', chip.getAttribute('data-note') === 'needed' ? 'What do they mean? (needed)' : 'Anything to add? (optional)');
    var eff = $('[data-rl-effect]', form);
    eff.textContent = '';
    var b = doc.createElement('b'); b.textContent = chip.getAttribute('data-eh') || '';
    eff.appendChild(b);
    eff.appendChild(doc.createTextNode(' ' + (chip.getAttribute('data-et') || '')));
    syncSave(form);
  }
  function setOpenAria(row, on) {
    $$('[data-rl-fix]', row).forEach(function (b) { b.setAttribute('aria-expanded', on ? 'true' : 'false'); });
  }
  function closePanel(row, keepFocus) {
    var panel = row && $('.rl-mpn', row); if (!panel) return;
    panel.classList.remove('on');
    row.classList.remove('is-open');
    setOpenAria(row, false);
    var form = formOf(row);
    // The form is cleared once the panel has closed, so the close itself does not jump.
    var wipe = function () {
      if (!form || row.classList.contains('is-open')) return;
      $('[data-rl-reason]', form).value = ''; $('[data-rl-storyid]', form).value = '';
      $('[data-rl-note]', form).value = '';
      $$('[data-rl-pick], [data-rl-story-pick]', form).forEach(function (c) { c.setAttribute('aria-pressed', 'false'); });
      $$('[data-rl-step]', form).forEach(function (s) { s.classList.remove('on'); });
      $('[data-rl-effect]', form).textContent = '';
      syncSave(form);
    };
    if (reduced() || !cx) wipe(); else cx.after(340, wipe);
    if (keepFocus) { var back = $('[data-rl-fix]', row); if (back) back.focus(); }
    landHeld();
  }
  function closeAllPanels(except) {
    $$('.rl-mrow.is-open').forEach(function (r) { if (r !== except) closePanel(r, false); });
  }
  function openPanel(row, preselect) {
    var panel = $('.rl-mpn', row); if (!panel) return;
    closeAllPanels(row);
    row.classList.add('is-open');
    panel.classList.add('on');
    setOpenAria(row, true);
    if (preselect) setPick(row, preselect);
    var first = $('[data-rl-pick][aria-pressed="true"]', panel) || $('[data-rl-pick]', panel);
    if (first && first.focus) first.focus({ preventScroll: true });
  }
  function toggleTag(btn) {
    var box = doc.getElementById(btn.getAttribute('aria-controls'));
    if (!box) return;
    var on = !box.classList.contains('on');
    box.classList.toggle('on', on);
    btn.setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  function toggleEvidence(btn) {
    var box = doc.getElementById('rl-evbody'); if (!box) return;
    var on = !box.classList.contains('on');
    box.classList.toggle('on', on);
    btn.setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  function onMatchClick(e) {
    var t = e.target; if (!t.closest) return;
    var b;
    if ((b = t.closest('[data-rl-evtoggle]'))) { toggleEvidence(b); return; }
    if ((b = t.closest('[data-rl-tag]'))) { toggleTag(b); return; }
    if ((b = t.closest('[data-rl-fix]'))) {
      var row = rowOf(b); if (!row) return;
      if (row.classList.contains('is-open') && !b.hasAttribute('data-rl-quick')) { closePanel(row, true); return; }
      openPanel(row, row.getAttribute('data-c') === 'met' ? '' : 'all');
      return;
    }
    if ((b = t.closest('[data-rl-pick]'))) { setPick(rowOf(b), b.getAttribute('data-rl-pick')); return; }
    if ((b = t.closest('[data-rl-story-pick]'))) { pickStory(rowOf(b), b.getAttribute('data-rl-story-pick')); return; }
    if ((b = t.closest('[data-rl-story-new]'))) {
      var r2 = rowOf(b); if (!r2) return;
      pendingRow = r2.getAttribute('data-id') || '';
      var tx = $('.rl-mt', r2);
      openStorySheet(tx ? tx.textContent.trim() : '');
      return;
    }
    if ((b = t.closest('[data-rl-mcancel]'))) { closePanel(rowOf(b), true); return; }
    if ((b = t.closest('[data-rl-offer-close]'))) { closeOffer(); }
  }
  function onMatchInput(e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-rl-note]')) syncSave(t.closest('[data-rl-mform]'));
  }
  function onMatchKey(e) {
    if (e.key !== 'Escape') return;
    var open = $('.rl-mrow.is-open');
    if (!open || !open.contains(doc.activeElement)) return;
    e.stopPropagation();
    closePanel(open, true);
  }

  /* The rows slide to their new places (FLIP), the saved one is ringed once (the server marks
     it), and what was open stays open. Reduced motion: no slide, nothing animated. */
  function onBeforeSwap(e) {
    var d = e.detail || {};
    if (!d.target || d.target.id !== 'rl-match') return;
    var pos = {};
    $$('[data-id]', d.target).forEach(function (n) { pos[n.getAttribute('data-id')] = n.getBoundingClientRect(); });
    // htmx puts focus back only on an element with an id, and the Save and Undo buttons have none
    // and are replaced: remember which row had the keyboard so the same row can take it back.
    var had = doc.activeElement, held = had && had.closest ? had.closest('.rl-mrow') : null;
    flip = {
      pos: pos,
      row: held && d.target.contains(held) ? held.getAttribute('data-id') : '',
      ev: !!$('#rl-evbody.on', d.target),
      tags: $$('.rl-mx.on', d.target).map(function (n) { return n.id; }),
    };
  }
  function onAfterSwap(e) {
    var t = e.target;
    if (t && t.id === 'rl-offer') {
      var o = $('[data-rl-offer]', t);
      if (o) { if (reduced()) o.classList.add('on'); else root.requestAnimationFrame(function () { root.requestAnimationFrame(function () { o.classList.add('on'); }); }); }
      return;
    }
    if (t && t.id === 'rl-match') landHeld();
    if (!t || t.id !== 'rl-match' || !flip) return;
    var f = flip; flip = null;
    var m = $('#rl-match'); if (!m) return;
    instant(function () {
      if (f.ev) { var eb = $('#rl-evbody', m), eh = $('[data-rl-evtoggle]', m); if (eb) eb.classList.add('on'); if (eh) eh.setAttribute('aria-expanded', 'true'); }
      f.tags.forEach(function (id) {
        var box = doc.getElementById(id); if (!box) return;
        box.classList.add('on');
        var btn = $('[data-rl-tag][aria-controls="' + id + '"]', m); if (btn) btn.setAttribute('aria-expanded', 'true');
      });
    });
    if (f.row) {
      var back = $$('.rl-mrow', m).filter(function (n) { return n.getAttribute('data-id') === f.row; })[0];
      var pen = back && ($('[data-testid="match-undo"]', back) || $('[data-rl-fix], .rl-pen', back));
      if (pen && pen.focus) pen.focus({ preventScroll: true });
    }
    if (!reduced()) {
      $$('[data-id]', m).forEach(function (n) {
        var a = f.pos[n.getAttribute('data-id')]; if (!a || !n.animate) return;
        var b = n.getBoundingClientRect();
        var dx = a.left - b.left, dy = a.top - b.top;
        if (Math.abs(dx) + Math.abs(dy) > 2) {
          n.animate([{ transform: 'translate(' + dx + 'px,' + dy + 'px)' }, { transform: 'none' }], { duration: 420, easing: 'cubic-bezier(.22,.8,.3,1)' });
        }
      });
    }
    if (cx) cx.after(1200, function () { $$('.rl-mrow.is-flash', m).forEach(function (n) { n.classList.remove('is-flash'); }); });
  }
  // The Your view strip is swapped out of band: keep its old state for the first frame, then
  // move to the new one, so it opens and closes instead of snapping.
  function onOobBefore(e) {
    var d = e.detail || {};
    if (!d.target || d.target.id !== 'rl-cx') return;
    var frag = d.fragment, nu = frag && (frag.querySelector ? frag.querySelector('#rl-cx') : null);
    if (!nu) return;
    cxWant = nu.classList.contains('on');
    nu.classList.toggle('on', d.target.classList.contains('on'));
  }
  function onOobAfter(e) {
    var t = e.target;
    if (!t || t.id !== 'rl-cx' || cxWant === null) return;
    var want = cxWant; cxWant = null;
    if (reduced()) t.classList.toggle('on', want);
    else root.requestAnimationFrame(function () { root.requestAnimationFrame(function () { t.classList.toggle('on', want); }); });
  }
  function onSimilar(e) {
    var h = e.detail && e.detail.hash;
    if (!h || !root.htmx) return;
    root.htmx.ajax('GET', '/opportunities/' + oppId() + '/role/similar?hash=' + encodeURIComponent(h), { target: '#rl-offer', swap: 'innerHTML' });
  }
  function closeOffer() {
    var host = $('#rl-offer'), o = host && $('[data-rl-offer]', host);
    if (!o) return;
    o.classList.remove('on');
    var clear = function () { if (host) host.innerHTML = ''; };
    if (reduced() || !cx) clear(); else cx.after(340, clear);
  }

  /* ------------------------------------------------------------------------- Your view */
  function syncAdj() {
    var form = $('.rl-adjin'); if (!form) return;
    var picked = $('#rl-pick button.is-on');
    var save = $('#rl-adjsave'); if (save) save.disabled = !picked;
    var score = $('#rl-score'); if (score) score.value = picked ? picked.getAttribute('data-n') : '';
  }
  function onAdjustClick(e) {
    var t = e.target;
    var ad = t.closest && t.closest('[data-rl-adjust]');
    if (ad) {
      var panel = $('#rl-adj'); if (!panel) return;
      var open = !panel.classList.contains('is-on');
      panel.classList.toggle('is-on', open);
      ad.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }
    if (t.closest && t.closest('[data-rl-adjcancel]')) {
      var p2 = $('#rl-adj'); if (p2) p2.classList.remove('is-on');
      $$('[data-rl-adjust]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
      return;
    }
    var pick = t.closest && t.closest('#rl-pick button');
    if (pick) {
      $$('#rl-pick button').forEach(function (b) {
        var on = b === pick; b.classList.toggle('is-on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      syncAdj();
      return;
    }
    var nudge = t.closest && t.closest('#rl-nud [data-n]');
    if (nudge) {
      var row = nudge.closest('.rl-nr'); if (!row) return;
      var v = parseFloat(row.getAttribute('data-v')) + 0.5 * parseInt(nudge.getAttribute('data-n'), 10);
      v = Math.max(1, Math.min(5, v));
      row.setAttribute('data-v', String(v));
      row.querySelector('b').textContent = v.toFixed(1);
      row.querySelector('input').value = String(v);
      var bar = row.querySelector('u'); if (bar) bar.style.setProperty('--w', (v / 5 * 100) + '%');
      row.classList.add('is-changed');
    }
  }

  /* ------------------------------------------------- Not for me, the More menu, Why?, Copy link */
  function onMenuClick(e) {
    var t = e.target;
    var nfm = t.closest && t.closest('[data-rl-nfm]');
    if (nfm) {
      var p = $('#rl-nfr'); if (!p) return;
      var open = !p.classList.contains('is-on');
      p.classList.toggle('is-on', open);
      nfm.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }
    if (t.closest && t.closest('[data-rl-nfm-close]')) {
      var p2 = $('#rl-nfr'); if (p2) p2.classList.remove('is-on');
      var b = $('[data-rl-nfm]'); if (b) b.setAttribute('aria-expanded', 'false');
      return;
    }
    var more = t.closest && t.closest('[data-rl-more] .rl-morebtn');
    var menus = $$('.rl-menu');
    if (more) {
      var menu = $('.rl-menu', more.parentNode);
      var show = menu.hidden;
      menus.forEach(function (m) { m.hidden = true; });
      menu.hidden = !show;
      more.setAttribute('aria-expanded', show ? 'true' : 'false');
      return;
    }
    if (!(t.closest && t.closest('.rl-menu')) || (t.closest && t.closest('.rl-menu [role="menuitem"]'))) {
      menus.forEach(function (m) { m.hidden = true; });
      $$('.rl-morebtn').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    }
    var why = t.closest && t.closest('[data-rl-open]');
    if (why) { e.preventDefault(); openModal($('#' + why.getAttribute('data-rl-open'))); return; }
    var href = t.closest && t.closest('[data-rl-help]');
    if (href) { if (root.KUI && root.KUI.go) root.KUI.go('/help'); else root.location.assign('/help'); return; }
    if (t.closest && t.closest('[data-rl-copy]')) {
      var url = root.location.origin + '/opportunities/' + oppId();
      var mine = cx;  // an answer that comes after the page was left says nothing
      var done = function () { if (mine && !mine.signal.aborted) toast('Link copied.'); };
      var show = function () { if (mine && !mine.signal.aborted) toast(url); };
      if (root.navigator.clipboard && root.navigator.clipboard.writeText) {
        root.navigator.clipboard.writeText(url).then(done, show);
      } else { toast(url); }
    }
  }
  function onEscape(e) {
    if (e.key === 'Escape') {
      $$('.rl-menu').forEach(function (m) { m.hidden = true; });
      if (focusKey) clearFocus();
    }
  }

  /* ------------------------------------------------------ dialogs the actions load, previews */
  function fitPreview(modal) {
    var stage = $('[data-rl-stage]', modal), page = $('[data-rl-page]', modal), frame = $('.rl-pvframe', modal);
    if (!stage || !page || !frame) return;
    var s = Math.min(1, Math.max(0.3, (stage.clientWidth - 48) / 816));
    page.style.setProperty('--s', String(s));
    var h = 1056;
    try { h = Math.max(1056, frame.contentDocument.documentElement.scrollHeight); } catch (err) { /* sandbox */ }
    frame.style.height = h + 'px';
    page.style.height = (h * s) + 'px';
  }
  function openLoaded() {
    var host = $('#rl-dialog'); if (!host) return;
    var m = $('.k-modal', host);
    // A dialog KUI gave back after it closed (data-k-returned) is done: only a new one opens.
    if (m && !m.classList.contains('is-open') && !m.hasAttribute('data-k-returned')) {
      openModal(m);
      if (m.classList.contains('rl-preview')) {
        var frame = $('.rl-pvframe', m);
        if (frame) frame.addEventListener('load', function () { fitPreview(m); });
        fitPreview(m);
      }
    }
  }
  // A dialog an action loaded lands in #rl-dialog; the observer opens it. It goes with the page.
  // Back and Forward make htmx put the saved body back, a new #rl-dialog: watch that one instead.
  function watchDialog() {
    var host = $('#rl-dialog'), mine = cx;
    if (dialogWatch) dialogWatch.disconnect();
    dialogWatch = null;
    if (!host || !root.MutationObserver) return;
    dialogWatch = mine.observe(new root.MutationObserver(function () { mine.after(0, openLoaded); }));
    dialogWatch.observe(host, { childList: true });
  }
  function onPreviewResize() {
    var m = $('#rl-dialog .rl-preview.is-open'); if (m) fitPreview(m);
  }
  function onClosed(e) {
    var el = e.target;
    if (el && el.closest && el.closest('#rl-dialog')) {
      cx.after(350, function () { var h = $('#rl-dialog'); if (h && el.parentNode === h) h.innerHTML = ''; });
    }
  }

  /* --------------------------------------------------- a reload after an action, and after Undo */
  /* A toast outlives Previous and Next (toasts sit outside the swapped body), so its refresh
     may name another role's area: load only what belongs to the role on screen now (D-UI-322). */
  function forThisRole(url) {
    var m = /^\/opportunities\/(\d+)\//.exec(String(url));
    var b = body();  // #rl-body is swapped by Prev and Next, #role is not
    return !m || m[1] === String(b ? b.getAttribute('data-opp') : oppId());
  }
  function reload(items) {
    if (!root.htmx) return;
    (items || []).forEach(function (it) {
      if (it && it.url && it.target && forThisRole(it.url)) {
        root.htmx.ajax('GET', it.url, { target: it.target, swap: it.swap || 'outerHTML' });
      }
    });
  }
  function onUndone(e) {
    var d = e.detail || {};
    reload(d.refresh);
  }
  function onRefresh() {
    var b = body(); if (!b || !root.htmx) return;
    root.htmx.ajax('GET', '/opportunities/' + oppId() + '/tab/' + (b.getAttribute('data-tab') || 'evaluation'),
      { target: '#rl-pane', swap: 'innerHTML' });
  }

  /* ------------------------------------------------------------ Being scored again (issue #74)
     While the pane shows the Being scored again line, one ctx timer asks where the score stands.
     A change from queued to running only rewrites the two spans, so nothing is redrawn. When the
     task is gone (scored, failed or cancelled) the pane is asked for again and swapped whole in
     one step, and the new score card eases in from the earlier one. With no such line on the page
     there is no timer at all, and the next swap that brings one starts it. The timer also stops
     when nothing can move the score on (`stalled`: the task was deferred, e.g. an online key out of
     credit, so it cannot run now; a task that merely failed and was requeued keeps polling), and after
     two minutes of watching it slows from 3 seconds to 15. */
  var RS_FAST = 3000, RS_SLOW = 15000, RS_SLOW_AFTER = 120000;
  var rsTimer = 0, rsBusy = false, rsLanding = false, rsStart = 0, rsSlow = false;
  var rsHeld = false;  // the new score is ready but an open correction panel is waiting
  function landHeld() {
    if (!rsHeld || $('.rl-mrow.is-open')) return;
    rsHeld = false; rsLanding = true; onRefresh();
  }
  function rsBanner() { return $('.rl-rescore[data-rescoring]'); }
  function syncRescore() {
    if (!cx) return;
    var b = rsBanner();
    if (b && b.getAttribute('data-stalled') !== '1') {
      if (!rsTimer) { rsStart = Date.now(); rsSlow = false; rsTimer = cx.every(RS_FAST, pollRescore); }
    } else if (rsTimer) {
      cx.clear(rsTimer); rsTimer = 0;
    }
  }
  function pollRescore() {
    var b = rsBanner();
    if (!b || rsBusy || doc.hidden || !root.fetch || !cx) return;
    var signal = cx.signal;
    if (!rsSlow && Date.now() - rsStart > RS_SLOW_AFTER) {
      rsSlow = true; cx.clear(rsTimer); rsTimer = cx.every(RS_SLOW, pollRescore);
    }
    rsBusy = true;
    root.fetch('/opportunities/' + oppId() + '/role/rescoring', { credentials: 'same-origin', signal: signal })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        rsBusy = false;
        var cur = rsBanner();
        if (!d || signal.aborted || !cur) return;
        if (d.state === 'none') {
          // A correction panel is open (maybe with a note typed): swapping the pane now would
          // throw it away. Say it is done and refresh once the person has closed or saved it.
          if ($('.rl-mrow.is-open')) {
            rsHeld = true;
            if (cx && rsTimer) { cx.clear(rsTimer); rsTimer = 0; }
            cur.setAttribute('data-rescoring', 'none');
            $$('[data-rs]', cur).forEach(function (n) {
              n.textContent = n.getAttribute('data-rs') === 'title'
                ? 'Scored again.' : 'The new score shows when you close or save your change.';
            });
            return;
          }
          rsLanding = true; onRefresh(); return;
        }
        cur.setAttribute('data-rescoring', d.state);
        cur.setAttribute('data-stalled', d.stalled ? '1' : '0');
        if (d.stalled && rsTimer) { cx.clear(rsTimer); rsTimer = 0; }
        $$('[data-rs]', cur).forEach(function (n) {
          var t = d[n.getAttribute('data-rs')];
          if (t != null && n.textContent !== t) n.textContent = t;
        });
      })
      .catch(function () { rsBusy = false; /* offline or restarting: the next tick asks again */ });
  }
  function onRescoreSwap(e) {
    var t = e.target;
    if (rsLanding && t && t.id === 'rl-pane') {
      rsLanding = false;
      var card = $('.rl-card--score', t);
      if (card && card.animate && !reduced()) {
        card.animate([{ opacity: 0.55 }, { opacity: 1 }], { duration: 320, easing: 'cubic-bezier(.22,.8,.3,1)' });
      }
    }
    syncRescore();
  }

  /* ------------------------------------------------ cover letter and engagement outline (D-UI-239)
     The sheet's tabs, the source card for a traced claim, the ask-first box that releases Send and
     draft, the live word count, and the inline "Discard this draft?" question. Everything the
     server decides (what is sent, what is saved) is a form post; nothing here sends anything. */
  function letter() { return $('#rl-letter'); }
  function setTab(box, name) {
    $$('[data-cl-tab]', box).forEach(function (b) {
      var on = b.getAttribute('data-cl-tab') === name;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $$('[data-cl-pane]', box).forEach(function (p) {
      p.classList.toggle('is-on', p.getAttribute('data-cl-pane') === name);
    });
    var d = $('#cl-draft', box); if (d) d.setAttribute('data-tab', name);
    $$('.cl-bar [data-cl-show]', box).forEach(function (b) {
      b.hidden = (' ' + b.getAttribute('data-cl-show') + ' ').indexOf(' ' + name + ' ') < 0;
      var p = b.getAttribute('data-cl-primary');
      if (p) b.classList.toggle('k-btn--primary', p === name);
    });
    var hid = $('input[name="tab"]', box); if (hid) hid.value = name === 'edit' ? 'edit' : 'check';
    if (name === 'preview') {
      fitPreview(box);
      // The one-page check makes the PDF, so it is asked for only when the Preview is opened.
      var fit = $('[data-cl-fit]', box);
      if (fit && root.htmx) root.htmx.trigger(fit, 'cl-fit');
    }
  }
  function showSource(box, key) {
    var any = false;
    $$('.cl-src', box).forEach(function (n) {
      var on = n.getAttribute('data-cl-src') === key;
      n.classList.toggle('is-on', on); any = any || on;
    });
    $$('.cl-claim[data-cl-key]', box).forEach(function (n) {
      n.classList.toggle('is-on', n.getAttribute('data-cl-key') === key);
    });
    var hint = $('[data-cl-hint]', box); if (hint) hint.hidden = any;
  }
  function wordsIn(area) { return (area.value.trim().match(/\S+/g) || []).length; }
  function countWords(area) {
    var n = wordsIn(area);
    var lo = parseInt(area.getAttribute('data-lo'), 10), hi = parseInt(area.getAttribute('data-hi'), 10);
    var max = parseInt(area.getAttribute('data-max'), 10) || 0;
    var out = $('[data-cl-count]', area.form || doc);
    if (!out) return;
    var first = out.firstChild;
    if (first && first.nodeType === 3) first.nodeValue = n + ' words';
    // Over the limit is a soft warning; over the save cap is a hard stop (the server agrees).
    out.setAttribute('data-state', max && n > max ? 'stop' : n > hi ? 'over' : n >= lo ? 'ok' : 'off');
  }
  function onLetterSubmit(e) {
    var form = e.target;
    if (!form || form.id !== 'cl-edit-form') return;
    var area = $('[data-cl-text]', form);
    var max = area ? parseInt(area.getAttribute('data-max'), 10) || 0 : 0;
    if (area && max && wordsIn(area) > max) {
      // Not posted: the hard cap. The typed text stays as it is; the server refuses it as well.
      e.preventDefault(); e.stopPropagation();
      var lim = $('[data-cl-limit]', form);
      if (lim) { lim.classList.remove('is-on'); void lim.offsetWidth; lim.classList.add('is-on'); }
      area.focus();
    }
  }
  function onLetterClick(e) {
    var t = e.target, box = letter();
    if (!box || !t.closest || !box.contains(t)) return;
    var send = t.closest('[data-cl-send]');
    if (send && send.form) {
      var ack = $('[data-cl-ack]', send.form);
      if (ack && !ack.checked) {
        // Not ticked: nothing is posted. The server refuses an unconfirmed post as well.
        e.preventDefault(); e.stopPropagation();
        var hint = $('[data-cl-tick]', send.form);
        if (hint) { hint.classList.remove('is-on'); void hint.offsetWidth; hint.classList.add('is-on'); }
        ack.focus();
        return;
      }
    }
    var tab = t.closest('[data-cl-tab]');
    if (tab) { setTab(box, tab.getAttribute('data-cl-tab')); return; }
    var claim = t.closest('.cl-claim[data-cl-key]');
    if (claim) { showSource(box, claim.getAttribute('data-cl-key')); return; }
    if (t.closest('[data-cl-leave]')) {
      toast('Still drafting. It will be in Documents when it is done.');
      if (root.KUI) root.KUI.closeModal();
      return;
    }
    var go = t.closest('[data-cl-go]');
    if (go) { setTab(box, go.getAttribute('data-cl-go')); return; }
    var ask = t.closest('[data-cl-ask]');
    if (ask) {
      var want = ask.getAttribute('data-cl-ask');
      $$('[data-cl-confirm]', box).forEach(function (c) {
        c.classList.toggle('is-on', c.getAttribute('data-cl-confirm') === want);
      });
      return;
    }
    if (t.closest('[data-cl-keep]')) {
      $$('[data-cl-confirm]', box).forEach(function (c) { c.classList.remove('is-on'); });
    }
  }
  function onLetterKey(e) {
    var t = e.target;
    if ((e.key === 'Enter' || e.key === ' ') && t && t.matches && t.matches('.cl-claim[data-cl-key]')) {
      e.preventDefault();
      var box = letter(); if (box) showSource(box, t.getAttribute('data-cl-key'));
    }
  }
  function onLetterChange(e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-cl-ack]')) {
      var hint = $('[data-cl-tick]', t.form || doc);
      if (hint && t.checked) hint.classList.remove('is-on');
    }
  }
  function onLetterInput(e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-cl-text]')) countWords(t);
  }
  function onLetterClose() { if (root.KUI) root.KUI.closeModal(); }
  function onLetterSwap(e) {
    var t = e.target, box = letter();
    if (!box || !t || t.id !== 'cl-main') return;
    var d = $('#cl-draft', box);
    if (d) setTab(box, d.getAttribute('data-tab') || 'check');
    var ta = $('[data-cl-text]', box); if (ta) countWords(ta);
    var fr = $('.rl-pvframe', box);
    if (fr) fr.addEventListener('load', function () { fitPreview(box); });
    var s = $('[data-cl-saved].is-on', box);
    if (s) cx.after(2300, function () { s.classList.remove('is-on'); });
  }

  /* ------------------------------------------------------------------------------ the page */
  root.KUI.page.define('role', {
    mount: function (main, ctx) {
      cx = ctx;
      pendingDir = 0; pendingKind = ''; focusKey = null; dialogWatch = null;
      flip = null; cxWant = null; pendingRow = ''; rsHeld = false;
      rsTimer = 0; rsBusy = false; rsLanding = false;
      // The order the file added them in: the capture-phase tab click stays ahead of the rest.
      ctx.on(root, 'resize', onInkResize);
      if (doc.fonts && doc.fonts.ready) {
        doc.fonts.ready.then(function () { if (!ctx.signal.aborted) placeInk(true); });
      }
      ctx.on(doc, 'click', onTabClick, true);
      ctx.on(doc, 'htmx:afterSwap', onBodySwap);
      ctx.on(doc, 'htmx:load', onLoad);
      placeInk(true);
      ctx.on(doc, 'click', onFocusClick);
      ctx.on(doc, 'click', onStoryClick);
      ctx.on(doc, 'rl-story-saved', onStorySaved);
      ctx.on(doc, 'click', onMatchClick);
      ctx.on(doc, 'input', onMatchInput);
      ctx.on(doc, 'keydown', onMatchKey, true);
      ctx.on(doc, 'htmx:beforeSwap', onBeforeSwap);
      ctx.on(doc, 'htmx:afterSwap', onAfterSwap);
      ctx.on(doc, 'htmx:oobBeforeSwap', onOobBefore);
      ctx.on(doc, 'htmx:oobAfterSwap', onOobAfter);
      ctx.on(doc, 'rl-similar', onSimilar);
      ctx.on(doc, 'click', onAdjustClick);
      ctx.on(doc, 'click', onMenuClick);
      ctx.on(doc, 'click', onLetterClick);
      ctx.on(doc, 'keydown', onLetterKey);
      ctx.on(doc, 'change', onLetterChange);
      ctx.on(doc, 'input', onLetterInput);
      ctx.on(doc, 'submit', onLetterSubmit, true);
      ctx.on(doc, 'rl-letter-close', onLetterClose);
      ctx.on(doc, 'htmx:afterSettle', onLetterSwap);
      ctx.on(doc, 'keydown', onEscape);
      watchDialog();
      ctx.on(doc, 'htmx:historyRestore', watchDialog);
      ctx.on(root, 'resize', onPreviewResize);
      ctx.on(doc, 'k-closed', onClosed);
      ctx.on(doc, 'kestrel:undone', onUndone);
      ctx.on(doc, 'rl-refresh', onRefresh);
      ctx.on(doc, 'htmx:afterSwap', onRescoreSwap);
      syncRescore();
      ctx.onUnmount(function () { if (cx === ctx) cx = null; });
    }
  });
})(typeof window !== 'undefined' ? window : globalThis);
