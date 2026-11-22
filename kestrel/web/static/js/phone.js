/* Phone formatting (#207, D-PHONE-5). Formats when the person leaves the field and when they
   paste, through one stateless server call. Never while typing, so the caret never moves. An
   unreadable entry stays exactly as typed and only a quiet note appears. Delegated and installed
   once, so an htmx swap needs no re-binding. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || root.KPhone) return;
  // One stale-reply counter per field: an answer for one field is never discarded because another
  // field started a request (a counter shared by the phone and LinkedIn fields did exactly that).
  function nextSeq(el) { el.kphSeq = (el.kphSeq || 0) + 1; return el.kphSeq; }

  function fieldOf(el) { return el && el.closest ? el.closest('[data-phone-field]') : null; }

  function show(note, text) {
    if (!note) return;
    if (text) { note.textContent = text; note.classList.add('ph-show'); }
    else { note.classList.remove('ph-show'); }
  }

  // fromStored: the home country changed, so reformat the stored number, not the old display.
  function format(field, fromStored) {
    var input = field.querySelector('[data-phone-input]');
    var sel = field.querySelector('[data-phone-country]');
    var kept = field.querySelector('[data-phone-entered]');
    var e164 = field.querySelector('[data-phone-e164]');
    var e164Text = field.querySelector('[data-phone-e164-text]');
    var shownUnder = field.querySelector('[data-phone-shown-under]');
    var note = field.querySelector('[data-phone-note]');
    var typed = input.value;
    if (!typed.trim()) { show(note, ''); if (kept) kept.value = ''; return; }
    var mine = nextSeq(input);
    // The pinned number is sent only while the field still holds the text it was computed from;
    // text typed since is read afresh, so a country change never sends a stale number over it.
    var pinned = e164 && e164Text && e164.value && input.value === e164Text.value;
    var asked = sel ? sel.value : '';
    // text_country: the country the shown text was last formatted under, so the server reads the
    // pinned number against the text as the person saw it. It only shapes what is displayed;
    // a save decides again from the server's own record.
    var body = new URLSearchParams({ phone: typed, country: asked, e164: pinned ? e164.value : '',
                                    text_country: shownUnder ? shownUnder.value : '' });
    root.fetch('/api/phone/format', {
      method: 'POST', body: body, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    }).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      // A newer edit already started: this answer is stale. Typing since then also wins.
      if (!j || mine !== input.kphSeq || input.value !== typed) return;
      if (j.readable) {
        if (kept && !fromStored) kept.value = typed;
        if (j.display !== typed) {
          input.value = j.display;
          input.classList.remove('ph-flash');
          void input.offsetWidth;
          input.classList.add('ph-flash');
          input.dispatchEvent(new Event('input', { bubbles: true }));
        }
        if (e164) e164.value = j.source || '';
        if (e164Text) e164Text.value = j.display;
        if (shownUnder) shownUnder.value = asked;
      } else {
        if (kept) kept.value = typed;
        if (e164) e164.value = '';
        if (e164Text) e164Text.value = '';
      }
      show(note, j.note);
    }).catch(function () { /* offline server: the save formats it */ });
  }

  // The person's own LinkedIn address: shape only, through the same stateless server (D-PHONE-6).
  function normalizeLinkedIn(field) {
    var input = field.querySelector('[data-linkedin-input]');
    var note = field.querySelector('[data-linkedin-note]');
    var typed = input.value;
    if (!typed.trim()) { show(note, ''); return; }
    var mine = nextSeq(input);
    root.fetch('/api/linkedin/normalize', {
      method: 'POST', body: new URLSearchParams({ linkedin: typed }), credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    }).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      if (!j || mine !== input.kphSeq || input.value !== typed) return;
      if (j.readable && j.display !== typed) {
        input.value = j.display;
        input.classList.remove('ph-flash');
        void input.offsetWidth;
        input.classList.add('ph-flash');
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      show(note, j.note);
    }).catch(function () { /* offline server: the save normalizes it */ });
  }

  function linkedinField(el) { return el && el.closest ? el.closest('[data-linkedin-field]') : null; }

  doc.addEventListener('focusout', function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-linkedin-input]')) normalizeLinkedIn(linkedinField(t));
  });
  doc.addEventListener('paste', function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-linkedin-input]')) {
      var f = linkedinField(t);
      root.setTimeout(function () { normalizeLinkedIn(f); }, 0);
    }
  });
  doc.addEventListener('focusout', function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-phone-input]')) format(fieldOf(t));
  });
  doc.addEventListener('paste', function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-phone-input]')) {
      var f = fieldOf(t);
      root.setTimeout(function () { format(f); }, 0);  // after the pasted text lands
    }
  });
  doc.addEventListener('change', function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('[data-phone-country]')) format(fieldOf(t), true);
  });
  root.KPhone = { format: format };
})(window);
