"""Internal tier and backend names never reach the screen.

"frontier" is the hosted backend key for deeper reads, and tier1/tier2/tier3 are setting names. The
UI names them plainly ("deeper reads", "Full review (Tier 2)"), the way the Models and Bench pages
already do through `kestrel.web.deps.tier_label`.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def test_hosted_tier_labels_are_plain():
    from kestrel.services.hosted_connect import tier_label

    assert tier_label("frontier") == "deeper reads"
    assert tier_label("tier1") == "quick screen (Tier 1)"
    assert tier_label("tier2") == "full review (Tier 2)"
    assert tier_label("docsel") == "document selection"


def test_the_save_message_names_tiers_plainly():
    from kestrel.services.hosted_models import SaveResult

    r = SaveResult(saved=[], refused=[])
    r.paid.extend(["frontier", "tier1"])
    msg = r.message
    assert "frontier" not in msg and "tier1" not in msg, msg
    assert "deeper reads" in msg and "quick screen (Tier 1)" in msg, msg


def test_models_page_and_disclosure_use_plain_names():
    from pathlib import Path

    from kestrel import paths

    templates = Path(paths.web_templates_dir())
    models = (templates / "models.html").read_text(encoding="utf-8")
    disclosure = (templates / "partials" / "hosted_disclosure.html").read_text(encoding="utf-8")
    assert "Hosted (frontier) backend" not in models
    assert "Tier 3 escalation" not in disclosure


def test_no_template_shows_tier_3_escalation_to_the_user():
    import re
    from pathlib import Path

    from kestrel import paths

    bad = []
    for f in Path(paths.web_templates_dir()).rglob("*.html"):
        text = re.sub(r"\{#.*?#\}", "", f.read_text(encoding="utf-8"), flags=re.S)
        if "Tier 3 escalation" in text:
            bad.append(f.name)
    assert not bad, bad
