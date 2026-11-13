"""The exact DOM operations the Shinden parsers use; a backend must provide all of them and nothing more is relied on."""

from collections.abc import Iterator, Mapping
from typing import Protocol


class Node(Protocol):
    @property
    def tag(self) -> str | None:
        """Lower-case element name; ``-text`` for a text node."""
        ...

    @property
    def attributes(self) -> Mapping[str, str | None]:
        """A valueless attribute maps to None; attribute names are lower-case."""
        ...

    @property
    def next(self) -> Node | None:
        """Next sibling, text nodes included."""
        ...

    @property
    def text_content(self) -> str | None:
        """The text of a ``-text`` node; None for an element."""
        ...

    def css(self, selector: str) -> list[Node]:
        """Descendants in document order; supports type, class, id, attribute, descendant, child and selector lists."""
        ...

    def css_first(self, selector: str) -> Node | None: ...

    def text(self, deep: bool = True, separator: str = "", strip: bool = False) -> str: ...

    def iter(self, include_text: bool = False) -> Iterator[Node]:
        """Direct children."""
        ...

    def __hash__(self) -> int:
        """Identity of the underlying element: two handles to one element hash equal within a document."""
        ...


class Document(Node, Protocol):
    pass
