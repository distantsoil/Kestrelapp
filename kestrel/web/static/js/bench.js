/* Model check (D-UI-105): the page's one piece of start-up. Its model pickers (Advanced) are
   upgraded to comboboxes by the shared static/js/model_combo.js, which the picker macro loads
   inside the markup and which upgrades what it finds as it runs. Arriving in place (D-UI-210) the
   markup comes without that script having run, so mount asks the shared script to look through
   the page. Everything else on the page is htmx (the run, the 2 s progress poll, Cancel) and goes
   with its elements. The page runs on the lifecycle (KUI.page): this file only defines it. */
(function (root) {
  'use strict';
  if (!root.document || !root.KUI || !root.KUI.page) return;

  root.KUI.page.define('bench', {
    mount: function (main) {
      if (root.keCombo) root.keCombo.scan(main);
    }
  });
})(window);
