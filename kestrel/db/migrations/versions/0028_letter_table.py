"""letter: cover letters and engagement outlines get their own table (issue #124, D-LETTER-1)

Until 0.7.5 a draft was a doc_render row whose text, claims and marks lived in its `config` JSON.
This adds the small `letter` table the owner chose (body, claims, flags, prompt version, the
application, word count) with the PDF render attached by `render_id`, and moves every existing
letter into it. The render keeps what belongs to the PDF (the file, its name, its page count and
`pdf_digest`); the text moves. Earlier versions did not record the prompt version.

At most one letter per role and kind is `current` (a partial unique index holds it, D-LETTER-5):
the one the application points at, else the newest. A role's other letter renders were never shown
by 0.7.5 (it showed the newest); they are kept as `older`, which no view shows and nothing undoes.

Safe to run again (D-LETTER-5): on SQLite the CREATE TABLE can be committed even when the data
move is rolled back, so the table existing proves nothing. The move takes only letter renders that
have no letter row yet, and runs whenever any remain. A value that cannot be read (a word count
that is not a number, claims that are not a list) is stored empty and logged; it never stops the
other rows.

The data move is written out here, not imported from the app, so this file keeps doing what it did
when it shipped. Downgrade (D-LETTER-6) keeps each role's current letter as the render 0.7.5 shows
(its newest): `proposed` and `previous` letters, and any older letter newer than the current one,
are deleted and logged (0.7.5 cannot show them), the text goes back into the render config, and the
table is dropped. Their PDF files are left on disk; a migration does not delete files.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

TABLE = "letter"
ONE_CURRENT = "ux_letter_one_current"
# What stays on the render: it describes the PDF, not the text.
_RENDER_KEYS = ("kind", "pdf_digest")
_EXTRA_KEYS = ("basis", "assumptions", "exclusions", "commercial", "too_few_exclusions")


def _has_table() -> bool:
    return TABLE in sa.inspect(op.get_bind()).get_table_names()


def _loads(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _create() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("render_id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("pursuit_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("generated_md", sa.Text(), nullable=True),
        sa.Column("claims_used", sa.JSON(), nullable=True),
        sa.Column("flags", sa.JSON(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("edited", sa.Boolean(), nullable=False),
        sa.Column("extras", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["render_id"], ["doc_render.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunity.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pursuit_id"], ["pursuit.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("render_id"),
    )


def _ensure_indexes() -> None:
    have = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    if "ix_letter_opportunity_id" not in have:
        op.create_index("ix_letter_opportunity_id", TABLE, ["opportunity_id"])
    if ONE_CURRENT not in have:
        op.create_index(ONE_CURRENT, TABLE, ["opportunity_id", "kind"], unique=True,
                        sqlite_where=sa.text("status = 'current'"))


def _int(value, fallback: int, render_id: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("letter move: render %s has an unreadable word count; counted again", render_id)
        return fallback


def _list(value, what: str, render_id: int) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    log.warning("letter move: render %s has unreadable %s; stored empty", render_id, what)
    return []


def _move() -> int:
    """Move every letter render that has no letter row yet. Returns how many were moved."""
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT r.id, r.opportunity_id, r.template, r.config, r.created_at FROM doc_render r"
        " WHERE r.template IN ('cover_letter', 'engagement_outline')"
        " AND r.opportunity_id IS NOT NULL"
        " AND r.id NOT IN (SELECT render_id FROM letter) ORDER BY r.id"
    )).fetchall()
    moved = 0
    for render_id, opp_id, template, raw, created in rows:
        cfg = _loads(raw)
        if raw and not cfg:
            log.warning("letter move: render %s has an unreadable config; moved empty", render_id)
        body = cfg.get("body_md")
        body = body if isinstance(body, str) else ""
        generated = cfg.get("generated_md")
        pursuit = bind.execute(
            sa.text("SELECT id FROM pursuit WHERE opportunity_id = :o"), {"o": opp_id}
        ).scalar()
        bind.execute(
            sa.text(
                "INSERT INTO letter (render_id, opportunity_id, pursuit_id, kind, status, body_md,"
                " generated_md, claims_used, flags, prompt_version, word_count, edited, extras,"
                " created_at, updated_at) VALUES (:r, :o, :p, :k, 'older', :b, :g, :c, :f,"
                " NULL, :w, :e, :x, :t, :t)"
            ),
            {
                "r": render_id, "o": opp_id, "p": pursuit,
                "k": template if cfg.get("kind") not in ("cover_letter", "engagement_outline")
                else cfg["kind"],
                "b": body, "g": generated if isinstance(generated, str) else body,
                "c": json.dumps(_list(cfg.get("claims_used"), "claims", render_id)),
                "f": json.dumps(_list(cfg.get("flags"), "marks", render_id)),
                "w": _int(cfg.get("words") if "words" in cfg else len(body.split()),
                          len(body.split()), render_id),
                "e": bool(cfg.get("edited")),
                "x": json.dumps({k: cfg[k] for k in _EXTRA_KEYS if k in cfg}),
                "t": created or "",
            },
        )
        kept = {k: cfg[k] for k in _RENDER_KEYS if k in cfg}
        bind.execute(
            sa.text("UPDATE doc_render SET config = :c WHERE id = :r"),
            {"c": json.dumps(kept), "r": render_id},
        )
        moved += 1
    return moved


def _choose_current() -> None:
    """Each role and kind with no current letter gets one: the one its application points at,
    else the newest; the others stay `older`."""
    bind = op.get_bind()
    groups = bind.execute(sa.text(
        "SELECT opportunity_id, kind FROM letter GROUP BY opportunity_id, kind"
        " HAVING SUM(CASE WHEN status = 'current' THEN 1 ELSE 0 END) = 0"
    )).fetchall()
    for opp_id, kind in groups:
        pick = bind.execute(sa.text(
            "SELECT l.render_id FROM letter l JOIN pursuit p ON p.cover_letter_id = l.render_id"
            " WHERE l.opportunity_id = :o AND l.kind = :k AND l.status = 'older'"
        ), {"o": opp_id, "k": kind}).scalar()
        if pick is None:
            pick = bind.execute(sa.text(
                "SELECT MAX(render_id) FROM letter WHERE opportunity_id = :o AND kind = :k"
                " AND status = 'older'"
            ), {"o": opp_id, "k": kind}).scalar()
        if pick is not None:
            bind.execute(sa.text("UPDATE letter SET status = 'current' WHERE render_id = :r"),
                         {"r": pick})


def upgrade() -> None:
    if not _has_table():
        _create()
    moved = _move()
    _choose_current()
    _ensure_indexes()
    if moved:
        log.info("letter move: %s letter(s) moved into the letter table", moved)


def downgrade() -> None:
    if not _has_table():
        return
    bind = op.get_bind()
    # 0.7.5 shows a role's newest letter render, and has no way to show a proposed or previous
    # draft: those are deleted (logged) so the current one stays the one shown. Their PDF files are
    # left on disk; a migration does not delete files.
    doomed = list(bind.execute(sa.text(
        "SELECT render_id, opportunity_id, status FROM letter"
        " WHERE status IN ('proposed', 'previous')"
    )).fetchall())
    # An older letter newer than the current one would be the one 0.7.5 shows: it goes too.
    doomed += bind.execute(sa.text(
        "SELECT o.render_id, o.opportunity_id, o.status FROM letter o JOIN letter c"
        " ON c.opportunity_id = o.opportunity_id AND c.kind = o.kind AND c.status = 'current'"
        " WHERE o.status = 'older' AND o.render_id > c.render_id"
    )).fetchall()
    for render_id, opp_id, status in doomed:
        log.warning("letter downgrade: deleting the %s draft (render %s) of role %s",
                    status, render_id, opp_id)
        bind.execute(sa.text("UPDATE pursuit SET cover_letter_id = NULL"
                             " WHERE cover_letter_id = :r"), {"r": render_id})
        bind.execute(sa.text("DELETE FROM letter WHERE render_id = :r"), {"r": render_id})
        bind.execute(sa.text("UPDATE selection_override SET doc_render_id = NULL"
                             " WHERE doc_render_id = :r"), {"r": render_id})
        bind.execute(sa.text("DELETE FROM doc_render WHERE id = :r"), {"r": render_id})
    rows = bind.execute(sa.text(
        "SELECT l.render_id, l.kind, l.body_md, l.generated_md, l.claims_used, l.flags,"
        " l.word_count, l.edited, l.extras, r.config, l.status FROM letter l"
        " JOIN doc_render r ON r.id = l.render_id"
    )).fetchall()
    for render_id, kind, body, generated, claims, flags, words, edited, extras, raw, _st in rows:
        cfg = _loads(raw)
        more = {k: v for k, v in _loads(extras).items() if k in _EXTRA_KEYS}
        cfg.update({
            "kind": kind, "body_md": body, "generated_md": generated,
            "claims_used": json.loads(claims or "[]"), "flags": json.loads(flags or "[]"),
            "words": words, "edited": bool(edited), **more,
        })
        bind.execute(
            sa.text("UPDATE doc_render SET config = :c WHERE id = :r"),
            {"c": json.dumps(cfg), "r": render_id},
        )
    op.drop_index(ONE_CURRENT, table_name=TABLE)
    op.drop_index("ix_letter_opportunity_id", table_name=TABLE)
    op.drop_table(TABLE)
