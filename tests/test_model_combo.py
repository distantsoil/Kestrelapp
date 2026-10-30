"""The model picker combobox (D-UI-69) and the operator's words for a rule adjustment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from kestrel import paths
from kestrel.web.deps import templates
from kestrel.web.fit import forced_words


@dataclass
class _M:
    id: str
    name: str
    free: bool = True
    context_length: int | None = 32000
    price: str = "free"
    mode_label: str = "structured"


_HOSTED = [
    _M("vendor/alpha", "Alpha", free=True),
    _M("vendor/beta", "vendor/beta", free=False, price="$1/$2 per M in/out"),
    _M("vendor/small", "Small", context_length=4000),
]


def _render(**kw) -> str:
    src = (
        '{% from "partials/model_picker.html" import model_select with context %}'
        "{{ model_select(**kw) }}"
    )
    return templates().env.from_string(src).render(kw=kw)


def _args(**over):
    base = dict(
        name="model", models=_HOSTED, reasons={"vendor/small": "context too small"},
        local_models=["kestrel-tier1"], hosted_label="Hosted (Test)", value_prefix="hosted:",
        testid="pick-x",
    )
    base.update(over)
    return base


def test_combobox_markup_roles_and_contract():
    html = _render(**_args())
    inp = re.search(r"<input[^>]*role=\"combobox\"[^>]*>", html).group(0)
    assert 'aria-expanded="false"' in inp and 'aria-controls="pick-x-combo-list"' in inp
    assert 'aria-autocomplete="list"' in inp and 'type="text"' in inp
    assert 'role="listbox"' in html and 'id="pick-x-combo-list"' in html
    assert html.count('role="option"') == 5  # placeholder + 1 local + 3 hosted
    assert html.count('role="group"') == 2
    assert "Local (Ollama)" in html and "Hosted (Test)" in html
    # The hidden input carries the submitted name, and hosted values keep their prefix.
    hidden = re.search(r"<input type=\"hidden\"[^>]*>", html).group(0)
    assert 'name="model"' in hidden
    assert 'data-value="hosted:vendor/alpha"' in html and 'data-value="kestrel-tier1"' in html
    # The native select is the fallback until the script upgrades the picker: it sits before the
    # combobox, and the hidden input starts disabled so the two never both submit.
    assert "<noscript>" not in html and 'data-combo-native' in html
    assert html.index('<select name="model"') < html.index("data-combo hidden")
    assert re.search(r"<input type=\"hidden\"[^>]*\bdisabled\b", html)
    assert 'value="hosted:vendor/alpha"' in html and '<optgroup label="Local (Ollama)">' in html


def test_blocked_models_stay_visible_disabled_with_reason_and_paid_label_kept():
    html = _render(**_args())
    opt = re.search(r"<div role=\"option\"[^>]*data-value=\"hosted:vendor/small\"[^>]*>[^<]*", html)
    assert 'aria-disabled="true"' in opt.group(0) and 'title="context too small"' in opt.group(0)
    assert "unavailable: context too small" in opt.group(0)
    assert "$1/$2 per M in/out" in html and "free · structured" in html
    # Paid options keep the Alpine free-only binding.
    beta = re.search(r"<div role=\"option\"[^>]*hosted:vendor/beta[^>]*>", html).group(0)
    assert "freeOnly" in beta and 'data-free="0"' in beta


def test_selected_value_and_label_and_xmodel_land_on_hidden_input():
    html = _render(**_args(selected="vendor/alpha", xmodel="picked"))
    hidden = re.search(r"<input type=\"hidden\"[^>]*>", html).group(0)
    assert 'value="hosted:vendor/alpha"' in hidden and 'x-model="picked"' in hidden
    combo = re.search(r"<input[^>]*role=\"combobox\"[^>]*>", html).group(0)
    assert 'value="Alpha (vendor/alpha)"' in combo
    assert 'aria-selected="true"' in html


def test_script_and_css_exist_and_use_theme_tokens_only():
    js = Path(paths.web_static_dir()) / "js" / "model_combo.js"
    text = js.read_text("utf-8")
    for needle in ("ArrowDown", "Escape", "Enter", "aria-activedescendant", "aria-expanded"):
        assert needle in text
    # Pages that load shell.css instead of kestrel.css (Models, Model check) need the rules too.
    for sheet in ("kestrel.css", "components.css"):
        css = (Path(paths.web_static_dir()) / "css" / sheet).read_text("utf-8")
        block = css.split("the model combobox (D-UI-69)")[1].split("\n\n")[0]
        assert ".ke-combo-list" in block and "focus-visible" in block, sheet
        assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), sheet
        assert ".model-picker" in css and ".ke-native[hidden]" in css, sheet


def test_pages_that_use_the_combobox_link_a_sheet_that_styles_it():
    web = Path(paths.web_static_dir()).parent / "templates"
    for page in ("bench.html", "models.html"):
        text = (web / page).read_text("utf-8")
        assert "css/shell.css" in text and "kestrel.css" not in text, page
    # shell.css is what those pages load, so components.css (loaded by base.html) must carry it.
    base = (web / "base.html").read_text("utf-8")
    assert "css/components.css" in base


def test_existing_callers_render_the_combobox():
    root = Path(paths.web_templates_dir())
    for rel in ("partials/bench_advanced.html", "partials/model_overrides.html",
                "partials/assess_model.html", "partials/score_again_dialog.html"):
        assert "model_select(" in (root / rel).read_text("utf-8"), rel


def test_forced_words_known_ids():
    assert forced_words(None) == "" and forced_words("") == ""
    assert forced_words("blocker:clearance, clearance; legitimacy_suspect_cap") == (
        "Capped by your rules: needs a security clearance. Also capped because the posting "
        "looks suspect."
    )
    assert forced_words("legitimacy_reject") == (
        "Capped because the posting may not be a real opening."
    )
    assert "pay is below" in forced_words("blocker:comp")
    assert forced_words("reranker") == "Adjusted by your ranker."
    assert forced_words("low_confidence_forced_keep").startswith("The quick screen was unsure")


def test_forced_words_consulting_kinds_and_legitimacy_wording():
    out = forced_words("blocker:rate, conflict_of_interest, capacity; legitimacy_suspect_cap")
    assert out.startswith("Capped by your rules: the rate is below")
    assert "conflict with your other work" in out and "capacity" in out
    assert out.endswith("Also capped because the posting looks suspect.")
    assert "rules" not in forced_words("legitimacy_suspect_cap")
    for kind in ("duration", "sector", "geography"):
        assert "_" not in forced_words(f"blocker:{kind}") and kind not in forced_words(
            f"blocker:{kind}"
        ).split(": ")[0]


def test_forced_words_unknown_ids_are_humanized_never_raw():
    out = forced_words("blocker:visa_type; blocker:visa_type; mystery_rule")
    assert out == "Capped by your rules: visa type; mystery rule."
    assert "blocker" not in out and "_" not in out
    only = forced_words("mystery_rule")
    assert only == "Adjusted by a rule: mystery rule."
    assert "—" not in out and "—" not in only


def test_script_respects_caller_disabled_and_exposes_api():
    text = (Path(paths.web_static_dir()) / "js" / "model_combo.js").read_text("utf-8")
    assert 'hasAttribute("data-combo-disabled")' in text and "setDisabled" in text
    assert "kecombo" in text and "previousElementSibling" in text
    assert "this.value.disabled = false" not in text


def test_assess_popup_uses_the_combobox():
    class _P:
        def __init__(self, i):
            self.id, self.name, self.free = f"v/m{i}", f"M{i}", True
            self.context_length, self.price, self.mode_label = 32000, "free", "structured"

    html = templates().get_template("partials/assess_model.html").render(
        master_id=1, doc_word="resume", current_label="cur", hosted_models=[_P(1)],
        hosted_reasons={}, local_models=[], hosted_label="Test", hosted_prefix="")
    assert html.count('data-current-label="cur"') == 2
    assert "dlg.querySelector('select');" not in html.replace(
        "|| dlg.querySelector('select');", "")  # the select is only the fallback
    assert "assess-model-pick" in html and '.ke-combo input[type="hidden"]' in html
