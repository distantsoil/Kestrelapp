"""`kestrel db upgrade` runs alembic upgrade head against the app database."""

from __future__ import annotations

from typer.testing import CliRunner

from kestrel.cli import app

runner = CliRunner()


def test_db_upgrade_then_current():
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    # head advanced with the operator's qualitative ratings for the offer comparison,
    # then the `egressed` column that records whether a payload ever reached the provider,
    # then the liveness strike count and close reason (two reads before a posting closes)
    assert "0020" in result.output

    current = runner.invoke(app, ["db", "current"])
    assert current.exit_code == 0
    assert "0020" in current.output
