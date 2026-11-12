"""Whether a partial may be continued: only with request-bound evidence and a durable checkpoint, else a decision."""

from dataclasses import dataclass
from enum import StrEnum

from alt_ani_core.rules.criteria import resume_candidate
from alt_ani_core.state import (
    AlternativesPolicy,
    DownloadJob,
    PartialTransfer,
    ResourceValidator,
    SelectionMode,
    SourceIdentity,
    VariantIdentity,
)

KIND_HTTP = "http"
KIND_HLS = "hls"
KIND_COMPLETE = "complete"
# fields that record progress, not the identity of the resource; identity fields must match exactly to resume
CHECKPOINT_FIELDS = frozenset({"length", "fragments"})


def identity_fields(validator: ResourceValidator) -> dict[str, str | int]:
    return {k: v for k, v in validator.fields if k not in CHECKPOINT_FIELDS}


def checkpoint_of(validator: ResourceValidator | None) -> tuple[int, int]:
    """(durable length, appended fragments); zeros when nothing durable was recorded."""
    if validator is None:
        return 0, 0
    fields = dict(validator.fields)
    length, fragments = fields.get("length", 0), fields.get("fragments", 0)
    return (length if isinstance(length, int) else 0), (fragments if isinstance(fragments, int) else 0)


def with_checkpoint(validator: ResourceValidator, length: int, fragments: int) -> ResourceValidator:
    fields = {**identity_fields(validator), "length": length}
    if validator.kind == KIND_HLS:
        fields["fragments"] = fragments
    return ResourceValidator(validator.kind, tuple(fields.items()))


def complete_evidence(size: int, sha256: str) -> ResourceValidator:
    return ResourceValidator(KIND_COMPLETE, (("sha256", sha256), ("size", size)))


def evidence_of(validator: ResourceValidator | None) -> tuple[int, str] | None:
    if validator is None or validator.kind != KIND_COMPLETE:
        return None
    fields = dict(validator.fields)
    size, digest = fields.get("size"), fields.get("sha256")
    return (size, digest) if isinstance(size, int) and isinstance(digest, str) else None


def has_resume_evidence(validator: ResourceValidator | None) -> bool:
    """A strong ETag (HTTP) or a full segment-ETag manifest (HLS), and a non-empty durable checkpoint."""
    if validator is None:
        return False
    fields = identity_fields(validator)
    length, fragments = checkpoint_of(validator)
    if validator.kind == KIND_HTTP:
        return isinstance(fields.get("etag"), str) and isinstance(fields.get("total"), int) and length > 0
    if validator.kind == KIND_HLS:
        manifest = fields.get("manifest")
        return isinstance(manifest, str) and bool(manifest) and length > 0 and fragments > 0
    return False


class ResumeStep(StrEnum):
    FRESH = "fresh"
    RESUME = "resume"
    FINALIZE = "finalize"
    IMPOSSIBLE = "impossible"


@dataclass(frozen=True)
class ResumePlan:
    step: ResumeStep
    reason: str | None = None


def plan_resume(partial: PartialTransfer | None, source: SourceIdentity, variant: VariantIdentity) -> ResumePlan:
    if evidence_of(partial.validator if partial else None) is not None:
        return ResumePlan(ResumeStep.FINALIZE)
    if partial is None or checkpoint_of(partial.validator)[0] == 0:
        # no durable byte was recorded: starting over loses nothing a resume could have kept
        return ResumePlan(ResumeStep.FRESH)
    if not has_resume_evidence(partial.validator):
        return ResumePlan(ResumeStep.IMPOSSIBLE, "no_validator")
    if not resume_candidate(partial, source, variant):
        return ResumePlan(ResumeStep.IMPOSSIBLE, "different_source")
    return ResumePlan(ResumeStep.RESUME)


def may_discard_partial(job: DownloadJob) -> bool:
    """Only the compatible-alternatives policy under automatic selection authorises starting over unasked."""
    return job.alternatives is AlternativesPolicy.COMPATIBLE and job.selection_mode is SelectionMode.AUTO
