/* Profile (D-UI-21, D-UI-119): the Save bar and its tab-switch nudge, Undo, title chips, the
   document Look, and the document preview as one true-size page that is scaled and never
   re-flowed (D-UI-126). Plain script,
   delegated, so nothing needs re-binding after an htmx swap. */
(function (root) {
  'use strict';
  var doc = root.document;
  var PAGE_W = 816;      // a Letter page at 96 dpi: the width the document is laid out at
  var PAGE_H = 1056;

  /* ------------------------------------------------------------ the near-floor hint
     The hint under the margin control is rendered for the saved stop; it follows the control. */
  doc.addEventListener('k-seg-change', function (e) {
    var box = e.target && e.target.closest ? e.target.closest('.pf-margin') : null;
    if (!box) return;
    var hint = box.querySelector('[data-pf-margin-hint]');
    var text = box.querySelector('[data-pf-margin-text]');
    var hidden = box.querySelector('input[type="hidden"]');
    if (!hint || !text || !hidden) return;
    var what = hint.getAttribute('data-what') || 'pay floor';
    text.textContent = hidden.value === '0'
      ? 'Exact: a role whose posted pay tops out below your ' + what + ' is set aside.'
      : 'Roles within ' + hidden.value + '% below your ' + what + ' stay in, marked "Just under your pay floor".';
  });

  /* ------------------------------------------------------------------ the Save bar */
  function dirtyForms() { return doc.querySelectorAll('form[data-pf-form][data-dirty]'); }

  function markDirty(form) {
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
  doc.addEventListener('input', function (e) {
    var form = formOf(e.target);
    if (form) markDirty(form);
  });
  doc.addEventListener('change', function (e) {
    var form = formOf(e.target);
    if (form) markDirty(form);
  });

  function nudge(form) {
    var bar = form.querySelector('[data-pf-savebar]');
    var msg = form.querySelector('[data-pf-msg]');
    if (msg) msg.textContent = 'Save or discard your changes before you switch tabs.';
    if (!bar) return;
    bar.classList.remove('is-nudge');
    void bar.offsetWidth;   // restart the animation
    bar.classList.add('is-nudge');
    root.setTimeout(function () { bar.classList.remove('is-nudge'); }, 700);
  }

  doc.addEventListener('pf-save-refused', function (e) {
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
  });

  doc.addEventListener('pf-title-accepted', function (e) {
    var box = doc.querySelector('[data-pf-chips="target_titles"]');
    if (!box || !e.detail.title) return;
    var form = box.closest('form[data-pf-form]');
    var dirty = form && form.hasAttribute('data-dirty');
    addChip(box, e.detail.title);
    // Accept already persisted this title. Other draft edits still need Save.
    if (form && !dirty) form.removeAttribute('data-dirty');
  });

  /* ------------------------------------------------------------- the document preview */
  function fit(page) {
    if (!page) return;
    var scale = page.querySelector('[data-pf-scale]');
    var frame = page.querySelector('iframe');
    if (!scale || !frame) return;
    var width = page.clientWidth || PAGE_W;
    var k = Math.min(1, width / PAGE_W);
    var h = PAGE_H;
    try {
      var d = frame.contentDocument;
      if (d && d.documentElement) h = Math.max(PAGE_H, d.documentElement.scrollHeight);
    } catch (err) { /* a frame that is not readable keeps one page */ }
    frame.style.height = h + 'px';
    var off = Math.max(0, (width - PAGE_W * k) / 2);   // centered when there is room
    scale.style.transform = 'translateX(' + off + 'px) scale(' + k + ')';
    page.style.height = Math.ceil(h * k) + 'px';
    page.setAttribute('data-pf-scale-k', k.toFixed(4));
  }

  function fitAll(scope) {
    Array.prototype.forEach.call((scope || doc).querySelectorAll('[data-pf-page]'), function (p) {
      var frame = p.querySelector('iframe');
      if (frame && !frame.__pfBound) {
        frame.__pfBound = true;
        frame.addEventListener('load', function () { fit(p); });
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
    root.setTimeout(function () { fitAll(body); }, 400);
    if (root.ResizeObserver && !body.__pfWatched) {
      body.__pfWatched = true;
      new root.ResizeObserver(function () { fitAll(body); }).observe(body);
    }
  }

  /* ------------------------------------------------------------------------ title chips
     The chips are the display; a hidden textarea carries one phrase per line, which is what the
     save route reads. Enter or a comma adds what was typed; the x removes a chip. */
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
  doc.addEventListener('click', function (e) {
    var remove = e.target.closest && e.target.closest('form[data-pf-form] [data-pf-chip-remove]');
    if (!remove) return;
    var box = remove.closest('[data-pf-chips]');
    var chip = remove.closest('[data-pf-chip]');
    if (box && chip) { chip.remove(); syncChips(box); }
  });
  doc.addEventListener('keydown', function (e) {
    var input = e.target.closest && e.target.closest('[data-pf-chip-input]');
    if (!input) return;
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();   // Enter must not submit the profile form
      var box = input.closest('[data-pf-chips]');
      if (box && addChip(box, input.value)) input.value = '';
    } else if (e.key === 'Backspace' && !input.value) {
      var box2 = input.closest('[data-pf-chips]');
      var last = box2 && box2.querySelectorAll('[data-pf-chip]');
      if (last && last.length) { last[last.length - 1].remove(); syncChips(box2); }
    }
  });
  doc.addEventListener('focusout', function (e) {
    var input = e.target.closest && e.target.closest('[data-pf-chip-input]');
    if (!input || !input.value.trim()) return;
    var box = input.closest('[data-pf-chips]');
    if (box && addChip(box, input.value)) input.value = '';
  });

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
  doc.addEventListener('click', function (e) {
    var sw = e.target.closest && e.target.closest('[data-pf-swatches] button.pf-swatch');
    if (sw) pickAccent(sw.closest('[data-pf-swatches]'), sw.getAttribute('data-accent') || '', sw);
  });
  doc.addEventListener('change', function (e) {
    var t = e.target;
    if (t && t.id === 'accent_custom') {
      var box = t.closest('[data-pf-swatches]');
      if (box) {
        e.stopPropagation();   // the accent change below saves and redraws once
        pickAccent(box, 'custom', t.closest('.pf-swatch'));
      }
    }
  }, true);

  /* --------------------------------------------------------------------------------- Undo
     A save raises "pf-saved" with the stamp of the file it wrote. Undo sends that stamp back,
     so a toast from an earlier save cannot restore the wrong version, and any failure says so in
     plain words instead of silently reloading. Undo goes back one save. */
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
  doc.addEventListener('pf-saved', function (e) {
    var d = e.detail || {};
    if (!root.KUI || !root.KUI.toast) return;
    root.KUI.toast(d.message || 'Saved', { undo: function () { undo(d); } });
  });

  /* ------------------------------------------------------------------------------ stories */
  doc.addEventListener('htmx:afterRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (!el || el.id !== 'pf-story-form' || !e.detail.successful) return;
    el.reset();
    if (root.KUI && root.KUI.closeModal) root.KUI.closeModal();
  });

  /* ----------------------------------------------------------------------- the clicks */
  doc.addEventListener('click', function (e) {
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
  }, true);

  doc.addEventListener('htmx:afterSwap', function (e) {
    var el = e.detail && e.detail.target;
    if (el && (el.id === 'preview' || (el.querySelector && el.querySelector('[data-pf-page]')))) {
      fitAll(el);
    }
  });
  // An edit to the document (a variant, a role, a bullet, the details) refreshes the preview.
  var refreshTimer = null;
  doc.addEventListener('htmx:afterRequest', function (e) {
    var cfg = e.detail && e.detail.requestConfig;
    var path = (cfg && cfg.path) || '';
    if (!e.detail.successful || !cfg || cfg.verb !== 'post' || path.indexOf('/documents/') !== 0) return;
    if (/\/(preview|accent|look|assess)/.test(path)) return;
    var pv = doc.getElementById('preview');
    if (!pv || !root.htmx) return;
    root.clearTimeout(refreshTimer);
    refreshTimer = root.setTimeout(function () { root.htmx.trigger(pv, 'pf-refresh'); }, 350);
  });
  if (root.ResizeObserver) {
    var ro = new root.ResizeObserver(function () { fitAll(doc); });
    var watch = function () {
      Array.prototype.forEach.call(doc.querySelectorAll('.pf-preview'), function (n) {
        if (!n.__pfWatched) { n.__pfWatched = true; ro.observe(n); }
      });
    };
    doc.addEventListener('htmx:load', watch);
    doc.addEventListener('DOMContentLoaded', watch);
  }
  root.addEventListener('resize', function () { fitAll(doc); });
})(window);
