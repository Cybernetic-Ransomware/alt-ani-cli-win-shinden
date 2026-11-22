import hashlib

_INLINE_LIMIT = 256


def pytest_assertrepr_compare(op, left, right):
    """Media comparisons report lengths, digests and the first difference instead of megabytes of repr."""
    if op != "==" or not isinstance(left, bytes) or not isinstance(right, bytes):
        return None
    if max(len(left), len(right)) <= _INLINE_LIMIT:
        return None
    first = next((i for i, (a, b) in enumerate(zip(left, right, strict=False)) if a != b), min(len(left), len(right)))
    return [
        "bytes differ",
        f"lengths: {len(left)} != {len(right)}" if len(left) != len(right) else f"lengths: both {len(left)}",
        f"sha256: {hashlib.sha256(left).hexdigest()[:16]} vs {hashlib.sha256(right).hexdigest()[:16]}",
        f"first difference at offset {first}: {left[first : first + 8]!r} vs {right[first : first + 8]!r}",
    ]
