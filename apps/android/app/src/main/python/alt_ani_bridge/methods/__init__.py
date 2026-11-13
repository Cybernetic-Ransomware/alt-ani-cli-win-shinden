"""Method handlers grouped by area; each module exports ``METHODS``."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from alt_ani_bridge.context import Call


@dataclass(frozen=True)
class Method:
    handler: Callable[[Call], Any]
    fields: frozenset[str] = frozenset()
    needs_engine: bool = True


def method(handler: Callable[[Call], Any], *fields: str, needs_engine: bool = True) -> Method:
    return Method(handler, frozenset(fields), needs_engine)
