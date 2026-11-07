"""Near titles in the generated searches (D-TTL-2): bounded variants per target title, deduped
across titles, capped in all, never pushing the searches past MAX_GENERATED or dropping a metro,
never LinkedIn, and removable from the Profile page. No live network: nothing is scraped."""

from __future__ import annotations

import asyncio

from kestrel import paths
from kestrel.ingest import generated as gen
from kestrel.services import profile_edit as pe
from kestrel.services import tracks

EMP = {
    "employment_terms": {"target_titles": ["Director of IT", "IT Director", "VP of IT"]},
    "geo_rules": {"remote_ok": True, "remote_country_allowlist": ["US"],
                  "onsite_metros": [{"name": "Austin", "aliases": ["Austin"]}]},
}


def test_variants_are_bounded_deduped_and_never_a_target():
    v = gen.title_variants(EMP["employment_terms"]["target_titles"])
    low = [x.casefold() for x in v]
    assert len(v) == len(set(low)) <= gen.MAX_VARIANTS
    assert "it director" not in low and "director of it" not in low  # both are targets
    assert "director of information technology" in low and "vice president of it" in low
    for x in v:  # never a reordering: each is its target with abbreviations swapped
        assert any(gen_tokens(x) == gen_tokens(t) for t in EMP["employment_terms"]["target_titles"])
    many = [f"Director of Function {i}" for i in range(10)] + ["Director of IT"]
    assert len(gen.title_variants(many)) <= gen.MAX_VARIANTS
    for t in ("Registered Nurse", "UX Designer", "HR Manager"):
        assert len(gen.title_variants([t])) <= gen.VARIANTS_PER_TITLE


def gen_tokens(title):
    from kestrel.rules.titles import token_set

    return token_set(title)


def test_removed_variants_are_not_searched():
    v = gen.title_variants(["Director of IT"], removed=["director of information technology"])
    assert "Director of Information Technology" not in v and "Director of Technology" in v


def test_searches_carry_the_variants_and_no_linkedin():
    specs, used, offered = gen.plan({"employment": EMP}, ("employment",), None)
    assert used and used == offered[:len(used)]  # trimmed from the end only
    for s in specs:
        assert "linkedin" not in [x.lower() for x in s.config["site_name"]]
        assert all(len(q) <= gen.MAX_QUERY_CHARS + 2 for q in [s.config["search_term"]])
    joined = " ".join(s.config["search_term"] for s in specs)
    assert all(f'"{v}"' in joined for v in used)
    plain = gen.build_specs({"employment": EMP}, ("employment",), None,
                            removed_variants=offered)
    assert all('"VP of Information Technology"' not in s.config["search_term"] for s in plain)


def test_variants_never_cost_a_metro_or_pass_the_cap():
    metros = [{"name": f"City {i}", "aliases": [f"City {i}"]} for i in range(7)]
    emp = {**EMP, "geo_rules": {**EMP["geo_rules"], "onsite_metros": metros}}
    titles = [f"Director of IT Region Number {i}" for i in range(6)]
    emp["employment_terms"] = {"target_titles": titles}
    base, _, _ = gen.plan({"employment": emp}, ("employment",), None,
                          removed_variants=gen.title_variants(titles))
    specs, used, offered = gen.plan({"employment": emp}, ("employment",), None)
    assert len(specs) <= gen.MAX_GENERATED
    assert len(specs) <= gen.variant_budget(len(base))
    assert offered and len(used) < len(offered)  # trimmed to fit
    near = {s.name.split(" (")[0] for s in specs if s.name.startswith("Jobs near")}
    assert near == {s.name.split(" (")[0] for s in base if s.name.startswith("Jobs near")}


def test_variants_add_at_most_half_again_the_searches():
    assert [gen.variant_budget(n) for n in (0, 1, 2, 3, 4, 7)] == [0, 1, 3, 4, 6, 10]
    assert gen.variant_budget(gen.MAX_GENERATED - 1) == gen.MAX_GENERATED  # never past the cap
    assert gen.variant_budget(gen.MAX_GENERATED + 3) == gen.MAX_GENERATED + 3  # adds nothing
    # One remote search without variants: half again rounded down is 0 extra, so only the
    # variants that fit inside that one search's query are kept.
    titles = [f"VP of IT and HR Region {i}" for i in range(3)]
    emp = {"employment_terms": {"target_titles": titles},
           "geo_rules": {"remote_ok": True, "remote_country_allowlist": ["US"],
                         "onsite_metros": []}}
    base, _, offered = gen.plan({"employment": emp}, ("employment",), None,
                                removed_variants=gen.title_variants(titles))
    specs, used, offered = gen.plan({"employment": emp}, ("employment",), None)
    full = gen._build({"employment": emp}, ("employment",), None, sec_use_titles=False,
                      variants=offered)
    assert len(base) == 1 and len(full) > 1  # every variant would have added searches
    assert len(specs) == 1 and 0 < len(used) < len(offered)


def _client(monkeypatch):
    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, reset_engine
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    forget_engine()
    tracks.forget_cache()
    return TestClient(build_app(worker=False))


def test_profile_lists_variants_and_a_removal_sticks(monkeypatch):
    client = _client(monkeypatch)
    try:
        pe.save_section("employment", "employment",
                        {"employment_terms": {"target_titles": ["Director of IT"]}})
        page = client.get("/profile?tab=employment&track=employment").text
        assert 'hx-get="/profile/title-variants"' in page
        body = client.get("/profile/title-variants").text
        assert 'data-testid="title-variants-used"' in body
        assert "Director of Information Technology" in body
        assert "also searches close versions of your titles, on every board" in body
        assert "—" not in body
        r = client.post("/title-variants/remove", data={"variant": "Director of Technology"})
        assert 'data-testid="title-variants-removed"' in r.text
        used = r.text.split('data-testid="title-variants-removed"')[0]
        assert "Director of Technology" not in used.split('data-testid="title-variants-used"')[-1]

        async def removed():
            from kestrel.db.engine import get_sessionmaker

            async with get_sessionmaker()() as s:
                return await gen.removed_variants(s)

        assert asyncio.run(removed()) == ["Director of Technology"]
        r = client.post("/title-variants/restore", data={"variant": "director of technology"})
        assert 'data-testid="title-variants-removed"' not in r.text
        assert client.post("/title-variants/nope", data={}).status_code == 404
    finally:
        pe.profile_path().unlink(missing_ok=True)
        tracks.forget_cache()


# ---- D-TTL-4 (#117): ranked by kind, fair across titles, chips name their title -------------
FOUR = ["VP of QA", "VP of BI", "VP of UX", "VP of HR"]  # three swaps each: 12 offered, cap 9


def _emp(titles, metros=0):
    return {
        "employment_terms": {"target_titles": titles},
        "geo_rules": {"remote_ok": True, "remote_country_allowlist": ["US"],
                      "onsite_metros": [{"name": f"City {i}", "aliases": [f"City {i}"]}
                                        for i in range(metros)]},
    }


def _title_of(variant, titles):
    return next(t for t in titles if gen_tokens(t) == gen_tokens(variant))


def test_four_titles_three_variants_cap_nine_every_title_is_searched():
    offered = gen.variant_sources(FOUR)
    assert len(offered) == gen.MAX_VARIANTS == 9
    assert {src for _, src, _ in offered} == set(FOUR)
    # round-robin: the first four are one per title, the next four are one per title again
    assert [src for _, src, _ in offered[:4]] == FOUR
    assert [src for _, src, _ in offered[4:8]] == FOUR
    # a tight budget: one search, 220 characters, so the cut leaves 4 of the 9
    specs, used, all_offered = gen.plan({"employment": _emp(FOUR)}, ("employment",), None)
    assert len(specs) == 1 and len(all_offered) == 9 and 4 <= len(used) < 9
    assert {_title_of(v, FOUR) for v in used} == set(FOUR)


def test_abbreviation_and_expansion_swaps_are_kept_before_other_phrasings():
    # Nine titles that only have a spelling-family variant come first in the list, and the
    # title with an abbreviation expansion comes last. The expansion must not be the one cut.
    family = [f"Software Developer {w}" for w in
              ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India")]
    titles = [*family, "VP of Finance"]
    got = gen.variant_sources(titles)
    assert len(got) == gen.MAX_VARIANTS
    kinds = [k for _, _, k in got]
    assert kinds[0] == "abbrev" and got[0][1] == "VP of Finance"
    assert kinds == sorted(kinds, key=("abbrev", "family").index)  # abbreviations first, in all
    # and across the kinds the cut costs the family spellings, never the expansion
    assert "Vice President of Finance" in gen.title_variants(titles)


def test_every_title_gets_its_expansion_before_any_title_gets_a_third_phrasing():
    titles = ["Director of IT", "VP of QA", "VP of HR", "Sr Director of IT", "VP of BI"]
    got = gen.variant_sources(titles)
    assert len(got) == gen.MAX_VARIANTS
    kept_abbrev = [src for _, src, k in got if k == "abbrev"]
    assert set(kept_abbrev) == set(titles)  # every title's expansion is searched
    assert sorted(src for _, src, _ in got[:5]) == sorted(titles)  # one each, first
    # the cut fell on the third phrasing and the family spellings, never on an expansion
    assert [k for _, _, k in got].count("family") == 0


def _seed_suggestion(title):
    from kestrel.db.engine import forget_engine, get_sessionmaker
    from kestrel.services.title_suggest import TitleSuggestion

    async def go():
        async with get_sessionmaker()() as s:
            s.add(TitleSuggestion(title=title, why="x", evidence=[], status="suggested",
                                  prompt_version="v"))
            await s.commit()

    asyncio.run(go())
    forget_engine()


def test_the_card_names_each_chips_title_and_the_titles_with_none(monkeypatch):
    client = _client(monkeypatch)
    try:
        stem = "VP of IT Regional Operations and Service Delivery Planning Region Number"
        long = [f"{stem} {i}" for i in range(8)]  # too long to fit
        pe.save_section("employment", "employment",
                        {"employment_terms": {"target_titles": [*long, "Barista"]},
                         "geo_rules": {"remote_ok": True, "remote_country_allowlist": ["US"]}})
        body = client.get("/profile/title-variants").text
        used = body.split('data-testid="title-variants-used"')[1].split("</ul>")[0]
        assert used.count('data-testid="title-variant-source"') == used.count("<li ") > 0
        left = body.split('data-testid="title-variants-left-out"')[1].split("</ul>")[0]
        assert left.count('data-testid="title-variant-source"') == left.count("<li ") > 0
        assert "from VP of IT Regional Operations" in left
        note = body.split('data-testid="title-variants-without"')[1].split("</p>")[0]
        assert "Barista (no close versions to add)" in note
        assert "(left out to stay under the search limit)" in note
        # removing a searched chip lets a left-out one in, and the card says so
        assert "Remove a searched one to let a left-out one in" in body
        first = used.split('<span class="pf-chip__v">')[1].split("<")[0]
        first_left = left.split('<span class="pf-chip__v">')[1].split("<")[0]
        again = client.post("/title-variants/remove", data={"variant": first}).text
        in_used = again.split('data-testid="title-variants-used"')[1].split("</ul>")[0]
        assert first not in in_used and first_left in in_used
        assert "—" not in body
    finally:
        pe.profile_path().unlink(missing_ok=True)
        tracks.forget_cache()


def test_accepting_a_suggestion_adds_its_variants_after_reload(monkeypatch):
    client = _client(monkeypatch)
    try:
        pe.save_section("employment", "employment",
                        {"employment_terms": {"target_titles": FOUR[:3]},
                         "geo_rules": {"remote_ok": True, "remote_country_allowlist": ["US"]}})
        before = client.get("/profile/title-variants").text
        assert "Vice President of Human Resources" not in before.split(
            'data-testid="title-variants-removed"')[0].split('data-testid="title-variants-used"')[1]
        _seed_suggestion("VP of HR")
        r = client.post("/title-suggestions/1/accept")
        assert r.status_code == 200
        after = client.get("/profile/title-variants").text
        used = after.split('data-testid="title-variants-used"')[1].split("</ul>")[0]
        assert "Vice President of Human Resources" in used and "from VP of HR<" in used
    finally:
        pe.profile_path().unlink(missing_ok=True)
        tracks.forget_cache()
