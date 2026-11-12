"""Row and JSON conversions for local state; the JSON columns refuse anything that looks like a URL."""

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from alt_ani_core.state import (
    DownloadCriteria,
    EpisodeKey,
    ItemStatus,
    JobError,
    OfflineItem,
    OfflineItemSpec,
    PartialTransfer,
    QualityCriterion,
    ResourceValidator,
    SourceIdentity,
    StorageLocator,
    VariantIdentity,
)

Row = Mapping[str, Any]


def _check_plain(value: object) -> None:
    if isinstance(value, str) and "://" in value:
        raise ValueError("local state never stores URLs in identity, validator or error fields")
    if isinstance(value, dict):
        for item in value.values():
            _check_plain(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _check_plain(item)


def dump(value: object) -> str:
    _check_plain(value)
    return json.dumps(value, sort_keys=True)


def key_of(row: Row) -> EpisodeKey:
    return EpisodeKey(row["series_id"], row["number"])


def criteria_of(audio: str, subs: str, quality: str) -> DownloadCriteria:
    return DownloadCriteria(audio=audio, subs=subs, quality=QualityCriterion.parse(quality))


def source_json(source: SourceIdentity | None) -> str | None:
    return None if source is None else dump(asdict(source))


def source_of(text: str | None) -> SourceIdentity | None:
    return None if text is None else SourceIdentity(**json.loads(text))


def variant_json(variant: VariantIdentity | None) -> str | None:
    return None if variant is None else dump(asdict(variant))


def variant_of(text: str | None) -> VariantIdentity | None:
    return None if text is None else VariantIdentity(**json.loads(text))


def validator_json(validator: ResourceValidator | None) -> str | None:
    if validator is None:
        return None
    return dump({"kind": validator.kind, "fields": [list(pair) for pair in validator.fields]})


def validator_of(text: str | None) -> ResourceValidator | None:
    if text is None:
        return None
    data = json.loads(text)
    return ResourceValidator(kind=data["kind"], fields=tuple((k, v) for k, v in data["fields"]))


def partial_columns(partial: PartialTransfer | None) -> tuple[str | None, str | None, str | None, str | None]:
    if partial is None:
        return None, None, None, None
    identity = dump({"source": asdict(partial.source), "variant": asdict(partial.variant)})
    return partial.locator.backend, partial.locator.key, identity, validator_json(partial.validator)


def partial_of(row: Row) -> PartialTransfer | None:
    if row["partial_backend"] is None or row["partial_identity_json"] is None:
        return None
    identity = json.loads(row["partial_identity_json"])
    return PartialTransfer(
        locator=StorageLocator(row["partial_backend"], row["partial_key"]),
        source=SourceIdentity(**identity["source"]),
        variant=VariantIdentity(**identity["variant"]),
        validator=validator_of(row["partial_validator_json"]),
    )


def error_columns(error: JobError | None) -> tuple[str | None, str | None]:
    if error is None:
        return None, None
    return error.code, dump(dict(error.params))


def error_of(row: Row) -> JobError | None:
    if row["last_error_code"] is None:
        return None
    params = json.loads(row["last_error_params"] or "{}")
    return JobError(row["last_error_code"], tuple(sorted(params.items())))


def locator_of(backend: str | None, key: str | None) -> StorageLocator | None:
    return None if backend is None or key is None else StorageLocator(backend, key)


def item_of(row: Row) -> OfflineItem:
    return OfflineItem(
        id=row["id"],
        key=key_of(row),
        status=ItemStatus(row["status"]),
        spec=OfflineItemSpec(
            locator=StorageLocator(row["storage_backend"], row["storage_key"]),
            size_bytes=row["size_bytes"],
            audio=row["audio"],
            subs=row["subs"],
            height=row["height"],
            variant_label=row["variant_label"],
            protocol=row["protocol"],
            container=row["container"],
            source_player=row["source_player"],
        ),
        created_at=row["created_at"],
        verified_at=row["verified_at"],
        revision=row["revision"],
    )
