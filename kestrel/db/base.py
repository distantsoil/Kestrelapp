"""Declarative base, the ISO8601-UTC timestamp helper, and the created/updated mixin.

Timestamps are stored as ISO8601 UTC TEXT (SPEC.md 4). This is lexicographically sortable, so
`lease_until < now` and next-action-due comparisons work as plain string comparisons."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    # Always emit 6 microsecond digits so ISO strings are a FIXED width and compare correctly:
    # without this, a zero-microsecond time renders "...:00+00:00" which sorts BEFORE
    # "...:00.000001+00:00" (`+` < `.`), corrupting `lease_until < now` comparisons.
    return dt.isoformat(timespec="microseconds")


def utcnow_iso() -> str:
    return iso(utcnow())


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow_iso, onupdate=utcnow_iso)
