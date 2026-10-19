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
        assert "searched on every board (Indeed and Glassdoor)" in body
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
