"""D-DB-1: a write transaction held open past the threshold is logged with what held it."""

from __future__ import annotations

from sqlalchemy import text
from structlog.testing import capture_logs

from kestrel.db import engine as eng


async def test_a_long_write_transaction_is_named_in_a_warning(sm, monkeypatch):
    monkeypatch.setattr(eng, "WRITE_HOLD_WARN_S", 0.0)
    with capture_logs() as logs:
        async with sm() as s:
            await s.execute(
                text(
                    "INSERT INTO setting (key, value, updated_at) VALUES ('hold.test', '1', 'now')"
                )
            )
            await s.commit()
    held = [e for e in logs if e["event"] == "write_transaction_held_long"]
    assert held and "INSERT INTO setting" in held[0]["first_statement"]


async def test_a_quick_write_and_a_read_are_silent(sm):
    with capture_logs() as logs:
        async with sm() as s:
            await s.execute(text("SELECT 1"))
            await s.execute(
                text(
                    "INSERT INTO setting (key, value, updated_at) VALUES ('hold.test', '1', 'now')"
                )
            )
            await s.commit()
    assert not [e for e in logs if e["event"] == "write_transaction_held_long"]


async def test_a_connection_returned_without_commit_leaves_no_stale_timer(sm, monkeypatch):
    """A request cancelled mid-write never reaches commit or rollback events of its own; the next
    user of that pooled connection must not be blamed for the old timer."""
    monkeypatch.setattr(eng, "WRITE_HOLD_WARN_S", 0.0)
    async with sm() as s:
        conn = await s.connection()
        raw = await conn.get_raw_connection()
        await s.execute(
            text("INSERT INTO setting (key, value, updated_at) VALUES ('stale', '1', 'now')")
        )
        record_info = raw._connection_record.info
        assert "write_t0" in record_info
        # Return it to the pool the way a cancelled request does: close without commit.
    assert "write_t0" not in record_info
    with capture_logs() as logs:
        async with sm() as s:
            await s.execute(text("SELECT 1"))
            await s.rollback()
    assert not [e for e in logs if e["event"] == "write_transaction_held_long"]


async def test_the_pool_listeners_raise_no_deprecation_warning(sm):
    # pyproject ignores DeprecationWarning, so a pre-2.0 event signature would pass silently.
    import warnings

    from sqlalchemy.exc import SADeprecationWarning

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with sm() as s:
            await s.execute(text("SELECT 1"))
            await s.commit()
    assert not [w for w in caught if issubclass(w.category, SADeprecationWarning)]
