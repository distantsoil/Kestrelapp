"""Walk the real web app in Edge using only an isolated, invented data home.

Run with ``uv run python scripts/walk_screens.py``. No network model action is taken.
The baseline seed comes from capture_screenshots.py; this script never imports Kestrel
until KESTREL_DATA_HOME and LOCALAPPDATA both point at its temporary directory.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images" / "walk"
VIEWPORTS = (("light", 1440), ("dark", 1440), ("narrow", 1000))


class LeakDetected(RuntimeError):
    """A screenshot contains a string outside the invented seed; never publish it."""


async def walk() -> int:
    from capture_screenshots import _real_identity_terms, _seed
    from playwright.async_api import async_playwright
    from sqlalchemy import select

    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import Offer, Opportunity, Org, OrgResearch, PrepPack, Pursuit
    from kestrel.services import onboarding as ob
    from kestrel.web.serve import running_server

    identity_terms = _real_identity_terms()  # raises rather than returning nothing
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    index: list[str] = [
        "# Screenshot walk",
        "",
        "All roles, employers, and people are invented.",
        "",
    ]
    findings: list[str] = ["# Walk findings", "", "Walked with Edge and invented data.", ""]
    filenames: set[str] = set()
    capture_errors = 0

    def finding(page: str, steps: str, screenshot: str, expected: str, actual: str) -> None:
        findings.extend(
            [
                f"## {len([x for x in findings if x.startswith('## ')]) + 1}. {page}",
                "",
                f"Steps: {steps}",
                "",
                f"Screenshot: [{screenshot}](images/walk/{screenshot})"
                if screenshot
                else "Screenshot: none; capture stopped before an image was written.",
                "",
                f"Expected: {expected}",
                "",
                f"Observed: {actual}",
                "",
            ]
        )

    async def snap(page, name: str, state: str, theme: str, *, note: str) -> str:
        filename = f"{name}-{state}-{theme}.png"
        target = OUT / filename
        await page.screenshot(path=str(target), full_page=True, animations="disabled")
        html = await page.content()
        # The owner's real names, read from the local profile at run time (never committed).
        # None is in the fictional seed, so a hit aborts publication.
        if any(term.lower() in html.lower() for term in identity_terms):
            target.unlink(missing_ok=True)
            raise LeakDetected(f"Possible personal data in {filename}")
        if filename not in filenames:
            filenames.add(filename)
            index.append(f"- [{filename}]({filename}) — {note}")
        overflow = await page.evaluate(
            "() => document.documentElement.scrollWidth > innerWidth + 4"
        )
        if overflow:
            finding(
                name,
                f"Open {page.url} at {page.viewport_size['width']} px",
                filename,
                "The page fits the viewport without horizontal scrolling.",
                "The document is wider than the viewport.",
            )
        return filename

    async def visit(page, name: str, state: str, path: str, theme: str, note: str) -> str:
        nonlocal capture_errors
        try:
            response = await page.goto(base + path, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(350)
            filename = await snap(page, name, state, theme, note=note)
            if response and response.status >= 400:
                finding(
                    name,
                    f"Open {path}",
                    filename,
                    "Page loads successfully.",
                    f"HTTP {response.status}.",
                )
                capture_errors += 1
            return filename
        except LeakDetected:
            raise
        except Exception as exc:
            capture_errors += 1
            finding(
                name,
                f"Open {path}",
                "",
                "Page loads and can be captured.",
                f"{type(exc).__name__}: {exc}",
            )
            return ""

    async def capture_tabs(page, name: str, theme: str) -> None:
        tabs = await page.locator('a[role="tab"][href]').evaluate_all(
            "els => els.map(e => ({text: e.textContent.trim(), href: e.getAttribute('href')}))"
        )
        seen: set[str] = set()
        for tab in tabs:
            href = tab["href"]
            if not href or href in seen or not href.startswith("/"):
                continue
            seen.add(href)
            slug = re.sub(r"[^a-z0-9]+", "-", tab["text"].lower()).strip("-")[:45]
            await visit(page, name, slug or "tab", href, theme, f"{name}: {tab['text']} tab.")

    migrate.run_migrations()
    sm = get_sessionmaker()
    with running_server(worker=False) as base:
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="msedge", headless=True)
            try:
                # An empty database, followed by all six wizard steps. Navigation changes only
                # the isolated onboarding setting; no search or model setup is started.
                ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
                page = await ctx.new_page()
                await visit(
                    page,
                    "first-run",
                    "empty",
                    "/",
                    "light",
                    "Fresh install before setup or any search.",
                )
                for theme, width in VIEWPORTS:
                    await page.set_viewport_size({"width": width, "height": 900})
                    await page.emulate_media(color_scheme="dark" if theme == "dark" else "light")
                    for key in ob.STEP_KEYS:
                        async with sm() as s:
                            await ob.save(
                                s, ob.OnboardingState(step=key, visited=list(ob.STEP_KEYS))
                            )
                            await s.commit()
                        await visit(
                            page,
                            "onboarding",
                            key,
                            "/onboarding",
                            theme,
                            f"Onboarding step: {key}.",
                        )
                await ctx.close()

                ids = await _seed(sm)
                async with sm() as s:
                    opp = await s.get(Opportunity, ids["opp"])
                    org = await s.get(Org, opp.org_id)
                    org_id = org.id
                    s.add(
                        OrgResearch(
                            org_id=org.id,
                            summary_md=(
                                "Northwind Freight operates a fictional regional freight network."
                            ),
                            signals=["Opened a second distribution center"],
                            unknowns=["Current vendor contract lengths"],
                            angle="Ask how the new site will connect to existing systems.",
                            sources=[
                                {"url": "https://example.com/northwind", "title": "Sample page"}
                            ],
                            researched_at=datetime.now(UTC).isoformat(),
                            insufficient_sources=True,
                        )
                    )
                    s.add(
                        PrepPack(
                            opportunity_id=opp.id,
                            content_md=(
                                "# Interview plan\nAsk about the distribution center rollout."
                            ),
                            stories={"one": "A fictional infrastructure migration"},
                            questions_to_ask=["What would success look like in the first quarter?"],
                            panel=["Hiring manager"],
                        )
                    )
                    pur = (
                        await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
                    ).scalar_one()
                    pur.status = "offer"
                    s.add(
                        Offer(
                            pursuit_id=pur.id,
                            base=210000,
                            bonus_target_pct=15,
                            level_title="VP of Information Technology",
                            status="verbal",
                            notes_md="Invented offer for screenshots.",
                        )
                    )
                    second = (
                        await s.execute(
                            select(Opportunity).where(
                                Opportunity.title == "Head of Infrastructure and Security"
                            )
                        )
                    ).scalar_one()
                    second_pursuit = Pursuit(
                        opportunity_id=second.id,
                        track="employment",
                        status="offer",
                        last_activity_at=datetime.now(UTC).isoformat(),
                        next_action="Review the invented offer terms",
                    )
                    s.add(second_pursuit)
                    await s.flush()
                    s.add(
                        Offer(
                            pursuit_id=second_pursuit.id,
                            base=195000,
                            bonus_target_pct=12,
                            level_title="Head of Infrastructure and Security",
                            status="written",
                            notes_md="Second invented offer for comparison screenshots.",
                        )
                    )
                    await s.commit()
                await reset_engine()

                # Create exactly one local render before comparing themes. Repeated visits to
                # /renders/new would add documents and change the Role page count mid-walk.
                render_ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
                render_page = await render_ctx.new_page()
                await render_page.goto(
                    base + f"/renders/new?master_id={ids['master']}&opportunity_id={ids['opp']}",
                    wait_until="domcontentloaded",
                )
                render_path = urlsplit(render_page.url).path
                await render_ctx.close()

                paths = [
                    ("today", "/", "Daily start and decisions waiting."),
                    ("pipeline", "/opportunities", "Role groups and decisions."),
                    ("role", f"/opportunities/{ids['opp']}", "Role evaluation."),
                    ("applications", "/pursuits", "Application board."),
                    ("applications", "/pursuits?view=table", "Application list."),
                    ("profile", "/profile", "Profile details."),
                    ("profile", "/profile?tab=consulting&track=consulting", "Consulting profile."),
                    ("stories", "/stories", "Story bank."),
                    ("documents", f"/documents?master_id={ids['master']}", "Resume source text."),
                    ("prep", f"/prep/{ids['opp']}", "Interview prep pack."),
                    ("mock", f"/mock/{ids['opp']}", "Practice interview entry."),
                    ("offers", "/offers", "Recorded offer."),
                    ("company", f"/orgs/{org_id}", "Researched fictional company."),
                    ("sources", "/sources", "Search sources."),
                    ("runs", "/runs", "Run history."),
                    ("needs-another-try", "/quarantine", "Quarantined role."),
                    ("calibrate", "/calibrate", "Rating queue."),
                    ("analytics", "/analytics", "Search summary."),
                    ("models", "/models", "Model choices."),
                    ("model-check", "/bench", "Model check."),
                    ("settings", "/settings", "Settings overview."),
                    ("help", "/help", "Help."),
                    ("about", "/about", "About and release information."),
                ]
                for theme, width in VIEWPORTS:
                    ctx = await browser.new_context(
                        viewport={"width": width, "height": 900},
                        color_scheme="dark" if theme == "dark" else "light",
                    )
                    page = await ctx.new_page()
                    for name, path, note in paths:
                        state = (
                            "list"
                            if name == "applications" and "table" in path
                            else "consulting"
                            if name == "profile" and "consulting" in path
                            else "main"
                        )
                        await visit(page, name, state, path, theme, note)
                        if (
                            name
                            in {
                                "role",
                                "profile",
                                "prep",
                                "offers",
                                "company",
                                "analytics",
                                "models",
                            }
                            and state != "consulting"
                        ):
                            await capture_tabs(page, name, theme)
                    await visit(
                        page,
                        "tailor",
                        "review",
                        render_path,
                        theme,
                        "Tailored resume review and source text choices.",
                    )
                    await visit(
                        page,
                        "analytics",
                        "table",
                        "/analytics?view=table",
                        theme,
                        "Analytics table view of the same numbers.",
                    )
                    await page.goto(base + "/analytics")
                    group_tabs = await page.locator("#an-tabs button[data-tab]").evaluate_all(
                        "els => els.map(e => e.getAttribute('data-tab'))"
                    )
                    for group in group_tabs:
                        await page.locator(f'#an-tabs button[data-tab="{group}"]').click()
                        await snap(
                            page,
                            "analytics",
                            f"group-{group}",
                            theme,
                            note=f"Analytics grouped by {group}.",
                        )

                    for name, path, selector, state in (
                        ("models", "/models", "[data-testid=mo-change-local]", "change-model"),
                        ("offers", "/offers", "[data-testid=of-record]", "record-dialog"),
                        ("sources", "/sources", "[data-testid=add-source]", "add-dialog"),
                        ("runs", "/runs", "[data-testid=rn-run-menu]", "run-menu"),
                    ):
                        await page.goto(base + path)
                        control = page.locator(selector).first
                        if await control.count() and await control.is_visible():
                            await control.click()
                            await page.wait_for_timeout(350)
                            await snap(
                                page,
                                name,
                                state,
                                theme,
                                note=f"{name}: {state} open without saving or sending.",
                            )

                    # Settings section anchors are the actual controls, including lazy sections.
                    for section in (
                        "computer",
                        "roles",
                        "scoring",
                        "privacy",
                        "data",
                        "preferences",
                    ):
                        await visit(
                            page,
                            "settings",
                            section,
                            f"/settings#{section}",
                            theme,
                            f"Settings: {section} section.",
                        )
                    await visit(
                        page,
                        "settings",
                        "appearance",
                        "/settings#preferences",
                        theme,
                        "Theme and color scheme controls.",
                    )
                    schemes = await page.locator("[data-st-scheme]").evaluate_all(
                        "els => els.map(e => e.getAttribute('data-st-scheme'))"
                    )
                    if theme == "light":
                        for scheme in schemes:
                            try:
                                await page.locator(f'[data-st-scheme="{scheme}"]').click()
                                await page.wait_for_timeout(350)
                                await snap(
                                    page,
                                    "settings",
                                    f"scheme-{scheme}",
                                    theme,
                                    note=f"Settings color scheme: {scheme}.",
                                )
                            except Exception as exc:
                                finding(
                                    "Settings",
                                    f"Select {scheme} color scheme",
                                    "",
                                    "Scheme applies and can be captured.",
                                    str(exc),
                                )
                    # Set the fictional database back to the default scheme.
                    if schemes:
                        await page.locator(f'[data-st-scheme="{schemes[0]}"]').click()

                    # Safe dialogs and menus only. Never activate an online Send action.
                    for name, path, selector, state in (
                        ("pipeline", "/opportunities", "[data-testid=more-filters]", "filters"),
                        ("pipeline", "/opportunities", "[data-testid=view-button]", "saved-views"),
                        (
                            "applications",
                            "/pursuits",
                            "[data-testid=pcard] [data-more]",
                            "card-menu",
                        ),
                        ("analytics", "/analytics", "#an-export summary", "export-menu"),
                        (
                            "role",
                            f"/opportunities/{ids['opp']}",
                            "[data-testid=more]",
                            "more-menu",
                        ),
                    ):
                        await page.goto(base + path, wait_until="domcontentloaded")
                        control = page.locator(selector).first
                        if await control.count():
                            try:
                                await control.click(timeout=3000)
                                await page.wait_for_timeout(300)
                                await snap(page, name, state, theme, note=f"{name}: {state} open.")
                            except Exception as exc:
                                finding(name, f"Click {selector}", "", "Control opens.", str(exc))
                        else:
                            finding(
                                name,
                                f"Find {selector}",
                                "",
                                "Control exists.",
                                "Control was not found in this seeded state.",
                            )
                    await page.goto(base + f"/opportunities/{ids['opp']}")
                    await page.locator("[data-testid=more]").click()
                    await page.locator("[data-testid=escalate]").click()
                    await page.wait_for_timeout(350)
                    await snap(
                        page,
                        "role",
                        "online-ask",
                        theme,
                        note="Deeper read consent dialog before any online send.",
                    )
                    await page.goto(base + "/opportunities")
                    await page.locator("[data-testid=pl-row]").first.click()
                    await page.wait_for_timeout(350)
                    await snap(
                        page,
                        "pipeline",
                        "role-panel",
                        theme,
                        note="Role side panel over the Pipeline list.",
                    )
                    await ctx.close()

                # These actions intentionally mutate only the throwaway DB, after every
                # comparable light/dark/narrow page has been captured.
                special_ctx = await browser.new_context(
                    viewport={"width": 1440, "height": 900}, color_scheme="light"
                )
                page = await special_ctx.new_page()
                await page.goto(base + "/")
                save = page.locator("[data-testid=td-save]").first
                if await save.count():
                    await save.click()
                    await page.wait_for_timeout(300)
                    await snap(
                        page,
                        "today",
                        "undo-toast",
                        "light",
                        note="Save for later confirmation with Undo.",
                    )
                await page.goto(base + "/settings#privacy")
                if await page.locator("[data-testid=st-conf-switch]").count():
                    await page.locator("label:has([data-testid=st-conf-switch])").click()
                    await page.wait_for_timeout(350)
                    await snap(
                        page,
                        "settings",
                        "confidential-on",
                        "light",
                        note="Confidential mode and employer preview.",
                    )
                    await visit(
                        page,
                        "pipeline",
                        "confidential-on",
                        "/opportunities",
                        "light",
                        "Pipeline with confidential mode enabled.",
                    )
                await page.goto(base + f"/opportunities/{ids['opp']}")
                await page.locator("[data-testid=more]").click()
                await page.locator("[data-testid=escalate]").click()
                await page.wait_for_timeout(300)
                await snap(
                    page,
                    "role",
                    "refused-online",
                    "light",
                    note="Deeper read is preview only without an online key.",
                )
                await page.goto(base + "/renders/999999999")
                await snap(
                    page,
                    "error",
                    "missing-render",
                    "light",
                    note="User-facing response to a stale render link.",
                )
                await special_ctx.close()
            finally:
                await browser.close()

    await reset_engine()
    (OUT / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    (ROOT / "docs" / "WALK-FINDINGS.md").write_text("\n".join(findings) + "\n", encoding="utf-8")
    written = len(list(OUT.glob("*.png")))
    print(f"{written} screenshots; {sum(x.startswith('## ') for x in findings)} findings")
    return 1 if capture_errors or written != len(filenames) else 0


def main() -> int:
    # Both redirects happen before any Kestrel import. LOCALAPPDATA also prevents a library
    # fallback from reading the owner's real Kestrel directory.
    temp_root = Path(tempfile.mkdtemp(prefix="kestrel-walk-", dir=ROOT))
    os.environ["KESTREL_DATA_HOME"] = str(temp_root / "data")
    os.environ["LOCALAPPDATA"] = str(temp_root / "localappdata")
    try:
        return asyncio.run(walk())
    finally:
        for _ in range(5):
            try:
                shutil.rmtree(temp_root)
                break
            except PermissionError:
                import time

                time.sleep(1)
        if temp_root.exists():
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--cleanup", str(temp_root)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print(f"Temporary data cleanup scheduled after exit: {temp_root}")
        else:
            print("Temporary fictional data removed.")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--cleanup":
        cleanup_path = Path(sys.argv[2]).resolve()
        if cleanup_path.parent != ROOT or not cleanup_path.name.startswith("kestrel-walk-"):
            raise SystemExit("Refusing unsafe cleanup path")
        import time

        for _ in range(30):
            time.sleep(1)
            try:
                shutil.rmtree(cleanup_path)
                break
            except PermissionError:
                continue
        raise SystemExit(1 if cleanup_path.exists() else 0)
    raise SystemExit(main())
