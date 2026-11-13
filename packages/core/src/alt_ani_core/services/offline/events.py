from dataclasses import dataclass

from alt_ani_core.events import EngineEvent


@dataclass(frozen=True)
class JobProgress(EngineEvent):
    job_id: str
    bytes_done: int
    bytes_total: int | None


@dataclass(frozen=True)
class InteractionRequired(EngineEvent):
    """The job waits until the platform completes an interaction and calls ``complete_interaction``."""

    job_id: str
    scope: str
    host: str | None


@dataclass(frozen=True)
class OrphanFound(EngineEvent):
    """A file under the managed root that no record explains; reported only, never removed."""

    backend: str
    key: str


@dataclass(frozen=True)
class AttemptStuck(EngineEvent):
    """An attempt's I/O did not end in time; its slot stays taken and ownership is kept until the process exits."""

    job_id: str


@dataclass(frozen=True)
class StoreUnavailable(EngineEvent):
    """A store write the executor needed failed; it is retried, and recovery settles it at the next start."""

    operation: str
