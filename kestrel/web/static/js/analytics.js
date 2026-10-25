/* Analytics page behavior (D-UI-101): the hover tip for chart marks, the group tabs of "Where
   roles come from", and closing the Export menu. Delegated on the document, so it keeps working
   when HTMX swaps the page body. No network, no storage. */
(function () {
  "use strict";
  var tip = null;

  function tipEl() {
    return tip && tip.isConnected ? tip : (tip = document.getElementById("an-tip"));
  }

  function hideTip() {
    var t = tipEl();
    if (t) t.classList.remove("is-on");
  }

  document.addEventListener("mousemove", function (e) {
    var t = tipEl();
    if (!t) return;
    var host = e.target.closest ? e.target.closest("[data-tip]") : null;
    if (!host || !host.closest(".an-page")) { hideTip(); return; }
    t.textContent = host.getAttribute("data-tip");
    t.style.left = Math.min(e.clientX + 14, window.innerWidth - 280) + "px";
    t.style.top = e.clientY + 14 + "px";
    t.classList.add("is-on");
  });
  document.addEventListener("scroll", hideTip, true);

  document.addEventListener("click", function (e) {
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
      setTimeout(function () { menu.open = false; }, 0);
    }
  });

  // The ask-first dialog (Rewrite summary on an online model) opens when HTMX swaps it in.
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "an-dialog") {
      var dlg = e.target.querySelector(".k-modal");
      if (dlg && window.KUI) window.KUI.openModal(dlg);
    }
  });

  document.addEventListener("keydown", function (e) {
    var menu = document.getElementById("an-export");
    if (e.key === "Escape" && menu && menu.open) {
      menu.open = false;
      var s = menu.querySelector("summary");
      if (s) s.focus();
    }
  });
})();
