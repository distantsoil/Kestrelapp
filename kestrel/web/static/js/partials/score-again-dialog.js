/* Moved out of templates/partials/score_again_dialog.html for the Content-Security-Policy (D-INST-8). Loaded where the
   inline script was, so it runs at the same point; inside an htmx swap it runs as the swap lands. */
(function () {
  // The name of the usual model, from the template (no inline script, D-INST-8).
  const USUAL_NAME = document.currentScript.getAttribute('data-usual-name') || 'your usual model';
  const dlg = document.currentScript.previousElementSibling;
  if (!dlg || dlg.tagName !== 'DIALOG') return;
  // Every listener below belongs to this dialog and is tied to one controller kept on it
  // (D-UI-210): running this again on the same dialog replaces the set instead of doubling it,
  // and closing the dialog ends the set.
  if (dlg.kBound) dlg.kBound.abort();
  const ac = new AbortController();
  dlg.kBound = ac;
  const sig = { signal: ac.signal };
  const wrap = dlg.closest('.score-again');
  const opener = wrap ? wrap.querySelector('.score-again-btn') : null;
  const form = dlg.querySelector('form');
  // The model picker is a combobox (D-UI-69): its hidden input carries the value and fires
  // change on a pick; a plain <select> is the no-JS fallback.
  const sel = dlg.querySelector('.ke-combo input[type="hidden"]') || dlg.querySelector('select');
  const pickBox = dlg.querySelector('.ke-combo [role="combobox"]') || sel;
  const other = dlg.querySelector('input[value="other"]');
  const same = dlg.querySelector('input[value="same"]');
  function focusables() {
    return Array.from(dlg.querySelectorAll('button, [href], input, select, textarea,'
      + ' [tabindex]:not([tabindex="-1"])')).filter((el) => !el.disabled);
  }
  function close() {
    if (dlg.open) dlg.close();
    dlg.remove();
    ac.abort();
    if (opener && opener.focus) opener.focus();
  }
  const combo = dlg.querySelector('.ke-combo');
  function setPickDisabled(d) {
    if (!sel) return;
    if (combo && combo.kecombo) combo.kecombo.setDisabled(d);
    else { if (combo) combo.toggleAttribute('data-combo-disabled', d); sel.disabled = d; }
  }
  function syncPick() {
    setPickDisabled(!(other && other.checked));
  }
  syncPick();
  [same, other].forEach((r) => r && r.addEventListener('change', syncPick, sig));
  if (sel) sel.addEventListener('change', () => { if (sel.value && other) { other.checked = true; } }, sig);
  dlg.querySelectorAll('[data-score-again-cancel]').forEach((b) => b.addEventListener('click', close, sig));
  const mainStep = dlg.querySelector('[data-sf-main]');
  const confirmStep = dlg.querySelector('[data-sf-confirm]');
  const footMain = dlg.querySelector('[data-sf-foot-main]');
  const footConfirm = dlg.querySelector('[data-sf-foot-confirm]');
  const confirmed = dlg.querySelector('[data-sf-confirmed]');
  const prefix = form ? (form.getAttribute('data-hosted-prefix') || 'hosted:') : 'hosted:';
  const usualOnline = form && form.getAttribute('data-usual-online') === '1';
  function online() {
    if (usualOnline && same && same.checked) return true;  // usual models are online too
    return !!(other && other.checked && sel && sel.value.indexOf(prefix) === 0);
  }
  function showConfirm(on) {
    if (!confirmStep) return;
    confirmStep.hidden = !on; mainStep.hidden = on;
    footConfirm.hidden = !on; footMain.hidden = on;
    if (on) {
      const label = dlg.querySelector('.ke-combo [role="combobox"]');
      const name = dlg.querySelector('[data-sf-model]');
      if (name) name.textContent = (same && same.checked) ? USUAL_NAME : ((label && label.value) || (sel && sel.value) || '');
      const send = dlg.querySelector('[data-sf-send]');
      if (send) send.focus();
    }
    if (confirmed) confirmed.disabled = true;
  }
  const sendBtn = dlg.querySelector('[data-sf-send]');
  if (sendBtn) sendBtn.addEventListener('click', () => { if (confirmed) confirmed.disabled = false; }, sig);
  const backBtn = dlg.querySelector('[data-sf-back]');
  if (backBtn) backBtn.addEventListener('click', () => showConfirm(false), sig);
  if (form) {
    // "A different model" needs a pick; "Same model" sends model=same (a disabled select sends
    // nothing, so the hidden fallback below carries it). An online pick asks first: the first
    // press shows what is sent, and only "Send and score" posts confirmed=1.
    form.addEventListener('submit', (e) => {
      if (other && other.checked && sel && !sel.value) {
        e.preventDefault(); e.stopImmediatePropagation(); setPickDisabled(false); pickBox.focus();
      } else if (online() && confirmed && confirmed.disabled) {
        e.preventDefault(); e.stopImmediatePropagation(); showConfirm(true);
      }
    }, { capture: true, signal: ac.signal });
    form.addEventListener('htmx:configRequest', (e) => {
      if (!(other && other.checked)) e.detail.parameters['model'] = 'same';
    }, sig);
    form.addEventListener('htmx:beforeRequest', () => { if (dlg.open) dlg.close(); }, sig);
    form.addEventListener('htmx:afterRequest', () => { dlg.remove(); ac.abort(); }, sig);
  }
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); }, sig);
  dlg.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key === 'Tab') {
      const els = focusables();
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }, sig);
  dlg.addEventListener('click', (e) => { if (e.target === dlg) close(); }, sig);
  if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  const first = dlg.querySelector('input[name="model_choice"]:checked') || focusables()[0];
  if (first) first.focus();
})();
