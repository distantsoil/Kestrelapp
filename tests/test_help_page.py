"""The Help page (D-UI-124, SPEC-INTERFACES 2.1 /help, locked mockup
the locked design mockup (kept privately)): search, topic chips, the four steps, 21 offline
articles read in the app, four helper cards, and the walkthrough control. Cloud-safe:
the page makes no network request and no test touches a model."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from kestrel.web.help_articles import TOPICS

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_TOPIC_TITLES = ["Getting started", "How scoring works", "Your data and privacy",
                 "Local and online models", "Troubleshooting"]


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _words(html: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", html).split())


def _articles():
    return [(t.key, a) for t in TOPICS for a in t.articles]


def _main(body: str) -> str:
    return body.split("<main")[1]


# ----------------------------------------------------------------------------- the content
def test_there_are_five_topics_and_twenty_one_articles():
    assert [t.title for t in TOPICS] == _TOPIC_TITLES
    assert len(_articles()) == 21
    keys = [f"{t}:{a.key}" for t, a in _articles()]
    assert len(set(keys)) == 21


def test_eight_articles_run_250_to_330_words_and_none_is_a_stub():
    counts = [_words(a.body) for _t, a in _articles()]
    long_ones = [n for n in counts if n >= 250]
    assert len(long_ones) >= 8, counts
    assert min(counts) >= 100, counts  # every article says something
    assert max(counts) <= 340, counts


def test_articles_stay_in_the_app_and_use_plain_copy():
    for topic, a in _articles():
        assert "<a " not in a.body and "http" not in a.body, (topic, a.key)
        assert "—" not in a.body and "–" not in a.body, (topic, a.key)
        assert "—" not in a.title, a.title
        assert set(re.findall(r"<(\w+)", a.body)) <= {"p", "h6", "ul", "ol", "li"}, a.key


def test_the_required_articles_exist():
    titles = {a.title for _t, a in _articles()}
    assert "Check that your models work" in titles  # the Health check article
    assert "Why is this greyed out?" in titles
    assert "The four steps" in titles


def test_the_boundaries_are_stated_in_the_first_article():
    what = " ".join(next(a for _t, a in _articles() if a.key == "what").body.split())
    assert "never applies to a job, sends an email or messages anyone" in what
    assert "does not search LinkedIn" in what and "pasted in by hand" in what


# ------------------------------------------------------------------------------ the page
def test_help_renders_search_chips_steps_topics_and_the_reader():
    with _client() as c:
        r = c.get("/help")
    assert r.status_code == 200
    body = r.text
    assert "<h1>Help</h1>" in body
    assert 'id="hp-q"' in body and 'aria-label="Search help"' in body
    assert "Search help, for example: online, resume, slow" in body
    chips = re.findall(r'<button[^>]*class="k-chip[^"]*"[^>]*data-topic="(\w+)"', body)
    assert chips == ["all", "start", "score", "privacy", "models", "fix"]
    for title in _TOPIC_TITLES:
        assert f">{title}<" in body, title
    # The four steps, each with its cost, the fourth optional.
    steps = body.split('data-testid="hp-steps"')[1].split("</details>")[0]
    for name, cost in (("Rules check", "Instant"), ("Quick screen", "Seconds each"),
                       ("Full review", "Tens of seconds each"),
                       ("Deeper read", "You confirm first")):
        assert name in steps and cost in steps, name
    assert len(re.findall(r'class="hp-step[ "]', steps)) == 4 and steps.count("is-optional") == 1
    assert 'data-help-article="score:steps"' in steps
    # Every article is on the page (offline), each reachable from a list row, with a reader.
    assert body.count('data-help-article="') >= 21
    for topic, a in _articles():
        assert f'id="hp-a-{topic}-{a.key}"' in body, (topic, a.key)
        assert f'data-help-article="{topic}:{a.key}"' in body, (topic, a.key)
    assert 'id="hp-reader"' in body and 'class="k-slideover"' in body
    assert 'id="hp-none"' in body and "No articles match." in body
    assert "—" not in _main(body)


def test_every_read_article_control_opens_an_article_that_exists():
    with _client() as c:
        body = c.get("/help").text
    ids = set(re.findall(r'id="hp-a-(\w+)-(\w+)"', body))
    wanted = set(re.findall(r'data-help-article="(\w+):(\w+)"', body))
    assert wanted and wanted <= ids, wanted - ids
    # Nothing on the page sends the reader to a website for an article.
    for href in re.findall(r'href="(https?://[^"]+)"', _main(body)):
        assert href == "https://kestrelapp.co", href


def test_the_page_loads_its_own_styles_not_the_legacy_sheet():
    with _client() as c:
        body = c.get("/help").text
    assert "/static/css/pages/help.css" in body and "/static/js/help.js" in body
    assert "/static/css/kestrel.css" not in body
    assert "announcement-slot" not in body
    for src in ("/static/css/pages/help.css", "/static/js/help.js"):
        assert c.get(src).status_code == 200, src


def test_helper_cards_say_what_the_mockup_says():
    with _client() as c:
        body = c.get("/help").text
    for key, title in (("wrong", "Something not working?"), ("promises", "What Kestrel promises"),
                       ("keys", "Shortcuts"), ("more", "More help")):
        card = body.split(f'data-testid="hp-helper-{key}"')[1].split("</article>")[0]
        assert title in card and f'data-k-flyout="#hp-fly-{key}"' in card, key
        assert f'id="hp-fly-{key}"' in body, key
    promises = body.split('data-testid="hp-helper-promises"')[1].split("</article>")[0]
    for line in ("Never applies, emails or messages anyone.", "Asks before anything goes online.",
                 "Free, and you are not the product."):
        assert line in promises
    assert 'href="/about#promises"' in promises
    keys = body.split('data-testid="hp-helper-keys"')[1].split("</article>")[0]
    assert "Ctrl K" in keys and "Esc" in keys and ">/<" in keys


def test_walkthrough_and_site_controls_are_on_the_more_help_card():
    from kestrel.db import migrate

    migrate.run_migrations()
    with _client() as c:
        body = c.get("/help").text
        more = body.split('data-testid="hp-more-actions"')[1].split("</div>")[0]
        assert "/guides/restore" not in body and "page guides" not in body.lower()
        assert 'href="/onboarding"' in more and "Replay the walkthrough" in more
        assert 'href="https://kestrelapp.co"' in more
        # The walkthrough link goes somewhere that answers.
        assert c.get("/onboarding").status_code == 200
        assert c.post("/guides/restore").status_code in (404, 405)


def test_the_model_names_stay_one_toggle_away_for_settings_to_link_to():
    with _client() as c:
        body = c.get("/help").text
    assert 'id="tiers"' in body and 'id="tier-names"' in body
    assert "Show details" in body.split('id="tier-names"')[1].split("</summary>")[0]
    for name in ("tier1", "tier2", "tier3", "docsel", "trigger", "frontier", "embed"):
        assert f'<span class="mono muted">{name}</span>' in body, name


def test_help_and_about_are_both_in_the_nav_and_link_to_each_other():
    with _client() as c:
        helped = c.get("/help").text
        about = c.get("/about").text
    for body in (helped, about):
        side = body.split('<aside class="side"')[1].split("</aside>")[0]
        assert 'href="/help"' in side and ">Help</a>" in side
        assert 'href="/about"' in side and ">About</a>" in side
        assert '{label: "About", href: "/about"}' in body  # the command palette
    assert 'href="/about#promises"' in helped
    assert 'href="/help"' in about and "Open Help" in about


def test_the_old_update_route_on_help_is_gone_and_help_has_no_version():
    with _client() as c:
        body = c.get("/help").text
        assert c.post("/help/update-check").status_code in (404, 405)
    assert 'hx-post="/updates/check"' not in body  # the check lives on About
    assert "Version " not in _main(body).split("hp-page")[1].split("hp-articles")[0]


def test_the_reference_blocks_other_pages_link_to_stay_on_help_behind_show_details():
    """Settings links /help#lan and /help#tiers, Sources links /help#sources and
    /help#remoterocketship. Each target exists, inside a collapsed block."""
    from kestrel.ingest.catalog import REGION_NOTE, source_catalog

    with _client() as c:
        body = c.get("/help").text
    for anchor in ("lan", "sources", "remoterocketship", "tiers", "tier-names"):
        assert f'id="{anchor}"' in body, anchor
    for block in ("lan", "sources", "tier-names"):
        assert re.search(rf'<details id="{block}"', body), block
        assert " open" not in body.split(f'<details id="{block}"')[1].split(">")[0]
    sources = body.split('id="sources"')[1].split("</details>")[0]
    for row in source_catalog():
        assert row.kind in sources and row.name in sources, row.kind
    assert "Where opportunities come from" in sources
    assert "Kestrel does not search LinkedIn; LinkedIn does not allow automated access." in sources
    assert REGION_NOTE.split(".")[0] in sources
    lan = body.split('id="lan"')[1].split("</details>")[0]
    assert "same home network" in lan and "Settings" in lan
    assert "settings.yml" not in lan.split("Turning it on")[1].split("</p>")[0]
    assert "—" not in sources and "—" not in lan
