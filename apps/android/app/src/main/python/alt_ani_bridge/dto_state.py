"""Plain-data views of local state; no locator key, attempt id or URL appears here except in a playback target."""

from pathlib import PurePosixPath
from typing import Any

from alt_ani_core.models import PlayerEntry
from alt_ani_core.rules.criteria import match_source, source_identity
from alt_ani_core.rules.job_state import available_actions
from alt_ani_core.state import (
    DownloadCriteria,
    DownloadJob,
    EpisodeKey,
    OfflineItem,
    PlaybackSession,
    Preferences,
    SourceIdentity,
    VariantIdentity,
    WatchState,
)
from alt_ani_core.storage.api import PlaybackTarget

_INTERACTION_PARAMS = ("scope", "host", "online_id")


def criteria(value: DownloadCriteria) -> dict[str, Any]:
    return {"audio": value.audio, "subs": value.subs, "quality": value.quality.encode()}


def source_id(value: SourceIdentity | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "online_id": value.online_id,
        "player": value.player,
        "lang_audio": value.lang_audio,
        "lang_subs": value.lang_subs,
        "max_res": value.max_res,
        "subs_author": value.subs_author,
        "source": value.source,
    }


def variant_id(value: VariantIdentity | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"label": value.label, "height": value.height, "protocol": value.protocol, "container": value.container}


def job(value: DownloadJob, title: tuple[str, str] | None = None) -> dict[str, Any]:
    error = value.last_error
    params = dict(error.params) if error else {}
    waiting_interaction = value.wait_reason is not None and value.wait_reason.value == "interaction"
    interaction = {k: params.get(k) for k in _INTERACTION_PARAMS} if waiting_interaction and error else None
    return {
        "job_id": value.id,
        "revision": value.revision,
        "queue_pos": value.queue_pos,
        "series": {"id": value.key.series_id, "title": title[0] if title else None},
        "episode": {"number": value.key.number, "title": title[1] if title else None},
        "state": value.state.value,
        "wait_reason": value.wait_reason.value if value.wait_reason else None,
        "stop_target": value.stop_target.value if value.stop_target else None,
        "criteria": criteria(value.criteria),
        "alternatives": value.alternatives.value,
        "selection_mode": value.selection_mode.value,
        "source": source_id(value.source),
        "variant": variant_id(value.variant),
        "cellular_allowed": value.cellular_allowed,
        "attempts": value.attempts,
        "retry_count": value.retry_count,
        "not_before": value.not_before,
        "bytes_done": value.bytes_done,
        "bytes_total": value.bytes_total,
        "has_partial": value.partial is not None,
        "last_error": {"code": error.code, "params": params} if error else None,
        "interaction": interaction,
        "offline_item_id": value.offline_item_id,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "completed_at": value.completed_at,
        "actions": sorted(a.value for a in available_actions(value)),
    }


def item(value: OfflineItem) -> dict[str, Any]:
    spec = value.spec
    return {
        "item_id": value.id,
        "revision": value.revision,
        "series_id": value.key.series_id,
        "number": value.key.number,
        "status": value.status.value,
        "file_name": PurePosixPath(spec.locator.key).name,
        "size_bytes": spec.size_bytes,
        "audio": spec.audio,
        "subs": spec.subs,
        "height": spec.height,
        "variant_label": spec.variant_label,
        "protocol": spec.protocol,
        "container": spec.container,
        "source_player": spec.source_player,
        "created_at": value.created_at,
        "verified_at": value.verified_at,
    }


def watch(value: WatchState | None, key: EpisodeKey) -> dict[str, Any]:
    """A missing row is reported as revision 0 with nothing known."""
    value = value or WatchState(key=key)
    return {
        "series_id": key.series_id,
        "number": key.number,
        "revision": value.revision,
        "last_opened_at": value.last_opened_at,
        "opened_via": value.opened_via.value if value.opened_via else None,
        "position_ms": value.position_ms,
        "duration_ms": value.duration_ms,
        "position_at": value.position_at,
        "watched": value.watched,
        "watched_source": value.watched_source.value if value.watched_source else None,
        "watched_at": value.watched_at,
    }


def resume(value: WatchState | None) -> dict[str, Any]:
    """The real progress, never reset for a watched episode; starting over is the client's own action."""
    return {
        "position_ms": value.position_ms if value else None,
        "duration_ms": value.duration_ms if value else None,
        "watched": value.watched if value else False,
        "watched_source": value.watched_source.value if value and value.watched_source else None,
    }


def playback_session(value: PlaybackSession) -> dict[str, Any]:
    return {"id": value.session_id, "series_id": value.key.series_id, "number": value.key.number}


def preferences(value: Preferences) -> dict[str, Any]:
    return {
        "audio": value.audio,
        "subs": value.subs,
        "quality": value.quality.encode(),
        "download_network": value.download_network.value,
        "auto_alternatives": value.auto_alternatives,
        "max_active_downloads": value.max_active_downloads,
        "revision": value.revision,
    }


def target(value: PlaybackTarget) -> dict[str, Any]:
    return {"kind": value.kind, "value": value.value}


def job_source(entry: PlayerEntry, wanted: DownloadCriteria, player: dict[str, Any]) -> dict[str, Any]:
    return {**player, "matches_criteria": match_source(entry, wanted), "identity": source_id(source_identity(entry))}
