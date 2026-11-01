"""The narrow-window segmented-control padding belongs to the Documents tab only: its rule is
scoped under the Documents grid, so no other Profile tab changes layout at 1300px and below."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).parents[1] / "kestrel/web/static/css/pages/profile.css"


def test_the_pf_seg_padding_rule_is_scoped_to_the_documents_grid():
    text = CSS.read_text()
    rules = re.findall(r"@container kwin \(max-width: 1300px\) \{([^}]*\{[^}]*\})\s*\}", text)
    seg = [r for r in rules if "pf-seg" in r]
    assert len(seg) == 1, seg
    selector = seg[0].split("{")[0]
    assert ".pf-dgrid" in selector and ".pf-seg" in selector
