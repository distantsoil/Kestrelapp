"""Setup wizard fixes: Suggest vs Suggest again, commas inside a place, Remote implies remote
roles, and the chip separator in the shipped script."""

from __future__ import annotations

from pathlib import Path

from kestrel.services import profile_edit as pe
from kestrel.web.deps import templates

_STATIC = Path(__file__).resolve().parent.parent / "kestrel" / "web" / "static" / "js"


def _render(**ctx) -> str:
    base = {"suggestions": [], "pending": False, "has_resume": True, "last": None,
            "note": None, "route": None, "version": "v"}
    base.update(ctx)
    return templates().get_template("partials/profile_title_suggestions.html").render(
        **base)


def test_button_says_suggest_until_a_pass_has_been_asked_for():
    assert ">Suggest</button>" in _render()
    assert "Suggest again" not in _render()


def test_button_says_suggest_again_after_a_pass():
    s = [{"id": 1, "title": "Analyst", "why": "x", "evidence": []}]
    assert "Suggest again" in _render(suggestions=s)
    assert "Suggest again" in _render(last={"at": "2026-01-01T00:00:00Z"})
    assert "Suggest again" in _render(pending=True)


def test_a_city_and_state_stay_one_place():
    out = pe.parse_metros("Austin, TX\nDallas, TX")
    assert [m["name"] for m in out] == ["Austin, TX", "Dallas, TX"]
    assert out[0]["aliases"] == ["Austin, TX"]


def test_chip_script_splits_on_semicolons_not_commas():
    js = (_STATIC / "profile.js").read_text(encoding="utf-8")
    assert "e.key === ';'" in js and "e.key === ','" not in js
    assert "split(/[;\\n]+/)" in js


def test_remote_work_style_implies_remote_roles():
    from kestrel.web.pages import onboarding as ob

    class F(dict):
        pass

    assert (ob._on(F(remote_ok="on"), "remote_ok") or ob._on(F(mode_remote="on"), "mode_remote"))
    src = (Path(ob.__file__)).read_text(encoding="utf-8")
    assert '_on(form, "remote_ok") or _on(form, "mode_remote")' in src
    js = (_STATIC / "onboarding.js").read_text(encoding="utf-8")
    assert "m.name !== 'mode_remote' || !m.checked" in js


def test_hybrid_work_style_implies_hybrid_requires_metro():
    from kestrel.web.pages import onboarding as ob

    src = Path(ob.__file__).read_text(encoding="utf-8")
    assert '_on(form, "mode_hybrid") and "hybrid_requires_metro" not in form' in src
    assert 'posted["hybrid_requires_metro"] = "on"' in src
