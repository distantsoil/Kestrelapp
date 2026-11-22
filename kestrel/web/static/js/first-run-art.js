/* The first-run notice's illustration (D-UI-249): a person in an armchair by a window, sipping
   coffee, with a floating status card. Isometric camera, rounded forms. Drawn as SVG from token
   colors only (tokens.css: --il-*, --orb, --z1, --z3, --paper, --line, --soft, --brand), so it
   follows the theme. Every class is prefixed sf-fra-; styles live in css/pages/surfaces.css.
   Motion is posed per frame (lean in, lift, eyes shut sip, lower, settle, happy squint, breathing,
   hair lag) on an 8 s loop. Either reduced-motion setting (the OS or Settings) draws one still
   pose and runs no loop. KestrelFirstRunArt.mount(svg) draws into an <svg viewBox="0 0 352 210">
   and returns {stop, pose}; the loop also stops by itself once the svg leaves the page. */
(function (root) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg', S = 13, OX = 176, OY = 100;
  function P(x, y, z) { return [OX + (x - y) * 0.866 * S, OY + (x + y) * 0.5 * S - z * S]; }
  function pts(a) { return a.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' '); }
  function el(tag, a, parent) { var e = document.createElementNS(NS, tag); for (var k in a) e.setAttribute(k, a[k]); if (parent) parent.appendChild(e); return e; }
  function v(name) { return 'var(--' + name + ')'; }
  // A soft iso box: three faces, each stroked in its own color with round joins.
  function box(g, x, y, z, w, d, h, c) {
    var f = v(c), s = el('g', { class: 'sf-fra-soft' }, g);
    el('polygon', { points: pts([P(x, y + d, z + h), P(x + w, y + d, z + h), P(x + w, y + d, z), P(x, y + d, z)]), fill: f, stroke: f, class: 'sf-fra-l' }, s);
    el('polygon', { points: pts([P(x + w, y, z + h), P(x + w, y + d, z + h), P(x + w, y + d, z), P(x + w, y, z)]), fill: f, stroke: f, class: 'sf-fra-r' }, s);
    el('polygon', { points: pts([P(x, y, z + h), P(x + w, y, z + h), P(x + w, y + d, z + h), P(x, y + d, z + h)]), fill: f, stroke: f }, s);
  }
  // A pillow: a box inset by r (iso units) on every side and stroked 2r wide with round joins,
  // so every edge is rounded with radius r while the box stays on the isometric grid.
  function pillow(g, x, y, z, w, d, h, c, r) {
    var f = v(c), sw = 2 * r * S, s = el('g', { class: 'sf-fra-pillow' }, g);
    x += r; y += r; z += r; w -= 2 * r; d -= 2 * r; h -= 2 * r;
    function face(a, cls) { el('polygon', { points: pts(a), fill: f, stroke: f, 'stroke-width': sw, 'stroke-linejoin': 'round', class: cls || '' }, s); }
    face([P(x, y + d, z + h), P(x + w, y + d, z + h), P(x + w, y + d, z), P(x, y + d, z)], 'sf-fra-l');
    face([P(x + w, y, z + h), P(x + w, y + d, z + h), P(x + w, y + d, z), P(x + w, y, z)], 'sf-fra-r');
    face([P(x, y, z + h), P(x + w, y, z + h), P(x + w, y + d, z + h), P(x, y + d, z + h)]);
    return s;
  }
  // A rolled arm front: an ellipse on the arm's +x face.
  function roll(g, x, y, z, d, h, c) {
    var a = P(x, y + d / 2, z + h / 2);
    el('ellipse', { cx: a[0], cy: a[1], rx: d * S * 0.42, ry: h * S * 0.5, fill: v(c), class: 'sf-fra-r', transform: 'rotate(-30 ' + a[0] + ' ' + a[1] + ')' }, g);
  }
  // An iso cylinder (round table, plant pot): side band, then the top ellipse.
  function cyl(g, x, y, z, r, h, c) {
    var b = P(x, y, z), t = P(x, y, z + h), rx = r * S, ry = rx * 0.577;
    el('path', { d: 'M' + (b[0] - rx) + ',' + b[1] + ' A' + rx + ',' + ry + ' 0 0 0 ' + (b[0] + rx) + ',' + b[1] + ' L' + (t[0] + rx) + ',' + t[1] + ' A' + rx + ',' + ry + ' 0 0 1 ' + (t[0] - rx) + ',' + t[1] + 'Z', fill: v(c), class: 'sf-fra-r' }, g);
    el('ellipse', { cx: t[0], cy: t[1], rx: rx, ry: ry, fill: v(c) }, g);
  }
  function limb(g, a, c, w) { return el('polyline', { points: pts(a), fill: 'none', stroke: v(c), 'stroke-width': w, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, g); }

  function scene(svg) {
    var g = el('g', {}, svg), parts = {};
    // Room: floor, two walls, a rug.
    var fl = el('g', { class: 'sf-fra-soft' }, g);
    el('polygon', { points: pts([P(0, 0, 0), P(8.4, 0, 0), P(8.4, 8.4, 0), P(0, 8.4, 0)]), fill: v('il-floor'), stroke: v('il-floor') }, fl);
    el('polygon', { points: pts([P(0, 0, 0), P(0, 8.4, 0), P(0, 8.4, 7.6), P(0, 0, 7.6)]), fill: v('il-wall'), class: 'sf-fra-l' }, g);
    el('polygon', { points: pts([P(0, 0, 0), P(8.4, 0, 0), P(8.4, 0, 7.6), P(0, 0, 7.6)]), fill: v('il-wall-2') }, g);
    var rc = P(4.2, 4.6, 0);
    el('ellipse', { cx: rc[0], cy: rc[1], rx: 52, ry: 30, fill: v('il-rug') }, g);
    // Window on the right wall: sky, sun, a drifting cloud, a cross frame.
    var wp = [P(3.4, 0, 6.4), P(7, 0, 6.4), P(7, 0, 3), P(3.4, 0, 3)];
    var clip = el('clipPath', { id: 'wc' + Math.random().toString(36).slice(2) }, svg);
    el('polygon', { points: pts(wp) }, clip);
    el('polygon', { points: pts(wp), fill: v('il-sky') }, g);
    var sky = el('g', { 'clip-path': 'url(#' + clip.id + ')' }, g);
    var sc = P(4.5, 0, 5.2); el('circle', { class: 'sf-fra-sun', cx: sc[0], cy: sc[1], r: 8, fill: v('orb') }, sky);
    var cl = P(5.6, 0, 5.9), cloud = el('g', { class: 'sf-fra-cloud' }, sky);
    el('ellipse', { cx: cl[0], cy: cl[1], rx: 12, ry: 4.5, fill: v('paper') }, cloud);
    el('circle', { cx: cl[0] + 4, cy: cl[1] - 3.5, r: 5.5, fill: v('paper') }, cloud);
    el('polygon', { points: pts(wp), fill: 'none', stroke: v('paper'), 'stroke-width': 3.5, 'stroke-linejoin': 'round' }, g);
    limb(g, [P(5.2, 0, 6.4), P(5.2, 0, 3)], 'paper', 2.5);
    limb(g, [P(3.4, 0, 4.7), P(7, 0, 4.7)], 'paper', 2.5);
    // Round side table with a plant, by the window.
    cyl(g, 4.6, 1.5, 0, 0.18, 2.1, 'il-wood');
    cyl(g, 4.6, 1.5, 2.1, 1.0, 0.18, 'il-wood');
    cyl(g, 4.6, 1.5, 2.28, 0.42, 0.62, 'il-pot');
    var pc = P(4.6, 1.5, 2.9), leaves = el('g', {}, g);
    [[-6, -11, -25], [0, -14, 0], [6, -10, 25]].forEach(function (lf) {
      el('ellipse', { class: 'sf-fra-leaf', cx: pc[0] + lf[0], cy: pc[1] + lf[1], rx: 4, ry: 9, fill: v('z3'), transform: 'rotate(' + lf[2] + ' ' + (pc[0] + lf[0]) + ' ' + (pc[1] + lf[1]) + ')' }, leaves);
    });
    // Armchair, back and far arm first.
    var shc = P(3, 5.2, 0);
    el('ellipse', { cx: shc[0] + 4, cy: shc[1] + 2, rx: 46, ry: 24, fill: v('il-hair'), opacity: .12 }, g);
    pillow(g, 1.0, 3.5, 0.15, 3.6, 3.4, 1.3, 'z1', 0.35);
    pillow(g, 0.9, 3.5, 1.1, 1.0, 3.4, 3.4, 'z1', 0.5);
    pillow(g, 1.0, 3.3, 1.0, 3.6, 0.85, 1.7, 'z1', 0.42);
    roll(g, 4.55, 3.3, 1.75, 0.85, 1.0, 'z1');
    pillow(g, 1.75, 4.1, 1.25, 2.85, 2.4, 0.6, 'z1', 0.28);

    // The person, seated, facing +x. Legs are static; the upper body is posed per frame.
    var hip = P(2.5, 5.2, 1.5), dx = [0.866 * S, 0.5 * S], dy = [-0.866 * S * 0.3, 0.5 * S * 0.3];
    function leg(off, shade) {
      var back = shade ? [-3, -1.5] : [0, 0];
      var h = [hip[0] + off[0], hip[1] + off[1]], k = [h[0] + dx[0] * 1.8, h[1] + dx[1] * 1.8], f = [k[0] - 1 + back[0], k[1] + 18 + back[1]];
      var lg = el('g', shade ? { class: 'sf-fra-l' } : {}, g);
      limb(lg, [h, k, f], 'il-pants', 10.5);
      var sc = [f[0] + 3.6, f[1] + 2.2];
      el('ellipse', { cx: sc[0], cy: sc[1] + 1.1, rx: 6, ry: 2.4, fill: v('paper'), opacity: .25, transform: 'rotate(26 ' + sc[0] + ' ' + (sc[1] + 1.1) + ')' }, lg);
      el('path', { d: 'M' + (sc[0] - 6) + ',' + (sc[1] - 1) + ' q0,-4 5,-3.6 q6,0.4 7.2,3.4 q0.6,2 -1.6,2.6 l-8.6,1.2 q-2,0.2 -2,-3.6z', fill: v('il-hair'), transform: 'rotate(18 ' + sc[0] + ' ' + sc[1] + ')' }, lg);
    }
    var ss = [hip[0] + dx[0] * 0.9, hip[1] + dx[1] * 0.9 + 3];
    el('ellipse', { cx: ss[0], cy: ss[1], rx: 18, ry: 7, fill: v('z1'), class: 'sf-fra-r', opacity: .9, transform: 'rotate(28 ' + ss[0] + ' ' + ss[1] + ')' }, g);
    leg([-dy[0], -dy[1]], true);
    var upper = el('g', {}, g);
    parts.farArm = limb(upper, [], 'il-shirt', 8); parts.farArm.setAttribute('class', 'sf-fra-r');
    parts.farHand = el('ellipse', { rx: 3.4, ry: 2.8, fill: v('il-skin-2') }, upper);
    parts.torsoG = el('g', {}, upper);
    parts.torsoShade = el('path', { fill: v('il-shirt'), class: 'sf-fra-r' }, parts.torsoG);
    parts.torso = el('path', { fill: v('il-shirt') }, parts.torsoG);
    parts.hem = el('rect', { x: -9.5, width: 19, height: 4, rx: 2, fill: v('il-cuff') }, parts.torsoG);
    var head = el('g', {}, upper); parts.head = head;
    el('rect', { x: -2.4, y: 8, width: 5.6, height: 5, rx: 2.6, fill: v('il-skin-2') }, head);
    parts.hairBack = el('g', { class: 'sf-fra-r' }, head);
    el('path', { d: 'M-12,-2 C-13,-13 -3,-18.5 4,-17.5 C12,-16.5 15.5,-9 14.5,-2 C14.3,4 13.2,8.6 10.4,11 C8.6,8.6 8.2,5.6 7.6,3.4 L-6,3.4 C-7,7.4 -9.4,10.4 -12.4,10.4 C-13.8,6.4 -13.2,2.2 -12,-2 Z', fill: v('il-hair') }, parts.hairBack);
    el('path', { d: 'M-6,-6 C-6,-12 0,-13.2 5,-12.2 C10,-11.2 12.2,-6.4 12.2,-1.6 C12.2,0.4 13.6,1.6 13.2,2.8 C12.8,3.8 12,3.8 11.8,4.6 C11,8.4 8.2,10.8 4,11.2 C0,11.6 -4,9.2 -5.5,5 C-6.5,2 -6.5,-2 -6,-6 Z', fill: v('il-skin') }, head);
    parts.hairFront = el('g', {}, head);
    el('path', { d: 'M-7.4,-2.4 C-8.6,-12.4 0,-17.6 7,-16 C12,-14.8 14.8,-9.6 13.8,-3.4 C11.6,-7.2 7.8,-9.8 3.6,-10 C1.6,-6.8 -2.6,-4.4 -7.4,-2.4 Z', fill: v('il-hair') }, parts.hairFront);
    el('path', { d: 'M-7.6,-3 C-6.2,1.6 -5.4,6.2 -6.8,10.8 C-4.2,10.2 -2.8,7 -3.2,3 C-3.5,0.4 -4.6,-1.8 -7.6,-3 Z', fill: v('il-hair') }, parts.hairFront);
    el('path', { d: 'M-3,-13 q4.6,-3 9.6,-1', fill: 'none', stroke: v('paper'), 'stroke-width': 1.2, 'stroke-linecap': 'round', opacity: .28 }, parts.hairFront);
    ['M0.3,-4.4 q2.2,-1.4 4.2,-0.2', 'M6.7,-4.4 q1.4,-0.9 2.6,0'].forEach(function (d) { el('path', { d: d, fill: 'none', stroke: v('il-hair'), 'stroke-width': 0.95, 'stroke-linecap': 'round' }, head); });
    parts.eyesOpen = el('g', {}, head);
    el('ellipse', { cx: 2.4, cy: 0.4, rx: 1.45, ry: 1.95, fill: v('il-hair') }, parts.eyesOpen);
    el('ellipse', { cx: 7.9, cy: 0.4, rx: 1.15, ry: 1.7, fill: v('il-hair') }, parts.eyesOpen);
    el('circle', { cx: 2.9, cy: -0.3, r: 0.5, fill: v('paper') }, parts.eyesOpen);
    el('circle', { cx: 8.3, cy: -0.3, r: 0.4, fill: v('paper') }, parts.eyesOpen);
    parts.lids = el('g', { opacity: 0 }, head);
    ['M0.8,1 q1.6,-2 3.2,0', 'M6.7,1 q1.2,-1.7 2.4,0'].forEach(function (d) { el('path', { d: d, fill: 'none', stroke: v('il-hair'), 'stroke-width': 1.35, 'stroke-linecap': 'round' }, parts.lids); });
    parts.eyesShut = el('g', { opacity: 0 }, head);
    ['M0.9,0.7 q1.6,1.5 3.2,0', 'M6.8,0.7 q1.2,1.3 2.4,0'].forEach(function (d) { el('path', { d: d, fill: 'none', stroke: v('il-hair'), 'stroke-width': 1.3, 'stroke-linecap': 'round' }, parts.eyesShut); });
    parts.mouth = el('path', { d: 'M3.7,6.4 q2,1.7 4,0.2', fill: 'none', stroke: v('il-hair'), 'stroke-width': 1.25, 'stroke-linecap': 'round' }, head);
    el('ellipse', { cx: 1.2, cy: 4.4, rx: 2, ry: 1.15, fill: v('z1'), opacity: .45 }, head);
    el('ellipse', { cx: 9.1, cy: 4.4, rx: 1.2, ry: 0.95, fill: v('z1'), opacity: .45 }, head);
    parts.collarG = el('g', {}, upper);
    parts.collar = el('ellipse', { cx: 1, rx: 6.4, ry: 2.8, fill: v('il-cuff') }, parts.collarG);
    parts.nearArm = limb(upper, [], 'il-shirt', 8.5); parts.nearArm.setAttribute('data-fra', 'arm');
    parts.mug = el('g', {}, upper);
    [[4.6, -1], [4.9, 2.2]].forEach(function (f) { el('ellipse', { cx: f[0], cy: f[1], rx: 2.2, ry: 1.5, fill: v('il-skin-2') }, parts.mug); });
    el('path', { d: 'M5,-3 q5,0 5,4 q0,4 -5,4', fill: 'none', stroke: v('il-mug'), 'stroke-width': 2.4, class: 'sf-fra-r' }, parts.mug);
    el('rect', { x: -5.5, y: -6.5, width: 11, height: 13, rx: 3.5, fill: v('il-mug'), class: 'sf-fra-r' }, parts.mug);
    el('rect', { x: -5.5, y: -6.5, width: 7.5, height: 13, rx: 3.5, fill: v('il-mug') }, parts.mug);
    el('ellipse', { cx: 0, cy: -6.5, rx: 5.5, ry: 2.2, fill: v('il-mug') }, parts.mug);
    el('ellipse', { cx: 0, cy: -6.2, rx: 4.1, ry: 1.5, fill: v('il-coffee') }, parts.mug);
    el('rect', { x: -6.4, y: -2.6, width: 7, height: 2.8, rx: 1.4, fill: v('il-skin') }, parts.mug);
    parts.cuff = el('circle', { r: 4.6, fill: v('il-cuff') }, upper);
    parts.hand = el('ellipse', { rx: 4.4, ry: 3.6, fill: v('il-skin') }, upper);
    parts.steam = el('g', { class: 'sf-fra-steam' }, upper);
    [-3, 1, 5].forEach(function (x) { el('path', { d: 'M' + x + ',0 c-2.5,-2.5 2.5,-5 0,-7.5' }, parts.steam); });
    leg([dy[0], dy[1]], false);
    // Near arm of the chair, in front of the person's hip.
    pillow(g, 1.0, 6.25, 1.0, 3.6, 0.85, 1.7, 'z1', 0.42);
    roll(g, 4.55, 6.25, 1.75, 0.85, 1.0, 'z1');

    // The floating status card from option A: step 1 live, a highlight sweeping its bar.
    var card = el('g', { class: 'sf-fra-bob' }, svg);
    el('rect', { x: 34, y: 16, width: 104, height: 50, rx: 11, fill: v('paper'), stroke: v('line') }, card);
    el('rect', { x: 46, y: 28, width: 46, height: 6, rx: 3, fill: v('line') }, card);
    el('circle', { cx: 124, cy: 31, r: 5, fill: v('z3') }, card);
    var cc = el('clipPath', { id: 'cc' + Math.random().toString(36).slice(2) }, svg);
    el('rect', { x: 46, y: 44, width: 18, height: 7, rx: 3.5 }, cc);
    el('rect', { x: 46, y: 44, width: 18, height: 7, rx: 3.5, fill: v('brand') }, card);
    var sw = el('g', { 'clip-path': 'url(#' + cc.id + ')' }, card);
    el('rect', { class: 'sf-fra-sweep', x: 46, y: 44, width: 8, height: 7, fill: v('paper'), opacity: .55 }, sw);
    [1, 2, 3].forEach(function (i) { el('rect', { x: 46 + i * 22, y: 44, width: 18, height: 7, rx: 3.5, fill: v('soft'), stroke: v('line') }, card); });

    parts.hip = hip;
    return parts;
  }

  // Posing. Times in seconds on an 8 s loop.
  function eio(t) { return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }
  function eout(t) { return 1 - Math.pow(1 - t, 3); }
  function clamp(t) { return Math.max(0, Math.min(1, t)); }
  function lerp(a, b, k) { return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k]; }
  function rot(p, deg, o) { var r = deg * Math.PI / 180, c = Math.cos(r), s = Math.sin(r), x = p[0] - o[0], y = p[1] - o[1]; return [o[0] + x * c - y * s, o[1] + x * s + y * c]; }
  function pose(t) {
    var k = 0, lean = 0, tilt = 0, shut = false;
    if (t >= 1.3 && t < 1.8) lean = 2.5 * eio(clamp((t - 1.3) / .5));                         // lean in
    else if (t >= 1.8 && t < 2.8) { var a = eout(clamp((t - 1.8) / 1)); k = a; lean = 2.5 - 3.5 * a; }  // lift
    else if (t >= 2.8 && t < 4.3) { k = 1; lean = -1; }                                      // sip
    else if (t >= 4.3 && t < 5.2) { var b = eio(clamp((t - 4.3) / .9)); k = 1 - b; lean = -1 + b * -1.2; } // lower
    else if (t >= 5.2 && t < 5.9) { lean = -2.2 + 2.2 * eout(clamp((t - 5.2) / .7)) + Math.sin(clamp((t - 5.2) / .7) * Math.PI) * .8; } // settle
    tilt = -10 * eio(clamp(k));
    shut = (t > 3.0 && t < 4.15) || (t > 6.7 && t < 6.82) || (t > .5 && t < .62);
    var breath = Math.sin(t / 8 * Math.PI * 4) * 0.8;
    var smile = t > 4.5 && t < 6.6;
    if (smile) tilt += 4 * Math.sin(clamp((t - 4.5) / 2.1) * Math.PI);
    return { k: k, lean: lean, tilt: tilt, shut: shut, breath: breath, smile: smile };
  }
  function apply(parts, st, lagTilt) {
    var hip = parts.hip, L = st.lean;
    var sh = [hip[0] - 5 + L, hip[1] - 24 - st.breath];
    var tb = [hip[0] + 1, hip[1] + 2], tt = [sh[0], sh[1] - 3], tl = Math.hypot(tt[0] - tb[0], tt[1] - tb[1]);
    var ta = Math.atan2(tt[0] - tb[0], tb[1] - tt[1]) * 180 / Math.PI;
        function trap(l, inset) { var T = -l, a = -12.5 + inset, b = -9.5 + inset;
      return 'M' + b + ',0 L9.5,0 Q11.5,0 11.3,-3 L12.4,' + (T + 6) + ' Q12.8,' + T + ' 7,' + T + ' L' + (a + 5.5) + ',' + T + ' Q' + a + ',' + T + ' ' + a + ',' + (T + 6) + ' L' + (b - 1.6) + ',-3 Q' + (b - 1.6) + ',0 ' + b + ',0Z'; }
    parts.torsoShade.setAttribute('d', trap(tl, 0)); parts.torso.setAttribute('d', trap(tl, 4.5));
    parts.hem.setAttribute('y', '-4'); parts.collar.setAttribute('cy', (-tl + 2.4).toFixed(1));
    var tx = 'translate(' + tb[0].toFixed(1) + ' ' + tb[1].toFixed(1) + ') rotate(' + ta.toFixed(1) + ')';
    parts.torsoG.setAttribute('transform', tx); parts.collarG.setAttribute('transform', tx);
    var neck = [sh[0] + 2, sh[1] - 3], hc = rot([sh[0] + 2, sh[1] - 18], st.tilt, neck);
    parts.head.setAttribute('transform', 'translate(' + hc[0].toFixed(1) + ' ' + hc[1].toFixed(1) + ') rotate(' + st.tilt.toFixed(1) + ') scale(1.15)');
    var lag = ((lagTilt === undefined ? st.tilt : lagTilt) - st.tilt) * 0.7;
    [parts.hairBack, parts.hairFront].forEach(function (h) { h.setAttribute('transform', 'rotate(' + lag.toFixed(2) + ' 1 -8)'); });
    parts.mouth.setAttribute('d', st.smile ? 'M3.1,6 q2.6,2.6 5.2,0.1' : 'M3.7,6.4 q2,1.7 4,0.2');
    parts.eyesOpen.setAttribute('opacity', st.shut || st.smile ? 0 : 1);
    parts.lids.setAttribute('opacity', st.smile && !st.shut ? 1 : 0);
    parts.eyesShut.setAttribute('opacity', st.shut ? 1 : 0);
    var fs = [sh[0] + 3, sh[1] - 1];
    var fe = [fs[0] - 1, fs[1] + 13], fh = [fe[0] + 13, fe[1] + 4];
    parts.farArm.setAttribute('points', pts([fs, fe, fh]));
    parts.farHand.setAttribute('cx', (fh[0] + 2.5).toFixed(1)); parts.farHand.setAttribute('cy', (fh[1] + 0.6).toFixed(1));
    var ns = [sh[0] - 3, sh[1] + 3];
    var mouth = rot([sh[0] + 2 + 6.6, sh[1] - 18 + 7.6], st.tilt, neck);
    var mugRest = [ns[0] + 21, ns[1] + 13], mugSip = [mouth[0] + 10.5, mouth[1] + 2.5];
    var mk = eio(st.k), m = lerp(mugRest, mugSip, mk), ang = -30 * mk;
    var hand = rot([m[0] - 5.5, m[1] + 2], ang, m);
    var elbow = lerp([ns[0] + 4, ns[1] + 15], [ns[0] + 9, ns[1] + 13], mk);
    parts.nearArm.setAttribute('points', pts([ns, elbow, hand]));
    parts.mug.setAttribute('transform', 'translate(' + m[0].toFixed(1) + ' ' + m[1].toFixed(1) + ') rotate(' + ang.toFixed(1) + ')');
    parts.hand.setAttribute('cx', hand[0].toFixed(1)); parts.hand.setAttribute('cy', hand[1].toFixed(1));
    var cf = lerp(elbow, hand, 0.78); parts.cuff.setAttribute('cx', cf[0].toFixed(1)); parts.cuff.setAttribute('cy', cf[1].toFixed(1));
    parts.steam.setAttribute('transform', 'translate(' + m[0].toFixed(1) + ' ' + (m[1] - 10).toFixed(1) + ')');
    parts.steam.setAttribute('opacity', st.k > .3 ? 0 : 1);
  }


  function reduced() {
    return document.documentElement.getAttribute('data-reduce-motion') === '1' ||
      !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function draw(rig, t) { var st = pose(t), lagT = pose(Math.max(0, t - 0.22)).tilt; apply(rig, st, lagT); }

  function mount(svg) {
    if (!svg) return null;
    var rig = scene(svg), running = !reduced(), acc = 0, last = null, frozen = false;
    draw(rig, 0.2);
    function tick(ts) {
      if (!running || !svg.isConnected) { running = false; return; }
      if (last !== null) acc += Math.min(0.1, (ts - last) / 1000);
      last = ts;
      if (!frozen) draw(rig, acc % 8);
      root.requestAnimationFrame(tick);
    }
    if (running) root.requestAnimationFrame(tick);
    return {
      stop: function () { running = false; },
      // Hold one moment of the loop (tests and screenshots); pose(null) lets it run again.
      pose: function (t) { frozen = t !== null; if (frozen) draw(rig, t); },
      running: function () { return running; }
    };
  }
  root.KestrelFirstRunArt = { mount: mount };
})(window);
