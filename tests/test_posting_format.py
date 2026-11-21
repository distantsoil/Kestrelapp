"""Posting text formatting (issue #210, D-UI-720 to D-UI-723). The description is hostile input:
it is parsed into our own node types and printed with autoescaping, so only the allowlisted
elements below can ever reach a page. Invented text only; no network."""

from __future__ import annotations

import time
from html.parser import HTMLParser

import pytest

from kestrel.web import posting_format as pf

ALLOWED_TAGS = {"p", "br", "strong", "em", "h3", "h4", "h5", "ul", "ol", "li", "span"}
ALLOWED_CLASSES = {
    "kp-p",
    "kp-rest",
    "kp-h",
    "kp-h--1",
    "kp-h--2",
    "kp-h--3",
    "kp-list",
    "kp-li",
    "kp-li--num",
    "kp-li--sub",
    "kp-num",
}
VOID = {"br"}


class _Shape(HTMLParser):
    """Every element and attribute the output holds, its depth, and its text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.depth = 0
        self.max_depth = 0
        self.text: list[str] = []
        self.other: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))
        if tag not in VOID:
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)

    def handle_endtag(self, tag):
        if tag not in VOID:
            self.depth -= 1

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, attrs))

    def handle_data(self, data):
        self.text.append(data)

    def handle_comment(self, data):
        self.other.append("comment")

    def handle_decl(self, decl):
        self.other.append("decl")

    def handle_pi(self, data):
        self.other.append("pi")


def _render(raw: str | None) -> str:
    from kestrel.web.deps import templates

    module = templates().env.get_template("partials/posting_macros.html").module
    return str(module.posting_body(pf.parse(raw)))


def _shape(out: str) -> _Shape:
    p = _Shape()
    p.feed(out)
    p.close()
    return p


def _assert_allowlisted(out: str) -> _Shape:
    shape = _shape(out)
    assert shape.other == []
    for tag, attrs in shape.tags:
        assert tag in ALLOWED_TAGS, (tag, out[:300])
        for name, value in attrs:
            assert name == "class", (tag, name)
            assert set((value or "").split()) <= ALLOWED_CLASSES, value
    assert shape.depth == 0  # every element closed
    return shape


# -------------------------------------------------------------------- the formatting subset
_REAL = (
    "**Who are we?**\n"
    "Bluefinch Bakery makes bread for twelve towns.\\\n"
    "We are growing.\n\n"
    "## What you will do\n"
    "* Run the *early* shift and the **ovens**\n"
    "* Train two new bakers\n"
    "  - one in pastry\n"
    "- Keep the stock sheet\n\n"
    "### Steps\n"
    "1. Apply\n"
    "2. Meet the team\n"
    "4. Bake a loaf\n\n"
    "Read more on [our page](https://bakery.example/jobs) or <https://bakery.example/about>.\n"
    "***Pay:*** $20 an hour"
)


def test_a_real_shaped_description_renders_the_allowlisted_subset_exactly():
    assert _render(_REAL) == (
        '<p class="kp-p"><strong>Who are we?</strong><br>'
        "Bluefinch Bakery makes bread for twelve towns.<br>We are growing.</p>"
        '<h4 class="kp-h kp-h--2">What you will do</h4>'
        '<ul class="kp-list"><li class="kp-li">Run the <em>early</em> shift and the '
        "<strong>ovens</strong></li>"
        '<li class="kp-li">Train two new bakers</li>'
        '<li class="kp-li kp-li--sub">one in pastry</li>'
        '<li class="kp-li">Keep the stock sheet</li></ul>'
        '<h5 class="kp-h kp-h--3">Steps</h5>'
        '<ol class="kp-list"><li class="kp-li kp-li--num"><span class="kp-num">1.</span> Apply</li>'
        '<li class="kp-li kp-li--num"><span class="kp-num">2.</span> Meet the team</li>'
        '<li class="kp-li kp-li--num"><span class="kp-num">4.</span> Bake a loaf</li></ol>'
        '<p class="kp-p">Read more on our page (https://bakery.example/jobs) or '
        "https://bakery.example/about.<br><strong><em>Pay:</em></strong> $20 an hour</p>"
    )


def test_heading_levels_map_to_three_sizes():
    out = _render("# One\n\n## Two\n\n### Three\n\n###### Six ##")
    assert out == (
        '<h3 class="kp-h kp-h--1">One</h3><h4 class="kp-h kp-h--2">Two</h4>'
        '<h5 class="kp-h kp-h--3">Three</h5><h5 class="kp-h kp-h--3">Six</h5>'
    )


def test_a_link_is_its_words_and_its_address_as_text_never_a_link():
    out = _render(
        "See [the role](https://jobs.example/7) and [https://a.example](https://a.example)"
        " and ![logo](https://img.example/x.png)"
    )
    assert "<a" not in out and "href" not in out and "<img" not in out
    assert out == (
        '<p class="kp-p">See the role (https://jobs.example/7) and https://a.example'
        " and logo (https://img.example/x.png)</p>"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "** not bold **",
        "**",
        "*",
        "* ",
        "**unpaired",
        "a ** b",
        "*lone",
        "2 * 3 * 4",
        "**a*",
        "*a**",
        "x***y",
        "*** x",
    ],
)
def test_unpaired_or_empty_markers_stay_literal(raw):
    out = _render(raw)
    assert "<strong>" not in out and "<em>" not in out
    assert "".join(_shape(out).text) == raw.strip()


@pytest.mark.parametrize("rule", ["***", "****", "** **", "---", "- - -", "___", " * * *"])
def test_a_rule_line_only_separates_blocks(rule):
    assert _render(f"One\n{rule}\nTwo") == '<p class="kp-p">One</p><p class="kp-p">Two</p>'


def test_escaped_markers_show_as_marks_and_never_pair():
    out = _render(r"Use \*\*care\*\* and \[not a link\](x) with C\# and 5\-10")
    assert out == '<p class="kp-p">Use **care** and [not a link](x) with C# and 5-10</p>'


def test_words_and_numbers_are_unchanged_by_formatting():
    shape = _assert_allowlisted(_render(_REAL))
    words = " ".join("".join(shape.text).split())
    for needle in (
        "Who are we?",
        "Bluefinch Bakery makes bread for twelve towns.",
        "Run the early shift and the ovens",
        "4.",
        "Bake a loaf",
        "$20 an hour",
        "our page (https://bakery.example/jobs)",
    ):
        assert needle in words


def test_raw_html_from_a_source_is_dropped_never_passed_and_angle_words_are_kept():
    out = _render(
        "<p>Join <b>Bluefinch</b> today.</p><p>Pay <USD 100k> and <5 years></p>"
        "<!-- note --><ul><li>Ovens</li></ul>"
    )
    shape = _assert_allowlisted(out)
    assert "".join(shape.text) == "Join Bluefinch today.Pay <USD 100k> and <5 years>Ovens"


# ------------------------------------------------------------------------- hostile input
HOSTILE = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
    "<img src=x onerror=alert(1)>",
    '<img src="x" onerror="alert(1)">hello',
    "<svg onload=alert(1)><circle/></svg>",
    "<svg><script>alert(1)</script></svg>",
    "<iframe src=javascript:alert(1)>",
    '<a href="javascript:alert(1)">click</a>',
    "[click](javascript:alert(1))",
    "[click](javascript:alert%281%29)",
    "[click](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "![x](data:image/svg+xml,<svg onload=alert(1)>)",
    '<https://x.example/"onmouseover=alert(1)>',
    "<javascript:alert(1)>",
    "**<script>alert(1)</script>**",
    "*<img src=x onerror=alert(1)>*",
    "# <script>alert(1)</script>",
    "* <b onclick=alert(1)>item</b>",
    "1. <style>body{display:none}</style>",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "&#60;img src=x onerror=alert(1)&#62;",
    "&amp;lt;b&amp;gt;double&amp;lt;/b&amp;gt;",
    "&#x3C;svg onload=alert(1)&#x3E;",
    "<scr<script>ipt>alert(1)</scr</script>ipt>",
    "<<script>script>alert(1)<</script>/script>",
    "<img/src=x/onerror=alert(1)>",
    '<p style="color:red" class="evil" id="x">styled</p>',
    "<!--<script>alert(1)</script>-->",
    "<![CDATA[<script>alert(1)</script>]]>",
    '<?xml version="1.0"?>',
    "<!DOCTYPE html>",
    "**[a](javascript:alert(1))**",
    "[**a**](https://ok.example) [*b*](https://ok.example)",
    "\\<script\\>alert(1)\\</script\\>",
    " sentinel chars",
    "\x00<script>\x00alert(1)",
    '"><script>alert(1)</script>',
    "' onmouseover='alert(1)",
]


@pytest.mark.parametrize("raw", HOSTILE, ids=[f"hostile{i}" for i in range(len(HOSTILE))])
def test_hostile_input_produces_only_allowlisted_elements_and_no_input_attribute(raw):
    out = _render(raw)
    _assert_allowlisted(out)
    # Nothing from the input reaches an attribute: the only attribute values are our classes.
    assert "<script" not in out.lower() and "<img" not in out.lower() and "<svg" not in out.lower()
    for _tag, attrs in _shape(out).tags:
        assert all(name == "class" for name, _ in attrs)


def test_entity_tricks_stay_text_after_one_read():
    shape = _assert_allowlisted(_render("&lt;script&gt;x&lt;/script&gt; &amp;lt;b&amp;gt;"))
    assert "".join(shape.text) == "<script>x</script> &lt;b&gt;"


@pytest.mark.parametrize(
    "raw",
    [
        "*" * 5000 + "a" + "*" * 5000,
        "**" * 20000,
        "**a " * 20000,
        "*a" * 20000,
        "[" * 50000 + "](x)",
        "<a" * 50000,
        "<!--" * 30000,
        "&#" * 50000,
        "\\*" * 50000,
        "".join(" " * i + "* deep\n" for i in range(3000)),
        "> " * 20000 + "quote",
        "#" * 20000 + " title",
        ("1. " * 5000) + "x",
    ],
    ids=lambda raw: f"len{len(raw)}-{raw[:6]!r}",
)
def test_deep_or_degenerate_input_stays_flat_and_allowlisted(raw):
    shape = _assert_allowlisted(_render(raw))
    assert shape.max_depth <= 4  # list, item, bold, italic at most


@pytest.mark.parametrize(
    "unit",
    [
        "a",
        "**a ",
        "*a",
        "* x\n",
        "[",
        "<a",
        "<!--",
        "![",
        "&#",
        "\\*",
        "**bold** and *it*\n",
        "## Head\n- item **b**\n\n",
        "<a ",
        "<x a=",
        "</x",
    ],
    ids=lambda unit: repr(unit[:8]),
)
def test_a_one_megabyte_description_is_bounded(unit):
    raw = unit * (1_000_000 // len(unit))
    started = time.perf_counter()
    blocks = pf.parse(raw)
    out = _render(raw) if len(raw) else ""
    elapsed = time.perf_counter() - started
    nodes = sum(1 + _count(b) for b in blocks)
    assert nodes <= pf.NODE_BUDGET * 2 + pf.LINE_FORMAT_CHARS
    assert elapsed < 10, elapsed  # generous: about 0.5 s on a laptop; quadratic would be minutes
    assert out


def _count(block) -> int:
    def inl(nodes) -> int:
        return sum(1 + (inl(n.children) if hasattr(n, "children") else 0) for n in nodes)

    if isinstance(block, pf.Heading):
        return inl(block.line)
    if isinstance(block, pf.Paragraph):
        return sum(inl(ln) for ln in block.lines)
    if isinstance(block, pf.ListBlock):
        return sum(1 + sum(inl(ln) for ln in it.lines) for it in block.items)
    return 1


def test_text_past_the_node_budget_is_kept_whole_as_one_plain_run():
    raw = "\n".join(f"* item {i} **b**" for i in range(pf.NODE_BUDGET * 2))
    blocks = pf.parse(raw)
    assert isinstance(blocks[-1], pf.Rest)
    assert blocks[-1].text.endswith(f"* item {pf.NODE_BUDGET * 2 - 1} **b**")
    shown = " ".join(pf.block_text(b) for b in blocks)
    assert f"item {pf.NODE_BUDGET * 2 - 1}" in shown and "item 0" in shown


def test_a_very_long_line_is_shown_as_plain_text():
    line = "*a* " * (pf.LINE_FORMAT_CHARS // 4 + 10)
    (block,) = pf.parse(line)
    assert block.lines == ((pf.Text(line.strip()),),)


@pytest.mark.parametrize("raw", [None, "", "  \n\t\n ", "<br/><p>&nbsp;</p>", "---", "* * *"])
def test_no_words_gives_no_blocks(raw):
    assert pf.parse(raw) == [] and pf.plain_text(raw) == ""


def test_plain_text_has_the_words_and_no_marks():
    assert pf.plain_text(_REAL).startswith(
        "Who are we? Bluefinch Bakery makes bread for twelve towns. We are growing. What you "
        "will do Run the early shift and the ovens"
    )
    assert "**" not in pf.plain_text(_REAL) and "##" not in pf.plain_text(_REAL)


def test_redaction_runs_after_markup_is_removed():
    raw = "<p>Join <b>Bluefinch</b> Bakery.</p> **Bluefinch&nbsp;Bakery** pays."
    out = pf.plain_text(raw, lambda t: t.replace("Bluefinch Bakery", "Current Employer"))
    assert "Bluefinch" not in out and out.count("Current Employer") == 2


def test_limit_blocks_keeps_the_first_blocks_and_says_when_it_cut():
    blocks = pf.parse("\n\n".join(["word " * 50] * 10))
    kept, cut = pf.limit_blocks(blocks, 600)
    assert cut and 1 <= len(kept) < len(blocks)
    assert pf.limit_blocks(blocks, 10**6) == (blocks, False)


def test_redaction_with_a_real_employer_map_finds_the_name_next_to_or_split_by_a_tag():
    from kestrel.privacy import Employer, EmployerMap
    from kestrel.web.quick_text import posting_text
    from kestrel.web.redact import redact_text

    mapping = EmployerMap([Employer(name="Bluefinch Bakery", shown_as="Current Employer")])

    def hide(t: str) -> str:
        return redact_text(t, mapping, enabled=True) or ""

    for raw in ("Join Bluefinch Bakery<sup>2</sup> now", "Bluefinch Bakery<!-- c -->Corp",
                "Join <b>Bluefinch</b> Bakery today", "Bluefinch&nbsp;Bakery pays",
                "**Bluefinch Bakery**<span>Labs</span>"):
        assert "Bluefinch" not in pf.plain_text(raw, hide), raw
        shown = _render_redacted(raw, hide)
        assert "Bluefinch" not in shown and "Current Employer" in shown, raw
        text = posting_text(raw, hide)
        assert "Bluefinch" not in " ".join(text.paragraphs) + text.excerpt, raw


def _render_redacted(raw, redact) -> str:
    from kestrel.web.deps import templates

    module = templates().env.get_template("partials/posting_macros.html").module
    return str(module.posting_body(pf.parse(raw, redact)))


def test_a_star_between_digits_is_arithmetic_not_italic():
    assert _render("Lift 2*3*4 = 24 boxes") == '<p class="kp-p">Lift 2*3*4 = 24 boxes</p>'


def test_limit_blocks_cuts_one_huge_first_block_to_the_cap():
    kept, cut = pf.limit_blocks(pf.parse("word " * 200000), 40000)
    assert cut and len(kept) == 1 and isinstance(kept[0], pf.Rest)
    assert 39000 < len(kept[0].text) <= 40000


@pytest.mark.parametrize(
    "unit",
    ["[", "<a", "**", "![", "[a](", "<!--", "**a ", "* x\n", "<x a=", "</x"],
    ids=lambda u: repr(u),
)
def test_the_quick_round_excerpt_is_bounded_on_a_one_megabyte_description(unit):
    from kestrel.web.quick_text import posting_text

    raw = unit * (1_000_000 // len(unit))
    started = time.perf_counter()
    posting_text(raw)
    assert time.perf_counter() - started < 10  # about 0.4 s at worst; quadratic was minutes


_SPLIT = [
    "Join Blue*finch* Bakery today",
    "Join Blue**finch** Bakery today",
    "Join **Blue**finch Bakery today",
    "Join ***Blue***finch Bakery today",
    "Join Blue[finch Bakery](https://x.example) today",
    "* Join Blue*finch* Bakery today",
    "## Blue**finch** Bakery",
    "Blue­finch Bakery pays",
    "Blue​finch Bake‍ry pays",
    "Blue&shy;finch Bakery pays",
]


@pytest.mark.parametrize("raw", _SPLIT, ids=[f"split{i}" for i in range(len(_SPLIT))])
def test_a_name_split_by_marks_or_invisible_characters_is_still_redacted(raw):
    from kestrel.privacy import Employer, EmployerMap
    from kestrel.web.quick_text import posting_text
    from kestrel.web.redact import redact_text

    mapping = EmployerMap([Employer(name="Bluefinch Bakery", shown_as="Current Employer")])

    def hide(t: str) -> str:
        return redact_text(t, mapping, enabled=True) or ""

    shown = _render_redacted(raw, hide)
    words = "".join(_shape(shown).text)
    assert "Bluefinch" not in words and "Current Employer" in words, shown
    assert "Bluefinch" not in pf.plain_text(raw, hide)
    text = posting_text(raw, hide)
    assert "Bluefinch" not in " ".join(text.paragraphs) + text.excerpt


def test_a_line_without_the_name_keeps_its_formatting_in_confidential_mode():
    from kestrel.privacy import Employer, EmployerMap
    from kestrel.web.redact import redact_text

    mapping = EmployerMap([Employer(name="Bluefinch Bakery", shown_as="Current Employer")])
    out = _render_redacted(
        "Run the **ovens** daily", lambda t: redact_text(t, mapping, enabled=True) or "")
    assert out == '<p class="kp-p">Run the <strong>ovens</strong> daily</p>'


@pytest.mark.parametrize("raw", [
    "a<wbr>b", "<q>a</q>b", "<cite>a</cite>b", "<kbd>a</kbd>b", "<caption>a</caption>b",
    '<path d="M0 0"/>ab', "<tt>a</tt>b", '<time datetime="2026-11-15">a</time>b',
    "<custom-el>a</custom-el>b", "<x-y data-k='1'>a</x-y>b",
])
def test_bare_tags_and_tags_with_attributes_are_dropped(raw):
    assert "".join(_assert_allowlisted(_render(raw)).text) == "ab"


@pytest.mark.parametrize("raw", ["Pay <USD 100k> a year", "Over <5 years> in ops",
                                 "Range <90k to 110k>"])
def test_words_in_angle_brackets_without_an_attribute_are_kept(raw):
    assert "".join(_assert_allowlisted(_render(raw)).text) == raw
