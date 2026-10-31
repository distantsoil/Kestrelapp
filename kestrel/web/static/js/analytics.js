/* Analytics page behavior (D-UI-101): the hover tip for chart marks, the group tabs of "Where
   roles come from", and closing the Export menu. Delegated on the document, so it keeps working
   when HTMX swaps the page body. No network, no storage. The page runs on the lifecycle
   (KUI.page, D-UI-210): this file only defines it, and mount starts every listener and timer
   through its context, so leaving stops all of them and coming back starts one set. */
(function () {
  "use strict";
  if (!window.KUI || !window.KUI.page) return;
  // Per visit: mount starts each of these afresh. cx is the mounted page's context (KUI.page).
  var tip = null;
  var cx = null;

  function tipEl() {
    return tip && tip.isConnected ? tip : (tip = document.getElementById("an-tip"));
  }

  function hideTip() {
    var t = tipEl();
    if (t) t.classList.remove("is-on");
  }

  function onMouseMove(e) {
    var t = tipEl();
    if (!t) return;
    var host = e.target.closest ? e.target.closest("[data-tip]") : null;
    if (!host || !host.closest(".an-page")) { hideTip(); return; }
    t.textContent = host.getAttribute("data-tip");
    t.style.left = Math.min(e.clientX + 14, window.innerWidth - 280) + "px";
    t.style.top = e.clientY + 14 + "px";
    t.classList.add("is-on");
  }

  function onClick(e) {
    var tab = e.target.closest ? e.target.closest("#an-tabs [data-tab]") : null;
    if (tab) {
      var card = tab.closest(".an-card");
      card.querySelectorAll("#an-tabs [data-tab]").forEach(function (b) {
        var on = b === tab;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      card.querySelectorAll("[data-panel]").forEach(function (p) {
        p.hidden = p.getAttribute("data-panel") !== tab.getAttribute("data-tab");
      });
      return;
    }
    var menu = document.getElementById("an-export");
    if (menu && menu.open && !e.target.closest("#an-export")) menu.open = false;
    if (menu && menu.open && e.target.closest(".an-menu a")) {
      cx.after(0, function () { menu.open = false; });
    }
  }

  // The ask-first dialog (Rewrite summary on an online model) opens when HTMX swaps it in.
  function onAfterSwap(e) {
    if (e.target && e.target.id === "an-dialog") {
      var dlg = e.target.querySelector(".k-modal");
      if (dlg && window.KUI) window.KUI.openModal(dlg);
    }
  }

  function onKeyDown(e) {
    var menu = document.getElementById("an-export");
    if (e.key === "Escape" && menu && menu.open) {
      menu.open = false;
      var s = menu.querySelector("summary");
      if (s) s.focus();
    }
  }

  window.KUI.page.define("analytics", {
    mount: function (main, ctx) {
      cx = ctx;
      tip = null;
      ctx.on(document, "mousemove", onMouseMove);
      ctx.on(document, "scroll", hideTip, true);
      ctx.on(document, "click", onClick);
      ctx.on(document, "htmx:afterSwap", onAfterSwap);
      ctx.on(document, "keydown", onKeyDown);
      ctx.onUnmount(function () {
        hideTip();
        tip = null;
        if (cx === ctx) cx = null;
      });
    }
  });
})();
