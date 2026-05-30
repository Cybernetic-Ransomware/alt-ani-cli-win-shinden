import re

from alt_ani_cli.content import EXCEPTIONS_PL
from alt_ani_cli.errors import ShindenError

_AGE_GATE_HINTS = (
    "musisz mieć ukończone 18",
    "ukończone 18 lat",
    "treści dla dorosłych",
    "adult content",
    "age verification",
    "potwierdź wiek",
)


def _normalize_title(t: str) -> str:
    t = re.sub(r"([:·])([A-Za-z])", r"\1 \2", t)
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
    return t.strip()


def _check_age_gate(html: str) -> None:
    lower = html.lower()
    if any(hint in lower for hint in _AGE_GATE_HINTS):
        raise ShindenError(EXCEPTIONS_PL["series"]["age_gate"])
