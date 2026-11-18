from pathlib import Path

import pytest

from alt_ani_core.dom import selectolax_backend, stdlib_backend

SHINDEN_FIXTURES = sorted((Path(__file__).parent.parent / "fixtures" / "shinden").glob("*.html"))
# selectolax synthesises these around fragments; the stdlib tree keeps the document as written
SYNTHESISED = {"html", "head", "body"}


def _dump(nodes) -> list[tuple]:
    return [
        (
            n.tag,
            dict(n.attributes),
            n.text(deep=False, strip=True),
            n.text(separator="|"),
            n.text(separator="|", strip=True),
            [(c.tag, c.text_content) for c in n.iter(include_text=True)],
        )
        for n in nodes
        if n.tag not in SYNTHESISED
    ]


def _both(html: str, selector: str, *, ordered: bool = True) -> tuple[list[tuple], list[tuple]]:
    expected, actual = _dump(selectolax_backend.parse(html).css(selector)), _dump(stdlib_backend.parse(html).css(selector))
    return (expected, actual) if ordered else (sorted(expected, key=repr), sorted(actual, key=repr))


@pytest.mark.unit
class TestFixtureEquivalence:
    @pytest.mark.parametrize("path", SHINDEN_FIXTURES, ids=lambda p: p.name)
    def test_whole_tree_matches_selectolax(self, path):
        html = path.read_text(encoding="utf-8")
        expected, actual = _both(html, "*")
        assert actual == expected


SNIPPETS = {
    "implied_li": "<ul><li>a<li>b <b>x</b><li>c</ul>",
    "nested_lists": "<ul><li>a<ul><li>a1<li>a2</ul><li>b</ul>",
    "li_through_div": "<ul><li>a<div>x<li>b</div></ul>",
    "implied_dt_dd": "<dl><dt>Emisja<dd>01.01.2024<dt>Status<dd>Zakończone</dl>",
    "p_closed_by_block": "<div><p>one<p>two<div>three</div><p>four<ul><li>x</ul></div>",
    "p_inside_span": "<p><span>a<div>b</div></span></p>",
    "table_cells": "<table><tbody><tr><td>1<td>2<tr><th>h<td>3</tbody></table>",
    "select_options": "<select><option value=1>a<option value=2 selected>b</select>",
    "void_and_self_closing": "<div>a<br>b<img src=x alt=''><br/>c<svg><path d='M0'/><circle r=1 /></svg>d</div>",
    "non_void_self_closing": "<div><span/>text</div>",
    "entities": '<a href="/x?a=1&amp;b=2" title="&quot;q&quot;">Tom &amp; Jerry &#8211; &eacute;</a>',
    "comments": "<div id=a> x <b> </b> y <!-- c --> z <i>w</i></div>",
    "script_and_style": "<div><script>if (a < b && c > d) { x = '</div>'.length }</script><style>p>a{}</style>t</div>",
    "json_script": '<script id="d" type="application/json">{"a": "<b>"}</script><p>after</p>',
    "attributes": '<a x y="" z=0 data-k="v" CLASS="one two">t</a>',
    "duplicate_attributes": "<a x=1 x=2>t</a>",
    "stray_end_tags": "<div>a</span>b</p>c</div>d",
    "whitespace_text": "<ol>\n  <li> a </li>\n  <li>\n b \n</li>\n</ol>",
    "lt_in_text": "<p>a < b and c<d</p>",
}
SELECTORS = [
    "*",
    "li",
    "ul > li",
    "ul li",
    "ul ul > li",
    "dl > dt",
    "dd",
    "p",
    "div p",
    "div > p",
    "td, th",
    "tr > td",
    "option[selected]",
    "option[value='2']",
    'a[href="/x?a=1&b=2"]',
    "[data-k=v]",
    "a.one.two",
    "a.one",
    ".two",
    "#a",
    "div#a > b",
    "script#d",
    "svg path",
    "span",
    "b, i",
]


@pytest.mark.unit
class TestSnippetEquivalence:
    @pytest.mark.parametrize("html", SNIPPETS.values(), ids=SNIPPETS.keys())
    @pytest.mark.parametrize("selector", SELECTORS)
    def test_selector_results_match_selectolax(self, html, selector):
        # lexbor groups child-combinator matches by parent; the contract (and the stdlib backend) is document order
        expected, actual = _both(html, selector, ordered=False)
        assert actual == expected

    @pytest.mark.parametrize("html", SNIPPETS.values(), ids=SNIPPETS.keys())
    def test_css_first_is_first_of_css(self, html):
        doc = stdlib_backend.parse(html)
        for selector in SELECTORS:
            found = doc.css(selector)
            assert doc.css_first(selector) is (found[0] if found else None)


@pytest.mark.unit
class TestNodeContract:
    def test_next_walks_siblings_including_text(self):
        div = stdlib_backend.parse("<div><b>a</b> mid <i>c</i></div>").css_first("div")
        assert div is not None
        first = div.css_first("b")
        assert first is not None
        text = first.next
        assert text is not None
        assert (text.tag, text.text_content) == ("-text", " mid ")
        last = text.next
        assert last is not None
        assert last.tag == "i"
        assert last.next is None

    def test_css_returns_document_order(self):
        doc = stdlib_backend.parse("<ul><li>a<ul><li>a1<li>a2</ul><li>b</ul>")
        assert [li.text(deep=False) for li in doc.css("ul > li")] == ["a", "a1", "a2", "b"]

    def test_text_content_is_none_for_elements(self):
        b = stdlib_backend.parse("<b>a</b>").css_first("b")
        assert b is not None
        assert b.text_content is None

    def test_same_element_hashes_equal_across_queries(self):
        doc = stdlib_backend.parse("<ul><li>a</li><li>b</li></ul>")
        assert {hash(n) for n in doc.css("li")} == {hash(n) for n in doc.css("ul > li")}
        assert len(set(doc.css("li")) | set(doc.css("ul li"))) == 2

    def test_css_is_scoped_to_descendants(self):
        doc = stdlib_backend.parse("<div id=o><p>x</p><div id=i><p>y</p></div></div><p>z</p>")
        inner = doc.css_first("#i")
        assert inner is not None
        assert [p.text() for p in inner.css("p")] == ["y"]
        assert [p.text() for p in inner.css("div p")] == ["y"]

    @pytest.mark.parametrize("selector", ["", " ", "a:hover", "a + b", "a ~ b", "> a", "a >", "a >> b", "a[", "a[x|=y]", "a,"])
    def test_unsupported_selectors_raise(self, selector):
        with pytest.raises(ValueError, match="unsupported selector"):
            stdlib_backend.parse("<a>x</a>").css(selector)
