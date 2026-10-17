"""RSS/Atom signal source: parse RSS 2.0 `<item>` and Atom (namespaced) `<entry>` elements into
trigger RawSignals with stdlib `xml.etree.ElementTree` only, strip HTML from descriptions/summaries,
and never let one broken feed sink the whole source."""

from __future__ import annotations

import pytest

from kestrel.ingest.signal_base import SourceConfigError
from kestrel.ingest.signal_sources.rss import RssSignalSource

_RSS_URL = "https://example.com/newsroom/rss"
_ATOM_URL = "https://example.com/layoffs/atom"

_RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Acme Newsroom</title>
    <link>https://example.com/newsroom</link>
    <item>
      <title>Acme raises $50M Series C</title>
      <link>https://example.com/newsroom/series-c</link>
      <description>&lt;p&gt;Acme today announced a &lt;b&gt;$50M&lt;/b&gt;
      round.&lt;/p&gt;</description>
      <guid>urn:acme:series-c</guid>
      <pubDate>Mon, 01 Jul 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>No link item</title>
      <description>Has a title but no link.</description>
    </item>
  </channel>
</rss>
"""

_ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Layoff Tracker</title>
  <entry>
    <title>Beta Corp lays off 200</title>
    <link rel="self" href="https://example.com/layoffs/beta-corp/self"/>
    <link rel="alternate" href="https://example.com/layoffs/beta-corp"/>
    <summary>&lt;p&gt;Beta Corp cut &lt;b&gt;200&lt;/b&gt; roles today.&lt;/p&gt;</summary>
    <id>tag:example.com,2024:beta-corp</id>
    <updated>2024-07-01T09:00:00Z</updated>
  </entry>
</feed>
"""


class _FakeHttp:
    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get_text(self, url, **kw):
        self.calls.append(url)
        if url not in self.routes:
            raise RuntimeError(f"unexpected url {url}")
        return self.routes[url]


def test_validate_config_requires_feeds():
    src = RssSignalSource()
    with pytest.raises(SourceConfigError):
        src.validate_config({})
    with pytest.raises(SourceConfigError):
        src.validate_config({"feeds": []})
    with pytest.raises(SourceConfigError):
        src.validate_config({"feeds": ["", "https://example.com/rss"]})
    src.validate_config({"feeds": ["https://example.com/rss"]})  # ok


async def test_parses_rss_2_0_items():
    src = RssSignalSource()
    http = _FakeHttp({_RSS_URL: _RSS_FEED})
    signals = await src.fetch({"feeds": [_RSS_URL]}, http)

    # the linkless item is skipped only if it also lacks a title; here it has a title so it is kept
    assert len(signals) == 2
    first = signals[0]
    assert first.signal_kind == "trigger"
    assert first.title == "Acme raises $50M Series C"
    assert first.source_url == "https://example.com/newsroom/series-c"
    assert first.external_id == "urn:acme:series-c"
    assert first.published_date == "Mon, 01 Jul 2024 12:00:00 GMT"
    assert first.org_name is None  # left for P10 to extract from text
    assert "Acme raises $50M Series C" in first.text
    assert "$50M" in first.text


async def test_parses_atom_entries_with_namespace_and_alternate_link():
    src = RssSignalSource()
    http = _FakeHttp({_ATOM_URL: _ATOM_FEED})
    signals = await src.fetch({"feeds": [_ATOM_URL]}, http)

    assert len(signals) == 1
    entry = signals[0]
    assert entry.title == "Beta Corp lays off 200"
    # rel="alternate" preferred over rel="self"
    assert entry.source_url == "https://example.com/layoffs/beta-corp"
    assert entry.external_id == "tag:example.com,2024:beta-corp"
    assert entry.published_date == "2024-07-01T09:00:00Z"


async def test_html_stripped_from_text():
    src = RssSignalSource()
    http = _FakeHttp({_RSS_URL: _RSS_FEED, _ATOM_URL: _ATOM_FEED})
    signals = await src.fetch({"feeds": [_RSS_URL, _ATOM_URL]}, http)

    for signal in signals:
        assert "<" not in signal.text
        assert ">" not in signal.text

    acme = next(s for s in signals if s.title == "Acme raises $50M Series C")
    assert "Acme today announced a" in acme.text
    beta = next(s for s in signals if s.title == "Beta Corp lays off 200")
    assert "Beta Corp cut" in beta.text


async def test_one_broken_feed_does_not_sink_the_source():
    src = RssSignalSource()
    http = _FakeHttp({_RSS_URL: _RSS_FEED, _ATOM_URL: "not xml at all <<<"})
    signals = await src.fetch({"feeds": [_ATOM_URL, _RSS_URL]}, http)

    # the broken feed is skipped; the good one still yields its signals
    assert len(signals) == 2
    assert all(s.raw["feed"] == _RSS_URL for s in signals)


_BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>
<rss version="2.0"><channel><item><title>&lol2;</title>
<link>https://x/1</link></item></channel></rss>
"""


async def test_doctype_entity_feed_is_refused():
    """A feed declaring a DOCTYPE/ENTITY is refused before parsing (stdlib ElementTree has no
    billion-laughs hardening and defusedxml is unavailable) — reviewer MEDIUM 5."""
    src = RssSignalSource()
    http = _FakeHttp({_RSS_URL: _BILLION_LAUGHS, _ATOM_URL: _ATOM_FEED})
    signals = await src.fetch({"feeds": [_RSS_URL, _ATOM_URL]}, http)
    # the entity-bearing feed is skipped; the clean feed still yields its signal
    assert len(signals) == 1
    assert signals[0].title == "Beta Corp lays off 200"
