/* The error page (D-UI-210 lifecycle): Show details toggles the technical details, Copy puts
   them on the clipboard. A page script file rather than an inline one, so an in-place page
   change loads it like every other page's. */
(function (root) {
  'use strict';
  if (root.KUI && root.KUI.page) {
    root.KUI.page.define('error', {
      mount: function (main, ctx) {
        var toggle = main.querySelector('#sf-show-details');
        var panel = main.querySelector('#sf-details');
        var copy = main.querySelector('#sf-copy');
        if (!toggle || !panel || !copy) return;
        ctx.on(toggle, 'click', function () {
          panel.hidden = !panel.hidden;
          toggle.setAttribute('aria-expanded', String(!panel.hidden));
          toggle.textContent = panel.hidden ? 'Show details' : 'Hide details';
        });
        ctx.on(copy, 'click', function () {
          var text = main.querySelector('#sf-code').textContent;
          var done = function () {
            if (root.KUI) root.KUI.toast('Details copied.');
          };
          if (root.navigator.clipboard && root.navigator.clipboard.writeText) {
            root.navigator.clipboard.writeText(text).then(done, function () {});
          }
        });
      }
    });
  }
})(window);
