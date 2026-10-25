/* Tailor resume (D-UI-120): the behavior HTMX does not give the page.
   The preview as one true-size Letter page, scaled and never re-flowed, patched in place when a
   line changes (no frame reload, no flash); hover links between a printed line, its card, what
   the role asks for and the posting, done by toggling attributes in place; the page-count guard;
   the confirm for a line that still needs a pick; and the toasts with Undo. Everything is
   delegated on the document, so it keeps working after a part is swapped. User-facing strings
   here are the guard's two sentences, the confirm's wording and the Undo answers. */
(function (root) {
  'use strict';
  function reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function smooth() { return reduced() ? 'auto' : 'smooth'; }
  var doc = root.document;
  if (!doc) return;

  var PAGE_W = 816;   // a Letter page at 96 dpi: the width the document is laid out at
  var PAGE_H = 1056;
  var zoom = 0;       // steps away from "Fit"
  var tab = 'prev';
  var openCards = {}; // cards the operator opened, kept across a swap
  var hlId = null;
  var gate = null;    // what the confirm is for: { kind: 'download' | 'use', href, n }

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function q(v) { return String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"'); }
  function frameDoc(frame) {
    try { return frame && frame.contentDocument && frame.contentDocument.body ? frame.contentDocument : null; }
    catch (err) { return null; }
  }
  function toast(msg, opts) { if (root.KUI && root.KUI.toast) return root.KUI.toast(msg, opts); return null; }
  function pageEl() { return $('.tl-page'); }
  /* A server message is shown only when the server sent it as text/plain, and only ever through
     textContent in the toast; HTML responses never reach a toast. */
  function plain(text, type) {
    if (String(type || '').toLowerCase().indexOf('text/plain') !== 0) return '';
    return String(text || '').trim();
  }

  /* ------------------------------------------------------------------ the scaled preview */
  function fit(page) {
    if (!page) return;
    var scale = $('[data-tl-scale]', page);
    var frame = $('iframe', page);
    if (!scale || !frame) return;
    var width = page.clientWidth || PAGE_W;
    var k = Math.min(1, width / PAGE_W);
    var inModal = !!page.closest('.k-modal');
    if (!inModal && zoom) k = Math.max(0.5, Math.min(1.3, k + zoom * 0.1));
    var h = PAGE_H;
    var d = frameDoc(frame);
    // Measure from one page tall, so a shorter document can shrink the frame again.
    if (d && d.documentElement) {
      frame.style.height = PAGE_H + 'px';
      h = Math.max(PAGE_H, d.documentElement.scrollHeight);
    }
    frame.style.height = h + 'px';
    var off = Math.max(0, (width - PAGE_W * k) / 2);
    scale.style.transform = 'translateX(' + off + 'px) scale(' + k + ')';
    page.style.height = Math.ceil(h * k) + 'px';
    page.style.overflowX = k * PAGE_W > width + 1 ? 'auto' : 'hidden';
    if (!inModal) {
      var zl = $('#tl-zl');
      if (zl) zl.textContent = zoom ? Math.round(k * 100) + '%' : 'Fit';
    }
    page.setAttribute('data-tl-k', k.toFixed(4));
  }
  function fitAll() { $$('[data-tl-page]').forEach(fit); }

  /* A page count from the preview: the frame's height against the printable height of a page.
     The saved count is the PDF's own, so it is only replaced once the picks have changed since
     (data-stale) or none was saved. */
  function estimatePages(d) {
    var narrow = d.body.getAttribute('data-margins') === 'narrow';
    var pad = (narrow ? 0.35 : 0.5) * 96 * 2;
    var h = d.documentElement.scrollHeight;
    return Math.max(1, Math.ceil((h - pad - 1) / (PAGE_H - pad)));
  }
  function updateGuard() {
    var g = $('#tl-guard');
    var d = frameDoc($('#tl-frame'));
    if (!g || !d) return;
    if (g.getAttribute('data-stale') !== '1' && g.getAttribute('data-saved') !== '') return;
    var n = estimatePages(d);
    var limit = parseInt(g.getAttribute('data-limit'), 10) || 1;
    var over = n > limit;
    g.classList.toggle('is-over', over);
    g.classList.toggle('is-ok', !over);
    var ok = $('.tl-pgw__ok', g), bad = $('.tl-pgw__over', g);
    if (ok) ok.hidden = over;
    if (bad) bad.hidden = !over;
    var okText = $('[data-tl-pgok]', g), badText = $('[data-tl-pgover]', g);
    if (okText) okText.textContent = 'Fits on ' + n + (n === 1 ? ' page.' : ' pages.');
    if (badText) badText.textContent = 'Runs to ' + n + ' pages. Your limit is ' + limit + '.';
  }

  /* --------------------------------------------------------------- hover links, in place */
  function mark(scopeDoc, sel, attr, on) {
    if (!scopeDoc) return;
    Array.prototype.forEach.call(scopeDoc.querySelectorAll(sel), function (el) {
      if (on) el.setAttribute(attr, ''); else el.removeAttribute(attr);
    });
  }
  function applyHl(id, on) {
    if (!id) return;
    var s = q(id);
    mark(frameDoc($('#tl-frame')), '[data-slot="' + s + '"]', 'data-hl', on);
    mark(doc, '.tl-slot[data-slot="' + s + '"]', 'data-hot', on);
    mark(doc, '.tl-req[data-req="' + s + '"], .tl-pq[data-req="' + s + '"]', 'data-hl', on);
  }
  function setHl(id) {
    id = id || null;
    if (id === hlId) return;
    if (hlId) applyHl(hlId, false);
    hlId = id;
    if (hlId) applyHl(hlId, true);
  }
  doc.addEventListener('mouseover', function (e) {
    var t = e.target.closest ? e.target.closest('.tl-slot[data-slot], .tl-req[data-req], .tl-pq[data-req]') : null;
    setHl(t ? (t.getAttribute('data-slot') || t.getAttribute('data-req')) : null);
  });
  doc.addEventListener('mouseleave', function () { setHl(null); });

  /* A line in the preview links back to its card and to what it answers. */
  function bindFrame(frame) {
    var d = frameDoc(frame);
    if (!d || d.__tlBound) return;
    d.__tlBound = true;
    d.addEventListener('mouseover', function (e) {
      var t = e.target.closest ? e.target.closest('[data-slot]') : null;
      setHl(t ? t.getAttribute('data-slot') : null);
    });
    d.addEventListener('mouseleave', function () { setHl(null); });
  }

  /* ------------------------------------------------- the frame patched without a reload */
  function textsBySlot(d) {
    var out = {};
    Array.prototype.forEach.call(d.querySelectorAll('[data-slot]'), function (el) {
      out[el.getAttribute('data-slot')] = (out[el.getAttribute('data-slot')] || '') + el.textContent;
    });
    return out;
  }
  function applySrc() {
    var src = $('#tl-src');
    var frame = $('#tl-frame');
    if (!src || !frame || !src.value) return;
    var d = frameDoc(frame);
    if (!d) { frame.srcdoc = src.value; return; }
    var next = new root.DOMParser().parseFromString(src.value, 'text/html');
    var olds = d.head.querySelectorAll('style');
    var news = next.head.querySelectorAll('style');
    if (olds.length !== news.length) { frame.srcdoc = src.value; return; }
    Array.prototype.forEach.call(news, function (s, i) {
      if (olds[i].textContent !== s.textContent) olds[i].textContent = s.textContent;
    });
    var before = textsBySlot(d);
    Array.prototype.slice.call(d.body.attributes).forEach(function (a) { d.body.removeAttribute(a.name); });
    Array.prototype.forEach.call(next.body.attributes, function (a) { d.body.setAttribute(a.name, a.value); });
    d.body.innerHTML = next.body.innerHTML;
    var flash = src.getAttribute('data-flash') || '';
    var after = textsBySlot(d);
    Array.prototype.forEach.call(d.querySelectorAll('[data-slot]'), function (el) {
      var key = el.getAttribute('data-slot');
      if (before[key] !== after[key] || key === flash) el.classList.add('tl-flash');
    });
    if (hlId) applyHl(hlId, true);
    fit($('[data-tl-page]'));
    updateGuard();
  }

  /* ------------------------------------------------------------ tabs, cards, small toggles */
  function applyTab() {
    $$('[data-tl-tab]').forEach(function (b) {
      var on = b.getAttribute('data-tl-tab') === tab;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    var pane = $('[data-tl-previewpane]'), chg = $('#tl-chg');
    if (pane) pane.hidden = tab !== 'prev';
    if (chg) chg.hidden = tab !== 'chg';
    var z = $$('[data-tl-zoom], [data-tl-enlarge], #tl-zl');
    z.forEach(function (el) { el.hidden = tab !== 'prev'; });
  }
  function restoreCards() {
    Object.keys(openCards).forEach(function (k) {
      var c = $('.tl-slot[data-slot="' + q(k) + '"]');
      if (c) {
        c.classList.add('is-open');
        var b = $('[data-tl-more]', c);
        if (b) b.setAttribute('aria-expanded', 'true');
      }
    });
  }
  function showMe() {
    var el = $('.tl-slot.is-err');
    if (!el) return;
    el.classList.add('is-open');
    el.scrollIntoView({ behavior: smooth(), block: 'center' });
    if (el.animate && !reduced()) el.animate([{ transform: 'translateX(-6px)' }, { transform: 'translateX(6px)' }, { transform: 'none' }], { duration: 350, delay: 300 });
    var f = $('input[type="radio"]', el);
    if (f) root.setTimeout(function () { f.focus({ preventScroll: true }); }, 450);
  }

  /* ----------------------------------------------------------- the confirm for open picks */
  function gateText(g) {
    var n = g.n;
    var title = n + (n === 1 ? ' line still needs your pick.' : ' lines still need your pick.');
    var verb = g.kind === 'download' ? 'Download anyway?' : 'Use it anyway?';
    return {
      title: title,
      body: 'Kestrel used your first wording there. ' + verb,
      anyway: g.kind === 'download' ? 'Download anyway' : 'Use anyway'
    };
  }
  function openGate(btn, kind) {
    var n = parseInt(btn.getAttribute('data-needs'), 10) || 0;
    gate = { kind: kind, href: btn.getAttribute('href') || '', n: n };
    var t = gateText(gate);
    var m = $('#tl-gate');
    if (!m) return;
    $('[data-tl-gate-title]', m).textContent = t.title;
    $('[data-tl-gate-body]', m).textContent = t.body;
    $('[data-tl-anyway]', m).textContent = t.anyway;
    if (root.KUI) root.KUI.openModal(m);
    var c = $('[data-tl-choose]', m);
    if (c) root.setTimeout(function () { c.focus(); }, 60);
  }
  /* The PDF is fetched, so an error (no browser to make it, a missing file) is a message on this
     page and never replaces the app with an error line or saves it as a resume. */
  function saveFrom(url, name) {
    if (!root.fetch) return;  // every supported webview has fetch
    root.fetch(url, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          toast(plain(t, r.headers.get('Content-Type')).slice(0, 200) || 'Could not make the PDF.');
        });
      }
      return r.blob().then(function (blob) {
        var a = doc.createElement('a');
        a.href = root.URL.createObjectURL(blob);
        a.download = name || 'resume.pdf';
        doc.body.appendChild(a);
        a.click();
        root.setTimeout(function () { root.URL.revokeObjectURL(a.href); a.parentNode.removeChild(a); }, 1000);
      });
    }).catch(function () { toast('Could not make the PDF.'); });
  }
  doc.addEventListener('click', function (e) {
    var dl = e.target.closest ? e.target.closest('[data-tl-gate="download"]') : null;
    if (dl && !(parseInt(dl.getAttribute('data-needs'), 10) > 0)) {
      e.preventDefault();
      saveFrom(dl.getAttribute('href'), dl.getAttribute('data-name'));
      return;
    }
    var b = e.target.closest ? e.target.closest('[data-tl-gate]') : null;
    if (!b || !(parseInt(b.getAttribute('data-needs'), 10) > 0)) return;
    e.preventDefault();
    e.stopPropagation();
    openGate(b, b.getAttribute('data-tl-gate'));
  }, true);

  function currentRender() { var p = pageEl(); return p ? p.getAttribute('data-render') : ''; }
  function goAnyway() {
    if (!gate) return;
    var id = currentRender();
    if (root.KUI) root.KUI.closeModal();
    if (gate.kind === 'download') {
      var nm = $('#tl-dl');
      saveFrom('/renders/' + id + '/download?anyway=1', nm ? nm.getAttribute('data-name') : '');
    } else if (root.htmx) {
      root.htmx.ajax('POST', '/renders/' + id + '/attach', { target: '#tl-head', swap: 'none', values: { anyway: '1' } });
    }
    gate = null;
  }
  function chooseNow() {
    gate = null;
    root.setTimeout(showMe, 200);
  }

  /* ----------------------------------------------------------------------- enlarge */
  function enlarge() {
    var frame = $('#tl-frame');
    var d = frameDoc(frame);
    var body = $('[data-tl-zoom-body]');
    if (!body || !d) return;
    mark(d, '[data-hl]', 'data-hl', false);
    var html = '<!doctype html>' + d.documentElement.outerHTML;
    body.innerHTML = '';
    var page = doc.createElement('div');
    page.className = 'tl-pwrap';
    page.setAttribute('data-tl-page', '');
    var scale = doc.createElement('div');
    scale.className = 'tl-pscale';
    scale.setAttribute('data-tl-scale', '');
    var f = doc.createElement('iframe');
    f.className = 'tl-frame';
    f.title = 'Preview, full size';
    f.addEventListener('load', function () { fit(page); });
    f.srcdoc = html;
    scale.appendChild(f);
    page.appendChild(scale);
    body.appendChild(page);
    if (root.KUI) root.KUI.openModal('#tl-zoom');
    root.requestAnimationFrame(function () { fit(page); });
    root.setTimeout(function () { fit(page); }, 400);
  }

  /* ------------------------------------------------------------------- click delegation */
  doc.addEventListener('click', function (e) {
    var t = e.target;
    if (!t.closest) return;
    var n;
    if ((n = t.closest('[data-tl-more]'))) {
      var card = n.closest('.tl-slot');
      var on = card.classList.toggle('is-open');
      n.setAttribute('aria-expanded', on ? 'true' : 'false');
      var key = card.getAttribute('data-slot');
      if (on) openCards[key] = true; else delete openCards[key];
      return;
    }
    if ((n = t.closest('[data-tl-tab]'))) { tab = n.getAttribute('data-tl-tab'); applyTab(); fitAll(); return; }
    if ((n = t.closest('[data-tl-zoom]'))) {
      zoom = Math.max(-5, Math.min(8, zoom + parseInt(n.getAttribute('data-tl-zoom'), 10)));
      fitAll();
      return;
    }
    if ((n = t.closest('[data-tl-enlarge]'))) { enlarge(); return; }
    if ((n = t.closest('.tl-pwrap')) && !n.closest('.k-modal') && !t.closest('a, button')) { enlarge(); return; }
    if (t.closest('[data-tl-show]')) { showMe(); return; }
    if ((n = t.closest('[data-tl-details]'))) {
      var m = $('#tl-model');
      if (m) {
        m.hidden = !m.hidden;
        n.textContent = m.hidden ? 'Show details' : 'Hide details';
        n.setAttribute('aria-expanded', m.hidden ? 'false' : 'true');
      }
      return;
    }
    if ((n = t.closest('[data-tl-look]'))) {
      var form = $('#tl-look-form');
      var input = form && form.querySelector('input[name="' + n.getAttribute('data-tl-look') + '"]');
      if (!input || input.value === n.value) return;
      input.value = n.value;
      if (root.htmx) root.htmx.trigger(form, 'tl-look');
      return;
    }
    if (t.closest('[data-tl-anyway]')) { goAnyway(); return; }
    if (t.closest('[data-tl-choose]')) { chooseNow(); return; }
  });

  /* ------------------------------------------------------------------ toasts and Undo */
  function refresh() {
    if (!root.htmx || !$('#tl-body')) return;
    root.htmx.ajax('GET', root.location.pathname + root.location.search, { target: '#tl-body', swap: 'innerHTML' });
  }
  doc.addEventListener('tl-toast', function (e) {
    var d = e.detail || {};
    var msg = d.message || d.value || '';
    var url = d.undoUrl;
    var opts = {};
    if (url) {
      opts.undo = function () {
        if (!root.fetch) return;
        root.fetch(url, { method: 'POST', credentials: 'same-origin' })
          .then(function (r) { return r.text(); })
          .then(function (txt) {
            if (/data-ok="0"/.test(txt)) toast('This can no longer be undone.');
            refresh();
          })
          .catch(function () { toast('This can no longer be undone.'); });
      };
    }
    toast(msg, opts);
  });

  /* ------------------------------------------------------------------ htmx hooks */
  doc.addEventListener('htmx:oobAfterSwap', function (e) {
    var t = e.detail && e.detail.target;
    if (t && t.id === 'tl-src') applySrc();
  });
  doc.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (t && t.id === 'tl-dialog') {
      var dlg = $('.k-modal', t);
      if (dlg && root.KUI) root.KUI.openModal(dlg);
    }
  });
  doc.addEventListener('htmx:afterRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (el && el.hasAttribute && el.hasAttribute('data-tl-pickform') && root.KUI) root.KUI.closeModal();
  });
  doc.addEventListener('htmx:afterSettle', function () {
    applyTab();
    restoreCards();
    var frame = $('#tl-frame');
    if (frame) bindFrame(frame);
    fitAll();
    updateGuard();
  });
  doc.addEventListener('htmx:responseError', function (e) {
    var xhr = e.detail && e.detail.xhr;
    var path = String((e.detail && e.detail.pathInfo && e.detail.pathInfo.requestPath) || '');
    if (path.indexOf('/renders/') !== 0 || !xhr) return;
    var text = plain(xhr.responseText, xhr.getResponseHeader('Content-Type'));
    if (text && text.length < 240) toast(text);
  });

  function onFrameLoad(e) {
    var f = e.target;
    if (!f || f.id !== 'tl-frame') return;
    bindFrame(f);
    fitAll();
    updateGuard();
  }
  doc.addEventListener('load', onFrameLoad, true);

  function init() {
    applyTab();
    var frame = $('#tl-frame');
    if (frame) {
      bindFrame(frame);
      if (frameDoc(frame) && frameDoc(frame).readyState === 'complete') { fitAll(); updateGuard(); }
    }
    fitAll();
    root.addEventListener('resize', fitAll);
    if (root.ResizeObserver) {
      var wrap = $('.tl-wrap');
      if (wrap) new root.ResizeObserver(fitAll).observe(wrap);
    }
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init); else init();
})(window);
