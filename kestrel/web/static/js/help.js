/* Help (D-UI-124): the small amount of behavior the page needs. Search filters the article
   list as you type (title and body, all offline), topic chips narrow it to one topic, "/" focuses
   the search, and any [data-help-article="topic:article"] control opens that article in the reader
   slide-over. Everything is delegated on the document. Articles are rendered by the server in
   #hp-articles; nothing is fetched. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc) return;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function norm(s) { return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  var query = '';
  var topic = 'all';
  var bodies = null;  // article key -> lowercase title and text, built once

  function bodyIndex() {
    if (bodies) return bodies;
    bodies = {};
    $$('#hp-articles > article').forEach(function (a) {
      bodies[a.id.replace(/^hp-a-/, '').replace('-', ':')] = norm(a.getAttribute('data-title') + ' ' + a.textContent);
    });
    return bodies;
  }

  function mark(text, q) {
    if (!q) return escapeHtml(text);
    var at = text.toLowerCase().indexOf(q);
    if (at < 0) return escapeHtml(text);
    return escapeHtml(text.slice(0, at)) + '<mark>' + escapeHtml(text.slice(at, at + q.length)) +
      '</mark>' + escapeHtml(text.slice(at + q.length));
  }

  function filter() {
    var idx = bodyIndex();
    var q = norm(query);
    var shown = 0;
    $$('.hp-topic').forEach(function (card) {
      var key = card.getAttribute('data-topic');
      var any = 0;
      $$('.hp-art', card).forEach(function (btn) {
        var hit = !q || (idx[btn.getAttribute('data-help-article')] || '').indexOf(q) >= 0;
        btn.parentNode.hidden = !hit;
        if (hit) any += 1;
        var label = $('.hp-art__t', btn);
        var title = btn.getAttribute('data-title');
        if (!title) { title = label.textContent; btn.setAttribute('data-title', title); }
        label.innerHTML = mark(title, hit ? q : '');
      });
      var hide = (topic !== 'all' && topic !== key) || any === 0;
      card.hidden = hide;
      if (!hide) shown += any;
    });
    var none = $('#hp-none');
    if (none) none.hidden = shown > 0;
    var steps = $('#hp-steps-card');
    if (steps) steps.hidden = !((topic === 'all' || topic === 'score') && !q);
    $$('.hp-lan').forEach(function (d) { d.hidden = !(topic === 'all' && !q); });
  }

  function setTopic(key) {
    topic = key;
    $$('#hp-chips .k-chip').forEach(function (c) {
      var on = c.getAttribute('data-topic') === key;
      c.classList.toggle('is-on', on);
      c.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    filter();
  }

  function openArticle(key, trigger) {
    var a = key && $('#hp-a-' + key.replace(':', '-'));
    var reader = $('#hp-reader');
    if (!a || !reader || !root.KUI) return;
    $('#hp-reader-t').textContent = a.getAttribute('data-title');
    var eyebrow = $('.k-slideover__head small', reader);
    if (eyebrow) eyebrow.textContent = a.getAttribute('data-topic-title');
    var body = $('#hp-reader-body');
    body.innerHTML = a.innerHTML;
    var scroller = $('.k-slideover__body', reader);
    if (scroller) scroller.scrollTop = 0;
    // A button inside a flyout is gone once the flyout closes; focus goes back to the link that
    // opened the flyout instead.
    var fly = trigger && trigger.closest ? trigger.closest('.k-flyout') : null;
    if (fly) trigger = $('[data-k-flyout="#' + fly.id + '"]') || null;
    root.KUI.closeFlyout();
    root.KUI.openSlideOver(reader, { trigger: trigger || null });
  }

  doc.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var chip = t.closest('#hp-chips .k-chip');
    if (chip) { setTopic(chip.getAttribute('data-topic')); return; }
    var open = t.closest('[data-help-article]');
    if (open) { openArticle(open.getAttribute('data-help-article'), open); }
  });

  doc.addEventListener('input', function (e) {
    if (e.target && e.target.id === 'hp-q') { query = e.target.value; filter(); }
  });

  doc.addEventListener('keydown', function (e) {
    var input = $('#hp-q');
    if (!input) return;
    var tag = (e.target && e.target.tagName) || '';
    var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
      (e.target && e.target.isContentEditable);
    var reader = $('#hp-reader');
    if (reader && reader.classList.contains('is-open')) return;  // the page behind is inert
    if (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      input.focus();
    } else if (e.key === 'Escape' && e.target === input && input.value) {
      input.value = '';
      query = '';
      filter();
    }
  });

  // A link to /help#fix:problem opens that article; one into a Show details block (#lan,
  // #sources, #remoterocketship, #tier-names) opens it and scrolls to it, even when the target
  // sits inside another block. Runs on load and when the hash changes on the page itself.
  function openHash() {
    var hash = root.location && root.location.hash.slice(1);
    if (!hash) return;
    if (/^[a-z]+:[a-z]+$/.test(hash)) { openArticle(hash, null); return; }
    var target = doc.getElementById(hash);
    if (!target) return;
    for (var n = target; n; n = n.parentElement) if (n.tagName === 'DETAILS') n.open = true;
    target.scrollIntoView();
  }

  function init() {
    filter();
    openHash();
  }
  root.addEventListener('hashchange', openHash);
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init); else init();
})(typeof window !== 'undefined' ? window : globalThis);
