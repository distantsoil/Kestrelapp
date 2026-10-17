"""Research, contacts, and the networking graph (SPEC-MODULES.md 2). The model synthesizes supplied
excerpts and never browses; every claim resolves to a source_index, every email must appear
verbatim in a source, and no person is invented. These rules are enforced in code, not merely in the
prompt. The system drafts and hands over — it never sends and never scrapes behind a login."""

from __future__ import annotations
