"""Display-only redaction of URL query values and secret headers — never apply to request data."""

import re
from collections.abc import Mapping

REDACTED = "<redacted>"

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
    }
)

# Query suffix of any URL-ish token, scheme optional; `(?<!\\\\)` skips Windows `\\?\` long-path prefixes.
_QUERY_RE = re.compile(r"(?<=\S)(?<!\\\\)(?P<sep>[?#])(?P<query>[^\s\"'`?#]*)")
_PARAM_RE = re.compile(r"(?P<lead>^|&)(?P<name>[^=&]+)=(?P<value>[^&]*)")
_USERINFO_RE = re.compile(r"(?<=://)[^/\s@?#]+@")


_TRAILING_PUNCT = ")]}>.,;:!"


def _redact_query(match: re.Match[str]) -> str:
    body, tail = match["query"], ""
    # Keep prose punctuation after a URL, but never split the `>` of an existing marker.
    while body and body[-1] in _TRAILING_PUNCT and not body.endswith(REDACTED):
        body, tail = body[:-1], body[-1] + tail
    body = _PARAM_RE.sub(lambda m: f"{m['lead']}{m['name']}={REDACTED}", body)
    return f"{match['sep']}{body}{tail}"


def redact_text(text: object) -> str:
    """Redact all query values and userinfo, keeping names/host/path; idempotent on its own output."""
    try:
        s = str(text)
    except Exception:
        return REDACTED
    s = _USERINFO_RE.sub(f"{REDACTED}@", s)
    return _QUERY_RE.sub(_redact_query, s)


def redact_header_value(name: str, value: object) -> str:
    if str(name).strip().lower() in SENSITIVE_HEADERS:
        return REDACTED
    return redact_text(value)


def redact_headers(headers: Mapping[str, object]) -> dict[str, str]:
    return {k: redact_header_value(k, v) for k, v in headers.items()}
