/* Moved out of templates/partials/quit_confirm.html for the Content-Security-Policy (D-INST-8). Loaded where the
   inline script was, so it runs at the same point; inside an htmx swap it runs as the swap lands. */
(function () {
  const dlg = document.getElementById('quit-confirm');
  if (!dlg) return;
  const go = dlg.querySelector('[data-quit-confirm]');
  const cancelBtn = dlg.querySelector('[data-quit-cancel]');
  let opener = null;

  function focusables() {
    return Array.from(dlg.querySelectorAll('button, [href], input, select, textarea,'
      + ' [tabindex]:not([tabindex="-1"])')).filter((el) => !el.disabled);
  }
  function close() {
    if (dlg.open) dlg.close();
    if (opener && opener.focus) opener.focus();
  }
  function quit() {
    if (dlg.open) dlg.close();
    // The closing screen (D-UI-90): the mark, "Closing Kestrel" and the reassurance line,
    // over the app, which then scales down and fades as the window goes.
    if (window.KUI && window.KUI.frame) window.KUI.frame.closing();
    fetch('/api/quit', {method: 'POST'});
  }
  function open(from) {
    opener = from || document.activeElement;
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    go.focus();
  }
  go.addEventListener('click', quit);
  cancelBtn.addEventListener('click', close);
  // Esc: the dialog's own cancel event; keep the focus return in one place.
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
  dlg.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key === 'Enter') {
      // Enter on Cancel cancels; anywhere else in the dialog it confirms.
      e.preventDefault();
      if (e.target === cancelBtn) close(); else quit();
      return;
    }
    if (e.key === 'Tab') {
      const els = focusables();
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  // A click on the backdrop (the dialog itself, outside the inner box) cancels, like Esc.
  dlg.addEventListener('click', (e) => { if (e.target === dlg) close(); });
  window.kestrelQuit = {open: open, close: close, quit: quit};
})();
