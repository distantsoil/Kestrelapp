/* The command palette (D-UI-91; the locked design mockup (kept privately) :545-600,
   :771-775, :1261-1296). Ctrl K (Cmd K on a Mac) or the search field opens it. With an empty
   query it shows Recent picks and two suggested actions; with a query it groups matches under
   Roles, Companies, Settings and Actions, highlights the matched text, and counts the results.
   Roles and companies come from GET /palette/search; settings and actions are listed here with
   plain-word synonyms. The palette only navigates and runs the actions below; it never sends
   anything anywhere. Pure functions (results, highlight) are exported for the tests. */
(function (root) {
  'use strict';

  var ICONS = {
    role: '<path d="M4 6h16M4 12h16M4 18h10"/>',
    company: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h6"/>',
    setting: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    calibrate: '<path d="M4 20V9M12 20V4M20 20v-8"/>',
    moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    open: '<path d="M7 17L17 7M9 7h8v8"/>',
    quit: '<path d="M12 3v8M6.3 6.3a8 8 0 1 0 11.4 0"/>'
  };

  /* Settings: plain words and synonyms (spec: "ollama", "backup", "linkedin", "dates"). */
  var SETTINGS = [
    { t: 'Models', s: 'Choose which AI scores your roles', href: '/models',
      k: 'ollama model ai local online key openrouter openai anthropic engine' },
    { t: 'Search rules', s: 'Roles Kestrel hides before scoring', href: '/settings#roles',
      k: 'rules filters hide titles roles exclude salary location' },
    { t: 'Your data', s: 'Back up, export, or download diagnostics', href: '/settings#data',
      k: 'backup restore export diagnostics delete reset data' },
    { t: 'Preferences', s: 'Theme, colors, text size, motion, region and dates', href: '/settings#preferences',
      k: 'dark light theme scheme colors text size motion scene region dates date format time' },
    { t: 'This computer', s: 'How Kestrel runs and the setup checklist', href: '/settings#computer',
      k: 'ollama install gpu hardware computer startup tray close setup' },
    { t: 'Scoring and learning', s: 'Weights, cutoffs and what Kestrel learned', href: '/settings#scoring',
      k: 'weights score scoring learning cutoffs calibrate' },
    { t: 'Privacy and access', s: 'Confidential mode, other devices and assistants', href: '/settings#privacy',
      k: 'privacy confidential employer lan phone network mcp access assistant' },
    { t: 'Sources', s: 'The job boards and company pages Kestrel searches', href: '/sources',
      k: 'sources boards searches indeed linkedin greenhouse lever workday ashby smartrecruiters sec' },
    { t: 'Model check', s: 'Compare models on your own roles', href: '/bench',
      k: 'bench benchmark test compare models check' },
    { t: 'Needs another try', s: 'Roles the model could not finish', href: '/quarantine',
      k: 'failed retry errors quarantine problem' }
  ];

  function lower(v) { return String(v || '').toLowerCase(); }
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  /* The first match of the query in the text, wrapped in <mark>; everything else escaped. */
  function highlight(text, q) {
    text = String(text || '');
    q = String(q || '').trim();
    if (!q) return esc(text);
    var k = lower(text).indexOf(lower(q));
    if (k < 0) {
      var w = lower(q).split(/\s+/)[0];
      k = w ? lower(text).indexOf(w) : -1;
      if (k < 0) return esc(text);
      q = text.slice(k, k + w.length);
    }
    return esc(text.slice(0, k)) + '<mark class="k-pal__m">' + esc(text.slice(k, k + q.length)) +
      '</mark>' + esc(text.slice(k + q.length));
  }
  function words(q) { return lower(q).trim().split(/\s+/).filter(Boolean); }
  function matches(item, ws) {
    var hay = lower([item.t, item.s, item.k, item.g].join(' '));
    return ws.every(function (w) { return hay.indexOf(w) > -1; });
  }

  /* The groups to show. src: {actions, pages, roles, companies, recent, dark}. Returns
     {rows: [{h: 'ROLES'} | {item, n}], items: [...], count}. */
  function results(q, src) {
    src = src || {};
    var actions = (src.actions || []).map(function (a) {
      if (a.id === 'dark') return Object.assign({}, a, { t: src.dark ? 'Turn off dark mode' : 'Turn on dark mode' });
      return a;
    });
    var groups = [];
    var ws = words(q);
    if (!ws.length) {
      var recent = (src.recent || []).slice(0, 4);
      var seen = recent.map(function (r) { return r.t; });
      var suggest = actions.filter(function (a) { return a.suggest && seen.indexOf(a.t) < 0; }).slice(0, 2);
      if (recent.length) groups.push(['Recent', recent]);
      if (suggest.length) groups.push(['Actions', suggest]);
    } else {
      var all = {
        Roles: (src.roles || []).map(function (r) { return Object.assign({ g: 'Roles', ic: 'role' }, r); }),
        Companies: (src.companies || []).map(function (c) { return Object.assign({ g: 'Companies', ic: 'company' }, c); }),
        Settings: SETTINGS.map(function (x) { return Object.assign({ g: 'Settings', ic: 'setting' }, x); }),
        Actions: actions.concat(src.pages || [])
      };
      ['Roles', 'Companies', 'Settings', 'Actions'].forEach(function (g) {
        // Roles and companies were matched by the server on the whole query.
        var list = (g === 'Roles' || g === 'Companies') ? all[g] : all[g].filter(function (x) { return matches(x, ws); });
        if (list.length) groups.push([g, list]);
      });
    }
    var rows = [], items = [];
    groups.forEach(function (g) {
      rows.push({ h: g[0].toUpperCase() });
      g[1].forEach(function (x) {
        rows.push({ item: x, n: items.length, recent: g[0] === 'Recent' });
        items.push(x);
      });
    });
    return { rows: rows, items: items, count: items.length };
  }

  function icon(name) {
    return '<svg class="k-ic" viewBox="0 0 24 24" aria-hidden="true">' + (ICONS[name] || ICONS.open) + '</svg>';
  }

  var api = { results: results, highlight: highlight, esc: esc, icon: icon, SETTINGS: SETTINGS };
  root.KPalette = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
