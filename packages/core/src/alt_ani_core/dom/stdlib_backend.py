"""Pure-stdlib backend for Android; unlike HTML5 it adds no implicit html/head/body/tbody and never re-parents elements."""

import re
from collections.abc import Iterator, Mapping
from functools import lru_cache
from html.parser import HTMLParser

from alt_ani_core.dom.api import Node

TEXT_TAG = "-text"
COMMENT_TAG = "_comment"
_ROOT_TAG = "-document"
_NON_ELEMENTS = frozenset({TEXT_TAG, COMMENT_TAG})

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
# start tags that close an open p (the HTML5 "close a p element" list)
_CLOSES_P = frozenset(
    {
        "address", "article", "aside", "blockquote", "details", "dialog", "div", "dl", "fieldset", "figcaption", "figure",
        "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol", "p",
        "pre", "section", "summary", "table", "ul",
    }
)  # fmt: skip
# the HTML5 "button scope" an open p is looked for in
_P_SCOPE = frozenset({"html", "table", "td", "th", "caption", "template", "button", "object"})
# elements an implied end tag never reaches past
_SCOPE = frozenset(
    {
        "html", "body", "table", "td", "th", "caption", "template", "button", "object", "select", "ul", "ol", "dl",
        "section", "article", "aside", "nav", "header", "footer", "main", "details", "fieldset", "form", "figure",
        "blockquote", "menu",
    }
)  # fmt: skip
_IMPLIED_BY = {
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "option": frozenset({"option"}),
    "tr": frozenset({"tr"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
}
_P = frozenset({"p"})
_ROW_SCOPE = frozenset({"table", "tbody", "thead", "tfoot", "html", "template"})
_CELL_SCOPE = frozenset({"tr", "table", "html", "template"})


class StdlibNode:
    __slots__ = ("_attributes", "_children", "_next", "_parent", "_tag", "_text")

    def __init__(self, tag: str, attributes: Mapping[str, str | None] | None = None, text: str | None = None) -> None:
        self._tag = tag
        self._attributes: Mapping[str, str | None] = attributes if attributes is not None else {}
        self._text = text
        self._parent: StdlibNode | None = None
        self._next: StdlibNode | None = None
        self._children: list[StdlibNode] = []

    @property
    def tag(self) -> str:
        return self._tag

    @property
    def attributes(self) -> Mapping[str, str | None]:
        return self._attributes

    @property
    def next(self) -> StdlibNode | None:
        return self._next

    @property
    def text_content(self) -> str | None:
        return self._text if self._tag == TEXT_TAG else None

    def iter(self, include_text: bool = False) -> Iterator[StdlibNode]:
        for child in self._children:
            if include_text or child._tag != TEXT_TAG:
                yield child

    def css(self, selector: str) -> list[Node]:
        groups = _compile(selector)
        return [node for node in self._elements() if any(_matches(node, steps, len(steps) - 1) for steps in groups)]

    def css_first(self, selector: str) -> StdlibNode | None:
        groups = _compile(selector)
        for node in self._elements():
            if any(_matches(node, steps, len(steps) - 1) for steps in groups):
                return node
        return None

    def text(self, deep: bool = True, separator: str = "", strip: bool = False) -> str:
        if self._tag == TEXT_TAG:
            text = self._text or ""
            return text.strip() if strip else text
        parts = self._texts(deep)
        # selectolax keeps pieces that strip to nothing, so "a<b> </b>c" joins as "a||c"
        return separator.join(part.strip() for part in parts) if strip else separator.join(parts)

    def _append(self, child: StdlibNode) -> None:
        child._parent = self
        if self._children:
            self._children[-1]._next = child
        self._children.append(child)

    def _elements(self) -> Iterator[StdlibNode]:
        stack = [child for child in reversed(self._children) if child._tag not in _NON_ELEMENTS]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(child for child in reversed(node._children) if child._tag not in _NON_ELEMENTS)

    def _texts(self, deep: bool) -> list[str]:
        if not deep:
            return [child._text or "" for child in self._children if child._tag == TEXT_TAG]
        out: list[str] = []
        stack = list(reversed(self._children))
        while stack:
            node = stack.pop()
            if node._tag == TEXT_TAG:
                out.append(node._text or "")
            elif node._tag != COMMENT_TAG:
                stack.extend(reversed(node._children))
        return out

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    def __repr__(self) -> str:
        return f"<StdlibNode {self._tag}>"


type _Part = tuple[str, str, str | None]
type _Step = tuple[str, str | None, tuple[_Part, ...]]

_TOKEN = re.compile(r"""\s*(>)\s*|\s+|((?:[^\s>\[]|\[[^\]]*\])+)""")
_COMPOUND = re.compile(r"""(?P<tag>[A-Za-z][\w-]*|\*)?(?P<rest>(?:[.#][\w-]+|\[[^\]]*\])*)""")
_PART = re.compile(
    r"""\.(?P<cls>[\w-]+)|#(?P<id>[\w-]+)"""
    r"""|\[\s*(?P<attr>[\w-]+)\s*(?:=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\]\s"']+)))?\s*\]"""
)


def _split_groups(selector: str) -> list[str]:
    groups, depth, quote, start = [], 0, "", 0
    for i, ch in enumerate(selector):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            groups.append(selector[start:i])
            start = i + 1
    groups.append(selector[start:])
    return groups


def _compile_compound(token: str, selector: str) -> tuple[str | None, tuple[_Part, ...]]:
    m = _COMPOUND.fullmatch(token)
    if m is None or not token:
        raise ValueError(f"unsupported selector: {selector!r}")
    tag = m["tag"].lower() if m["tag"] and m["tag"] != "*" else None
    parts: list[_Part] = []
    rest = m["rest"]
    pos = 0
    for p in _PART.finditer(rest):
        if p.start() != pos:
            raise ValueError(f"unsupported selector: {selector!r}")
        pos = p.end()
        if p["cls"]:
            parts.append(("class", p["cls"], None))
        elif p["id"]:
            parts.append(("id", p["id"], None))
        else:
            value = next((v for v in (p["dq"], p["sq"], p["bare"]) if v is not None), None)
            parts.append(("attr", p["attr"].lower(), value))
    if pos != len(rest):
        raise ValueError(f"unsupported selector: {selector!r}")
    return tag, tuple(parts)


@lru_cache(maxsize=256)
def _compile(selector: str) -> tuple[tuple[_Step, ...], ...]:
    """Each group is a tuple of steps; a step's combinator joins it to the step before."""
    groups: list[tuple[_Step, ...]] = []
    for group in _split_groups(selector):
        steps: list[_Step] = []
        combinator = " "
        pos = 0
        text = group.strip()
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if m is None or m.end() == pos:
                raise ValueError(f"unsupported selector: {selector!r}")
            pos = m.end()
            if m.group(1):
                if not steps or combinator == ">":
                    raise ValueError(f"unsupported selector: {selector!r}")
                combinator = ">"
            elif m.group(2):
                tag, parts = _compile_compound(m.group(2), selector)
                steps.append((combinator, tag, parts))
                combinator = " "
        if not steps or combinator == ">":
            raise ValueError(f"unsupported selector: {selector!r}")
        groups.append(tuple(steps))
    return tuple(groups)


def _matches_compound(node: StdlibNode, tag: str | None, parts: tuple[_Part, ...]) -> bool:
    if tag is not None and node._tag != tag:
        return False
    attrs = node._attributes
    for kind, name, value in parts:
        if kind == "class":
            if name not in (attrs.get("class") or "").split():
                return False
        elif kind == "id":
            if attrs.get("id") != name:
                return False
        elif name not in attrs or (value is not None and attrs[name] != value):
            return False
    return True


def _matches(node: StdlibNode, steps: tuple[_Step, ...], i: int) -> bool:
    combinator, tag, parts = steps[i]
    if not _matches_compound(node, tag, parts):
        return False
    if i == 0:
        return True
    parent = node._parent
    if combinator == ">":
        return parent is not None and parent._tag != _ROOT_TAG and _matches(parent, steps, i - 1)
    while parent is not None and parent._tag != _ROOT_TAG:
        if _matches(parent, steps, i - 1):
            return True
        parent = parent._parent
    return False


def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
    # as in selectolax: an empty value reads as None and the last duplicate wins
    return {name: value or None for name, value in attrs}


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = StdlibNode(_ROOT_TAG)
        self._stack = [self.root]

    def _find_open(self, targets: frozenset[str], scope: frozenset[str]) -> int | None:
        for idx in range(len(self._stack) - 1, 0, -1):
            tag = self._stack[idx]._tag
            if tag in targets:
                return idx
            if tag in scope:
                return None
        return None

    def _close_in_scope(self, targets: frozenset[str], scope: frozenset[str]) -> None:
        if (idx := self._find_open(targets, scope)) is not None:
            del self._stack[idx:]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _CLOSES_P:
            self._close_in_scope(_P, _P_SCOPE)
        if tag == "tr":
            self._close_in_scope(_IMPLIED_BY[tag], _ROW_SCOPE)
        elif tag in ("td", "th"):
            self._close_in_scope(_IMPLIED_BY[tag], _CELL_SCOPE)
        elif tag in _IMPLIED_BY:
            self._close_in_scope(_IMPLIED_BY[tag], _SCOPE)
        node = StdlibNode(tag, _attributes(attrs))
        self._stack[-1]._append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # a self-closing non-void HTML tag is really a start tag; inside svg/math it is complete
        if tag not in _VOID and not any(n._tag in ("svg", "math") for n in self._stack):
            self.handle_starttag(tag, attrs)
            return
        if tag in _CLOSES_P:
            self._close_in_scope(_P, _P_SCOPE)
        self._stack[-1]._append(StdlibNode(tag, _attributes(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._find_open(_P, _P_SCOPE) is None:
            # a stray </p> opens and closes an empty p
            self._stack[-1]._append(StdlibNode("p", {}))
            return
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx]._tag == tag:
                del self._stack[idx:]
                return

    def handle_comment(self, data: str) -> None:
        self._stack[-1]._append(StdlibNode(COMMENT_TAG))

    def handle_data(self, data: str) -> None:
        parent = self._stack[-1]
        if parent._children and parent._children[-1]._tag == TEXT_TAG:
            last = parent._children[-1]
            last._text = (last._text or "") + data
            return
        parent._append(StdlibNode(TEXT_TAG, text=data))


def parse(html: str) -> StdlibNode:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root
