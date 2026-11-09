import re
from collections import Counter
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from alt_ani_cli import diagnostics
from alt_ani_cli.content import EXCEPTIONS, EXCEPTIONS_PL
from alt_ani_cli.errors import ParseError, ShindenError

_AGE_GATE_HINTS = (
    "musisz mieć ukończone 18",
    "ukończone 18 lat",
    "treści dla dorosłych",
    "adult content",
    "age verification",
    "potwierdź wiek",
)

_CHALLENGE_TITLES = ("just a moment", "attention required")


def _normalize_title(t: str) -> str:
    t = re.sub(r"([:·])([A-Za-z])", r"\1 \2", t)
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
    return t.strip()


def _check_age_gate(html: str) -> None:
    lower = html.lower()
    if any(hint in lower for hint in _AGE_GATE_HINTS):
        raise ShindenError(EXCEPTIONS_PL["series"]["age_gate"])


def _check_challenge(tree: HTMLParser, page: str) -> None:
    """A 200 Cloudflare interstitial must not be mistaken for an empty or drifted page."""
    title = tree.css_first("title")
    text = title.text(strip=True).lower() if title else ""
    if any(marker in text for marker in _CHALLENGE_TITLES) or tree.css_first("#challenge-form, #cf-challenge-running"):
        raise ParseError(EXCEPTIONS["shinden"]["challenge"].format(page=page))


def drift(page: str, detail: str) -> ParseError:
    return ParseError(EXCEPTIONS["shinden"]["parser_drift"].format(page=page, detail=detail))


def _fmt_reasons(reasons: Counter[str]) -> str:
    return ",".join(f"{reason}:{count}" for reason, count in sorted(reasons.items()))


@dataclass
class ParseTally:
    """Each record found is exactly one of valid, invalid (required field broken) or excluded (skipped on purpose)."""

    page: str
    valid: int = 0
    invalid: Counter[str] = field(default_factory=Counter)
    excluded: Counter[str] = field(default_factory=Counter)

    @property
    def found(self) -> int:
        return self.valid + self.invalid.total() + self.excluded.total()

    def accept(self) -> None:
        self.valid += 1

    def reject(self, reason: str) -> None:
        self.invalid[reason] += 1

    def exclude(self, reason: str) -> None:
        self.excluded[reason] += 1

    def finish(self) -> None:
        """Exclusions alone never raise; invalid records raise only when no record is valid."""
        n_invalid = self.invalid.total()
        if not n_invalid:
            return
        diagnostics.shinden_parse_partial(
            self.page,
            found=self.found,
            valid=self.valid,
            invalid=_fmt_reasons(self.invalid),
            excluded=_fmt_reasons(self.excluded),
        )
        if self.valid == 0:
            raise drift(self.page, f"all {n_invalid} expected records are invalid ({_fmt_reasons(self.invalid)})")
