/* Help (D-UI-124): the small amount of behavior the page needs. Search filters the article
   list as you type (title and body, all offline), topic chips narrow it to one topic, "/" focuses
   the search, and any [data-help-article="topic:article"] control opens that article in the reader
   flyout. Everything is delegated on the document. Articles are rendered by the server in
   #hp-articles; nothing is fetched. The page runs on the lifecycle (KUI.page, D-UI-210): this
   file only defines it, and mount starts every listener through its context, so leaving stops
   all of them and coming back starts one set. */
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !root.KUI || !root.KUI.page) return;

  function $(sel, scope) { return (scope || doc).querySelector(sel); }
  function $$(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function norm(s) { return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  // Per visit: mount starts each of these afresh.
  var query = '';
  var topic = 'all';
  var bodies = null;  // article key -> lowercase title and text, built once per visit

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
    if (!a || !reader) return;
    $('#hp-reader-t').textContent = a.getAttribute('data-title');
    var eyebrow = $('.k-flyout__head small', reader);
    if (eyebrow) eyebrow.textContent = a.getAttribute('data-topic-title');
    var body = $('#hp-reader-body');
    body.innerHTML = a.innerHTML;
    // The copy must not repeat the article's ids on the page; its anchors become data attributes.
    Array.prototype.forEach.call(body.querySelectorAll('[id]'), function (n) {
      n.setAttribute('data-hp-anchor', n.id);
      n.removeAttribute('id');
    });
    // Same for test hooks: the reader's copy must not duplicate the article's data-testid.
    Array.prototype.forEach.call(body.querySelectorAll('[data-testid]'), function (n) {
      n.setAttribute('data-hp-testid', n.getAttribute('data-testid'));
      n.removeAttribute('data-testid');
    });
    var scroller = $('.k-flyout__body', reader);
    if (scroller) scroller.scrollTop = 0;
    // A button inside a flyout is gone once the flyout closes; focus goes back to the link that
    // opened the flyout instead.
    var fly = trigger && trigger.closest ? trigger.closest('.k-flyout') : null;
    if (fly) trigger = $('[data-k-flyout="#' + fly.id + '"]') || null;
    root.KUI.closeFlyout();
    root.KUI.openFlyout(reader, { trigger: trigger || null });
    return reader;
  }

  function onClick(e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var chip = t.closest('#hp-chips .k-chip');
    if (chip) { setTopic(chip.getAttribute('data-topic')); return; }
    var open = t.closest('[data-help-article]');
    if (open) { openArticle(open.getAttribute('data-help-article'), open); }
  }

  function onInput(e) {
    if (e.target && e.target.id === 'hp-q') { query = e.target.value; filter(); }
  }

  function onKeyDown(e) {
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
  }

  // A link to /help#fix:problem opens that article; one of the reference anchors (#lan, #sources,
  // #remoterocketship, #tier-names) opens its article in the reader. Runs at mount and when the
  // hash changes on the page itself.
  // The reference blocks the mockup does not draw are articles now; their anchors open them.
  var ANCHORS = {
    sources: 'start:sources', remoterocketship: 'start:sources',
    lan: 'privacy:lan', 'tier-names': 'models:names'
  };
  function openHash() {
    var hash = root.location && root.location.hash.slice(1);
    if (!hash) return;
    if (/^[a-z]+:[a-z]+$/.test(hash)) { openArticle(hash, null); return; }
    if (ANCHORS[hash]) {
      var reader = openArticle(ANCHORS[hash], null);
      var spot = reader && $('#hp-reader-body [data-hp-anchor="' + hash + '"]');
      if (spot && spot.scrollIntoView) spot.scrollIntoView();
      return;
    }
    var target = doc.getElementById(hash);
    if (!target) return;
    for (var n = target; n; n = n.parentElement) if (n.tagName === 'DETAILS') n.open = true;
    target.scrollIntoView();
  }

  root.KUI.page.define('help', {
    mount: function (main, ctx) {
      // A page mounted over markup that already holds a search or a chip starts from what it shows.
      var box = $('#hp-q'), chip = $('#hp-chips .k-chip.is-on');
      query = box ? box.value : '';
      topic = chip ? chip.getAttribute('data-topic') : 'all';
      bodies = null;
      ctx.on(doc, 'click', onClick);
      ctx.on(doc, 'input', onInput);
      ctx.on(doc, 'keydown', onKeyDown);
      ctx.on(root, 'hashchange', openHash);
      filter();
      openHash();
      // An article left open in the reader goes with the page; its scrim and inert siblings too.
      ctx.onUnmount(function () {
        var reader = $('#hp-reader');
        if (reader && reader.classList.contains('is-open')) root.KUI.closeFlyout();
        bodies = null;
      });
    }
  });
})(typeof window !== 'undefined' ? window : globalThis);
