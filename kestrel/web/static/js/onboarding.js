/* Onboarding (D-UI-30, D-UI-155): the small bits of behavior the wizard needs beyond htmx. Plain
   script, delegated, so nothing needs re-binding after a step swap. The server decides every
   requirement; this only repeats the same rules live so a button is not pressed for nothing:
     - folds ("Add more details", "Paste text") and the near-minimum switch
     - the key links that follow the chosen service, and the Connect button's gate
     - the Save and continue gate on About you and What you want (the same hints the server gives)
     - suggestion chips, the live "Kestrel will search" line, the resume drop zone
     - the starting overlay while Start searching runs */
(function (root) {
  'use strict';
  var doc = root.document;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }

  /* -------------------------------------------------------------------------------- folds */
  function setFold(id, on) {
    var el = doc.getElementById(id);
    if (el) el.classList.toggle('on', !!on);
  }
  doc.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-ob-fold]');
    if (t) {
      var id = t.getAttribute('data-ob-fold');
      var fold = doc.getElementById(id);
      if (!fold) return;
      var on = !fold.classList.contains('on');
      fold.classList.toggle('on', on);
      t.setAttribute('aria-expanded', on ? 'true' : 'false');
      var label = t.getAttribute(on ? 'data-fold-less' : 'data-fold-more');
      if (label) t.textContent = label;
      if (on) { var f = $('textarea, input', fold); if (f && t.getAttribute('data-fold-focus') !== null) f.focus(); }
      return;
    }
    var rm = e.target.closest && e.target.closest('.ob-wiz [data-pf-chip-remove]');
    if (rm) {
      // profile.js builds chips but has no handler for the x on one; the wizard supplies it.
      var chip = rm.closest('[data-pf-chip]');
      var box0 = rm.closest('[data-pf-chips]');
      if (chip && box0) {
        chip.remove();
        var area = $('[data-pf-chips-value]', box0);
        if (area) {
          area.value = $$('[data-pf-chip] > span', box0).map(function (n) { return n.textContent; }).join('\n');
        }
      }
      return;
    }
    var sug = e.target.closest && e.target.closest('[data-ob-sug]');
    if (sug) {
      var box = $('[data-pf-chips="' + sug.getAttribute('data-ob-sug') + '"]');
      var input = box && $('[data-pf-chip-input]', box);
      if (input) {
        input.value = sug.getAttribute('data-v');
        input.dispatchEvent(new root.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
        sug.remove();
      }
      return;
    }
    if (e.target.closest && e.target.closest('[data-ob-restart]')) {
      var holder = doc.getElementById('import-proposal');
      if (holder) holder.innerHTML = '';
      var file = doc.getElementById('ob-imp-file');
      if (file) file.value = '';
      var name = $('[data-ob-filename]');
      if (name) name.textContent = name.getAttribute('data-default') || name.textContent;
    }
  });
  doc.addEventListener('change', function (e) {
    var sw = e.target.closest && e.target.closest('[data-ob-fold-switch]');
    if (sw) setFold(sw.getAttribute('data-ob-fold-switch'), sw.checked);
  });

  /* ------------------------------------------------------------------- the online connect box */
  function connectState() {
    var form = $('[data-ob-connect]');
    if (!form) return;
    var btn = $('[data-ob-connect-btn]', form);
    var hint = $('[data-ob-connect-hint]', form);
    var key = $('#ob-key', form);
    var ack = $('#ob-ack', form);
    var radio = $('[data-ob-provider]:checked', form);
    $$('.ob-keylinks li', form).forEach(function (li) {
      li.hidden = !radio || li.getAttribute('data-provider') !== radio.value;
    });
    var saved = !!(radio && radio.hasAttribute('data-saved'));
    var typed = key ? key.value.trim().length : 0;
    var why = '';
    if (!typed && !saved) why = 'Paste your key first';
    else if (typed && typed < 8) why = 'That key looks too short';
    else if (!ack || !ack.checked) why = 'Tick the box first';
    if (btn) btn.disabled = !!why;
    if (hint) hint.textContent = why;
  }

  /* ---------------------------------------------------------------------- About you and Wants */
  function chipValue(name) {
    var a = $('[data-pf-chips="' + name + '"] [data-pf-chips-value]');
    return a ? a.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean) : [];
  }
  function list(a) {
    if (a.length < 2) return a[0] || '';
    return a.slice(0, -1).join(', ') + ' and ' + a[a.length - 1];
  }
  function gateState() {
    var btn = $('[data-ob-gate]');
    var hintEl = $('[data-ob-hint]');
    if (!btn) return;
    var why = '';
    if (btn.getAttribute('data-ob-gate') === 'about') {
      var first = $('[data-ob-need="first"]');
      var jobs = $('[data-ob-track="jobs"]');
      var cons = $('[data-ob-track="consulting"]');
      if (first && !first.value.trim()) why = 'Add your first name first';
      else if (!((jobs && jobs.checked) || (cons && cons.checked))) why = 'Pick at least one to continue';
    } else {
      var hasTitles = !!$('[data-pf-chips="target_titles"]');
      var remote = $('#ob-remote');
      if (hasTitles && !chipValue('target_titles').length) why = 'Add a job title first';
      else if (!chipValue('onsite_metros').length && !(remote && remote.checked)) why = 'Add a place or turn on remote first';
    }
    btn.disabled = !!why;
    if (hintEl) hintEl.textContent = why;
  }
  function trackSummary() {
    var el = $('[data-ob-tsum]');
    if (!el) return;
    var jobs = $('[data-ob-track="jobs"]');
    var cons = $('[data-ob-track="consulting"]');
    var j = jobs && jobs.checked, c = cons && cons.checked;
    el.textContent = !j && !c ? 'Pick at least one to continue.'
      : j && c ? 'You will answer a short section for each on the "What you want" step.'
      : j ? 'Jobs only. The consulting section is left out.'
      : 'Consulting only. The jobs section is left out.';
  }
  function preview() {
    var el = $('[data-ob-preview]');
    if (!el) return;
    var places = chipValue('onsite_metros').map(function (p) { return p.split(':')[0].trim(); });
    var remote = $('#ob-remote');
    if (remote && remote.checked) places.push('remote (US)');
    var where = list(places) || 'anywhere you choose';
    var titles = chipValue('target_titles');
    var kinds = chipValue('service_lines');
    var jobs = !!$('[data-pf-chips="target_titles"]');
    el.textContent = '';
    var lead = doc.createElement('span');
    lead.textContent = 'Kestrel will search: ';
    el.appendChild(lead);
    var b = doc.createElement('b');
    b.textContent = jobs ? (titles.length ? list(titles) : '(add a job title)')
      : (kinds.length ? list(kinds) : 'consulting and contract work');
    el.appendChild(b);
    el.appendChild(doc.createTextNode(' in ' + where));
  }
  function refresh() { connectState(); gateState(); trackSummary(); preview(); watchChips(); bindDrop(); }

  var watched = typeof root.WeakSet === 'function' ? new root.WeakSet() : null;
  function watchChips() {
    $$('[data-pf-chips]').forEach(function (box) {
      if (watched && watched.has(box)) return;
      if (watched) watched.add(box);
      if (root.MutationObserver) {
        new root.MutationObserver(function () { gateState(); preview(); }).observe(box, { childList: true, subtree: true });
      }
    });
  }

  doc.addEventListener('input', function (e) {
    if (e.target.closest && e.target.closest('[data-ob-connect], [data-ob-form]')) refresh();
  });
  doc.addEventListener('change', function (e) {
    if (e.target.closest && e.target.closest('[data-ob-connect], [data-ob-form]')) refresh();
    if (e.target.id === 'ob-imp-file') {
      var name = $('[data-ob-filename]');
      if (name) {
        if (!name.getAttribute('data-default')) name.setAttribute('data-default', name.textContent);
        var f = e.target.files && e.target.files[0];
        name.textContent = f ? f.name : name.getAttribute('data-default');
      }
    }
  });

  /* ------------------------------------------------------------------- the resume drop zone */
  function bindDrop() {
    var zone = $('[data-ob-drop]');
    if (!zone || zone.__obBound) return;
    zone.__obBound = true;
    ['dragenter', 'dragover'].forEach(function (n) {
      zone.addEventListener(n, function (e) { e.preventDefault(); zone.classList.add('over'); });
    });
    ['dragleave', 'drop'].forEach(function (n) {
      zone.addEventListener(n, function () { zone.classList.remove('over'); });
    });
    zone.addEventListener('drop', function (e) {
      e.preventDefault();
      var input = doc.getElementById('ob-imp-file');
      if (input && e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new root.Event('change', { bubbles: true }));
      }
    });
  }

  /* ------------------------------------------------------------------ the starting overlay */
  doc.addEventListener('htmx:beforeRequest', function (e) {
    if (e.detail.elt && e.detail.elt.hasAttribute && e.detail.elt.hasAttribute('data-ob-start-form')) {
      var o = doc.getElementById('ob-start');
      if (o) o.hidden = false;
    }
  });
  doc.addEventListener('htmx:beforeOnLoad', function (e) {
    var xhr = e.detail && e.detail.xhr;
    if (!xhr || !e.detail.elt || !e.detail.elt.hasAttribute('data-ob-start-form')) return;
    if (xhr.getResponseHeader && xhr.getResponseHeader('HX-Redirect')) {
      $$('[data-ob-start-row]').forEach(function (r) {
        r.className = 'ob-ok ob-ok--good';
        r.innerHTML = '<svg class="k-ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>';
      });
    }
  });
  doc.addEventListener('htmx:afterRequest', function (e) {
    if (e.detail.elt && e.detail.elt.hasAttribute && e.detail.elt.hasAttribute('data-ob-start-form')) {
      var o = doc.getElementById('ob-start');
      if (o && !e.detail.successful) o.hidden = true;
    }
  });

  doc.addEventListener('htmx:afterSettle', refresh);
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', refresh);
  else refresh();
})(window);
