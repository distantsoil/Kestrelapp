"""RSS/Atom signal source (SPEC.md 5.2/5.3): company newsrooms and layoff trackers, one
`signal_kind="trigger"` RawSignal per feed entry for P10 to classify (funding, leadership change,
expansion, compliance, layoffs).

STDLIB-XML CONSTRAINT: `feedparser` and `lxml` are not available in this environment. Feeds are
parsed with `xml.etree.ElementTree` only, handling both RSS 2.0 (`<rss><channel><item>`) and Atom
(namespaced `<feed><entry>`) by matching each element's local tag name (stripping the `{ns}`
prefix) rather than depending on a specific namespace URI.

LEGAL / POLITE (constraint 7): public feeds only, fetched through the shared HttpClient, which
sends a descriptive User-Agent and rate-limits per host. Never behind a login. One broken feed is
logged and skipped; it never sinks the whole source (uncertainty escalates, it never rejects)."""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree

import structlog

from kestrel.ingest.signal_base import RawSignal, SourceConfigError

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTINL_RE = re.compile(r"\n\s*\n\s*\n+")
_DEFAULT_RATE = 2.0

log = structlog.get_logger()


class RssSignalSource:
    kind = "rss"

    def validate_config(self, config: dict) -> None:
        feeds = config.get("feeds")
        if not isinstance(feeds, list) or not feeds:
            raise SourceConfigError("rss source requires a non-empty 'feeds' list of URLs")
        if not all(isinstance(f, str) and f for f in feeds):
            raise SourceConfigError("rss source 'feeds' entries must be non-empty strings")

    async def fetch(self, config: dict, client: Any) -> list[RawSignal]:
        self.validate_config(config)
        rate = float(config.get("rate_per_sec", _DEFAULT_RATE))

        signals: list[RawSignal] = []
        for url in config["feeds"]:
            try:
                text = await client.get_text(url, rate_per_sec=rate)
                # stdlib ElementTree expands internal entities and has no XXE/billion-laughs
                # hardening (defusedxml is not available). Refuse any feed that declares a DOCTYPE
                # or ENTITY rather than risk resource exhaustion on a malicious/compromised feed.
                if _has_doctype_or_entity(text):
                    raise ValueError(
                        "feed declares a DOCTYPE/ENTITY; refused (entity-expansion risk)"
                    )
                root = ElementTree.fromstring(text)
                signals.extend(_parse_feed(root, url))
            except Exception as exc:  # one broken feed never sinks the whole source
                log.warning("rss_feed_error", feed=url, error=str(exc))
                continue
        return signals


_DOCTYPE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


def _has_doctype_or_entity(text: str) -> bool:
    # Only scan the head; a legitimate feed declares neither, and billion-laughs needs them early.
    return _DOCTYPE_RE.search(text[:4096]) is not None


def _local(tag: str) -> str:
    """Strip a `{namespace}` prefix so Atom's namespaced tags match like RSS's bare ones."""
    return tag.rsplit("}", 1)[-1]


def _parse_feed(root: ElementTree.Element, feed_url: str) -> list[RawSignal]:
    root_tag = _local(root.tag)
    if root_tag == "feed":
        return _parse_atom(root, feed_url)
    return _parse_rss(root, feed_url)


def _parse_rss(root: ElementTree.Element, feed_url: str) -> list[RawSignal]:
    signals: list[RawSignal] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        title = _child_text(item, "title")
        link = _child_text(item, "link")
        if not link and not title:
            continue
        description = _child_text(item, "description")
        guid = _child_text(item, "guid")
        pub_date = _child_text(item, "pubDate")
        signals.append(_build_signal(feed_url, title, link, description, guid, pub_date))
    return signals


def _parse_atom(root: ElementTree.Element, feed_url: str) -> list[RawSignal]:
    signals: list[RawSignal] = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        title = _child_text(entry, "title")
        link = _atom_link(entry)
        if not link and not title:
            continue
        summary = _child_text(entry, "summary") or _child_text(entry, "content")
        entry_id = _child_text(entry, "id")
        published = _child_text(entry, "updated") or _child_text(entry, "published")
        signals.append(_build_signal(feed_url, title, link, summary, entry_id, published))
    return signals


def _build_signal(
    feed_url: str,
    title: str | None,
    link: str | None,
    summary: str | None,
    guid: str | None,
    published: str | None,
) -> RawSignal:
    clean_summary = _strip_html(summary) if summary else ""
    text = f"{title or ''}\n\n{clean_summary}".strip()
    return RawSignal(
        signal_kind="trigger",
        org_name=None,  # left None so P10 extracts the org from the text (see docstring)
        source_url=link,
        external_id=guid or link,
        title=title,
        text=text,
        published_date=published,
        form=None,
        raw={"feed": feed_url},
    )


def _child_text(elem: ElementTree.Element, name: str) -> str | None:
    for child in elem:
        if _local(child.tag) == name and child.text:
            text = child.text.strip()
            if text:
                return text
    return None


def _atom_link(entry: ElementTree.Element) -> str | None:
    """Atom links are attributes (`<link href="…" rel="alternate"/>`), possibly multiple; prefer
    rel="alternate" (or unspecified rel, which defaults to alternate) over e.g. rel="self"."""
    fallback: str | None = None
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if not href:
            continue
        rel = child.get("rel")
        if rel in (None, "alternate"):
            return href
        if fallback is None:
            fallback = href
    return fallback


def _strip_html(text: str) -> str:
    stripped = _TAG_RE.sub(" ", text)
    stripped = stripped.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
    stripped = _WS_RE.sub(" ", stripped)
    return _MULTINL_RE.sub("\n\n", stripped).strip()
