"""HTML parsing behind a contract, so the parser library can change without touching the Shinden parsers."""

from collections.abc import Callable
from typing import cast

from alt_ani_core.dom.api import Document, Node

_backend: Callable[[str], Document] | None = None


def set_backend(parse: Callable[[str], Document]) -> None:
    """Install ``parse(html) -> Document``; meant for platforms that cannot install selectolax."""
    global _backend
    _backend = parse


def parse(html: str) -> Document:
    global _backend
    backend = _backend
    if backend is None:
        try:
            from alt_ani_core.dom.selectolax_backend import parse as selectolax_parse
        except ImportError as exc:
            raise RuntimeError(
                "No HTML backend available: install alt-ani-core[selectolax] or call alt_ani_core.dom.set_backend()"
            ) from exc
        backend = _backend = cast("Callable[[str], Document]", selectolax_parse)
    return backend(html)


__all__ = ["Document", "Node", "parse", "set_backend"]
