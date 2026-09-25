"""Replay resolve_result failures recorded in a diagnostics session log against live hosts.

Reads a ``CACHE_DIR/diagnostics/session_*.log`` file, rebuilds the (series, episode, player)
context for every logged ``resolve_result ok=False``, and re-runs the same
``shinden_api.resolve_embed`` -> ``extract.resolve`` sequence the interactive UI would have run,
without any UI, player launch, history update, or download. Never prints a full embed/stream
URL, header, cookie, or token -- only hostnames (via ``diagnostics._host_of_url``) and exception
class names, matching the redaction discipline already enforced by ``diagnostics.py``.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from curl_cffi.requests import exceptions as cffi_exceptions

from alt_ani_cli import extract
from alt_ani_cli.diagnostics import _host_of_url
from alt_ani_cli.errors import AntiBotError, NoStreamError, ShindenError
from alt_ani_cli.shinden import api as shinden_api
from alt_ani_cli.shinden import http as shinden_http
from alt_ani_cli.ui import progress

_RESOLVE_DIAG_FIELDS = (
    "layer",
    "category",
    "http_status",
    "used_fallback",
    "fallback_layer",
    "fallback_category",
    "fallback_http_status",
)

_UNKNOWN_BY_FIELD: dict[str, frozenset] = {
    "layer": frozenset({None}),
    "category": frozenset({None, "unknown"}),
    "http_status": frozenset({None}),
    "used_fallback": frozenset({None}),
    "fallback_layer": frozenset({None}),
    "fallback_category": frozenset({None, "unknown"}),
    "fallback_http_status": frozenset({None}),
}

_BOOL_FIELDS = frozenset({"ok", "used_fallback"})
_INT_FIELDS = frozenset({"http_status", "fallback_http_status"})
_FLOAT_FIELDS = frozenset({"number"})


class Verdict(StrEnum):
    FIXED = "FIXED"
    SAME_FAILURE = "SAME_FAILURE"
    DIAGNOSTICS_IMPROVED = "DIAGNOSTICS_IMPROVED"
    CHANGED_FAILURE = "CHANGED_FAILURE"
    SOURCE_ROTATED = "SOURCE_ROTATED"
    REPLAY_ERROR = "REPLAY_ERROR"


class LogParseError(Exception):
    """The log file contained no recognizable diagnostics events."""


@dataclass(frozen=True)
class ReplayCase:
    series_id: str | None
    series_title: str | None
    episode_number: float | None
    episode_title: str | None
    online_id: str | None
    player_name: str | None
    host_before: str | None
    exc_before: str | None
    layer_before: str | None
    category_before: str | None
    http_status_before: int | None
    used_fallback_before: bool | None
    fallback_layer_before: str | None
    fallback_category_before: str | None
    fallback_http_status_before: int | None


@dataclass(frozen=True)
class ReplayOutcome:
    case: ReplayCase
    verdict: Verdict
    now_ok: bool
    now_exc: str | None
    now_host: str | None
    now_layer: str | None = None
    now_category: str | None = None
    now_http_status: int | None = None
    now_used_fallback: bool | None = None
    now_fallback_layer: str | None = None
    now_fallback_category: str | None = None
    now_fallback_http_status: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class _NowSnapshot:
    ok: bool
    host: str | None
    harness_error: bool
    layer: str | None = None
    category: str | None = None
    http_status: int | None = None
    used_fallback: bool | None = None
    fallback_layer: str | None = None
    fallback_category: str | None = None
    fallback_http_status: int | None = None


def _coerce(key: str, value: str) -> object:
    if key in _BOOL_FIELDS:
        if value not in ("True", "False"):
            raise ValueError(f"invalid bool for {key}: {value!r}")
        return value == "True"
    if key in _INT_FIELDS:
        return int(value)
    if key in _FLOAT_FIELDS:
        return float(value)
    return value


def _parse_line(line: str) -> tuple[str, dict[str, object]] | None:
    stripped = line.rstrip("\n")
    if not stripped.strip():
        return None
    parts = stripped.split(" ", 2)
    if len(parts) != 3:
        return None
    _date, _time, rest = parts
    tokens = shlex.split(rest)
    if not tokens:
        return None

    fields: dict[str, object] = {}
    event: str | None = None
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep:
            return None
        if key == "event":
            event = value
        else:
            fields[key] = _coerce(key, value)
    if event is None:
        return None
    return event, fields


def parse_diagnostics_log(path: Path) -> tuple[list[ReplayCase], int]:
    """Sequentially scan a diagnostics log, returning (failed-resolve cases, skipped-line count)."""
    cases: list[ReplayCase] = []
    skipped = 0
    recognized = 0

    current_series: tuple[str | None, str | None] | None = None
    current_episode: tuple[float | None, str | None] | None = None
    current_player: tuple[str | None, str | None] | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        try:
            parsed = _parse_line(raw_line)
        except ValueError:
            parsed = None

        if parsed is None:
            if raw_line.strip():
                skipped += 1
            continue

        event, fields = parsed
        recognized += 1

        if event == "series_selected":
            current_series = (fields.get("id"), fields.get("title"))
            current_episode = None
            current_player = None
        elif event == "episode_selected":
            current_episode = (fields.get("number"), fields.get("title"))
            current_player = None
        elif event == "player_selected":
            current_player = (fields.get("online_id"), fields.get("player"))
        elif event == "resolve_result" and fields.get("ok") is False:
            series_id, series_title = current_series or (None, None)
            ep_number, ep_title = current_episode or (None, None)
            online_id, player_name = current_player or (None, None)
            cases.append(
                ReplayCase(
                    series_id=series_id,
                    series_title=series_title,
                    episode_number=ep_number,
                    episode_title=ep_title,
                    online_id=online_id,
                    player_name=player_name,
                    host_before=fields.get("host"),
                    exc_before=fields.get("exc"),
                    layer_before=fields.get("layer"),
                    category_before=fields.get("category"),
                    http_status_before=fields.get("http_status"),
                    used_fallback_before=fields.get("used_fallback"),
                    fallback_layer_before=fields.get("fallback_layer"),
                    fallback_category_before=fields.get("fallback_category"),
                    fallback_http_status_before=fields.get("fallback_http_status"),
                )
            )

    if recognized == 0:
        raise LogParseError(f"no recognized diagnostics events in {path}")

    return cases, skipped


def classify_outcome(case: ReplayCase, now: _NowSnapshot) -> Verdict:
    if now.harness_error:
        return Verdict.REPLAY_ERROR

    if case.host_before is not None and now.host is not None and case.host_before != now.host:
        # Priority over FIXED: a different host answering does not prove the resolver was fixed.
        return Verdict.SOURCE_ROTATED

    if now.ok:
        return Verdict.FIXED

    before_tuple = (
        case.layer_before,
        case.category_before,
        case.http_status_before,
        case.used_fallback_before,
        case.fallback_layer_before,
        case.fallback_category_before,
        case.fallback_http_status_before,
    )
    now_tuple = (
        now.layer,
        now.category,
        now.http_status,
        now.used_fallback,
        now.fallback_layer,
        now.fallback_category,
        now.fallback_http_status,
    )

    improved = False
    for field_name, before_val, now_val in zip(_RESOLVE_DIAG_FIELDS, before_tuple, now_tuple, strict=True):
        if before_val == now_val:
            continue
        unknown = _UNKNOWN_BY_FIELD[field_name]
        if before_val in unknown and now_val not in unknown:
            improved = True
            continue
        return Verdict.CHANGED_FAILURE

    return Verdict.DIAGNOSTICS_IMPROVED if improved else Verdict.SAME_FAILURE


def _finalize(case: ReplayCase, now: _NowSnapshot, exc: Exception | None) -> ReplayOutcome:
    return ReplayOutcome(
        case=case,
        verdict=classify_outcome(case, now),
        now_ok=now.ok,
        now_exc=type(exc).__name__ if exc is not None else None,
        now_host=now.host,
        now_layer=now.layer,
        now_category=now.category,
        now_http_status=now.http_status,
        now_used_fallback=now.used_fallback,
        now_fallback_layer=now.fallback_layer,
        now_fallback_category=now.fallback_category,
        now_fallback_http_status=now.fallback_http_status,
    )


def replay_case(client, case: ReplayCase) -> ReplayOutcome:
    """Re-run resolve_embed -> extract.resolve for one case. Never launches a player or touches history."""
    if case.online_id is None:
        now = _NowSnapshot(ok=False, host=None, harness_error=True)
        return ReplayOutcome(
            case=case,
            verdict=Verdict.REPLAY_ERROR,
            now_ok=False,
            now_exc=None,
            now_host=None,
            note="missing player context in log",
        )

    try:
        embed = shinden_api.resolve_embed(client, case.online_id)
    except AntiBotError as exc:
        now = _NowSnapshot(ok=False, host=None, harness_error=False, layer="shinden_api", category="anti_bot")
        return _finalize(case, now, exc)
    except (ShindenError, cffi_exceptions.RequestException) as exc:
        now = _NowSnapshot(
            ok=False,
            host=None,
            harness_error=False,
            layer="shinden_api",
            category=extract._classify(exc),
            http_status=extract._http_status(exc),
        )
        return _finalize(case, now, exc)
    except Exception as exc:
        now = _NowSnapshot(ok=False, host=None, harness_error=True)
        return ReplayOutcome(
            case=case,
            verdict=Verdict.REPLAY_ERROR,
            now_ok=False,
            now_exc=type(exc).__name__,
            now_host=None,
            note="unexpected error resolving embed",
        )

    host = _host_of_url(embed.url)
    try:
        extract.resolve(embed.url, embed.referer)
    except NoStreamError as exc:
        fields = {name: getattr(exc, name, None) for name in _RESOLVE_DIAG_FIELDS}
        now = _NowSnapshot(ok=False, host=host, harness_error=False, **fields)
        return _finalize(case, now, exc)
    except Exception as exc:
        return ReplayOutcome(
            case=case,
            verdict=Verdict.REPLAY_ERROR,
            now_ok=False,
            now_exc=type(exc).__name__,
            now_host=host,
            note="unexpected error extracting stream",
        )

    now = _NowSnapshot(ok=True, host=host, harness_error=False)
    return _finalize(case, now, None)


_VERDICT_STYLE = {
    Verdict.FIXED: "bold green",
    Verdict.SAME_FAILURE: "bold red",
    Verdict.CHANGED_FAILURE: "bold yellow",
    Verdict.DIAGNOSTICS_IMPROVED: "bold yellow",
    Verdict.SOURCE_ROTATED: "bold blue",
    Verdict.REPLAY_ERROR: "bold white on grey23",
}


def _fmt_signature(
    prefix: str,
    host: str | None,
    exc: str | None,
    layer,
    category,
    http_status,
    used_fallback,
    fallback_layer,
    fallback_category,
    fallback_http_status,
) -> str:
    bits = [f"host={host}", f"exc={exc}", f"layer={layer}", f"category={category}", f"http_status={http_status}"]
    if used_fallback:
        bits += [
            f"fallback_layer={fallback_layer}",
            f"fallback_category={fallback_category}",
            f"fallback_http_status={fallback_http_status}",
        ]
    return f"{prefix}: " + " ".join(bits)


def render_case(outcome: ReplayOutcome) -> None:
    case = outcome.case
    style = _VERDICT_STYLE[outcome.verdict]
    ep = case.episode_number if case.episode_number is not None else "?"
    header = (
        f"{case.series_title or '?'} — ep {ep} {case.episode_title or ''} — "
        f"player={case.player_name or '?'} online_id={case.online_id or '?'}"
    )
    progress.output(header)
    progress.output(
        _fmt_signature(
            "  BEFORE",
            case.host_before,
            case.exc_before,
            case.layer_before,
            case.category_before,
            case.http_status_before,
            case.used_fallback_before,
            case.fallback_layer_before,
            case.fallback_category_before,
            case.fallback_http_status_before,
        )
    )
    progress.output(
        _fmt_signature(
            "  NOW",
            outcome.now_host,
            outcome.now_exc,
            outcome.now_layer,
            outcome.now_category,
            outcome.now_http_status,
            outcome.now_used_fallback,
            outcome.now_fallback_layer,
            outcome.now_fallback_category,
            outcome.now_fallback_http_status,
        )
    )
    verdict_line = f"  VERDICT: [{style}]{outcome.verdict}[/{style}]"
    if outcome.verdict is Verdict.SOURCE_ROTATED:
        verdict_line += f" (now {'success' if outcome.now_ok else 'failure'}, host {case.host_before} -> {outcome.now_host})"
    if outcome.note:
        verdict_line += f" ({outcome.note})"
    progress.output(verdict_line)
    progress.output("")


def render_summary(outcomes: list[ReplayOutcome]) -> None:
    counts: dict[Verdict, int] = {}
    for outcome in outcomes:
        counts[outcome.verdict] = counts.get(outcome.verdict, 0) + 1
    progress.rule("Summary")
    for verdict in Verdict:
        if counts.get(verdict):
            style = _VERDICT_STYLE[verdict]
            progress.output(f"[{style}]{verdict}[/{style}]: {counts[verdict]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_path", type=Path, help="Path to a diagnostics session_*.log file")
    args = parser.parse_args(argv)

    try:
        cases, skipped = parse_diagnostics_log(args.log_path)
    except OSError as exc:
        progress.error(f"Cannot read log file: {type(exc).__name__}")
        return 1
    except LogParseError as exc:
        progress.error(str(exc))
        return 1

    if skipped:
        progress.warn(f"Skipped {skipped} unparsable line(s) in the log.")

    if not cases:
        progress.info("No failed resolve_result entries found in the log.")
        return 0

    try:
        client = shinden_http.make_client()
    except Exception as exc:
        progress.error(f"Could not create an HTTP client: {type(exc).__name__}")
        return 1

    outcomes: list[ReplayOutcome] = []
    try:
        for case in cases:
            outcome = replay_case(client, case)
            outcomes.append(outcome)
            render_case(outcome)
    finally:
        client.close()

    render_summary(outcomes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
