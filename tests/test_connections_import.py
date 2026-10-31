"""The LinkedIn connections import, phases 1 and 2 (D-UI-115, D-UI-131). Fictional people only.
No network. Includes the privacy guarantee that no email address reaches the database or a log."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import structlog
from sqlalchemy import func, select

from kestrel.db.models import (
    Connection,
    ConnectionPosition,
    Contact,
    ContactImport,
    Interaction,
    Org,
    OutreachDraft,
)
from kestrel.research import connections as cx

FIX = Path(__file__).parent / "fixtures" / "connections"


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


FIRST = _text("Connections_first.csv")
SECOND = _text("Connections_second.csv")


async def _orgs(session, *names: str) -> dict[str, int]:
    out = {}
    for n in names:
        org = Org(name=n, normalized_name=n.lower())
        session.add(org)
        await session.flush()
        out[n] = org.id
    await session.commit()
    return out


# ------------------------------------------------------------------------------- the parser
def test_parser_skips_the_preamble_and_reads_the_rows():
    parsed = cx.parse_connections_csv(FIRST)
    assert len(parsed.rows) == 6 and parsed.skipped_no_name == 0
    imogen = parsed.rows[0]
    assert (imogen.first_name, imogen.last_name) == ("Imogen", "Fairweather")
    assert imogen.company == "Cedar Ridge Foods Inc."
    assert imogen.position == "Plant Manager"
    assert imogen.connected_on == "2023-03-04"
    assert parsed.rows[1].position == "Director, Operations"  # quoted comma
    pell = parsed.rows[4]
    assert pell.company is None and pell.position is None  # blank cells are None, not ""


def test_parser_has_no_email_or_url_field_at_all():
    parsed = cx.parse_connections_csv(FIRST)
    assert set(cx.ParsedConnection.__dataclass_fields__) == {
        "first_name", "last_name", "company", "position", "connected_on"
    }
    blob = repr(parsed)
    assert "@" not in blob and "mail.invalid" not in blob and "linkedin.com" not in blob


def test_parser_handles_reordered_columns_a_bom_and_missing_optional_columns():
    text = "﻿Company,Connected On,First Name,Last Name,Position\nNorthwind,02 Jan 2024,A,B,Lead\n"
    row = cx.parse_connections_csv(text).rows[0]
    assert (row.first_name, row.last_name, row.company, row.position) == (
        "A", "B", "Northwind", "Lead"
    )
    assert row.connected_on == "2024-01-02"


def test_parser_counts_nameless_rows_and_refuses_a_file_without_a_header():
    text = "First Name,Last Name,Company\n,,Acme\nZed,,Acme\n"
    parsed = cx.parse_connections_csv(text)
    assert parsed.skipped_no_name == 1 and [r.name for r in parsed.rows] == ["Zed"]
    with pytest.raises(cx.ConnectionsFileError):
        cx.parse_connections_csv("just,some,columns\n1,2,3\n")
    with pytest.raises(cx.ConnectionsFileError):  # prose that mentions the words is not a header
        cx.parse_connections_csv('Notes:\n"First Name and Company are columns"\n')


def test_an_unreadable_date_is_none_not_an_error():
    assert cx.parse_connected_on("04 Mar 2023") == "2023-03-04"
    assert cx.parse_connected_on("2023-03-04") == "2023-03-04"
    assert cx.parse_connected_on("someday") is None and cx.parse_connected_on("") is None


# --------------------------------------------------------------------------- company matching
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Cedar Ridge Foods Inc.", "cedar ridge foods"),
        ("Harbor & Sons Co.", "Harbor and Sons"),
        ("Northwind Logistics, LLC", "Northwind Logistics"),
        ("The Ledgerwood Company", "Ledgerwood"),
        ("O'Neil Corp", "ONeil"),
    ],
)
def test_normalization_ignores_suffixes_case_punctuation_and_ampersand(a, b):
    assert cx.company_key(a) == cx.company_key(b)


def test_normalization_keeps_distinct_names_distinct():
    assert cx.company_key("Cedar Ridge") != cx.company_key("Cedar Ridge Foods")
    assert cx.company_key("Co") == "co"  # a name that is only a suffix is left alone


async def test_sure_and_possible_matches(sm):
    async with sm() as s:
        ids = await _orgs(s, "Cedar Ridge Foods", "Harbor and Sons", "Northwind Logistics Inc")
        parsed = cx.parse_connections_csv(FIRST)
        prev = await cx.preview_import(s, parsed)
    by_name = {c.company_name: c for c in prev.companies}
    assert by_name["Cedar Ridge Foods Inc."].match == "sure"
    assert by_name["Cedar Ridge Foods Inc."].org_id == ids["Cedar Ridge Foods"]
    assert by_name["Harbor & Sons Co."].match == "sure"
    assert by_name["Northwind Logistics LLC"].match == "sure"
    cedar = by_name["Cedar Ridge"]  # contained in "Cedar Ridge Foods": proposed, not applied
    assert cedar.match == "possible" and cedar.needs_confirmation
    assert cedar.org_id == ids["Cedar Ridge Foods"]
    assert by_name["Ledgerwood Partners"].match == "none"


async def test_a_possible_match_is_never_linked_until_confirmed(sm):
    async with sm() as s:
        ids = await _orgs(s, "Cedar Ridge Foods")
        await cx.apply_import(s, cx.parse_connections_csv(FIRST), file_name="a.csv")
        dmitri = (await s.execute(select(Contact).where(Contact.name.like("Dmitri%")))
                  ).scalar_one()
        assert dmitri.org_id is None  # Possible: the contact is not linked
        pos = (await s.execute(select(ConnectionPosition).where(
            ConnectionPosition.contact_id == dmitri.id))).scalar_one()
        assert pos.match == "possible" and pos.org_id == ids["Cedar Ridge Foods"]
        imogen = (await s.execute(select(Contact).where(Contact.name.like("Imogen%")))
                  ).scalar_one()
        assert imogen.org_id == ids["Cedar Ridge Foods"]  # Sure: linked

        assert await cx.confirm_match(s, "Cedar Ridge") == 1
        await s.refresh(dmitri)
        await s.refresh(pos)
        assert dmitri.org_id == ids["Cedar Ridge Foods"] and pos.match == "manual"


async def test_not_a_match_is_remembered_by_the_next_import(sm):
    async with sm() as s:
        await _orgs(s, "Cedar Ridge Foods")
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        assert await cx.reject_match(s, "Cedar Ridge") == 1
        prev = await cx.preview_import(s, cx.parse_connections_csv(SECOND))
        cedar = next(c for c in prev.companies if c.company_name == "Cedar Ridge")
        assert cedar.match == "none" and cedar.decided
        await cx.apply_import(s, cx.parse_connections_csv(SECOND))
        pos = (await s.execute(select(ConnectionPosition).where(
            ConnectionPosition.company_name == "Cedar Ridge",
            ConnectionPosition.is_current.is_(True)))).scalar_one()
        assert pos.match == "none" and pos.org_id is None and pos.operator_decided


async def test_a_changed_org_choice_is_remembered_too(sm):
    async with sm() as s:
        ids = await _orgs(s, "Cedar Ridge Foods", "Cedar Ridge Holdings")
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        await cx.set_match(s, "Cedar Ridge", ids["Cedar Ridge Holdings"])
        await cx.apply_import(s, cx.parse_connections_csv(SECOND))
        pos = (await s.execute(select(ConnectionPosition).where(
            ConnectionPosition.company_name == "Cedar Ridge",
            ConnectionPosition.is_current.is_(True)))).scalar_one()
        assert pos.match == "manual" and pos.org_id == ids["Cedar Ridge Holdings"]
        with pytest.raises(ValueError):
            await cx.set_match(s, "Cedar Ridge", 99999)


async def test_orgs_that_arrive_later_are_matched_on_request(sm):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        ledger = (await s.execute(select(Contact).where(Contact.name.like("Wren%")))
                  ).scalar_one()
        assert ledger.org_id is None
        ids = await _orgs(s, "Ledgerwood Partners")
        assert await cx.rematch_undecided(s) >= 1
        await s.refresh(ledger)
        assert ledger.org_id == ids["Ledgerwood Partners"]


# ----------------------------------------------------------------------- phase 1: first import
async def test_first_import_creates_contacts_positions_and_one_batch_row(sm):
    async with sm() as s:
        await _orgs(s, "Cedar Ridge Foods")
        batch = await cx.apply_import(s, cx.parse_connections_csv(FIRST), file_name="Conn.csv")
        assert (batch.row_count, batch.n_new, batch.n_changed, batch.n_missing) == (6, 6, 0, 0)
        assert batch.file_name == "Conn.csv"
        contacts = (await s.execute(select(Contact))).scalars().all()
        assert len(contacts) == 6
        assert all(c.source == "linkedin_import" and c.email is None for c in contacts)
        assert all(c.linkedin_url is None for c in contacts)
        assert (await s.execute(select(func.count()).select_from(Connection))).scalar() == 6
        positions = (await s.execute(select(ConnectionPosition))).scalars().all()
        assert len(positions) == 6 and all(p.is_current for p in positions)
        assert all(p.first_import_id == batch.id == p.last_import_id for p in positions)
        pell = next(p for p in positions if p.title is None)
        assert pell.company_name is None and pell.match == "none"  # counted, not matched
        assert (await s.execute(select(func.count()).select_from(ContactImport))).scalar() == 1


async def test_preview_writes_nothing(sm):
    async with sm() as s:
        prev = await cx.preview_import(s, cx.parse_connections_csv(FIRST))
        assert prev.first_import and len(prev.new) == 6 and prev.n_no_company == 1
        assert (await s.execute(select(func.count()).select_from(Contact))).scalar() == 0
        assert (await s.execute(select(func.count()).select_from(ContactImport))).scalar() == 0


async def test_importing_the_same_file_twice_changes_nothing(sm):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        again = await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        assert (again.n_new, again.n_changed, again.n_missing) == (0, 0, 0)
        assert (await s.execute(select(func.count()).select_from(Contact))).scalar() == 6
        assert (await s.execute(select(func.count()).select_from(ConnectionPosition))
                ).scalar() == 6
        # Every position was seen again by the second import.
        assert (await s.execute(select(func.count()).select_from(ConnectionPosition).where(
            ConnectionPosition.last_import_id == again.id))).scalar() == 6


# --------------------------------------------------------------------- phase 1: the diff
async def test_diff_reports_new_changed_and_missing(sm):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        prev = await cx.preview_import(s, cx.parse_connections_csv(SECOND))
    assert [p.name for p in prev.new] == ["Isolde Marchetti"]
    assert sorted(c.name for c in prev.changed) == ["Imogen Fairweather", "Tobias Quill"]
    assert [p.name for p in prev.missing] == ["Wren Calloway"]
    assert prev.n_unchanged == 3 and not prev.first_import
    imogen = next(c for c in prev.changed if c.name.startswith("Imogen"))
    assert (imogen.old_company, imogen.new_company) == (
        "Cedar Ridge Foods Inc.", "Northwind Logistics"
    )


async def test_apply_second_import_and_keep_the_missing(sm):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        batch = await cx.apply_import(s, cx.parse_connections_csv(SECOND), keep_missing=True)
        assert (batch.n_new, batch.n_changed, batch.n_missing) == (1, 2, 1)
        assert batch.kept_missing is True
        names = set((await s.execute(select(Contact.name))).scalars())
        assert "Wren Calloway" in names and "Isolde Marchetti" in names


async def test_apply_second_import_and_remove_the_missing(sm):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        wren = (await s.execute(select(Contact).where(Contact.name.like("Wren%")))).scalar_one()
        batch = await cx.apply_import(s, cx.parse_connections_csv(SECOND), keep_missing=False)
        assert batch.n_missing == 1 and batch.kept_missing is False
        names = set((await s.execute(select(Contact.name))).scalars())
        assert "Wren Calloway" not in names
        assert (await s.execute(select(func.count()).select_from(ConnectionPosition).where(
            ConnectionPosition.contact_id == wren.id))).scalar() == 0
        assert (await s.execute(select(func.count()).select_from(Connection).where(
            Connection.contact_id == wren.id))).scalar() == 0


async def test_a_title_only_change_updates_in_place_without_history(sm):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        await cx.apply_import(s, cx.parse_connections_csv(SECOND))
        tobias = (await s.execute(select(Contact).where(Contact.name.like("Tobias%")))
                  ).scalar_one()
        rows = (await s.execute(select(ConnectionPosition).where(
            ConnectionPosition.contact_id == tobias.id))).scalars().all()
        assert len(rows) == 1 and rows[0].is_current
        assert rows[0].title == "Senior Director Operations" and tobias.title == rows[0].title


# ------------------------------------------------------------ phase 2: history, warm paths
async def test_company_change_keeps_the_earlier_position_as_history(sm):
    async with sm() as s:
        await _orgs(s, "Cedar Ridge Foods", "Northwind Logistics")
        first = await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        second = await cx.apply_import(s, cx.parse_connections_csv(SECOND))
        imogen = (await s.execute(select(Contact).where(Contact.name.like("Imogen%")))
                  ).scalar_one()
        rows = (await s.execute(select(ConnectionPosition).where(
            ConnectionPosition.contact_id == imogen.id).order_by(ConnectionPosition.id))
        ).scalars().all()
        assert [r.is_current for r in rows] == [False, True]
        old, new = rows
        assert old.company_name == "Cedar Ridge Foods Inc." and old.last_import_id == first.id
        assert new.company_name == "Northwind Logistics" and new.first_import_id == second.id
        assert imogen.org_id == new.org_id and imogen.title == "Operations Director"


async def test_warm_paths_works_here_now_and_used_to_work_here(sm):
    async with sm() as s:
        ids = await _orgs(s, "Cedar Ridge Foods", "Northwind Logistics")
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        # After one import Imogen works at Cedar Ridge Foods (Sure); Dmitri is Possible.
        paths = await cx.connection_paths(s, ids["Cedar Ridge Foods"])
        assert [(p.name, p.kind, p.confirmed) for p in paths] == [
            ("Imogen Fairweather", cx.WORKS_HERE, True),
            ("Dmitri Okonkwo-Hale", cx.WORKS_HERE, False),  # unconfirmed Possible, listed last
        ]
        assert paths[0].imported_at and paths[0].title == "Plant Manager"

        await cx.apply_import(s, cx.parse_connections_csv(SECOND))
        cedar = await cx.connection_paths(s, ids["Cedar Ridge Foods"])
        by = {p.name: p for p in cedar}
        assert by["Imogen Fairweather"].kind == cx.USED_TO_WORK_HERE
        assert by["Isolde Marchetti"].kind == cx.WORKS_HERE
        assert [p.kind for p in cedar].index(cx.USED_TO_WORK_HERE) > [
            p.kind for p in cedar
        ].index(cx.WORKS_HERE)  # works-here-now ranks first
        north = await cx.connection_paths(s, ids["Northwind Logistics"])
        assert {(p.name, p.kind) for p in north} == {
            ("Imogen Fairweather", cx.WORKS_HERE), ("Marisol Venn", cx.WORKS_HERE)
        }


async def test_someone_who_works_here_again_is_not_also_listed_as_a_former_employee(sm):
    async with sm() as s:
        ids = await _orgs(s, "Acme")
        a = cx.ParsedFile(rows=[cx.ParsedConnection("Ro", "Vale", "Acme", "Lead", "2020-01-01")])
        b = cx.ParsedFile(rows=[cx.ParsedConnection("Ro", "Vale", "Beta", "Lead", "2020-01-01")])
        await cx.apply_import(s, a)
        await cx.apply_import(s, b)
        await cx.apply_import(s, a)
        paths = await cx.connection_paths(s, ids["Acme"])
        assert [(p.name, p.kind) for p in paths] == [("Ro Vale", cx.WORKS_HERE)]


async def test_a_company_nobody_matches_has_no_paths(sm):
    async with sm() as s:
        ids = await _orgs(s, "Unrelated Org")
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        assert await cx.connection_paths(s, ids["Unrelated Org"]) == []


# ------------------------------------------------------------------------- remove all contacts
async def test_remove_all_deletes_only_imported_rows_and_keeps_what_the_operator_wrote(sm):
    async with sm() as s:
        ids = await _orgs(s, "Cedar Ridge Foods")
        public = Contact(name="Public Person", org_id=ids["Cedar Ridge Foods"],
                         source="research", contact_type="exec")
        s.add(public)
        await s.commit()
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        imogen = (await s.execute(select(Contact).where(Contact.name.like("Imogen%")))
                  ).scalar_one()
        tobias = (await s.execute(select(Contact).where(Contact.name.like("Tobias%")))
                  ).scalar_one()
        s.add(Interaction(contact_id=imogen.id, kind="message"))
        s.add(OutreachDraft(contact_id=imogen.id, channel="email", body="draft"))
        tobias.notes = "met at the plant tour"
        s.add(Interaction(contact_id=public.id, kind="call"))
        await s.commit()
        assert await cx.imported_contact_count(s) == 6

        result = await cx.remove_all_imported(s)
        assert (result.removed, result.kept) == (4, 2)
        names = {c.name: c for c in (await s.execute(select(Contact))).scalars()}
        assert set(names) == {"Public Person", "Imogen Fairweather", "Tobias Quill"}
        # The kept ones are no longer imports; their records are untouched.
        assert names["Imogen Fairweather"].source == cx.KEPT_SOURCE
        assert names["Tobias Quill"].notes == "met at the plant tour"
        for model in (ConnectionPosition, ContactImport, Connection):
            assert (await s.execute(select(func.count()).select_from(model))).scalar() == 0
        assert (await s.execute(select(func.count()).select_from(OutreachDraft))).scalar() == 1
        assert (await s.execute(select(func.count()).select_from(Interaction))).scalar() == 2
        assert (await s.execute(select(func.count()).select_from(Org))).scalar() == 1


async def test_a_missing_person_with_operator_records_is_kept_even_when_removal_is_chosen(sm):
    """Regression: removal used to delete the interactions and drafts the operator wrote."""
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        wren = (await s.execute(select(Contact).where(Contact.name.like("Wren%")))).scalar_one()
        s.add(OutreachDraft(contact_id=wren.id, channel="email", body="hello"))
        s.add(Interaction(contact_id=wren.id, kind="call"))
        await s.commit()
        batch = await cx.apply_import(s, cx.parse_connections_csv(SECOND), keep_missing=False)
        assert batch.n_missing == 1
        assert (await s.execute(select(Contact).where(Contact.name.like("Wren%")))
                ).scalar_one().id == wren.id
        assert (await s.execute(select(func.count()).select_from(OutreachDraft))).scalar() == 1
        assert (await s.execute(select(func.count()).select_from(Interaction))).scalar() == 1
        # Still an import, still shown as missing next time.
        prev = await cx.preview_import(s, cx.parse_connections_csv(SECOND))
        assert [p.name for p in prev.missing] == ["Wren Calloway"]


async def test_the_import_after_a_removal_is_a_fresh_first_import(sm):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        await cx.remove_all_imported(s)
        prev = await cx.preview_import(s, cx.parse_connections_csv(FIRST))
        assert prev.first_import and len(prev.new) == 6


# ----------------------------------------------------------------- the legacy one-step wrapper
async def test_the_one_step_wrapper_stores_no_email(sm):
    from kestrel.research.graph import import_linkedin_csv

    async with sm() as s:
        assert await import_linkedin_csv(s, FIRST) == 5  # five of the six have a company
        assert await import_linkedin_csv(s, "not a connections file") == 0
        assert all(c.email is None for c in (await s.execute(select(Contact))).scalars())


# ------------------------------------------------------------------- privacy: no email, ever
def _dump(path) -> str:
    con = sqlite3.connect(path)
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


async def test_no_email_reaches_the_database_or_the_logs(tmp_path, capsys):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db import migrate
    from kestrel.db.engine import make_async_engine

    db = tmp_path / "priv.db"
    migrate.run_migrations(db)
    engine = make_async_engine(db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    emails = [
        "imogen.fairweather@mail.invalid", "tobias.quill@mail.invalid",
        "dmitri.hale@mail.invalid", "wren.calloway@mail.invalid",
        "isolde.marchetti@mail.invalid",
    ]
    assert all(e in FIRST + SECOND for e in emails[:3])  # the fixtures do carry emails
    try:
        with structlog.testing.capture_logs() as logs:
            async with sm() as s:
                await _orgs(s, "Cedar Ridge Foods")
                parsed = cx.parse_connections_csv(FIRST)
                await cx.preview_import(s, parsed)
                await cx.apply_import(s, parsed, file_name="Connections.csv")
                parsed2 = cx.parse_connections_csv(SECOND)
                await cx.preview_import(s, parsed2)
                await cx.apply_import(s, parsed2, keep_missing=False)
                await cx.confirm_match(s, "Cedar Ridge")
                await cx.connection_paths(s, 1)
                await cx.remove_all_imported(s)
                await cx.apply_import(s, parsed)
    finally:
        await engine.dispose()
    dump = _dump(db)
    captured = capsys.readouterr()
    haystacks = {
        "database": dump,
        "structlog events": repr(logs),
        "stdout and stderr": captured.out + captured.err,
    }
    for where, text in haystacks.items():
        low = text.lower()
        assert "mail.invalid" not in low, f"an email domain reached the {where}"
        for e in emails:
            assert e not in low, f"{e} reached the {where}"
        assert "linkedin.com/in/" not in low, f"a profile URL reached the {where}"
    # And the log events that were emitted carry counts only.
    events = [e for e in logs if e.get("event", "").startswith("connections_")]
    assert events, "the import logs its counts"
    for e in events:
        assert all(isinstance(v, (int, bool)) or k in ("event", "log_level") for k, v in e.items())


def test_the_email_column_is_not_read_even_if_it_is_the_only_one_with_data(tmp_path):
    text = ("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
            "Zed,Rowe,u,zed.rowe@mail.invalid,Acme,Lead,01 Jan 2020\n")
    parsed = cx.parse_connections_csv(text)
    assert "mail.invalid" not in repr(parsed)
    assert parsed.rows[0].company == "Acme"


# ----------------------------------------------------------------- review fixes: parser
def test_quoted_fields_survive_unusual_line_breaks_exactly():
    text = ("First Name,Last Name,Company,Position,Connected On\r\n"
            'Ana,Lorn,"Acme\x85Works, Inc.","Head of\u2028Ops",03 Mar 2023\r\n')
    row = cx.parse_connections_csv(text).rows[0]
    assert row.company == "Acme Works, Inc." and row.position == "Head of Ops"
    assert row.connected_on == "2023-03-03"


def test_dates_do_not_depend_on_the_computers_locale():
    assert cx.parse_connected_on("12 Mar 2024") == "2024-03-12"
    assert cx.parse_connected_on("5 September 2021") == "2021-09-05"
    assert cx.parse_connected_on("Mar 12, 2024") == "2024-03-12"
    assert cx.parse_connected_on("31 Feb 2024") is None
    assert cx.parse_connected_on("12 Xyz 2024") is None  # not a month


def test_an_oversize_file_is_refused_not_truncated(monkeypatch):
    monkeypatch.setattr(cx, "MAX_ROWS", 3)
    head = "First Name,Last Name,Company,Position,Connected On\n"
    body = "".join(f"P{i},Q,Acme,Lead,01 Jan 2020\n" for i in range(4))
    with pytest.raises(cx.ConnectionsFileError, match="more than 3"):
        cx.parse_connections_csv(head + body)
    ok = "".join(f"P{i},Q,Acme,Lead,01 Jan 2020\n" for i in range(3))
    assert len(cx.parse_connections_csv(head + ok).rows) == 3


async def test_an_oversize_file_removes_nobody(sm, monkeypatch):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        monkeypatch.setattr(cx, "MAX_ROWS", 2)
        with pytest.raises(cx.ConnectionsFileError):
            cx.parse_connections_csv(SECOND)
        assert await cx.imported_contact_count(s) == 6


# ----------------------------------------------------------------- review fixes: pairing
async def test_a_date_that_becomes_readable_does_not_duplicate_or_delete(sm):
    head = "First Name,Last Name,Company,Position,Connected On\n"
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(head + "Ana,Lorn,Acme,Lead,\n"))
        again = cx.parse_connections_csv(head + "Ana,Lorn,Acme,Lead,03 Mar 2023\n")
        prev = await cx.preview_import(s, again)
        assert prev.new == [] and prev.missing == [] and prev.n_unchanged == 1
        batch = await cx.apply_import(s, again, keep_missing=False)
        assert (batch.n_new, batch.n_missing) == (0, 0)
        assert await cx.imported_contact_count(s) == 1
        pos = (await s.execute(select(ConnectionPosition))).scalar_one()
        assert pos.connected_on == "2023-03-03"


# ----------------------------------------------------------------- review fixes: matching
async def test_a_ruling_whose_org_was_deleted_is_flagged_not_rematched(sm):
    async with sm() as s:
        ids = await _orgs(s, "Cedar Ridge Foods", "Cedar Ridge Holdings")
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        await cx.set_match(s, "Cedar Ridge", ids["Cedar Ridge Holdings"])
        await s.execute(ConnectionPosition.__table__.update().values(org_id=None)
                        .where(ConnectionPosition.org_id == ids["Cedar Ridge Holdings"]))
        await s.execute(Contact.__table__.update().values(org_id=None)
                        .where(Contact.org_id == ids["Cedar Ridge Holdings"]))
        await s.delete(await s.get(Org, ids["Cedar Ridge Holdings"]))
        await s.commit()
        prev = await cx.preview_import(s, cx.parse_connections_csv(SECOND))
        cedar = next(c for c in prev.companies if c.company_name == "Cedar Ridge")
        assert cedar.ruling_lost and cedar.needs_attention
        assert cedar.match == "none" and cedar.org_id is None  # not silently auto-matched


async def test_rematch_never_erases_or_downgrades_an_existing_link(sm):
    async with sm() as s:
        ids = await _orgs(s, "Cedar Ridge Foods")
        await cx.apply_import(s, cx.parse_connections_csv(FIRST))
        imogen = (await s.execute(select(Contact).where(Contact.name.like("Imogen%")))
                  ).scalar_one()
        assert imogen.org_id == ids["Cedar Ridge Foods"]
        await _orgs(s, "Cedar Ridge Foods Holdings West")
        await cx.rematch_undecided(s)
        await s.refresh(imogen)
        assert imogen.org_id == ids["Cedar Ridge Foods"]
        pos = (await s.execute(select(ConnectionPosition).where(
            ConnectionPosition.contact_id == imogen.id))).scalar_one()
        assert pos.match == "sure"
        wren = (await s.execute(select(ConnectionPosition).where(
            ConnectionPosition.company_name == "Ledgerwood Partners"))).scalar_one()
        assert wren.match == "none"  # a name that still matches nothing is left as it was


# ----------------------------------------------------------- review fixes: the one-step wrapper
async def test_the_one_step_importer_links_unmatched_companies_without_seeded_orgs(sm):
    from kestrel.research.graph import import_linkedin_csv, warm_paths

    async with sm() as s:
        assert await import_linkedin_csv(s, FIRST) == 5
        orgs = {o.name: o.id for o in (await s.execute(select(Org))).scalars()}
        assert "Ledgerwood Partners" in orgs and "Cedar Ridge Foods Inc." in orgs
        wren = (await s.execute(select(Contact).where(Contact.name.like("Wren%")))).scalar_one()
        assert wren.org_id == orgs["Ledgerwood Partners"] and wren.source == cx.SOURCE
        got = await warm_paths(s, orgs["Ledgerwood Partners"])
        assert [(w.contact.name, w.tier) for w in got] == [("Wren Calloway", 1)]
        assert await import_linkedin_csv(s, FIRST) == 5  # again: refreshed, not duplicated
        assert await cx.imported_contact_count(s) == 6


# ----------------------------------------------------------------- last review items
HEAD = "First Name,Last Name,Company,Position,Connected On\n"


async def test_name_fallback_never_merges_two_people(sm):
    async with sm() as s:
        first = cx.parse_connections_csv(
            HEAD + "Sam,Lee,Acme,Lead,01 Jan 2020\nSam,Lee,Beta,Lead,02 Feb 2021\n"
        )
        await cx.apply_import(s, first)
        # No date and a third company: either stored Sam Lee could be this row, so it is new.
        ambiguous = cx.parse_connections_csv(HEAD + "Sam,Lee,Gamma,Lead,\n")
        prev = await cx.preview_import(s, ambiguous)
        assert [p.name for p in prev.new] == ["Sam Lee"] and len(prev.missing) == 2
        # Same company as exactly one of them, with a different date: that one, and only that one.
        clear = cx.parse_connections_csv(HEAD + "Sam,Lee,Beta,Lead,09 Sep 2022\n")
        prev = await cx.preview_import(s, clear)
        assert prev.new == [] and [p.company for p in prev.missing] == ["Acme"]
        assert prev.n_unchanged == 1


async def test_two_rows_cannot_claim_the_same_stored_person(sm):
    async with sm() as s:
        await cx.apply_import(s, cx.parse_connections_csv(HEAD + "Sam,Lee,Acme,Lead,\n"))
        both = cx.parse_connections_csv(
            HEAD + "Sam,Lee,Acme,Lead,01 Jan 2020\nSam,Lee,Acme,Lead,02 Feb 2021\n"
        )
        prev = await cx.preview_import(s, both)
        assert len(prev.new) == 1 and prev.n_unchanged == 1 and prev.missing == []


async def test_the_one_step_importer_is_one_transaction(sm, monkeypatch):
    from kestrel.research.graph import import_linkedin_csv

    async def boom(*a, **k):
        raise RuntimeError("adopt failed")

    monkeypatch.setattr(cx, "adopt_org", boom)
    async with sm() as s:
        with pytest.raises(RuntimeError):
            await import_linkedin_csv(s, FIRST)
    async with sm() as s:
        for model in (Org, Contact, ContactImport, ConnectionPosition):
            assert (await s.execute(select(func.count()).select_from(model))).scalar() == 0
