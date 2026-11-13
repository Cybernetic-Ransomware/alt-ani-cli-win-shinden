"""Contract version and input limits; ``bridge_info`` reports them as they are enforced."""

CONTRACT = "2.0"
MAJOR = 2

LIMITS: dict[str, int] = {
    "request_bytes": 1024 * 1024,
    "string_chars": 4096,
    "list_items": 5000,
    "item_ids": 1000,
    "episodes": 5000,
    "job_ids": 10000,
    "events_max": 500,
    "wait_ms": 30000,
}

RESERVED_FIELDS = frozenset({"engine_id", "op_id", "contract"})
