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

# Stops only at chars invalid unencoded in a URL (or `#`), so ambiguous tails get redacted; skips `\\?\` paths.
_QUERY_RE = re.compile(r"(?<=\S)(?<!\\\\)(?P<sep>[?#])(?P<query>[^\s\"`#]*)")
_PARAM_RE = re.compile(r"(?P<lead>^|&)(?P<name>[^=&]+)=(?P<value>[^&]*)")
_USERINFO_RE = re.compile(r"(?<=://)[^/\s@?#]+@")


def _redact_query(match: re.Match[str]) -> str:
    body = _PARAM_RE.sub(lambda m: f"{m['lead']}{m['name']}={REDACTED}", match["query"])
    return f"{match['sep']}{body}"


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
