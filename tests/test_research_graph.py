"""Offline, deterministic tests for the networking graph: LinkedIn CSV import (a first-party
export, no scraping) and warm-path finding (a plain graph query over contact + connection, no ML).
No network, no model."""

from __future__ import annotations

from sqlalchemy import func, select

from kestrel.db.models import Connection, Contact, Interaction, Org
from kestrel.ingest.orgs import resolve_org
from kestrel.research.graph import import_linkedin_csv, record_interaction, warm_paths

# A real LinkedIn "Connections.csv" export prepends a few "Notes:" preamble lines and a blank
# line before the actual header row. One Position value is quoted with an embedded comma to
# exercise csv.DictReader's quoted-field handling. Bob has a blank Email Address (LinkedIn only
# shares email for connections who've opted in).
CSV_FIXTURE = """Notes:
"When exporting your connection data, you download a file that contains information about your
connections in a CSV file. This file will show up in your Downloads folder as Connections.csv."

First Name,Last Name,Email Address,Company,Position,Connected On
Alice,Anderson,alice@example.com,Acme Corp,VP Engineering,01 Jan 2024
Bob,Brown,,Acme Corp,"Director, Sales Ops",02 Feb 2024
Carol,Carter,,Beta Industries,Former COO,03 Mar 2024
Dan,Dawson,,Gamma LLC,Engineer,04 Apr 2024
Eve,Evans,,Delta Co,COO,05 May 2024
"""


async def _seed_orgs(session) -> None:
    """The importer matches company names to organizations that already exist (it never creates
    one), so the fixture's companies are created first, as a scan would have."""
    for name in ("Acme Corp", "Beta Industries", "Gamma LLC", "Delta Co"):
        await resolve_org(session, name=name, domain=None)
    await session.commit()


async def test_import_linkedin_csv_skips_preamble_and_creates_rows(sm):
    async with sm() as session:
        await _seed_orgs(session)
        count = await import_linkedin_csv(session, CSV_FIXTURE)
        assert count == 5

        contact_count = (
            await session.execute(select(func.count()).select_from(Contact))
        ).scalar()
        connection_count = (
            await session.execute(select(func.count()).select_from(Connection))
        ).scalar()
        assert contact_count == 5
        assert connection_count == 5

        # The preamble text never became a contact/org (it contains no "Company" column value).
        names = set(
            (await session.execute(select(Contact.name))).scalars().all()
        )
        assert names == {"Alice Anderson", "Bob Brown", "Carol Carter", "Dan Dawson", "Eve Evans"}

        alice = (
            await session.execute(select(Contact).where(Contact.name == "Alice Anderson"))
        ).scalar_one()
        assert alice.email is None  # the email column is dropped at read time (D-UI-131)
        assert alice.source == "linkedin_import"
        assert alice.relationship == "known"

        bob = (
            await session.execute(select(Contact).where(Contact.name == "Bob Brown"))
        ).scalar_one()
        assert bob.email is None
        assert bob.title == "Director, Sales Ops"  # quoted embedded-comma field parsed correctly

        acme = (
            await session.execute(select(Org).where(Org.name == "Acme Corp"))
        ).scalar_one()
        assert alice.org_id == acme.id and bob.org_id == acme.id

        # Every imported contact got a Connection row (the operator's own network).
        conn_count_for_alice = (
            await session.execute(
                select(func.count()).select_from(Connection).where(
                    Connection.contact_id == alice.id
                )
            )
        ).scalar()
        assert conn_count_for_alice == 1


async def test_import_linkedin_csv_twice_does_not_duplicate(sm):
    async with sm() as session:
        await _seed_orgs(session)
        assert await import_linkedin_csv(session, CSV_FIXTURE) == 5
        # The second export carries an updated title for Alice and an email for Bob.
        updated = CSV_FIXTURE.replace("VP Engineering", "SVP Engineering").replace(
            "Bob,Brown,,", "Bob,Brown,bob@example.com,"
        )
        assert await import_linkedin_csv(session, updated) == 5

        contacts = (await session.execute(select(func.count()).select_from(Contact))).scalar()
        connections = (
            await session.execute(select(func.count()).select_from(Connection))
        ).scalar()
        assert contacts == 5 and connections == 5  # same counts: upserted, not duplicated

        alice = (
            await session.execute(select(Contact).where(Contact.name == "Alice Anderson"))
        ).scalar_one()
        assert alice.title == "SVP Engineering"
        bob = (
            await session.execute(select(Contact).where(Contact.name == "Bob Brown"))
        ).scalar_one()
        assert bob.email is None  # still no email, even when the export carries one


async def _seeded_graph(session):
    """Import the fixture, then layer on the former-employer / investor overlap facts a real
    operator would add (LinkedIn's export carries only current company, not history)."""
    await _seed_orgs(session)
    await import_linkedin_csv(session, CSV_FIXTURE)

    acme = (await session.execute(select(Org).where(Org.name == "Acme Corp"))).scalar_one()
    delta = (await session.execute(select(Org).where(Org.name == "Delta Co"))).scalar_one()

    carol = (
        await session.execute(select(Contact).where(Contact.name == "Carol Carter"))
    ).scalar_one()
    dan = (
        await session.execute(select(Contact).where(Contact.name == "Dan Dawson"))
    ).scalar_one()

    # Tier 2: Carol is now at Beta Industries but was formerly at the target org (Acme Corp).
    carol.former_orgs = ["Acme Corp"]

    # Tier 4: Dan (at Gamma LLC) shares a former employer ("Zenith Partners") with a decision
    # maker at Acme Corp who is NOT one of the operator's own connections.
    dan.former_orgs = ["Zenith Partners"]
    diana = Contact(
        org_id=acme.id,
        name="Diana Decisionmaker",
        title="CTO",
        contact_type="exec",
        relationship="none",
        source="manual",
        former_orgs=["Zenith Partners"],
    )
    session.add(diana)

    # Tier 3: Eve's employer (Delta Co) shares a PE investor with the target org (Acme Corp).
    acme.signals = [
        {"category": "pe_investment", "firm": "Summit Capital Partners", "statement": "invested"}
    ]
    delta.signals = [
        {"category": "acquisition", "firm": "Summit Capital Partners", "statement": "acquired"}
    ]

    await session.commit()
    return acme, diana


async def test_warm_paths_ranks_tiers_correctly(sm):
    async with sm() as session:
        acme, diana = await _seeded_graph(session)

        paths = await warm_paths(session, acme.id)

        by_name = {p.contact.name: p for p in paths}

        # No contact appears in more than one tier / more than once in the result.
        names_seen = [p.contact.name for p in paths]
        assert len(names_seen) == len(set(names_seen))

        # Tier 1: Alice and Bob are direct connections currently at the target org.
        assert by_name["Alice Anderson"].tier == 1
        assert "Currently at Acme Corp" in by_name["Alice Anderson"].reason
        assert by_name["Bob Brown"].tier == 1

        # Tier 2: Carol is a direct connection formerly at the target org.
        assert by_name["Carol Carter"].tier == 2
        assert "Formerly at Acme Corp" in by_name["Carol Carter"].reason

        # Tier 3: Eve's org shares a PE investor with the target org.
        assert by_name["Eve Evans"].tier == 3
        assert "Summit Capital Partners" in by_name["Eve Evans"].reason
        assert "Acme Corp" in by_name["Eve Evans"].reason

        # Tier 4: Dan shares a former employer with a decision maker (Diana) at the target org.
        assert by_name["Dan Dawson"].tier == 4
        assert "Zenith Partners" in by_name["Dan Dawson"].reason
        assert "Diana Decisionmaker" in by_name["Dan Dawson"].reason

        # The tier-1 connection is surfaced ahead of tier 2, which is ahead of tier 4.
        idx = {name: i for i, name in enumerate(names_seen)}
        assert idx["Alice Anderson"] < idx["Carol Carter"] < idx["Dan Dawson"]

        # Diana (a decision maker, not the operator's own connection) is never surfaced herself.
        assert "Diana Decisionmaker" not in by_name


async def test_warm_paths_respects_limit(sm):
    async with sm() as session:
        acme, _diana = await _seeded_graph(session)
        paths = await warm_paths(session, acme.id, limit=2)
        assert len(paths) == 2
        assert paths[0].tier <= paths[1].tier


async def test_warm_paths_unknown_org_returns_empty(sm):
    async with sm() as session:
        paths = await warm_paths(session, 999999)
        assert paths == []


async def test_record_interaction_appends_row(sm):
    async with sm() as session:
        org = await resolve_org(session, name="Acme Corp")
        contact = Contact(org_id=org.id, name="Alice Anderson", source="manual")
        session.add(contact)
        await session.flush()
        await session.commit()

        interaction = await record_interaction(
            session,
            contact.id,
            "call",
            direction="outbound",
            summary="Intro call about the CTO opening.",
            outcome="warm",
        )

        assert interaction.id is not None
        assert interaction.kind == "call"
        assert interaction.direction == "outbound"

        count = (
            await session.execute(
                select(func.count()).select_from(Interaction).where(
                    Interaction.contact_id == contact.id
                )
            )
        ).scalar()
        assert count == 1
