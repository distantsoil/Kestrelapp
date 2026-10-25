"""The UI overhaul's back-end schema: first and last name, hidden companies, connection imports

* `profile.first_name` / `profile.last_name` (D-UI-117). An existing `full_name` is split on its
  first space into the two (D-UI-117); `full_name` itself is kept as written (it is the resume
  header's name and the original text is never discarded). A one-word name leaves `last_name` empty.
* `org.hidden_at` (D-UI-123): a hidden company's roles stay out of Pipeline and Today.
* `org_summary` (SPEC-MODULES 2.1, D-LLM-9): the sourced company summary, one row per org.
* `contact_import` and `connection_position` (D-UI-115, SPEC-MODULES 2.3): the LinkedIn
  connections import and its position history. No email column exists anywhere (D-UI-131).

* Contacts from the older importer (`source` = `linkedin_csv`, which also stored an email
  address) are adopted into the new model: each gets a current `connection_position` built from
  its organization and title, its `source` becomes `linkedin_import`, and **any stored email is
  erased** (D-UI-131). They are then diffed, matched and removed with the rest. This step is not
  undone by the downgrade.

The downgrade drops `org.hidden_at` (so every company is shown again, and which ones were hidden
is lost), `profile.first_name` and `last_name` (the kept `full_name` still holds the name),
`org_summary`, `contact_import` and `connection_position` (the import history, positions and
rulings are lost; the contacts themselves stay). Columns are added and dropped with plain
ALTER TABLE, never a table copy: `org` is a parent table, and with foreign_keys=ON a copy's
DROP TABLE fails on any row that references it (see 0013).

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def _split(full_name: str | None) -> tuple[str | None, str | None]:
    """(first, last) on the first space. Inlined, not imported: a migration must keep working
    when application code changes."""
    text = " ".join(str(full_name or "").split())
    if not text:
        return None, None
    first, sep, last = text.partition(" ")
    return first, (last if sep else None)


def upgrade() -> None:
    with op.batch_alter_table("profile", recreate="never") as batch:
        batch.add_column(sa.Column("first_name", sa.String(), nullable=True))
        batch.add_column(sa.Column("last_name", sa.String(), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, full_name FROM profile WHERE full_name IS NOT NULL")
    ).all()
    for row_id, full_name in rows:
        first, last = _split(full_name)
        if first is None:
            continue
        bind.execute(
            sa.text("UPDATE profile SET first_name = :f, last_name = :l WHERE id = :i"),
            {"f": first, "l": last, "i": row_id},
        )

    with op.batch_alter_table("org", recreate="never") as batch:
        batch.add_column(sa.Column("hidden_at", sa.String(), nullable=True))
    op.create_index("ix_org_hidden_at", "org", ["hidden_at"])

    op.create_table(
        "org_summary",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("sentences", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("generated_at", sa.String(), nullable=False),
        sa.Column("insufficient_sources", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.UniqueConstraint("org_id", name="uq_org_summary_org_id"),
    )
    op.create_table(
        "contact_import",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("imported_at", sa.String(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_missing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kept_missing", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "connection_position",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contact_id", sa.Integer(), sa.ForeignKey("contact.id"), nullable=False),
        sa.Column("company_name", sa.String(), nullable=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=True),
        sa.Column("match", sa.String(), nullable=False, server_default="none"),
        sa.Column("operator_decided", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("connected_on", sa.String(), nullable=True),
        sa.Column(
            "first_import_id", sa.Integer(), sa.ForeignKey("contact_import.id"), nullable=True
        ),
        sa.Column(
            "last_import_id", sa.Integer(), sa.ForeignKey("contact_import.id"), nullable=True
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_connection_position_contact_id", "connection_position", ["contact_id"])
    op.create_index("ix_connection_position_org_id", "connection_position", ["org_id"])

    # Adopt the older importer's contacts (see the docstring), and any import contact that has no
    # position (a downgrade drops the positions, and this upgrade runs again). Positions first,
    # while `source` still says where they came from.
    bind.execute(sa.text(
        "INSERT INTO connection_position (contact_id, company_name, org_id, match,"
        " operator_decided, title, connected_on, first_import_id, last_import_id, is_current)"
        " SELECT c.id, o.name, c.org_id,"
        " CASE WHEN c.org_id IS NULL THEN 'none' ELSE 'sure' END, 0, c.title, NULL, NULL, NULL, 1"
        " FROM contact c LEFT JOIN org o ON o.id = c.org_id"
        " WHERE c.source IN ('linkedin_csv', 'linkedin_import')"
        " AND NOT EXISTS (SELECT 1 FROM connection_position p WHERE p.contact_id = c.id)"
    ))
    bind.execute(sa.text(
        "UPDATE contact SET source = 'linkedin_import', email = NULL"
        " WHERE source = 'linkedin_csv'"
    ))


def downgrade() -> None:
    op.drop_index("ix_connection_position_org_id", table_name="connection_position")
    op.drop_index("ix_connection_position_contact_id", table_name="connection_position")
    op.drop_table("connection_position")
    op.drop_table("contact_import")
    op.drop_table("org_summary")
    op.drop_index("ix_org_hidden_at", table_name="org")
    with op.batch_alter_table("org", recreate="never") as batch:
        batch.drop_column("hidden_at")
    with op.batch_alter_table("profile", recreate="never") as batch:
        batch.drop_column("last_name")
        batch.drop_column("first_name")
