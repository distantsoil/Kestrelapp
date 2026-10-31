/* The model picker combobox (D-UI-69). Progressive enhancement of the markup in
   templates/partials/model_picker.html: one text input, a listbox right under it, a hidden
   input that carries the submitted value. Without script the <noscript> <select> submits.

   Shared by several pages and safe to load more than once (D-UI-210): the picker macro emits this
   script after every picker, and a page swapped in place may emit it again. Only the first load
   defines window.keCombo and its one htmx:load listener; every load, and every swap, then scans
   for pickers not yet bound. A picker is bound once per element, tracked in a WeakSet rather than
   by its markup, so a picker copied from saved HTML (data-ready and all) is still upgraded and a
   second scan never doubles its listeners. The listeners live on the picker's own elements and
   go when the picker does; the page lifecycle never has to unbind them. */
(function () {
  "use strict";
  if (!window.keCombo) {
    var bound = typeof WeakSet === "function" ? new WeakSet() : null;
    var visible = function (o) {
      return !o.hidden && !o.classList.contains("is-filtered");
    };

    var Combo = function (root) {
      this.root = root;
      this.input = root.querySelector("[data-combo-input]");
      this.value = root.querySelector("[data-combo-value]");
      this.list = root.querySelector("[role=listbox]");
      this.empty = root.querySelector("[data-combo-empty]");
      this.options = Array.prototype.slice.call(root.querySelectorAll("[role=option]"));
      this.active = null;
      this.typed = false;
      // Upgrade: the native <select> (the fallback when this script never loads) steps aside.
      var native = root.previousElementSibling;
      if (native && native.hasAttribute("data-combo-native")) {
        Array.prototype.forEach.call(native.querySelectorAll("select"), function (s) {
          s.disabled = true;
        });
        native.hidden = true;
      }
      root.hidden = false;
      root.setAttribute("data-ready", "1");
      // A caller may have marked the picker inert (data-combo-disabled) before this ran.
      this.setDisabled(root.hasAttribute("data-combo-disabled"));
      var self = this;
      root.kecombo = {
        setDisabled: function (d) { self.setDisabled(d); },
        focus: function () { self.input.focus(); },
        get value() { return self.value.value; }
      };
      this.sync(this.value.value, false);
      this.bind();
    };

    // Inert pickers send nothing (the hidden input is disabled) and cannot be typed into.
    Combo.prototype.setDisabled = function (d) {
      d = !!d;
      this.root.toggleAttribute("data-combo-disabled", d);
      this.value.disabled = d;
      this.input.disabled = d;
      var btn = this.root.querySelector("[data-combo-toggle]");
      if (btn) { btn.disabled = d; }
      if (d && this.isOpen()) { this.close(true); }
    };

    Combo.prototype.byValue = function (v) {
      for (var i = 0; i < this.options.length; i++) {
        if (this.options[i].getAttribute("data-value") === v) { return this.options[i]; }
      }
      return null;
    };

    // Show the option for `v` in the input and mark it selected.
    Combo.prototype.sync = function (v, fire) {
      var opt = this.byValue(v);
      if (!opt) { opt = this.byValue(""); v = ""; }
      this.value.value = v;
      this.options.forEach(function (o) {
        o.setAttribute("aria-selected", o === opt ? "true" : "false");
      });
      this.input.value = v ? opt.getAttribute("data-label") : "";
      this.typed = false;
      if (fire) {
        this.value.dispatchEvent(new Event("input", { bubbles: true }));
        this.value.dispatchEvent(new Event("change", { bubbles: true }));
      }
    };

    Combo.prototype.isOpen = function () { return !this.list.hidden; };

    Combo.prototype.open = function () {
      this.filter();
      this.list.hidden = false;
      this.input.setAttribute("aria-expanded", "true");
      if (!this.active || !visible(this.active)) {
        this.setActive(this.byValueVisible(this.value.value) || this.firstEnabled());
      }
    };

    Combo.prototype.close = function (restore) {
      this.list.hidden = true;
      this.input.setAttribute("aria-expanded", "false");
      this.input.removeAttribute("aria-activedescendant");
      if (this.active) { this.active.classList.remove("is-active"); }
      this.active = null;
      if (restore) { this.sync(this.value.value, false); }
    };

    Combo.prototype.byValueVisible = function (v) {
      var o = v ? this.byValue(v) : null;
      return o && visible(o) && o.getAttribute("aria-disabled") !== "true" ? o : null;
    };

    Combo.prototype.enabled = function () {
      return this.options.filter(function (o) {
        return visible(o) && o.getAttribute("aria-disabled") !== "true";
      });
    };

    Combo.prototype.firstEnabled = function () { return this.enabled()[0] || null; };

    Combo.prototype.setActive = function (o) {
      if (this.active) { this.active.classList.remove("is-active"); }
      this.active = o;
      if (o) {
        o.classList.add("is-active");
        this.input.setAttribute("aria-activedescendant", o.id);
        if (o.scrollIntoView) { o.scrollIntoView({ block: "nearest" }); }
      } else {
        this.input.removeAttribute("aria-activedescendant");
      }
    };

    Combo.prototype.move = function (step) {
      var en = this.enabled();
      if (!en.length) { return; }
      var i = en.indexOf(this.active);
      if (step === "first") { i = 0; }
      else if (step === "last") { i = en.length - 1; }
      else { i = i < 0 ? (step > 0 ? 0 : en.length - 1) : (i + step + en.length) % en.length; }
      this.setActive(en[i]);
    };

    // Hide the options that do not match what was typed; hide a group heading with no options.
    Combo.prototype.filter = function () {
      var q = this.typed ? this.input.value.trim().toLowerCase() : "";
      var tokens = q ? q.split(/\s+/) : [];
      var any = false;
      this.options.forEach(function (o) {
        var isPlaceholder = o.getAttribute("data-value") === "";
        var text = (o.textContent + " " + o.getAttribute("data-value")).toLowerCase();
        var hit = isPlaceholder ? !tokens.length : tokens.every(function (t) {
          return text.indexOf(t) > -1;
        });
        o.classList.toggle("is-filtered", !hit);
      });
      Array.prototype.forEach.call(this.root.querySelectorAll("[role=group]"), function (g) {
        var some = Array.prototype.some.call(g.querySelectorAll("[role=option]"), visible);
        g.classList.toggle("is-filtered", !some);
        if (some) { any = true; }
      });
      if (!any && !tokens.length) { any = this.options.some(visible); }
      if (this.empty) { this.empty.hidden = any; }
      if (this.active && !visible(this.active)) { this.setActive(null); }
      if (tokens.length && !this.active) { this.setActive(this.firstEnabled()); }
    };

    Combo.prototype.choose = function (o) {
      if (!o || o.getAttribute("aria-disabled") === "true") { return; }
      this.sync(o.getAttribute("data-value"), true);
      this.close(false);
    };

    Combo.prototype.bind = function () {
      var self = this;
      this.input.addEventListener("focus", function () { self.input.select(); });
      this.input.addEventListener("click", function () {
        if (self.isOpen()) { return; }
        self.open();
      });
      this.input.addEventListener("input", function () {
        self.typed = true;
        self.open();
        self.filter();
      });
      this.input.addEventListener("keydown", function (e) {
        var k = e.key;
        if (k === "ArrowDown" || k === "ArrowUp") {
          e.preventDefault();
          if (!self.isOpen()) { self.open(); } else { self.move(k === "ArrowDown" ? 1 : -1); }
        } else if (k === "Home" && self.isOpen() && e.ctrlKey) {
          e.preventDefault(); self.move("first");
        } else if (k === "End" && self.isOpen() && e.ctrlKey) {
          e.preventDefault(); self.move("last");
        } else if (k === "Enter") {
          if (self.isOpen()) { e.preventDefault(); self.choose(self.active); }
        } else if (k === "Escape") {
          if (self.isOpen()) { e.preventDefault(); e.stopPropagation(); self.close(true); }
        } else if (k === "Tab") {
          self.close(true);
        }
      });
      this.input.addEventListener("blur", function () {
        // A click on an option keeps focus here (mousedown is cancelled below), so a blur is a
        // real leave: put the chosen model back in the box.
        self.close(true);
      });
      this.list.addEventListener("mousedown", function (e) { e.preventDefault(); });
      this.list.addEventListener("click", function (e) {
        var o = e.target.closest ? e.target.closest("[role=option]") : null;
        if (o) { self.choose(o); self.input.focus(); }
      });
      this.list.addEventListener("mousemove", function (e) {
        var o = e.target.closest ? e.target.closest("[role=option]") : null;
        if (o && o !== self.active && o.getAttribute("aria-disabled") !== "true") {
          self.setActive(o);
        }
      });
      var btn = this.root.querySelector("[data-combo-toggle]");
      if (btn) {
        btn.addEventListener("mousedown", function (e) { e.preventDefault(); });
        btn.addEventListener("click", function () {
          if (self.isOpen()) { self.close(true); } else { self.input.focus(); self.open(); }
        });
      }
    };

    // scan() looks through the whole document; scan(el) only through el (and el itself).
    window.keCombo = {
      scan: function (scope) {
        var host = scope && scope.querySelectorAll ? scope : document;
        var roots = Array.prototype.slice.call(host.querySelectorAll("[data-combo]"));
        if (host.matches && host.matches("[data-combo]")) { roots.unshift(host); }
        roots.forEach(function (r) {
          if (bound ? bound.has(r) : r.getAttribute("data-ready") === "1") { return; }
          if (bound) { bound.add(r); }
          new Combo(r);
        });
      }
    };
    document.addEventListener("htmx:load", function (e) {
      window.keCombo.scan(e && e.target && e.target.querySelectorAll ? e.target : document);
    });
  }
  window.keCombo.scan();
})();
