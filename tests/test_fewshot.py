"""Few-shot selection: gated below min_labels, and diversified across the verdict range."""

from __future__ import annotations

from kestrel.llm.fewshot import FewShotExample, select_few_shot


def _ex(i: int, vec, verdict: str) -> FewShotExample:
    return FewShotExample(id=i, embedding=vec, verdict=verdict, holistic=3.0)


def test_below_min_labels_is_zero_shot():
    exs = [_ex(i, [1.0, 0.0], "apply") for i in range(5)]
    assert select_few_shot([1.0, 0.0], exs, k=3, min_labels=15) == []


def test_diversity_not_three_of_the_same_verdict():
    # 15 near-identical 'apply' examples plus one 'consider' and one 'pass'
    exs = [_ex(i, [1.0, 0.0], "apply") for i in range(15)]
    exs.append(_ex(100, [0.9, 0.1], "consider"))
    exs.append(_ex(101, [0.8, 0.2], "pass"))
    sel = select_few_shot([1.0, 0.0], exs, k=3, min_labels=15)
    assert len(sel) == 3
    verdicts = {e.verdict for e in sel}
    assert len(verdicts) >= 2  # not all 'apply' despite them being the most similar
    assert "consider" in verdicts and "pass" in verdicts


def test_single_verdict_pool_still_returns_k():
    exs = [_ex(i, [1.0, 0.0], "apply") for i in range(20)]
    sel = select_few_shot([1.0, 0.0], exs, k=3, min_labels=15)
    assert len(sel) == 3 and all(e.verdict == "apply" for e in sel)
