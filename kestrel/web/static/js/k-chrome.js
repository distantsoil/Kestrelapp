/* The top bar's Alpine components (the command palette, theme and track in kChrome, the bell
   panel) and the bell badge. Moved out of base.html for the Content-Security-Policy (D-INST-8):
   what the template computed arrives in the #k-chrome-data JSON block, and the components are
   registered with Alpine.data, because the CSP build of Alpine reads no globals. Deferred, before
   alpine.min.js. */
var CHROME = (function () {
  var el = document.getElementById('k-chrome-data');
  try { return Object.assign({desktop: false, pages: [], multi: false, cur: ''}, JSON.parse(el ? el.textContent : '{}')); }
  catch (e) { return {desktop: false, pages: [], multi: false, cur: ''}; }
})();
function kChrome() {
  return {
    theme: (function () { try { return localStorage.getItem('kestrel-theme') || 'auto'; } catch (e) { return 'auto'; } })(),
    scheme: (function () {
      var s = null;
      try { s = localStorage.getItem('kestrel-scheme'); } catch (e) {}
      if (s === 'indigo' || s === 'teal') return s;
      return document.documentElement.getAttribute('data-scheme') || 'indigo';
    })(),
    palette: false, opener: null, q: '', idx: 0, more: false,
    view: { rows: [], items: [], count: 0 }, found: { roles: [], companies: [] }, timer: null,
    // Actions: what the palette may run (D-UI-91); every nav page is an "Open" action.
    actions: [
      {id: 'run', g: 'Actions', t: 'Run a search now', s: 'Look for new roles right away', ic: 'search', href: '/scan/choose?page=1', suggest: true, k: 'scan search run now find'},
      {id: 'cal', g: 'Actions', t: 'Open Calibrate', s: 'Teach Kestrel what a good fit means to you', ic: 'calibrate', href: '/calibrate', suggest: true, k: 'calibrate teach learn'},
      {id: 'dark', g: 'Actions', t: 'Turn on dark mode', s: 'Switch the look of Kestrel', ic: 'moon', k: 'dark light theme mode'},
      {id: 'pause', g: 'Actions', t: 'Pause searching', s: 'Stop scheduled searches until you resume', ic: 'pause', k: 'pause stop worker searching'},
    ].concat(CHROME.desktop ? [
        {id: 'quit', g: 'Actions', t: 'Quit Kestrel', s: 'Stop searching and close', ic: 'quit', k: 'quit exit close'},
      ] : []),
    pages: CHROME.pages.map(function (p) {
      return {g: 'Actions', t: p.t, s: p.s, ic: 'open', href: p.href};
    }),
    dark: false,
    // A recent row's tag: its group in the singular ("Roles" reads "Role").
    groupTag(row) { return row.recent ? (row.item.g || '').replace(/s$/, '') : ''; },
    init() {
      // One source for appearance writes: KUI (kestrel-ui.js) owns storage and the attribute.
      this.$watch('theme', v => { window.KUI.setTheme(v); this.dark = this.isDark(); });
      this.dark = this.isDark();
      if (window.matchMedia) {
        const mq = window.matchMedia('(prefers-color-scheme: dark)');
        if (mq.addEventListener) mq.addEventListener('change', () => { this.dark = this.isDark(); });
      }
      // The search field's shortcut follows the operating system (D-UI-91): Cmd K on a Mac.
      const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
      document.querySelectorAll('[data-k-kbd]').forEach(k => { k.textContent = mac ? '\u2318K' : 'Ctrl K'; });
      if (this.theme !== 'auto') document.documentElement.setAttribute('data-theme', this.theme);
      this.$watch('scheme', v => { window.KUI.setScheme(v); });
      // The palette is modal: Tab stays in its field, and closing returns focus to the opener.
      this.$watch('palette', v => {
        if (!v && this.opener && this.opener.focus && document.contains(this.opener)) {
          this.opener.focus();
        }
        if (!v) this.opener = null;
      });
      document.documentElement.setAttribute('data-scheme', this.scheme);
      // The browser and LAN modes keep the offline cache; the desktop window never registers it.
      if ('serviceWorker' in navigator && document.documentElement.getAttribute('data-k-desktop') !== '1') {
        navigator.serviceWorker.register('/sw.js').catch(() => {});
      }
      window.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === 'k') { e.preventDefault(); this.openPalette(); }
      });
      // Keys typed between Ctrl+K and the search box taking focus belong to the search, not to
      // whatever had focus before (a fast typist lost the first word).
      // They never reach the page behind it either (its j/k, "/" and Space shortcuts).
      window.addEventListener('keydown', (e) => {
        const pq = this.$refs.pq;
        if (!this.palette || !pq || document.activeElement === pq) return;
        const key = String(e.key || '');
        const altGr = e.getModifierState && e.getModifierState('AltGraph');
        if (key === 'Enter' || key === 'ArrowDown' || key === 'ArrowUp') {
          e.preventDefault(); e.stopImmediatePropagation();
          if (key === 'Enter') this.go(this.view.items[this.idx]);
          else this.move(key === 'ArrowDown' ? 1 : -1);
          return;
        }
        if (key.length !== 1 || e.metaKey || ((e.ctrlKey || e.altKey) && !altGr)) return;
        e.preventDefault(); e.stopImmediatePropagation();
        this.q += key; this.idx = 0; this.search();
      }, true);
      // Persist the track tab across pages (D-UI-57): localStorage as before, plus a cookie the
      // server reads, so a page opened without ?track= (Today at /, a bookmark) shows the
      // remembered tab. With one track in use (consulting switched off, D-UI-28) a stored
      // choice is ignored.
      const multi = CHROME.multi;
      const here = new URLSearchParams(location.search).get('track');
      if (multi) {
        if (here === 'employment' || here === 'consulting') { this.setTrack(here); }
        else {
          let t = null;
          try { t = localStorage.getItem('kestrel-track'); } catch (e) {}
          if ((t === 'employment' || t === 'consulting') && t !== CHROME.cur
              && !document.cookie.includes('kestrel-track=')) {
            this.setTrack(t);  // an older install remembered the tab in localStorage only
            if (document.cookie.includes('kestrel-track=' + t)) location.reload();
          }
        }
      }
    },
    isDark() {
      const t = document.documentElement.getAttribute('data-theme');
      if (t === 'dark' || t === 'light') return t === 'dark';
      return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    },
    // One click toggles light and dark (the moon button); Settings > Preferences keeps
    // System and the color schemes.
    toggleTheme() { this.theme = this.isDark() ? 'light' : 'dark'; },
    openPalette() {
      this.opener = document.activeElement;
      this.q = ''; this.idx = 0; this.found = { roles: [], companies: [] };
      this.palette = true; this.more = false;
      this.render();
      this.$nextTick(() => {
        const pq = this.$refs.pq;
        if (!pq) return;
        pq.focus();
        pq.setSelectionRange(pq.value.length, pq.value.length);  // after any early keys
      });
    },
    recent() {
      try { return JSON.parse(localStorage.getItem('kestrel-palette-recent') || '[]') || []; }
      catch (e) { return []; }
    },
    remember(item) {
      const keep = {g: item.g, t: item.t, s: item.s, ic: item.ic, href: item.href, id: item.id};
      const list = [keep].concat(this.recent().filter(r => r.t !== item.t)).slice(0, 4);
      try { localStorage.setItem('kestrel-palette-recent', JSON.stringify(list)); } catch (e) {}
    },
    render() {
      this.view = window.KPalette.results(this.q, {actions: this.actions, pages: this.pages,
        roles: this.found.roles, companies: this.found.companies, recent: this.recent(),
        dark: this.isDark()});
      if (this.idx >= this.view.count) this.idx = 0;
    },
    // The CSP build of Alpine has no x-html and no statement lists (D-SEC-58): the palette sets
    // its own markup (icon() and hl() escape what they are given, as they did for x-html).
    paint(el, html) { if (el.innerHTML !== html) el.innerHTML = html; },
    restart() { this.idx = 0; this.search(); },
    hover(row) { if (!row.h) this.idx = row.n; },
    search() {
      this.render();
      clearTimeout(this.timer);
      const q = this.q.trim();
      if (q.length < 2) { this.found = { roles: [], companies: [] }; this.render(); return; }
      this.timer = setTimeout(() => {
        fetch('/palette/search?q=' + encodeURIComponent(q), {credentials: 'same-origin'})
          .then(r => r.ok ? r.json() : {roles: [], companies: []})
          .then(d => { if (this.q.trim() === q) { this.found = d; this.render(); } })
          .catch(() => {});
      }, 120);
    },
    move(d) {
      const n = this.view.count;
      if (!n) return;
      this.idx = (this.idx + d + n) % n;
      this.$nextTick(() => {
        const el = document.getElementById('palette-opt-' + this.idx);
        if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
      });
    },
    hl(t) { return window.KPalette.highlight(t, this.q); },
    icon(name) { return window.KPalette.icon(name); },
    go(item) {
      if (!item) return;
      this.remember(item);
      this.palette = false;
      if (item.id === 'dark') {
        const was = this.isDark();
        this.toggleTheme();
        if (window.KUI) window.KUI.toast(was ? 'Dark mode turned off' : 'Dark mode turned on');
        return;
      }
      if (item.id === 'pause') {
        fetch('/api/worker/pause', {method: 'POST', credentials: 'same-origin'})
          .then(r => { if (window.KUI) window.KUI.toast(r.ok ? 'Searching paused. Resume it on Runs.' : 'Could not pause. Nothing changed.', {tone: r.ok ? '' : 'warn'}); })
          .catch(() => {});
        return;
      }
      if (CHROME.desktop && item.id === 'quit') { if (window.kestrelQuit) window.kestrelQuit.open(); return; }
      if (item.href) {
        if (window.kLeaveGuard && !window.kLeaveGuard()) return;
        if (window.KUI && window.KUI.go) window.KUI.go(item.href, {guarded: true}); else location.href = item.href;
      }
    },
    setTrack(v) {
      try { localStorage.setItem('kestrel-track', v); } catch (e) {}
      document.cookie = 'kestrel-track=' + v + '; path=/; max-age=31536000; samesite=lax';
    },
  };
}
// The bell badge (#95): the server sends the true unread count in HX-Trigger-After-Swap, so the
// button's aria-label carries it even when the badge shows "9+", and a changed label ticks once.
document.body.addEventListener('bellCount', function (e) {
  var n = Number(e.detail && e.detail.value) || 0;
  var btn = document.querySelector('[data-testid="bell"]');
  var badge = document.getElementById('bellcount');
  if (btn) btn.setAttribute('aria-label', n ? 'Notifications, ' + n + ' unread' : 'Notifications');
  if (!badge) return;
  var label = badge.textContent, last = badge.dataset.last;
  // Only a real change after load moves it: a badge that appears replays its entry, a count
  // that changes ticks once. Both classes clear on animationend, so the next change replays.
  var cls = last === label || !label ? '' : (last === '' ? 'is-in' : 'is-tick');
  if (cls) {
    badge.classList.remove('is-in', 'is-tick'); void badge.offsetWidth; badge.classList.add(cls);
  }
  badge.dataset.last = label;
});
(function () {
  var badge = document.getElementById('bellcount');
  if (badge) badge.addEventListener('animationend', function () {
    badge.classList.remove('is-in', 'is-tick');
  });
})();
// The bell panel: opens under the bell over a scrim.
function bellPanel() {
  return {
    open: false,
    toggle() { this.open ? this.close() : (this.open = true); },
    close() { if (this.open) { this.open = false; if (this.$refs.bell) this.$refs.bell.focus(); } },
  };
}
// The names a current employer's section hides (partials/doc_section_edit.html).
function hideNames(names) {
  return {
    names: Array.isArray(names) ? names.slice() : [],
    adding: '',
    drop(n) { this.names = this.names.filter(function (x) { return x !== n; }); },
  };
}
document.addEventListener('alpine:init', function () {
  window.Alpine.data('kChrome', kChrome);
  window.Alpine.data('bellPanel', bellPanel);
  window.Alpine.data('hideNames', hideNames);
});
