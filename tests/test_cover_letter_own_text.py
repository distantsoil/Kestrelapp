"""D-LETTER-10: a letter is checked against everything the prompt carried that is the person's
own (proof points, resume lines, headline, narrative), and the employer's name is never an
unverified claim. A name or number from nowhere is still marked."""

from __future__ import annotations

import asyncio

import pytest

from kestrel.pipeline import profile as profile_mod
from tests import role_seed
from tests.letter_helpers import generate
from tests.test_cover_letter_draft import GEN, Model, _client, _go_online

pytestmark = pytest.mark.usefixtures("_isolate_data_home")



@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


ALERT = 'data-testid="cover-letter-unverified"'


def _with_narrative(monkeypatch, narrative: str) -> None:
    real = profile_mod.load_profile

    def patched(track):
        p = real(track)
        p.narrative = narrative
        return p

    monkeypatch.setattr(profile_mod, "load_profile", patched)


def test_the_employers_name_in_a_first_person_sentence_is_not_marked(ids, monkeypatch):
    # The seeded strong role is at Larkspur Logistics.
    Model(monkeypatch, body="Dear Hiring Team,\n\nI am excited to bring weekly reporting to "
          "Larkspur Logistics.\n\nI would like a short call to walk through the forecast.")
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert ALERT not in r.text


def test_another_company_the_person_never_named_is_still_marked(ids, monkeypatch):
    Model(monkeypatch, body="Dear Hiring Team,\n\nI led weekly reporting at Quillfeather "
          "Mills.\n\nI would like a short call to walk through the forecast.")
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert ALERT in r.text and "Quillfeather Mills" in r.text


BODY_YEARS = ("Dear Hiring Team,\n\nI bring nineteen years of warehouse reporting.\n\n"
              "I would like a short call to walk through the forecast.")


def test_a_length_of_time_from_the_persons_narrative_is_not_marked(ids, monkeypatch):
    _with_narrative(monkeypatch, "Nineteen years in warehouse reporting and inventory control.")
    Model(monkeypatch, body=BODY_YEARS)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert ALERT not in r.text


def test_the_same_length_of_time_with_no_narrative_is_marked(ids, monkeypatch):
    _with_narrative(monkeypatch, "")
    Model(monkeypatch, body=BODY_YEARS)
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert ALERT in r.text


def test_a_past_job_claimed_at_the_employer_is_still_marked(ids, monkeypatch):
    Model(monkeypatch, body="Dear Hiring Team,\n\nI spent a long stretch running weekly "
          "reporting at Larkspur Logistics.\n\nI would like a short call to walk through the "
          "forecast.")
    with _client() as c:
        r = generate(c, GEN.format(ids["strong"]), {})
    assert ALERT in r.text


def test_a_number_from_the_narrative_does_not_trigger_the_re_ask(ids, monkeypatch):
    # The draft path grounds on the narrative too: a number mark there would ask the model again
    # and could delete a true fact.
    _with_narrative(monkeypatch, "Nineteen years in warehouse reporting and inventory control.")
    model = Model(monkeypatch, body=BODY_YEARS)
    with _client() as c:
        generate(c, GEN.format(ids["strong"]), {})
    assert model.calls == 1


def test_a_later_narrative_edit_never_clears_a_mark_on_an_old_letter(ids, monkeypatch):
    _with_narrative(monkeypatch, "")
    Model(monkeypatch, body=BODY_YEARS)
    with _client() as c:
        first = generate(c, GEN.format(ids["strong"]), {})
        assert ALERT in first.text
        _with_narrative(monkeypatch, "Nineteen years in warehouse reporting.")
        again = generate(c, GEN.format(ids["strong"]), {})  # the saved draft, checked again
    assert ALERT in again.text


def test_the_ask_first_panel_says_the_headline_and_narrative_are_sent(ids):
    _go_online()
    with _client() as c:
        html = c.get(f"/opportunities/{ids['strong']}/role/cover-letter").text
    assert "your headline and narrative" in html
