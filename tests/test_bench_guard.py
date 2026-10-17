"""Bench fitness guard (AFTERACTION 3.2).

The audit's finding was not a wrong number. It was a number READ AS IF it meant something it did
not: kestrel-tier1 is a fast triage model, and its 70% first-attempt structured-output rate on a
placeholder gold set was recorded in the build log as a tier-1 "quality/headroom" signal. The number
was fine; the framing made it a false claim. So the guard is about presentation, and these tests
pin the two conditions under which a bench number must not be offered as comparable.

Offline: no model is called. check_model_fitness inspects configuration, and the num_ctx probe is
driven through a stub client.
"""

from __future__ import annotations

from kestrel.bench.harness import check_model_fitness, off_tier_models
from kestrel.config import get_settings


class _StubClient:
    """Stands in for LLMClient.effective_num_ctx without touching Ollama."""

    def __init__(self, values: dict[str, int | None], raises: bool = False) -> None:
        self._values = values
        self._raises = raises

    def effective_num_ctx(self, model: str) -> int | None:
        if self._raises:
            raise RuntimeError("ollama unreachable")
        return self._values.get(model)


def test_the_tier1_triage_model_is_flagged_as_off_tier():
    """The exact model whose number was misread in the audit."""
    settings = get_settings()
    warnings = check_model_fitness(["kestrel-tier1"], settings)
    assert [w.kind for w in warnings] == ["off_tier"]
    assert "tier1" in warnings[0].detail
    assert "not comparable" in warnings[0].detail


def test_a_real_tier2_candidate_is_not_flagged():
    # A guard that flags everything is as useless as one that flags nothing.
    settings = get_settings()
    assert check_model_fitness(["kestrel-tier2"], settings) == []
    assert check_model_fitness(["some-unconfigured-model"], settings) == []


def test_every_non_tier2_role_counts_as_off_tier():
    settings = get_settings()
    roles = off_tier_models(settings)
    assert "kestrel-tier2" not in roles, "the tier being measured must never be flagged"
    assert roles.get("kestrel-tier1") == "tier1"
    # Derived from config rather than hardcoded, so adding a role (Wave 1C added `frontier`) does
    # not silently fall outside the guard and does not break this test either.
    configured = set(settings.llm.tiers) - {"tier2", "embed"}
    assert set(roles.values()) <= configured
    assert roles, "no off-tier models resolved; the guard would flag nothing"


def test_an_under_provisioned_model_is_flagged():
    """A num_ctx below tier 2's budget truncates the input silently, so the evaluation is WRONG
    rather than merely worse. That is the most likely cause of "runs fine, results are bad"."""
    from kestrel.doctor import required_num_ctx

    settings = get_settings()
    required = required_num_ctx("tier2", settings)
    client = _StubClient({"tiny-model": required - 1})
    warnings = check_model_fitness(["tiny-model"], settings, client=client)
    assert [w.kind for w in warnings] == ["under_provisioned"]
    assert str(required) in warnings[0].detail


def test_a_sufficiently_provisioned_model_passes():
    from kestrel.doctor import required_num_ctx

    settings = get_settings()
    client = _StubClient({"big-model": required_num_ctx("tier2", settings) + 1})
    assert check_model_fitness(["big-model"], settings, client=client) == []


def test_a_model_with_no_explicit_num_ctx_is_flagged():
    # Ollama silently applies the model default, far below what tier 2 sends, with no error.
    settings = get_settings()
    client = _StubClient({"defaulted": None})
    warnings = check_model_fitness(["defaulted"], settings, client=client)
    assert [w.kind for w in warnings] == ["unknown_context"]
    assert "PARAMETER num_ctx" in warnings[0].detail


def test_an_unreadable_num_ctx_is_reported_not_raised():
    # The bench must still run and still report; it just must not claim the model is fit.
    settings = get_settings()
    warnings = check_model_fitness(["x"], settings, client=_StubClient({}, raises=True))
    assert [w.kind for w in warnings] == ["unknown_context"]
    assert "could not read" in warnings[0].detail


def test_off_tier_and_under_provisioned_are_reported_together():
    from kestrel.doctor import required_num_ctx

    settings = get_settings()
    client = _StubClient({"kestrel-tier1": required_num_ctx("tier2", settings) - 1})
    kinds = {w.kind for w in check_model_fitness(["kestrel-tier1"], settings, client=client)}
    assert kinds == {"off_tier", "under_provisioned"}


def test_the_probe_client_can_actually_read_num_ctx():
    """The guard was first wired with LLMClient, which talks to the OpenAI-compatible /v1 endpoint
    and cannot report num_ctx at all. Every model then came back "could not read num_ctx", so the
    real check never ran AND healthy tier-2 candidates were flagged. A guard that cries wolf on
    good models trains the operator to ignore it, which is worse than having no guard.

    Offline: only checks the client type exposes the method, never calls Ollama.
    """
    from kestrel.cli import _ollama_probe_client

    client = _ollama_probe_client(get_settings())
    assert hasattr(client, "effective_num_ctx"), (
        "the bench probe client cannot read num_ctx, so the under-provisioned check is dead"
    )
