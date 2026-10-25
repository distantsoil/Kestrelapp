// Loads the scene files the way the browser does (one shared global), draws every scene and
// seasonal touch, and exercises the band renderer against a fake element. Prints JSON.
const vm = require('vm');
const fs = require('fs');

const dir = process.argv[2];
const ctx = vm.createContext({ console });
ctx.window = ctx;
for (const f of ['scenes-base', 'scenes-a', 'scenes-b', 'seasons-a', 'seasons-b', 'scene-band']) {
  vm.runInContext(fs.readFileSync(dir + '/' + f + '.js', 'utf8'), ctx, { filename: f });
}

const ids = (markup) => markup.match(/ id="[^"]+"/g) || [];
const stray = (markup, prefix) => ids(markup).filter((x) => !x.startsWith(' id="' + prefix)).length;
const res = { scenes: [], seasons: [] };
for (const s of ctx.SCENES) {
  const m = s.draw();
  res.scenes.push([s.id, m.length, stray(m, 'sc-'), ids(m).map((x) => x.slice(5))]);
}
for (const s of ctx.SEASONS) {
  const m = s.draw();
  res.seasons.push([s.id, m.length, stray(m, 'ss-'), ids(m).map((x) => x.slice(5))]);
}

const sample = '<defs><linearGradient id="g"/></defs><rect fill="url(#g)"/>';
const a = ctx.KSceneBand.uniq(sample);
const b = ctx.KSceneBand.uniq(sample);
res.uniq = [a !== b, /id="g-u\d+"/.test(a), /url\(#g-u\d+\)/.test(a)];

const el = {
  attrs: { 'data-scene': 'coast', 'data-touch': ctx.SEASONS[0].id },
  html: '',
  getAttribute(k) { return this.attrs[k] || null; },
  setAttribute(k, v) { this.attrs[k] = String(v); },
  querySelectorAll() { return []; },
  insertAdjacentHTML(_where, h) { this.html = h; },
};
ctx.KSceneBand.render(el);
res.band = [
  (el.html.match(/<svg /g) || []).length,
  el.html.includes('k-scene__scene'),
  el.html.includes('k-scene__touch'),
];
const before = el.html;
el.html = 'SENTINEL';
ctx.KSceneBand.render(el);  // same scene and touch: skipped, markup untouched
res.skipped = el.html === 'SENTINEL' && before.length > 0;
el.html = '';
el.attrs['data-scene'] = 'none';
el.attrs['data-touch'] = '';
el.html = '';
ctx.KSceneBand.render(el);
res.none = el.html;
console.log(JSON.stringify(res));
