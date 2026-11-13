"""Failure categories shared by the extractor chain and the Shinden XHR client."""

from alt_ani_core.errors import JavaScriptRequiredError, UnsupportedHostError
from alt_ani_core.transport.api import DecodeError, HttpStatusError, TransportError, TransportTimeout

CATEGORY_PARSER_DRIFT = "parser_drift"  # our own regex/URL-shape assumption no longer matches what we received
CATEGORY_NO_STREAM_URL = "no_stream_url"  # request succeeded and parsed, but no playable URL was in it


# Classify by a tagged .category or exception type only — messages may carry URLs/tokens.
def classify_exception(exc: BaseException) -> str:
    tagged = getattr(exc, "category", None)
    if isinstance(tagged, str):
        return tagged
    if isinstance(exc, (JavaScriptRequiredError, UnsupportedHostError)):
        return "unsupported_host"
    if isinstance(exc, DecodeError):
        return CATEGORY_PARSER_DRIFT
    if isinstance(exc, TransportTimeout):
        return "timeout"
    if isinstance(exc, HttpStatusError):
        return "http_error"
    if isinstance(exc, TransportError):
        return "network_error"
    return "unknown"


def http_status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "http_status", None)
    if status is None:
        status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None
