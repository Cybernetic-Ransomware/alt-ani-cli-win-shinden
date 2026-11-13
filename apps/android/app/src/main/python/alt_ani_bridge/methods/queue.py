"""Download queue: preview bound to commit by ``basis_seq``, job actions guarded by the job's revision."""

from typing import Any

from alt_ani_bridge import dto, dto_state
from alt_ani_bridge.context import Call
from alt_ani_bridge.errors import BridgeError
from alt_ani_bridge.methods import common, method
from alt_ani_bridge.schema import InputError, Reader
from alt_ani_core.errors import UnknownRecordError
from alt_ani_core.rules.criteria import source_identity
from alt_ani_core.rules.episodes import MODES, select_episodes
from alt_ani_core.state import DownloadJob, JobState


def _titles(c: Call, job: DownloadJob) -> tuple[str, str] | None:
    try:
        series, episode = c.runtime.state.queue.episode_of(job.key)
    except UnknownRecordError:
        return None
    return series.title, episode.title


def job_view(c: Call, job: DownloadJob) -> dict[str, Any]:
    return dto_state.job(job, _titles(c, job))


def _executor_status(c: Call) -> dict[str, Any]:
    queue = c.runtime.queue
    status = queue.status() if queue is not None else None
    return {"state": status.state.value if status else "stopped", "stuck_jobs": list(status.stuck_jobs) if status else []}


def queue_preview(c: Call) -> Any:
    runtime = c.runtime
    series = common.series(c.r)
    listing = common.episodes(c.r)
    sel = c.r.obj("selection", ("mode", "numbers", "range"))
    mode = sel.choice("mode", MODES)
    watched = [s.key.number for s in runtime.state.watch.for_series(series.id) if s.watched] if mode == "unwatched" else []
    numbers = sel.numbers("numbers", default=[]) if mode == "episodes" else []
    if mode == "episodes" and not sel.has("numbers"):
        raise InputError(sel.field("numbers"), "missing")
    unknown = sorted(set(numbers) - {ep.number for ep in listing})
    if unknown:
        raise InputError(sel.field("numbers"), "not_in_listing", numbers=unknown)
    chosen = select_episodes(listing, mode, numbers=numbers, range_text=sel.text("range", None), watched=watched)
    if chosen is None:
        raise InputError(sel.field("range"), "unknown_format")
    prefs = runtime.state.preferences.get()
    criteria = common.criteria_overrides(c.r, prefs)
    alternatives = common.alternatives(c.r, prefs)
    plan = runtime.state.queue.preview(series.id, [ep.number for ep in chosen])
    adding = {key.number for key in plan.to_add}
    return {
        "plan": {
            "series": dto.series_ref(series),
            "episodes": [dto.episode(ep) for ep in chosen if ep.number in adding],
            "criteria": dto_state.criteria(criteria),
            "alternatives": alternatives.value,
            "selection": mode,
            "cellular_allowed": c.r.flag("cellular_allowed", False),
            "basis_seq": plan.basis_seq,
        },
        "skipped": [{"number": key.number, "reason": reason.value} for key, reason in plan.skipped],
    }


_PLAN_FIELDS = ("series", "episodes", "criteria", "alternatives", "selection", "cellular_allowed", "basis_seq")


def queue_enqueue(c: Call) -> Any:
    """Commits a preview's plan as given; the preferences are not read again, so nothing is silently recomputed."""
    plan: Reader = c.r.obj("plan", _PLAN_FIELDS)
    plan.choice("selection", MODES, "episodes")
    result = c.runtime.state.queue.enqueue(
        common.series(plan),
        common.episodes(plan),
        common.full_criteria(plan),
        alternatives=common.alternatives(plan, None),
        cellular_allowed=plan.flag("cellular_allowed", False),
        basis_seq=plan.integer("basis_seq", minimum=0),
    )
    c.runtime.changed()
    return {
        "added": [job_view(c, job) for job in result.added],
        "skipped": [{"number": key.number, "reason": reason.value} for key, reason in result.skipped],
    }


def queue_snapshot(c: Call) -> Any:
    queue = c.runtime.state.queue
    states = None
    if c.r.has("states"):
        wrapper = Reader({str(i): v for i, v in enumerate(c.r.array("states"))}, "states")
        states = [wrapper.enum(str(i), JobState) for i in range(len(wrapper.data))]
    revision = queue.queue_revision()
    return {
        "queue_revision": revision,
        "jobs": [job_view(c, job) for job in queue.jobs(states)],
        "executor": _executor_status(c),
    }


def queue_job(c: Call) -> Any:
    return {"job": job_view(c, c.runtime.state.queue.get(c.r.text("job_id")))}


def _act(c: Call, name: str) -> Any:
    queue = c.runtime.state.queue
    job_id, expected = c.r.text("job_id"), c.r.integer("expected_revision", minimum=0)
    job = getattr(queue, name)(job_id, expected)
    c.runtime.changed()
    return {"job": job_view(c, job)}


def queue_allow_cellular(c: Call) -> Any:
    job = c.runtime.state.queue.allow_cellular(
        c.r.text("job_id"), c.r.flag("allowed"), c.r.integer("expected_revision", minimum=0)
    )
    c.runtime.changed()
    return {"job": job_view(c, job)}


def queue_job_sources(c: Call) -> Any:
    job = c.runtime.state.queue.get(c.r.text("job_id"))
    _series, episode = c.runtime.state.queue.episode_of(job.key)
    found = c.open_session(episode)
    partial = job.partial
    return {
        "session_id": found.session_id,
        "sources": [dto_state.job_source(e, job.criteria, dto.player(e)) for e in found.session.sources],
        "partial": {"source": dto_state.source_id(partial.source), "variant": dto_state.variant_id(partial.variant)}
        if partial
        else None,
    }


def queue_choose_source(c: Call) -> Any:
    job_id = c.r.text("job_id")
    found = c.session(c.r.text("session_id"))
    online_id = c.r.text("online_id")
    entry = next((e for e in found.session.sources if e.online_id == online_id), None)
    if entry is None:
        raise BridgeError("unknown_source", "no such source in this episode session", online_id=online_id)
    job = c.runtime.state.queue.choose_source(
        job_id,
        source_identity(entry),
        common.variant(c.r),
        restart=c.r.flag("restart", False),
        expected_revision=c.r.integer("expected_revision", minimum=0),
    )
    c.runtime.changed()
    return {"job": job_view(c, job)}


def queue_retry_interaction(c: Call) -> Any:
    """No grant crosses the bridge: the job gets another attempt with its choice kept and may wait again."""
    job = c.runtime.state.queue.retry_interaction(c.r.text("job_id"), c.r.integer("expected_revision", minimum=0))
    c.runtime.changed()
    return {"job": job_view(c, job)}


def queue_reorder(c: Call) -> Any:
    revision = c.runtime.state.queue.reorder(c.r.strs("job_ids", "job_ids"), c.r.integer("expected_revision", minimum=0))
    c.runtime.changed()
    return {"queue_revision": revision}


_ACTION_FIELDS = ("job_id", "expected_revision")

METHODS = {
    "queue_preview": method(queue_preview, "series", "episodes", "selection", "criteria", "alternatives", "cellular_allowed"),
    "queue_enqueue": method(queue_enqueue, "plan"),
    "queue_snapshot": method(queue_snapshot, "states"),
    "queue_job": method(queue_job, "job_id"),
    "queue_pause": method(lambda c: _act(c, "pause"), *_ACTION_FIELDS),
    "queue_resume": method(lambda c: _act(c, "resume"), *_ACTION_FIELDS),
    "queue_cancel": method(lambda c: _act(c, "cancel"), *_ACTION_FIELDS),
    "queue_retry": method(lambda c: _act(c, "retry"), *_ACTION_FIELDS),
    "queue_allow_cellular": method(queue_allow_cellular, "job_id", "allowed", "expected_revision"),
    "queue_job_sources": method(queue_job_sources, "job_id"),
    "queue_choose_source": method(
        queue_choose_source, "job_id", "session_id", "online_id", "variant", "restart", "expected_revision"
    ),
    "queue_retry_interaction": method(queue_retry_interaction, *_ACTION_FIELDS),
    "queue_reorder": method(queue_reorder, "job_ids", "expected_revision"),
}
