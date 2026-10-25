/* Scene band renderer. Draws the chosen scene, plus an optional seasonal touch stacked over it,
   into every <div class="k-scene"> on the page. The server supplies the choice as data
   attributes:
     data-scene="mountains"   a SCENES id, or "none" for sky only
     data-touch="lantern"     a SEASONS id, or empty for no touch
     data-animate="1|0"       0 keeps the art still (the Still mode setting)
   Reduced motion is honored without help from here: every scene writes its animation inside
   @media (prefers-reduced-motion: no-preference) and components.css stops the rest.
   Load order: scenes-base.js, scenes-a.js, scenes-b.js, seasons-a.js, seasons-b.js, this file.
   Runs again for swapped content on htmx:load (kestrel-ui.js calls KSceneBand.renderAll(root));
   a band whose data-rendered already matches its scene and touch is skipped. */
(function () {
  'use strict';
  window.SCENES = window.SCENES || [];
  window.SEASONS = window.SEASONS || [];

  var SVG_OPEN = '<svg class="k-scene__art %CLS%" viewBox="0 0 560 110" ' +
    'preserveAspectRatio="xMaxYMax meet" aria-hidden="true" focusable="false">';
  var counter = 0;

  /* Every render gets its own gradient, mask and clip ids, so two copies on one page (or a
     re-render) never resolve each other's defs. Same rewrite as the scene gallery's uniq(). */
  function uniq(markup) {
    var tag = 'u' + (counter++);
    return markup
      .replace(/id="([^"]+)"/g, 'id="$1-' + tag + '"')
      .replace(/url\(#([^)]+)\)/g, 'url(#$1-' + tag + ')')
      .replace(/href="#([^"]+)"/g, 'href="#$1-' + tag + '"');
  }

  function find(list, id) {
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }

  function draw(entry) {
    if (!entry || typeof entry.draw !== 'function') return '';
    try { return entry.draw() || ''; } catch (e) { return ''; }
  }

  function render(el) {
    // Already drawn for this scene and touch: leave the art (and its animation) alone.
    var key = (el.getAttribute('data-scene') || '') + '|' + (el.getAttribute('data-touch') || '');
    if (el.getAttribute('data-rendered') === key) return el;
    var scene = find(window.SCENES, el.getAttribute('data-scene') || '');
    var touch = find(window.SEASONS, el.getAttribute('data-touch') || '');
    var old = el.querySelectorAll('.k-scene__art');
    for (var i = 0; i < old.length; i++) old[i].remove();
    var html = '';
    if (scene) html += SVG_OPEN.replace('%CLS%', 'k-scene__scene') + uniq(draw(scene)) + '</svg>';
    if (touch) html += SVG_OPEN.replace('%CLS%', 'k-scene__touch') + uniq(draw(touch)) + '</svg>';
    if (html) el.insertAdjacentHTML('afterbegin', html);
    el.setAttribute('data-rendered', key);
    return el;
  }

  function renderAll(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var bands = [];
    if (scope.matches && scope.matches('.k-scene')) bands.push(scope);
    var found = scope.querySelectorAll('.k-scene');
    for (var i = 0; i < found.length; i++) bands.push(found[i]);
    bands.forEach(render);
    return bands.length;
  }

  function setAnimate(el, on) { el.setAttribute('data-animate', on ? '1' : '0'); }

  window.KSceneBand = {
    render: render,
    renderAll: renderAll,
    setAnimate: setAnimate,
    uniq: uniq,
    scenes: function () { return window.SCENES.slice(); },
    seasons: function () { return window.SEASONS.slice(); }
  };

  if (typeof document !== 'undefined' && document.addEventListener) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { renderAll(document); });
    } else {
      renderAll(document);
    }
  }
})();
