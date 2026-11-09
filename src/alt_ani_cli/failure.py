"""Failure categories shared by the extractor chain and the Shinden XHR client."""

from curl_cffi.requests import exceptions as cffi_exceptions

from alt_ani_cli.errors import JavaScriptRequiredError, UnsupportedHostError

CATEGORY_PARSER_DRIFT = "parser_drift"  # our own regex/URL-shape assumption no longer matches what we received
CATEGORY_NO_STREAM_URL = "no_stream_url"  # request succeeded and parsed, but no playable URL was in it


# Classify by a tagged .category or exception type only — messages may carry URLs/tokens.
def classify_exception(exc: Exception) -> str:
    tagged = getattr(exc, "category", None)
    if isinstance(tagged, str):
        return tagged
    if isinstance(exc, (JavaScriptRequiredError, UnsupportedHostError)):
        return "unsupported_host"
    if isinstance(exc, cffi_exceptions.JSONDecodeError):
        return CATEGORY_PARSER_DRIFT
    if isinstance(exc, cffi_exceptions.Timeout):
        return "timeout"
    if isinstance(exc, cffi_exceptions.HTTPError):
        return "http_error"
    if isinstance(exc, cffi_exceptions.RequestException):
        return "network_error"
    return "unknown"


def http_status_of(exc: Exception) -> int | None:
    status = getattr(exc, "http_status", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None
