"""The guard against a dead-on-arrival filters.yml (SPEC.md 6.1). simpleeval cannot evaluate
generator expressions and has no any/all; if a rule reintroduces one, or references an unknown
symbol, this test fails loudly instead of the shipped config failing on the operator's first run."""

from __future__ import annotations

import pytest

from kestrel import paths
from kestrel.config import get_settings
from kestrel.rules import helpers
from kestrel.rules.sample import sample_context


def test_packaged_filters_every_rule_evaluates():
    settings = get_settings()
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    ctx = sample_context()
    # Raises FilterConfigError naming the rule + symbol if any rule is invalid.
    helpers.check_filters(filters, settings=settings, **ctx)

    # sanity: both tracks and both kinds are actually present and non-empty
    rules = list(helpers.iter_rules(filters))
    tracks = {track for track, _, _ in rules}
    assert tracks == {"employment", "consulting"}
    assert len(rules) >= 25


def test_generator_expression_is_rejected():
    settings = get_settings()
    ctx = sample_context()
    bad = {
        "employment": {
            "hard_blockers": [
                {"id": "genexpr", "when": "any(t in opp.title for t in ['x'])", "reason": "x"}
            ]
        }
    }
    with pytest.raises(helpers.FilterConfigError):
        helpers.check_filters(bad, settings=settings, **ctx)


def test_genexpr_through_whitelisted_function_is_rejected():
    # simpleeval's EvalWithCompoundTypes WOULD evaluate this; the AST guard must reject it anyway,
    # so the ban does not depend on `any`/`all` being undefined.
    settings = get_settings()
    ctx = sample_context()
    bad = {
        "employment": {
            "soft_signals": [
                {"id": "g", "when": "max(len(t) for t in ['a', 'bb']) > 1", "weight": 1.0}
            ]
        }
    }
    with pytest.raises(helpers.FilterConfigError):
        helpers.check_filters(bad, settings=settings, **ctx)


def test_list_literal_is_allowed():
    # the flip side: list literals (used by source_kind_in) must NOT be rejected
    settings = get_settings()
    ctx = sample_context()
    ok = {
        "employment": {
            "soft_signals": [
                {
                    "id": "ok",
                    "when": "source_kind_in(source, ['greenhouse', 'lever'])",
                    "weight": 0.8,
                }
            ]
        }
    }
    helpers.check_filters(ok, settings=settings, **ctx)  # must not raise


def test_unknown_symbol_is_rejected():
    settings = get_settings()
    ctx = sample_context()
    bad = {"employment": {"soft_signals": [{"id": "b", "when": "frobnicate(opp)", "weight": 1.0}]}}
    with pytest.raises(helpers.FilterConfigError):
        helpers.check_filters(bad, settings=settings, **ctx)


def _find(filters, track, rule_id):
    for t, _, rule in helpers.iter_rules(filters):
        if t == track and rule.get("id") == rule_id:
            return rule
    raise AssertionError(f"rule {track}/{rule_id} not found")


def test_guards_honor_hard_rule_4_unknown_title_and_missing_dates_pass():
    """The exact hard-rule-4 traps (decision log): an ambiguous title and missing dates must
    NOT be rejected by the seniority/stale blockers."""
    settings = get_settings()
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    ctx = sample_context()
    opp = ctx["opp"]
    opp.title = "Chief Vibes Curator"  # ranks -1 (unknown)
    opp.date_posted = None
    opp.event_date = None
    ev = helpers.build_evaluator(settings=settings, **{**ctx, "opp": opp})

    assert ev.eval(_find(filters, "employment", "seniority_floor")["when"]) is False
    assert ev.eval(_find(filters, "employment", "stale")["when"]) is False
    assert ev.eval(_find(filters, "consulting", "stale_trigger")["when"]) is False
