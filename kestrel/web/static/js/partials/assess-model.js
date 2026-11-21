/* Moved out of templates/partials/assess_model.html for the Content-Security-Policy (D-INST-8). Loaded where the
   inline script was, so it runs at the same point; inside an htmx swap it runs as the swap lands. */
(function () {
  const dlg = document.getElementById('assess-model');
  if (!dlg) return;
  const opener = document.getElementById('assess-btn');
  // The picker is a combobox (D-UI-69): its hidden input carries the value; a plain <select>
  // is the fallback when the script did not load. The label targets the text input.
  const sel = dlg.querySelector('.ke-combo input[type="hidden"]') || dlg.querySelector('select');
  const pickBox = dlg.querySelector('.ke-combo [role="combobox"]') || sel;
  if (pickBox) pickBox.id = 'assess-model-pick';
  function focusables() {
    return Array.from(dlg.querySelectorAll('button, [href], input, select, textarea,'
      + ' [tabindex]:not([tabindex="-1"])')).filter((el) => !el.disabled);
  }
  function close() {
    if (dlg.open) dlg.close();
    dlg.remove();
    if (opener && opener.focus) opener.focus();
  }
  dlg.querySelectorAll('[data-assess-cancel]').forEach((b) => b.addEventListener('click', close));
  // A run closes the popup; the result lands in #assessment below the buttons.
  dlg.querySelectorAll('form[data-assess-run]').forEach((f) => {
    f.addEventListener('submit', (e) => {
      const pick = f.querySelector('.ke-combo input[type="hidden"]') || f.querySelector('select');
      if (pick && !pick.value) {
        e.preventDefault(); e.stopImmediatePropagation();
        const box = f.querySelector('.ke-combo [role="combobox"]') || pick;
        if (box.focus) box.focus();
      }
    }, true);
    // The popup closes as the run starts, so the "running" state lives outside it: a panel in
    // #assessment naming the model with an elapsed timer, and the Assess button disabled and
    // reading "Assessing…" until the reply lands (operator: "I have no idea if it's running").
    f.addEventListener('htmx:beforeRequest', () => {
      const box = f.querySelector('.ke-combo [role="combobox"]');
      const nat = f.querySelector('select');
      const name = box ? box.value : (nat ? (nat.selectedOptions[0] || {}).textContent
                                          : f.dataset.currentLabel);
      const out = document.getElementById('assessment');
      if (out) {
        const label = name ? ' with ' + name.trim().split(' (')[0] : '';
        const mk = (tag, cls) => { const n = document.createElement(tag);
                                   if (cls) n.className = cls; return n; };
        const panel = mk('div', 'assess-running');
        panel.setAttribute('role', 'status');
        panel.setAttribute('aria-live', 'polite');
        panel.dataset.testid = 'assess-running';
        const spin = mk('span', 'assess-spinner');
        spin.setAttribute('aria-hidden', 'true');
        const body = mk('span');
        const bold = mk('b');
        bold.textContent = 'Assessing' + label + '.';
        const muted = mk('span', 'muted');
        const elapsed = mk('span', 'assess-elapsed');
        elapsed.textContent = '0s';
        muted.append('Running for ', elapsed, '. Free and local models can take a few minutes;'
          + ' the result appears here.');
        body.append(bold, ' ', muted);
        panel.append(spin, body);
        out.replaceChildren(panel);
        const t0 = Date.now(), el = out.querySelector('.assess-elapsed');
        const timer = setInterval(() => {
          if (!document.body.contains(el)) { clearInterval(timer); return; }
          const s = Math.round((Date.now() - t0) / 1000);
          el.textContent = s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's';
        }, 1000);
      }
      if (opener) { opener.disabled = true; opener.dataset.label = opener.textContent;
                    opener.textContent = 'Assessing…'; }
      if (dlg.open) dlg.close();
    });
    f.addEventListener('htmx:afterRequest', (e) => {
      if (opener) { opener.disabled = false;
                    if (opener.dataset.label) opener.textContent = opener.dataset.label; }
      const out = document.getElementById('assessment');
      if (out && !e.detail.successful) {
        const warn = document.createElement('div');
        warn.className = 'warnbox';
        warn.setAttribute('role', 'alert');
        warn.textContent = 'The assessment did not finish ('
          + (e.detail.xhr ? e.detail.xhr.status : 'no reply') + '). Try again, or pick another'
          + ' model.';
        out.replaceChildren(warn);
      }
      dlg.remove();
    });
  });
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
  dlg.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key === 'Tab') {
      const els = focusables();
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  dlg.addEventListener('click', (e) => { if (e.target === dlg) close(); });
  if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  const first = focusables()[0];
  if (first) first.focus();
})();
