"""doctor's "Ingest contact" check (hard rule 7): the shipped placeholder User-Agent WARNS (never
blocks) with a remediation that names ingest.user_agent in settings.yml; a real contact PASSES."""

from __future__ import annotations

from kestrel.config import get_settings
from kestrel.doctor import PASS, WARN, DoctorReport, _check_ingest_contact


def test_placeholder_user_agent_warns_with_remediation():
    s = get_settings()
    s.ingest.user_agent = "Kestrel/1.0 (+https://github.com/kestrel-app; contact: op@example.com)"
    rep = DoctorReport()
    _check_ingest_contact(rep, s)
    row = next(r for r in rep.results if r.name == "Ingest contact")
    assert row.status == WARN and row.blocking is False
    assert "ingest.user_agent" in row.remediation and "settings.yml" in row.remediation
    assert rep.has_blocking_failure is False


def test_real_contact_passes():
    s = get_settings()
    s.ingest.user_agent = "Kestrel/1.0 (ops@kestrel-test.org)"
    rep = DoctorReport()
    _check_ingest_contact(rep, s)
    row = next(r for r in rep.results if r.name == "Ingest contact")
    assert row.status == PASS
