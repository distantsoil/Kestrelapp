"""Help page (SPEC-INTERFACES 2.1 /help, D-UI-124): in-app, offline.

A search field, topic chips, "The four steps", 21 short articles (kestrel/web/help_articles.py)
that open in a reader flyout beside the page, and four helper cards. Three reference articles the
rest of the app links to are listed with the others and rendered from partials/help_ref_*.html,
because the locked mockup draws no extra blocks (D-UI-187): the model names (/help#tier-names),
where opportunities come from (/help#sources, /help#remoterocketship) and Kestrel from another
device (/help#lan). static/js/help.js opens the right article for those anchors.
Everything is rendered on the server and filtered in the browser (static/js/help.js), so Help
works with no network. The version, the folders, the update check and the diagnostics are on
/about (pages/about.py).

Each page's How it works button is the page help; Replay the walkthrough links to /onboarding.
Kestrel never downloads or installs anything."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kestrel.ingest.catalog import REGION_NOTE, REQUEST_NEW_PROVIDER, source_catalog
from kestrel.web.deps import (
    templates,
)
from kestrel.web.help_articles import TOPICS, Article

router = APIRouter()

# The four steps every role goes through (SPEC.md 6), as the locked mockup words them. Each opens
# the "steps" article. The last is optional, which the page shows with a dashed number.
STEPS = (
    {
        "name": "Rules check",
        "what": "Your own filters. No AI.",
        "cost": "Instant",
        "optional": False,
    },
    {
        "name": "Quick screen",
        "what": "A small model reads the key facts.",
        "cost": "Seconds each",
        "optional": False,
    },
    {
        "name": "Full review",
        "what": "The main model reads the whole posting.",
        "cost": "Tens of seconds each",
        "optional": False,
    },
    {
        "name": "Deeper read",
        "what": "Optional. One role, only when you ask.",
        "cost": "You confirm first",
        "optional": True,
    },
)

# The tier NAMES as the Settings pages print them (SPEC.md 8.2 and providers.HOSTED_TIERS), for
# the operator who meets "docsel" or "frontier" in a table before reading the spec.
TIER_NAMES = (
    {
        "name": "tier1",
        "label": "Quick screen (Tier 1)",
        "what": "The fast keep-or-set-aside read of every new posting.",
    },
    {
        "name": "tier2",
        "label": "Full review (Tier 2)",
        "what": "The deep evaluation that scores a role and gives a verdict.",
    },
    {
        "name": "tier3",
        "label": "Deeper read (Tier 3)",
        "what": "The model behind the work you start yourself: cover letters, engagement "
        "outlines, research summaries, contact finding, negotiation prep.",
    },
    {
        "name": "docsel",
        "label": "Document selection",
        "what": "Document selection: picks which of your own resume variants fill each slot. "
        "It only selects; every line in the PDF is your text.",
    },
    {
        "name": "trigger",
        "label": "Consulting signals",
        "what": "The consulting-track classifier for filings and feeds (SEC EDGAR, RSS, PE "
        "portfolio pages).",
    },
    {
        "name": "frontier",
        "label": "Online deeper read",
        "what": "The online model a deeper read goes to, one opportunity at a time, after "
        "you preview and confirm the exact payload.",
    },
    {
        "name": "embed",
        "label": "Matching",
        "what": "The embedding model behind duplicate detection, few-shot examples and the "
        "learned ranking. Always on your computer.",
    },
)


# Helper cards: two or three lines each, and a "More about this" flyout that adds information. A
# card's `article` is the "topic:article" key its flyout offers to open in full.
HELPERS = (
    {
        "key": "wrong",
        "title": "Something not working?",
        "accent": "warn",
        "icon": "life",
        "lines": (
            "Check my models in Models tests each model.",
            "Download diagnostics from About.",
            "The logs folder is one click away.",
        ),
        "article": "fix:problem",
    },
    {
        "key": "promises",
        "title": "What Kestrel promises",
        "accent": "good",
        "icon": "shield",
        "lines": (
            "Never applies, emails or messages anyone.",
            "Asks before anything goes online.",
            "Free, and you are not the product.",
        ),
        "article": "privacy:where",
    },
    {
        "key": "keys",
        "title": "Shortcuts",
        "accent": "info",
        "icon": "plus-square",
        "lines": (),
        "article": "",
    },
    {
        "key": "more",
        "title": "More help",
        "accent": "info",
        "icon": "book",
        "lines": (
            "Longer guides live on kestrelapp.co.",
            "Report a bug or ask for a feature on GitHub.",
            "Each page has a How it works button with its own help.",
        ),
        "article": "fix:diag",
    },
)


# Reference articles shown beside the written ones. Their body is empty: the template renders the
# matching partials/help_ref_<key>.html (the source catalog is generated, never typed here).
_REFERENCE = {
    "start": Article("sources", "Where opportunities come from", ""),
    "privacy": Article("lan", "Kestrel from another device", ""),
    "models": Article("names", "What the model names mean", ""),
}


def _topics_with_reference():
    return tuple(
        t._replace(articles=t.articles + (_REFERENCE[t.key],)) if t.key in _REFERENCE else t
        for t in TOPICS
    )


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    ctx = {
        "title": "Help",
        "query": dict(request.query_params),
        "topics": _topics_with_reference(),
        "steps": STEPS,
        "tier_names": TIER_NAMES,
        "helpers": HELPERS,
        "catalog": source_catalog(),
        "region_note": REGION_NOTE,
        "request_new_provider": REQUEST_NEW_PROVIDER,
    }
    return templates().TemplateResponse(request, "help.html", ctx)
