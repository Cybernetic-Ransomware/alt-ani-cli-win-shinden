import unicodedata

MAX_COMPONENT_BYTES = 120
_ALLOWED_PUNCTUATION = " _-"
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _truncate(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def safe_component(text: str, fallback: str, *, limit: int = MAX_COMPONENT_BYTES) -> str:
    """Letters, digits, space, ``_`` and ``-`` only; never empty, a reserved Windows name or ending in a dot or space."""
    normalized = unicodedata.normalize("NFC", text)
    kept = "".join(c for c in normalized if c.isalnum() or c in _ALLOWED_PUNCTUATION)
    cleaned = _truncate(" ".join(kept.split()), limit).strip(" .")
    if not cleaned or cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = fallback
    return cleaned


def safe_extension(ext: str) -> str:
    cleaned = "".join(c for c in ext.lower() if c.isascii() and c.isalnum())[:8]
    return cleaned or "bin"


def publish_key(series_id: str, series_title: str, episode_label: str, extension: str, attempt: int = 1) -> str:
    """``attempt`` > 1 adds `` (n)`` to resolve a collision; the result is a relative POSIX key."""
    sid = safe_component(series_id, "series", limit=32)
    title = safe_component(series_title, f"series-{sid}", limit=80)
    folder = f"{title} [{sid}]"
    label = safe_component(episode_label, "0", limit=16)
    suffix = f" ({attempt})" if attempt > 1 else ""
    stem = _truncate(f"{title} - ep{label}", MAX_COMPONENT_BYTES - len(suffix) - 9).strip(" .")
    return f"{folder}/{stem}{suffix}.{safe_extension(extension)}"
