"""The bell's unread badge (#95, D-UI-236): the label caps at "9+" and hides at zero, on both the
poll and the panel's out-of-band swap; the true count rides in a header for the aria-label; the
badge and glyph tokens meet their contrast in every scheme and theme. No browser, no network."""

from __future__ import annotations

import json
import re

import pytest

from tests.test_ui_tokens import _tokens, contrast

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

SCHEMES = ["indigo", "teal", "plum", "graphite", "contrast"]
THEMES = ["light", "dark"]


def _seed_unread(n: int) -> None:
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Notification
    from tests.test_surfaces import _run

    async def go() -> None:
        async with get_sessionmaker()() as s:
            s.add_all(Notification(kind="run_finished", severity="info", title=f"Run {i}")
                      for i in range(n))
            await s.commit()

    _run(go())


@pytest.mark.parametrize(
    "count,label", [(0, ""), (1, "1"), (9, "9"), (10, "9+"), (120, "9+")])
def test_bell_label_caps_at_nine_plus_and_hides_at_zero(count, label):
    from tests.test_surfaces import _client

    client = _client()
    _seed_unread(count)
    r = client.get("/notifications/bell")
    assert r.text == label
    # The true count is not capped: it rides in a header the page turns into the aria-label.
    assert json.loads(r.headers["HX-Trigger-After-Swap"]) == {"bellCount": count}
    # The panel's out-of-band swap carries the same capped label into the same badge.
    panel = client.get("/notifications/list").text
    oob = re.search(r'<span id="bellcount"[^>]*hx-swap-oob="innerHTML">([^<]*)</span>', panel)
    assert oob and oob.group(1) == label
    assert json.loads(client.get("/notifications/list").headers["HX-Trigger-After-Swap"]) == {
        "bellCount": count}


def test_bell_button_names_the_count_and_the_badge_is_hidden_from_readers():
    from tests.test_surfaces import _client

    html = _client().get("/settings").text
    tools = html.split('<header class="k-tools"')[1].split("</header>")[0]
    assert 'id="bellcount" class="k-icon__n" aria-hidden="true"' in tools
    # The page script writes "Notifications, N unread" from the header's true count.
    assert "'Notifications, ' + n + ' unread'" in html and "'bellCount'" in html


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("theme", THEMES)
def test_badge_and_bell_tokens_meet_contrast_in_every_scheme(scheme, theme):
    t = _tokens(theme, scheme)
    assert contrast(t["brand-ink"], t["brand"]) >= 4.5, "badge number on its fill"
    assert contrast(t["brand"], t["paper"]) >= 3, "badge fill against the ring and circle"
    assert contrast(t["bell"], t["paper"]) >= 3, "bell glyph on its circle"
    assert contrast(t["bell"], t["soft"]) >= 3, "bell glyph on the hover circle"


@pytest.mark.parametrize("count,label,aria", [
    (0, "", "Notifications"), (3, "3", "Notifications, 3 unread"),
    (12, "9+", "Notifications, 12 unread")])
def test_top_row_prints_the_badge_on_the_server(count, label, aria):
    """The first paint is final (#95): label and aria-label are in the HTML, from the same count
    the poll uses, and nothing in the markup asks for an entry animation."""
    from tests.test_surfaces import _client

    client = _client()
    _seed_unread(count)
    html = client.get("/settings").text
    tools = html.split('<header class="k-tools"')[1].split("</header>")[0]
    assert f'aria-label="{aria}"' in tools
    lab = re.escape(label)
    assert re.search(
        rf'<span id="bellcount" class="k-icon__n" aria-hidden="true"'
        rf' data-last="{lab}">{lab}</span>', tools)
    assert "is-in" not in tools and "is-tick" not in tools
    assert client.get("/notifications/bell").text == label  # the poll agrees with the print
