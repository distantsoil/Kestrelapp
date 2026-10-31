/* Onboarding (D-UI-30, D-UI-155): the small bits of behavior the wizard needs beyond htmx. Plain
   script, delegated, so nothing needs re-binding after a step swap. The server decides every
   requirement; this only repeats the same rules live so a button is not pressed for nothing:
     - folds ("Add more details", "Paste text") and the near-minimum switch
     - the key links that follow the chosen service, and the Connect button's gate
     - the Save and continue gate on About you and What you want (the same hints the server gives)
     - suggestion chips, the live "Kestrel will search" line, the resume drop zone
     - the starting overlay while Start searching runs
   The page runs on the lifecycle (KUI.page, D-UI-210): this file defines the page "onboarding",
   whose mount first mounts the Profile form behavior (profile.js, KProfile.mount, loaded before
   this file) and then its own, every listener, timer and observer through the page's context. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page || !root.KProfile) return;
  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var cx = null;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }

  /* -------------------------------------------------------------------------------- folds */
  function setFold(id, on) {
    var el = doc.getElementById(id);
    if (el) el.classList.toggle('on', !!on);
  }
  function onClick(e) {
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
  }
  function onFoldSwitch(e) {
    var sw = e.target.closest && e.target.closest('[data-ob-fold-switch]');
    if (sw) setFold(sw.getAttribute('data-ob-fold-switch'), sw.checked);
  }

  /* ------------------------------------------------------------------- the online connect box */
  function connectState() {
    var form = $('[data-ob-connect]');
    if (!form) return;
    var btn = $('[data-ob-connect-btn]', form);
    var hint = $('[data-ob-connect-hint]', form);
    var key = $('#ob-key', form);
    var ack = $('#ob-ack', form);
    var radio = $('[data-ob-provider]:checked', form);
    var free = $('[data-ob-free]', form);
    var train = $('[data-ob-train]', form);
    var more = $('[data-ob-more]', form);
    $$('.ob-keylinks li', form).forEach(function (li) {
      li.hidden = !radio || li.getAttribute('data-provider') !== radio.value;
    });
    var saved = !!(radio && radio.hasAttribute('data-saved'));
    var typed = key ? key.value.trim().length : 0;
    var why = '';
    if (!typed && !saved) why = 'Paste your key first';
    else if (typed && typed < 8) why = 'That key looks too short';
    else if (!ack || !ack.checked) why = 'Tick the box first';
    else if (free && free.checked && !(train && train.checked) && radio && radio.value === 'openrouter') {
      // The server refuses this pair (free models are served by providers that keep prompts),
      // so say it before the click and open the box that holds the fix.
      why = 'Free models also need "Allow providers that may train"';
      if (more) more.open = true;
    }
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

  // One observer per chip box this visit; a box a step swap removed is let go with its observer.
  var chipObs = [];
  function watchChips() {
    chipObs = chipObs.filter(function (c) {
      if (c.box.isConnected) return true;
      c.obs.disconnect();
      return false;
    });
    $$('[data-pf-chips]').forEach(function (box) {
      if (chipObs.some(function (c) { return c.box === box; })) return;
      if (!root.MutationObserver) return;
      var obs = cx.observe(new root.MutationObserver(function () { gateState(); preview(); }));
      obs.observe(box, { childList: true, subtree: true });
      chipObs.push({ box: box, obs: obs });
    });
  }

  function onInput(e) {
    if (e.target.closest && e.target.closest('[data-ob-connect], [data-ob-form]')) refresh();
  }
  function onChange(e) {
    if (e.target.closest && e.target.closest('[data-ob-connect], [data-ob-form]')) refresh();
    if (e.target.id === 'ob-imp-file') {
      var name = $('[data-ob-filename]');
      if (name) {
        if (!name.getAttribute('data-default')) name.setAttribute('data-default', name.textContent);
        var f = e.target.files && e.target.files[0];
        name.textContent = f ? f.name : name.getAttribute('data-default');
      }
    }
  }

  /* ------------------------------------------------------------------- the resume drop zone */
  // Bound once per zone this visit; a zone a step swap removed is let go with its listeners.
  var drops = [];
  function bindDrop() {
    var zone = $('[data-ob-drop]');
    drops = drops.filter(function (d) {
      if (d.zone.isConnected) return true;
      d.offs.forEach(function (off) { off(); });
      return false;
    });
    if (!zone || drops.some(function (d) { return d.zone === zone; })) return;
    var offs = [];
    drops.push({ zone: zone, offs: offs });
    ['dragenter', 'dragover'].forEach(function (n) {
      offs.push(cx.on(zone, n, function (e) { e.preventDefault(); zone.classList.add('over'); }));
    });
    ['dragleave', 'drop'].forEach(function (n) {
      offs.push(cx.on(zone, n, function () { zone.classList.remove('over'); }));
    });
    offs.push(cx.on(zone, 'drop', function (e) {
      e.preventDefault();
      var input = doc.getElementById('ob-imp-file');
      if (input && e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new root.Event('change', { bubbles: true }));
      }
    }));
  }

  /* ------------------------------------------------------------------ the starting overlay */
  /* It fades in (mockup .ob-start, .3s) with a progress bar under the three rows. The bar eases
     toward the end while the request runs and completes when it answers. */
  var startTimer = 0;
  function startOverlay(show) {
    var o = doc.getElementById('ob-start');
    if (!o) return;
    var bar = $('[data-ob-start-bar]', o);
    cx.clear(startTimer);
    if (show) {
      o.hidden = false;
      if (bar) { bar.style.transition = 'none'; bar.style.width = '0'; }
      root.requestAnimationFrame(function () {
        o.classList.add('on');
        if (bar) {
          root.requestAnimationFrame(function () { bar.style.transition = ''; bar.style.width = '88%'; });
        }
      });
    } else {
      o.classList.remove('on');
      startTimer = cx.after(320, function () { o.hidden = true; });
    }
  }
  function onStartRequest(e) {
    if (e.detail.elt && e.detail.elt.hasAttribute && e.detail.elt.hasAttribute('data-ob-start-form')) {
      startOverlay(true);
    }
  }
  function onStartAnswer(e) {
    var xhr = e.detail && e.detail.xhr;
    if (!xhr || !e.detail.elt || !e.detail.elt.hasAttribute('data-ob-start-form')) return;
    if (xhr.getResponseHeader && xhr.getResponseHeader('HX-Redirect')) {
      var bar = $('[data-ob-start-bar]');
      if (bar) bar.style.width = '100%';
      $$('[data-ob-start-row]').forEach(function (r) {
        r.className = 'ob-ok ob-ok--good';
        r.innerHTML = '<svg class="k-ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>';
      });
    }
  }
  function onStartDone(e) {
    if (e.detail.elt && e.detail.elt.hasAttribute && e.detail.elt.hasAttribute('data-ob-start-form')) {
      if (!e.detail.successful) startOverlay(false);
    }
  }

  /* ------------------------------------------------------------------- the step slide */
  /* Next slides the old pane out to the left while the new one comes in from the right; Back
     does the reverse (mockup go(): obOutF/obInF and obOutB/obInB, 48px, both at once). htmx
     swaps the whole frame at once, so just before it does, a still copy of the old pane is
     taken; after it, the copy lies over the new pane (.ob-leaving) and slides out while the new
     pane slides in. Waiting for the old pane to leave first left the content area blank between
     steps (owner, 2026-10-30, macOS and Windows; D-UI-222). The direction comes from the
     request that is being swapped in, never from a remembered value a background poll could
     have changed, and it is set only on the new frame: changing it on the old one restarted the
     old pane's own entrance. */
  var leaving = null;
  function slideFor(path) {
    // A change inside the step (a model card, a connect, a refresh) re-renders it in place:
    // no slide, which read as the whole screen flashing on every click (owner round 13). Back
    // and an Edit link from Searches go back (mockup goto: go(g, -1)).
    if (/\/onboarding\/(back|edit\/)/.test(path)) return 'b';
    if (/\/onboarding\/(models\/|step)/.test(path)) return 's';
    return 'f';
  }
  function stillCopy(pane) {
    // What is on screen, with nothing live: no ids, names or hooks (data-ob-, data-pf-, data-k-,
    // data-testid) a script or test could find, and Alpine told to leave it alone. Other data-
    // attributes stay: styles read them. An id's own rules (a margin) are kept inline.
    var copy = pane.cloneNode(true);
    var from = [pane].concat([].slice.call(pane.querySelectorAll('*')));
    var to = [copy].concat([].slice.call(copy.querySelectorAll('*')));
    for (var i = 0; i < from.length && i < to.length; i++) {
      var o = from[i], c = to[i];
      if (o.tagName === 'INPUT' && (o.type === 'checkbox' || o.type === 'radio')) c.checked = o.checked;
      else if (o.tagName === 'SELECT') c.selectedIndex = o.selectedIndex;
      else if ((o.tagName === 'INPUT' && o.type !== 'file') || o.tagName === 'TEXTAREA') c.value = o.value;
      if (o.id) {
        var cs = root.getComputedStyle(o);
        c.style.margin = cs.margin;
        c.style.padding = cs.padding;
      }
      [].slice.call(c.attributes).forEach(function (at) {
        if (at.name === 'id' || at.name === 'name' || at.name === 'for' || at.name === 'form'
            || /^(hx-|data-(ob-|pf-|k-|testid$))/.test(at.name)) {
          c.removeAttribute(at.name);
        }
      });
    }
    copy.classList.add('ob-leaving');
    copy.setAttribute('aria-hidden', 'true');
    copy.setAttribute('x-ignore', '');
    copy.inert = true;
    return copy;
  }
  function onSlideBeforeSwap(e) {
    leaving = null;
    var d = e.detail || {};
    var wiz = d.target;
    if (!wiz || wiz.id !== 'ob-step' || !d.shouldSwap) return;
    var dir = slideFor((d.requestConfig && d.requestConfig.path) || '');
    var pane = wiz.querySelector('.ob-pane');
    var body = wiz.querySelector('.ob-body');
    leaving = { dir: dir, step: wiz.getAttribute('data-step'), copy: null, top: 0 };
    if (dir === 's' || !pane || !body) return;
    leaving.copy = stillCopy(pane);
    leaving.top = pane.offsetTop - body.scrollTop;
  }
  function onSlideSwap(e) {
    var wiz = doc.getElementById('ob-step');
    if (!wiz || !e.target || (e.target !== wiz && e.target.id !== 'ob-step')) return;
    var out = leaving;
    leaving = null;
    // The same step back again (a refusal, a re-render) is not a step change: no slide.
    var dir = !out || out.step === wiz.getAttribute('data-step') ? 's' : out.dir;
    wiz.setAttribute('data-ob-dir', dir);
    var body = wiz.querySelector('.ob-body');
    if (dir === 's' || !out.copy || !body) return;
    var copy = out.copy;
    // Held to the visible area (bottom: 0, clipped), so it never makes the body scroll.
    copy.style.top = out.top + 'px';
    body.appendChild(copy);
    function drop() { if (copy.parentNode) copy.parentNode.removeChild(copy); }
    // Reduced motion stops the animation: then the copy goes at once.
    if (!copy.getAnimations || !copy.getAnimations().length) { drop(); return; }
    cx.on(copy, 'animationend', function (ev) { if (ev.target === copy) drop(); });
    cx.after(600, drop);
  }

  /* ------------------------------------------------------ chips: an Add button and a pop */
  /* The title and place fields are the Profile chip component. The wizard adds the mockup's Add
     button beside its input (it presses Enter in the input) and pops a chip when it is added. */
  function enhanceChips(scope) {
    $$('.ob-wiz [data-pf-chips]', scope).forEach(function (box) {
      var input = $('[data-pf-chip-input]', box);
      if (!input || input.parentNode.querySelector('[data-ob-chip-add]')) return;
      var b = doc.createElement('button');
      b.type = 'button';
      b.className = 'k-btn k-btn--sm';
      b.setAttribute('data-ob-chip-add', '');
      b.textContent = 'Add';
      input.parentNode.appendChild(b);
    });
  }
  /* Picking Remote as a work style turns on "Include remote roles". Only the change that makes
     Remote chosen syncs; clearing it later leaves the switch as the person set it. */
  function onRemoteMode(e) {
    var m = e.target;
    if (!m || m.name !== 'mode_remote' || !m.checked || !m.closest('.ob-wiz')) return;
    var sw = $('#ob-remote');
    if (sw && !sw.checked) {
      sw.checked = true;
      sw.dispatchEvent(new root.Event('change', { bubbles: true }));
      gateState(); preview();
    }
  }
  function onChipAdd(e) {
    var add = e.target.closest && e.target.closest('[data-ob-chip-add]');
    if (!add) return;
    var input = $('[data-pf-chip-input]', add.parentNode);
    if (!input) return;
    input.dispatchEvent(new root.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    input.focus();
  }
  /* A chip the person just added pops in (mockup .ob-pop). Chips that arrive with a swapped
     step do not: only an Enter in a chip input, the Add button or a suggestion arms it. */
  var armedAt = 0;
  function arm() { armedAt = Date.now(); }
  // Both registered in the capture phase (mount).
  function onArmKey(e) {
    if (e.key === 'Enter' && e.target.closest && e.target.closest('.ob-wiz [data-pf-chip-input]')) arm();
  }
  function onArmClick(e) {
    if (e.target.closest && e.target.closest('.ob-wiz [data-ob-chip-add], .ob-wiz [data-ob-sug]')) arm();
  }
  function watchPop() {
    if (!root.MutationObserver) return;
    cx.observe(new root.MutationObserver(function (list) {
      if (Date.now() - armedAt > 500) return;
      list.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType === 1 && n.matches && n.matches('.pf-chip')) n.classList.add('ob-pop');
        });
      });
    })).observe(doc.body, { childList: true, subtree: true });
  }

  function onSettleChips() { enhanceChips(doc); }
  /* A Connect redraws the whole step and its result sits at the foot of the online card, below
     the fold: bring it into view so a refusal never looks like the click did nothing. */
  var connectSent = false;
  function onConnectRequest(e) {
    // Set only by a Connect, so a poll of the local box in flight never clears it.
    if (e.detail.elt && e.detail.elt.hasAttribute && e.detail.elt.hasAttribute('data-ob-connect')) connectSent = true;
  }
  function onConnectSettle() {
    if (!connectSent) return;
    connectSent = false;
    var msg = $('[data-testid="ob-connect-message"]');
    if (msg) msg.scrollIntoView({ block: 'center' });
  }
  // The step scrolls inside .ob-body, not the page: the round scroll cues go on it, again for
  // each step htmx swaps in (owner round 13: no cue on setup).
  function cueBody() {
    var body = $('.ob-body');
    if (body && !body.__kCue && root.KUI && root.KUI.cue) { body.__kCue = true; root.KUI.cue(body); }
  }
  function boot() { refresh(); enhanceChips(doc); watchPop(); cueBody(); }

  root.KUI.page.define('onboarding', {
    mount: function (main, ctx) {
      root.KProfile.mount(main, ctx);  // the chips, the Save bar and the leave guard, as before
      cx = ctx;
      chipObs = []; drops = []; startTimer = 0; leaving = null; armedAt = 0; connectSent = false;
      ctx.on(doc, 'click', onClick);
      ctx.on(doc, 'change', onFoldSwitch);
      ctx.on(doc, 'input', onInput);
      ctx.on(doc, 'change', onChange);
      ctx.on(doc, 'htmx:beforeRequest', onStartRequest);
      ctx.on(doc, 'htmx:beforeOnLoad', onStartAnswer);
      ctx.on(doc, 'htmx:afterRequest', onStartDone);
      ctx.on(doc, 'htmx:beforeSwap', onSlideBeforeSwap);
      ctx.on(doc, 'htmx:afterSwap', onSlideSwap);
      ctx.on(doc, 'change', onRemoteMode);
      ctx.on(doc, 'click', onChipAdd);
      ctx.on(doc, 'keydown', onArmKey, true);
      ctx.on(doc, 'click', onArmClick, true);
      ctx.on(doc, 'htmx:afterSettle', onSettleChips);
      ctx.on(doc, 'htmx:afterSettle', refresh);
      ctx.on(doc, 'htmx:beforeRequest', onConnectRequest);
      ctx.on(doc, 'htmx:afterSettle', onConnectSettle);
      ctx.on(doc, 'htmx:load', cueBody);
      boot();
    }
  });
})(window);
