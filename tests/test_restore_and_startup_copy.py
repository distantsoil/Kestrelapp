"""Copy rules on the startup card and the restore pages: American English and no `(s)` plurals
(CLAUDE.md Output style). Invented values only."""

from __future__ import annotations

import pytest

from kestrel.services.backup import RestoreResult
from kestrel.services.backup_preview import Item, SettingsPreview

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def test_the_canceled_start_card_uses_american_spelling():
    from kestrel.desktop import shell

    text = shell.START_CANCELED + " " + shell.START_CANCELED_DETAILS
    assert "cancelled" not in text.lower()
    assert "canceled" in text


def _all_notes(n: int) -> str:
    result = RestoreResult(
        db_restored=True, paths_cleared=n, rerankers_disabled=n, addresses_kept=n,
        backends_dropped=n, settings_kept=n, entries_refused=n, tables_ignored=n,
        tasks_not_restored=n, config_skipped=n,
    )
    return " ".join(result.summary_lines())


def test_every_restore_note_reads_as_a_singular_sentence():
    joined = _all_notes(1)
    assert "(s)" not in joined
    assert " 1 settings" not in joined and "were kept as they are" not in joined
    assert "1 model server kept" in joined
    assert "1 setting that only this computer can change was kept as it is here." in joined
    assert "1 config file in the backup is not one Kestrel uses and was skipped." in joined


def test_every_restore_note_reads_as_a_plural_sentence():
    joined = _all_notes(2)
    assert "(s)" not in joined
    assert "2 settings that only this computer can change were kept as they are here." in joined


def _render_preview(other: int) -> str:
    from kestrel.web.deps import templates

    pv = SettingsPreview(switches=[Item("Invented switch", "on")], other_count=other)
    return templates().env.get_template("partials/st_restore_preview.html").render(pv=pv)


def test_the_preview_counts_one_other_setting_in_the_singular():
    assert "1 other setting of this kind is not listed." in _render_preview(1)
    assert "3 other settings of this kind are not listed." in _render_preview(3)
