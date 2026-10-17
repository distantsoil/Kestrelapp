"""D-UI-54: one word list per track; both lists carry every key."""

from __future__ import annotations

from kestrel.web.words import CONSULTING, EMPLOYMENT, PLAIN, words


def test_both_tracks_name_every_thing_and_consulting_never_reads_as_a_job_search():
    assert set(EMPLOYMENT) == set(CONSULTING)
    assert words("consulting")["add_to_list"] == "Add to my leads"
    assert words("employment")["add_to_list"] == "Add to my shortlist"
    assert words(None) is EMPLOYMENT
    for key in ("add_to_list", "board", "applied_q", "doc", "tailor"):
        assert "resume" not in CONSULTING[key].lower() and "apply" not in CONSULTING[key].lower()


def test_plain_labels_have_no_jargon():
    for label in PLAIN.values():
        assert "tier" not in label.lower() and "quarantine" not in label.lower()
