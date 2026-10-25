"""Capture the README/docs screenshots from a real running Kestrel, on FICTIONAL data.

Run: `.venv/Scripts/python.exe -m kestrel.cli screenshots` (or this file directly).

EVERY SCREENSHOT USES INVENTED DATA. The operator's real profile, organizations, compensation, and
contacts must never appear in a committed image. Three things enforce that rather than assume it:

  1. `KESTREL_DATA_HOME` is redirected to a throwaway directory BEFORE any kestrel module is
     imported, so the app builds a brand new database and never opens the operator's. This is not
     a nicety: a bare script that forgets it writes to `%LOCALAPPDATA%\\Kestrel` and would render
     the operator's real pipeline into a PNG destined for a public README.
  2. The seed below is a fixed list of invented organizations and roles, checked against a
     forbidden-substring list drawn from the operator's actual config at run time.
  3. After capture, every PNG is OCR-free but content-checked: the page HTML captured alongside
     each shot is scanned for the same forbidden substrings, and the run fails if any appears.

The throwaway data directory is deleted at the end, pass or fail.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2

# Invented. Any resemblance to a real employer is accidental and should be corrected here.
_ORGS = [
    ("Northwind Freight", "logistics", "pe_backed", "500-1000"),
    ("Cascade Regional Health", "healthcare", "nonprofit", "1000-5000"),
    ("Beacon Robotics", "manufacturing", "vc_backed", "200-500"),
    ("Vantage Mutual", "insurance", "public", "5000+"),
    ("Harbor Point Analytics", "software", "bootstrapped", "50-200"),
    ("Ridgeline Utilities", "energy", "public", "1000-5000"),
    ("Juniper Bay Foods", "consumer goods", "pe_backed", "1000-5000"),
    ("Copperleaf Education", "education", "nonprofit", "200-500"),
]

# (title, track, fit, score, comp, location). `fit` picks the Pipeline group the row lands in:
#   strong / good / call / maybe / notfit = a Tier 2 evaluation (call = needs_review, "your call")
#   t0 = a Tier 0 rule reject, parked = a parked Tier 2 quarantine, new = kept, not scored yet.
_ROLES = [
    ("VP of Information Technology", "employment", "strong", 4.7, 210000, "Columbus, OH"),
    ("Director of IT Operations", "employment", "strong", 4.6, 185000, "Remote (US)"),
    ("Head of Infrastructure and Security", "employment", "good", 4.1, 195000, "Denver, CO"),
    ("Chief Information Officer", "employment", "good", 4.0, 260000, "Seattle, WA"),
    ("Director of Enterprise Applications", "employment", "call", 3.9, 175000, "Remote (US)"),
    ("IT Program Manager", "employment", "maybe", 3.3, 150000, "Chicago, IL"),
    ("Senior IT Manager", "employment", "maybe", 3.1, 140000, "Phoenix, AZ"),
    ("Network Operations Lead", "employment", "notfit", 1.8, 120000, "Tulsa, OK"),
    ("Help Desk Supervisor", "employment", "t0", None, 70000, "Boise, ID"),
    ("Associate Director of Technology", "employment", "parked", None, 160000, "Remote (US)"),
    ("Manager of Digital Platforms", "employment", "new", None, 155000, "Denver, CO"),
    ("Fractional CIO, post-acquisition integration", "consulting", "strong", 4.6, 2400, "Remote"),
    ("Interim CTO for a carve-out", "consulting", "good", 4.1, 2200, "Remote (US)"),
    ("IT due diligence, 3 week engagement", "consulting", "maybe", 3.2, 2600, "Chicago, IL"),
]
# Pursuit per role title: (status, days since applied or None, follow-up offset in days)
_PURSUITS = {
    "VP of Information Technology": ("shortlisted", None, None),
    "Director of IT Operations": ("applied", 9, -2),  # nine days, no reply: the quiet card
    "Chief Information Officer": ("interviewing", 14, 3),
    "Director of Enterprise Applications": ("shortlisted", None, None),
    "IT Program Manager": ("rejected", 30, None),
    "Fractional CIO, post-acquisition integration": ("shortlisted", None, None),
    "Interim CTO for a carve-out": ("outreach_sent", 6, 1),
}


_FICTIONAL_ORG_NAMES = {name for name, *_ in _ORGS}


class LeakCheckUnusable(RuntimeError):
    """The leak check could not run. Never downgraded to a warning: these images are published."""


def _real_identity_terms() -> list[str]:
    """Strings from the operator's REAL data that must never appear in a captured page.

    Deliberately reads the live config and database rather than guessing, and raises instead of
    returning an empty list if it cannot read them. An earlier version returned [] on any problem,
    which made every downstream check iterate zero times and pass vacuously. A guard that cannot
    fail is not a guard.
    """
    terms: list[str] = []
    real_config = Path(os.environ.get("_KESTREL_REAL_CONFIG", ""))

    profile = real_config / "profile.yml"
    if profile.exists():
        import yaml

        try:
            data = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise LeakCheckUnusable(f"cannot parse the real profile.yml: {exc}") from exc
        for track in data.values():
            if not isinstance(track, dict):
                continue
            for key in (
                "first_name", "last_name", "full_name", "email", "phone", "location", "headline",
            ):
                val = track.get(key)
                if isinstance(val, str) and len(val.strip()) > 3:
                    terms.append(val.strip())

    # The confidential employer lives in doc_section.org_label, NOT in the profile, so a
    # profile-only denylist would miss the most damaging field even when it did run.
    real_db = Path(os.environ.get("_KESTREL_REAL_DB", ""))
    if real_db.exists():
        import sqlite3

        try:
            con = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "select org_label from doc_section where org_label is not null"
                ).fetchall()
                terms += [r[0].strip() for r in rows if r[0] and len(r[0].strip()) > 3]
                rows = con.execute(
                    "select distinct name from org where name is not null"
                ).fetchall()
                terms += [r[0].strip() for r in rows if r[0] and len(r[0].strip()) > 3]
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise LeakCheckUnusable(f"cannot read the real database: {exc}") from exc

    # Never flag a term that is also one of our own invented names, or the check fails on itself.
    return [t for t in dict.fromkeys(terms) if t not in _FICTIONAL_ORG_NAMES]


def _org_names_in(html: str) -> set[str]:
    """Every organization name the page displays, per the seeded database.

    This is the POSITIVE half of the check and the half that cannot pass by doing nothing: it
    asserts that the orgs visible in the capture are drawn from the fictional list, rather than
    merely asserting that a possibly-empty denylist is absent.
    """
    return {name for name in _FICTIONAL_ORG_NAMES if name in html}


def _check_page(name: str, html: str, forbidden: list[str], expect_orgs: bool) -> list[str]:
    problems = []
    for term in forbidden:
        if term.lower() in html.lower():
            problems.append(f"{name}: contains real operator data {term!r}")
    if expect_orgs and not _org_names_in(html):
        problems.append(
            f"{name}: no fictional organization appears on the page, so this capture is not "
            f"showing the seeded data and may be showing something else"
        )
    return problems


async def _seed(sm) -> dict:
    from kestrel.db.models import (
        DocMaster,
        DocSection,
        DocSlot,
        DocVariant,
        Evaluation,
        Opportunity,
        Org,
        Pursuit,
        Quarantine,
        Run,
        Source,
        StatusEvent,
    )
    from kestrel.docs.seed import _ensure_layouts
    from kestrel.services import onboarding as ob

    ids: dict = {}
    async with sm() as s:
        src_e = Source(track="employment", kind="greenhouse", name="Greenhouse board", enabled=True)
        src_c = Source(track="consulting", kind="rss", name="Consulting feed", enabled=True)
        s.add_all([src_e, src_c])
        await s.flush()

        orgs = []
        for name, sector, ownership, band in _ORGS:
            org = Org(
                name=name, normalized_name=name.lower(), sector=sector, ownership=ownership,
                size_band=band, domain=f"{name.split()[0].lower()}.example.com",
                signals=["leadership change", "recent funding"],
            )
            s.add(org)
            orgs.append(org)
        await s.flush()

        now = datetime.now(UTC)
        first = None
        for i, (title, track, fit, score, comp, loc) in enumerate(_ROLES):
            org = orgs[i % len(orgs)]
            stage = {"t0": "t0_reject", "parked": "quarantined", "new": "t1_done"}.get(
                fit, "t2_done"
            )
            reason = None
            if fit == "t0":
                reason = "blocker:seniority_floor: title is below the operator's floor"
            opp = Opportunity(
                track=track,
                opp_type="job_posting" if track == "employment" else "contract_posting",
                org_id=org.id, org_name=org.name, title=title,
                url=f"https://boards.example.com/{org.name.split()[0].lower()}/jobs/{100 + i}",
                stage=stage, stage_reason=reason,
                first_seen_at=(now - timedelta(days=i % 5, hours=i)).isoformat(),
                seen_count=1 + (i % 3),
                ghost_score=round(0.05 + i * 0.03, 2), pre_score=round(1.0 + i * 0.2, 2),
                comp_min=int(comp * 0.85), comp_max=comp, comp_source="parsed",
                comp_interval="year" if track == "employment" else "day",
                location_raw=loc, remote_type="remote" if "Remote" in loc else "hybrid",
                description_md=(
                    f"{org.name} is hiring a {title}. You will own the technology roadmap, "
                    f"vendor accountability, security posture, and a team of 20 to 60. "
                    f"Reports to the CFO with a seat on the executive operations committee. "
                    f"This is invented sample data for documentation screenshots."
                ),
            )
            s.add(opp)
            await s.flush()
            if fit == "strong" and first is None:
                first = opp.id
            if fit == "parked":
                s.add(
                    Quarantine(
                        opportunity_id=opp.id, tier=2, model="kestrel-tier2",
                        raw_response="(invented sample: the reply was not valid JSON)",
                        error="The model reply did not match the required schema.", attempts=2,
                    )
                )
            if score is not None:
                verdict = {
                    "strong": "apply", "good": "apply", "call": "apply",
                    "maybe": "consider", "notfit": "pass",
                }[fit]
                if track == "consulting" and verdict == "apply":
                    verdict = "pursue"
                s.add(
                    Evaluation(
                        opportunity_id=opp.id, tier=2, track=track, verdict=verdict,
                        verdict_model=verdict, score=score, weighted_score=round(score - 0.1, 2),
                        needs_review=(fit == "call"), model="kestrel-tier2",
                        dimensions={
                            "scope": {"score": min(5.0, score + 0.2), "why": "Owns the function."},
                            "comp": {"score": score, "why": "Band is at or above target."},
                            "stability": {
                                "score": max(1.0, score - 0.6),
                                "why": "Recent leadership churn.",
                            },
                            "growth": {"score": score, "why": "Clear path to a broader remit."},
                        },
                        blocks={
                            "reason": "Strong scope match against the operator's stated targets."
                            if score >= 3.5
                            else "Partial match: the scope is narrower than the stated targets."
                        },
                        blockers=[],
                        legitimacy={"status": "clean", "signals": []},
                    )
                )
            plan = _PURSUITS.get(title)
            if plan:
                status, applied_days, follow = plan
                applied_at = (
                    (now - timedelta(days=applied_days)).isoformat() if applied_days else None
                )
                closed = status in ("rejected", "accepted", "withdrawn")
                pur = Pursuit(
                    opportunity_id=opp.id, track=track, status=status, applied_at=applied_at,
                    last_activity_at=applied_at or now.isoformat(),
                    next_action=None if closed else (
                        "Send the follow-up note" if follow is not None
                        else "Tailor the resume and apply"
                    ),
                    next_action_at=(
                        (now + timedelta(days=follow)).isoformat() if follow is not None else None
                    ),
                    outcome="rejected" if closed else None,
                    outcome_stage="screening" if closed else None,
                    closed_at=now.isoformat() if closed else None,
                )
                s.add(pur)
                await s.flush()
                s.add(
                    StatusEvent(
                        pursuit_id=pur.id, from_status="shortlisted", to_status=status,
                        note="Moved from the list.", occurred_at=now.isoformat(),
                    )
                )

        master = DocMaster(
            track="employment", name="Executive resume", doc_kind="resume",
            header={
                "name": "Jordan A. Rivera", "email": "jordan.rivera@example.com",
                "phone": "+1-614-555-0142", "location": "Columbus, OH",
                "headline": "Technology executive, infrastructure and security",
            },
            summary_variants=[
                {"key": "transformation", "text": "Technology executive who turns "
                 "under-invested IT functions into dependable, secure platforms for growth."},
                {"key": "operator", "text": "Pragmatic IT leader focused on reliability, cost, "
                 "and measurable commitments over roadmaps."},
            ],
            skill_groups=[
                {"key": "identity", "label": "Identity and Endpoint", "default_visible": True,
                 "items": ["Okta", "Entra ID", "Intune", "Jamf"]},
                {"key": "security", "label": "Security and Compliance", "default_visible": True,
                 "items": ["SOC 2", "HIPAA", "SIEM", "Zero Trust"]},
            ],
            education=[
                {"id": "edu000000001", "institution": "Lakeshore State University",
                 "credential": "B.S.", "field": "Computer Science", "dates": "2008"},
                {"id": "edu000000002", "institution": "Northgate Business School",
                 "credential": "Executive MBA", "field": "Technology Management",
                 "dates": "2016", "detail": "Evening program while leading a datacenter move"},
            ],
            certifications=[
                {"id": "cer000000001", "name": "Certified Information Security Manager",
                 "issuer": "Example Assurance Board", "year": "2018"},
                {"id": "cer000000002", "name": "ITIL 4 Managing Professional",
                 "issuer": "Example Service Institute", "year": "2020"},
            ],
        )
        s.add(master)
        await s.flush()
        roles = [
            ("Vantage Mutual", "Insurance carrier", "Vice President, Information Technology",
             "2019", "present", "Insurance carrier, 5000+ employees",
             ["Consolidated four datacenters into two regions, cutting run cost 28 percent.",
              "Rebuilt vendor governance across 60 contracts; recovered 1.4M in year one.",
              "Led the security program to a clean external audit with zero material findings."]),
            ("Harbor Point Analytics", "Analytics software company",
             "Director of Infrastructure", "2013", "2019", "Software company, 150 employees",
             ["Moved 300 servers to a managed cloud with no unplanned downtime.",
              "Cut the on-call page rate by 60 percent through runbook automation.",
              "Stood up the first SOC 2 program and passed the audit on the first attempt."]),
        ]
        for order, (org_label, redacted, rtitle, start, end, ctxline, bullets) in enumerate(roles):
            section = DocSection(
                doc_master_id=master.id, section_kind="role", sort_order=order,
                org_label=org_label, org_label_redacted=redacted, title=rtitle,
                start_date=start, end_date=end, context_line=ctxline,
            )
            s.add(section)
            await s.flush()
            slot = DocSlot(
                doc_section_id=section.id, key=f"impact_{order + 1}", sort_order=0,
                default_variant_key="v1",
            )
            s.add(slot)
            await s.flush()
            for j, text in enumerate(bullets):
                s.add(DocVariant(doc_slot_id=slot.id, key=f"v{j + 1}", text=text, tags=[]))
        await _ensure_layouts(s)
        # A finished onboarding, so "/" is Today and "/setup" is Settings > System.
        await ob.save(s, ob.OnboardingState(completed=True, completed_at=now.isoformat()))

        s.add(
            Run(
                name="pipeline",
                track=None,
                status="done",
                mode="local",
                started_at="2026-10-12T06:10:00+00:00",
                finished_at="2026-10-12T06:24:00+00:00",
                counts={
                    "tier0": {"pass": 19, "reject": 12},
                    "tier1": {"completed": 19, "errors": 0},
                    "tier2": {"completed": 11, "errors": 0},
                },
            )
        )
        await s.commit()
        ids["opp"] = first
        ids["master"] = master.id
    return ids


async def _connect_hosted(browser, base: str) -> None:
    """Connect a fictional hosted provider through the real Settings route. The provider's two
    endpoints are answered from tests/fixtures/openrouter_models.json by respx, so no live
    request is made and the model list is a captured payload shape, not a live one."""
    import json

    import respx
    from httpx import Response

    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "openrouter_models.json").read_text(encoding="utf-8")
    )
    api = "https://openrouter.ai/api/v1"
    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        await page.goto(f"{base}/settings", wait_until="networkidle")
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{api}/key").mock(
                return_value=Response(200, json={"data": {"label": "k", "is_free_tier": True}})
            )
            router.get(f"{api}/models").mock(return_value=Response(200, json=fixture))
            body = await page.evaluate(
                """async () => (await fetch('/settings/hosted/connect', {method: 'POST',
                  headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                  body: new URLSearchParams({provider: 'openrouter',
                    api_key: 'sk-or-v1-fictional-0000', ack: 'on', switch: 'on',
                    free_only: 'on', allow_training: 'on'})})).text()"""
            )
            if 'data-connect-ok="true"' not in body:
                raise RuntimeError("the fictional hosted connect did not succeed")
            # Prime the model list now, while the mock is active, so the picker opens from cache.
            await page.goto(f"{base}/models?x=1", wait_until="networkidle")
            await page.evaluate("fetch('/models/hosted/overrides?open=1')")
            await page.wait_for_timeout(500)
    finally:
        await ctx.close()


async def _capture() -> int:

    from playwright.async_api import async_playwright

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.web.serve import running_server

    migrate.run_migrations()
    ids = await _seed(get_sessionmaker())
    await reset_engine()

    forbidden = _real_identity_terms()
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    # (name, path, expect a fictional org name on the page). The first block is the original page
    # list; applications and the Table view of pursuits are the two views of /pursuits.
    shots = [
        ("dashboard", "/", False),
        ("setup", "/setup", False),
        ("pipeline", "/opportunities", True),
        ("opportunity-detail", f"/opportunities/{ids['opp']}", True),
        ("calibrate", "/calibrate", False),
        ("documents", f"/documents?master_id={ids['master']}", False),
        ("pursuits", "/pursuits?view=table", True),
        ("applications", "/pursuits", True),
        ("analytics", "/analytics", False),
        ("models", "/models", False),
        ("runs", "/runs", False),
        ("sources", "/sources", False),
        ("help", "/help", False),
        ("about", "/about", False),
        ("backup", "/backup", False),
    ]
    # Pages whose first screen is not the whole story: shoot the full scroll height instead.
    full_page = {"pipeline", "applications", "documents"}

    # The render pipeline launches a browser too; point it at the same Chromium when Edge is absent.
    if os.environ.get("KESTREL_CHROMIUM_PATH"):
        os.environ.setdefault("KESTREL_BROWSER_PATH", os.environ["KESTREL_CHROMIUM_PATH"])

    async def snap(page, name: str, theme: str, *, full: bool = False, expect_orgs: bool = False):
        target = OUT / f"{name}-{theme}.png"
        await page.screenshot(path=str(target), full_page=full)
        html = await page.content()
        failures.extend(_check_page(target.name, html, forbidden, expect_orgs=expect_orgs))
        print(f"  {target.relative_to(ROOT)}")
        return html

    with running_server(worker=False) as base:
        async with async_playwright() as p:
            # Edge is the browser Kestrel drives in production (no bundled Chromium in the
            # installer). A cloud or Linux checkout has no Edge; fall back to Playwright's own
            # Chromium so the UI shots can still be refreshed there, and say so.
            try:
                browser = await p.chromium.launch(channel="msedge", headless=True)
            except Exception as exc:  # noqa: BLE001 - any launch failure means "no Edge here"
                print(f"  (Edge unavailable: {type(exc).__name__}; using Playwright Chromium)")
                # KESTREL_CHROMIUM_PATH points at a Chromium binary when Playwright's own
                # download is absent or a different revision than the installed package.
                exe = os.environ.get("KESTREL_CHROMIUM_PATH")
                browser = await p.chromium.launch(headless=True, executable_path=exe or None)
            try:
                for theme in ("light", "dark"):
                    ctx = await browser.new_context(
                        viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme=theme
                    )
                    page = await ctx.new_page()
                    for name, path, expect_orgs in shots:
                        await page.goto(f"{base}{path}", wait_until="networkidle")
                        await page.wait_for_timeout(400)
                        await snap(
                            page, name, theme, full=name in full_page, expect_orgs=expect_orgs
                        )

                    # Settings > Hosted (BYOK): the tab loads its panel over HTMX, so click the
                    # tab and wait for the panel's own heading. No key is connected yet, which is
                    # exactly the state a new operator sees (the connect happens further down).
                    await page.goto(f"{base}/settings", wait_until="networkidle")
                    await page.click("button:has-text(\"Hosted (BYOK)\")")
                    await page.wait_for_selector("#hosted-panel #mode-switch", timeout=10_000)
                    await page.wait_for_timeout(400)
                    html = await snap(page, "settings-hosted", theme)
                    if "Run Kestrel on" not in html or "hosted-assignments" not in html:
                        failures.append(f"settings-hosted-{theme}.png: the Hosted panel is missing")

                    # The render review page. /renders/new creates a DocRender (a real PDF) and
                    # redirects to it. A render failure is not fatal: the shot would be misleading,
                    # so it is skipped rather than committed.
                    await page.goto(
                        f"{base}/renders/new?master_id={ids['master']}", wait_until="networkidle"
                    )
                    await page.wait_for_timeout(800)
                    if re.search(r"/renders/\d+", page.url):
                        await snap(page, "render-review", theme)
                    else:
                        failures.append(f"render-review-{theme}.png: the render did not complete")

                    # The Tailor page as reached from a role on the Applications board: the same
                    # review page with the role's name on it and the way back to the board.
                    await page.goto(
                        f"{base}/renders/new?master_id={ids['master']}"
                        f"&opportunity_id={ids['opp']}&from=/pursuits",
                        wait_until="networkidle",
                    )
                    await page.wait_for_timeout(800)
                    if re.search(r"/renders/\d+", page.url):
                        html = await snap(page, "tailor", theme, full=True)
                        if "Swap" not in html:
                            failures.append(f"tailor-{theme}.png: no Swap controls on the page")
                    else:
                        failures.append(f"tailor-{theme}.png: the render did not complete")

                    # The Tier 3 confirmation gate: the visual proof that nothing leaves the
                    # machine without the operator reading the payload and its destination first.
                    await page.goto(f"{base}/opportunities/{ids['opp']}", wait_until="networkidle")
                    await page.click("[data-testid=escalate]")
                    await page.wait_for_timeout(1200)
                    html = await snap(page, "tier3-confirmation", theme, expect_orgs=True)
                    if "off this computer" not in html:
                        failures.append(
                            f"tier3-confirmation-{theme}.png: the transmission warning is not on "
                            f"the page, so this is not a picture of the confirmation gate"
                        )
                    await ctx.close()

                # Connect a hosted provider once (provider calls mocked, nothing leaves the
                # process), then shoot the Models page override panel open in both themes.
                await _connect_hosted(browser, base)
                for theme in ("light", "dark"):
                    ctx = await browser.new_context(
                        viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme=theme
                    )
                    page = await ctx.new_page()
                    await page.goto(f"{base}/models", wait_until="networkidle")
                    await page.click("[data-testid=override-open]")
                    await page.wait_for_selector("[data-testid=save-all]", timeout=15_000)
                    await page.locator("#hosted-overrides").scroll_into_view_if_needed()
                    await page.wait_for_timeout(400)
                    target = OUT / f"models-override-{theme}.png"
                    await page.locator("#hosted-overrides").screenshot(path=str(target))
                    html = await page.content()
                    failures += _check_page(target.name, html, forbidden, expect_orgs=False)
                    print(f"  {target.relative_to(ROOT)}")
                    await ctx.close()
            finally:
                await browser.close()

    await reset_engine()
    if failures:
        print("\nFAILED content checks:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    # Counted from disk. An arithmetic guess drifts the moment a shot is added or skipped, and it
    # was already wrong: it printed 14 while writing 16.
    written = len(list(OUT.glob("*.png")))
    print(f"\n{written} screenshots in {OUT.relative_to(ROOT)}")
    return 0


def main() -> int:
    """Redirect the data home BEFORE importing kestrel, then capture, then delete the seed."""
    import asyncio

    real_config = os.environ.get("LOCALAPPDATA", "")
    tmp = Path(tempfile.mkdtemp(prefix="kestrel-shots-"))
    os.environ["_KESTREL_REAL_CONFIG"] = str(Path(real_config) / "Kestrel" / "config")
    os.environ["_KESTREL_REAL_DB"] = str(
        Path(real_config) / "Kestrel" / "data" / "kestrel.db"
    )
    os.environ["KESTREL_DATA_HOME"] = str(tmp)
    try:
        return asyncio.run(_capture())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # Report what actually happened. ignore_errors=True plus an unconditional success message
        # would claim the seed was deleted while a held SQLite handle left it on disk.
        if tmp.exists():
            print(
                f"WARNING: the seed database could NOT be deleted; remove it by hand: {tmp}",
                file=sys.stderr,
            )
        else:
            print(f"seed database deleted ({tmp})")


if __name__ == "__main__":
    raise SystemExit(main())
