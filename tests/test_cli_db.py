"""`kestrel db upgrade` runs alembic upgrade head against the app database."""

from __future__ import annotations

from typer.testing import CliRunner

from kestrel.cli import app

runner = CliRunner()


def test_db_upgrade_then_current():
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert "0016" in result.output  # head advanced with the run summary indexes (D-UI-58)

    current = runner.invoke(app, ["db", "current"])
    assert current.exit_code == 0
    assert "0016" in current.output
