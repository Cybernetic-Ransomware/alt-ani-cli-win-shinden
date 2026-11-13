"""Per-episode watch state: last opening, playback position fenced by session, and the watched flag."""

import sqlite3
import uuid

from alt_ani_core.errors import StateConflictError, UnknownRecordError
from alt_ani_core.history.codec import Row, key_of
from alt_ani_core.history.database import Database, upsert_episode
from alt_ani_core.models import EpisodeRow, SeriesRef
from alt_ani_core.state import EpisodeKey, OpenedVia, PlaybackSession, WatchedSource, WatchState

_COLUMNS = """series_id, number, last_opened_at, opened_via, position_ms, duration_ms, position_at, watched,
              watched_source, watched_at, playback_session_id, playback_seq, revision"""


def _state(row: Row) -> WatchState:
    return WatchState(
        key=key_of(row),
        last_opened_at=row["last_opened_at"],
        opened_via=OpenedVia(row["opened_via"]) if row["opened_via"] else None,
        position_ms=row["position_ms"],
        duration_ms=row["duration_ms"],
        position_at=row["position_at"],
        watched=bool(row["watched"]),
        watched_source=WatchedSource(row["watched_source"]) if row["watched_source"] else None,
        watched_at=row["watched_at"],
        playback_session_id=row["playback_session_id"],
        playback_seq=row["playback_seq"],
        revision=row["revision"],
    )


def _non_negative(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _seq(value: int) -> None:
    # a session starts at 0, so the first write of a session is 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("seq must be a positive integer")


def _duration(value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ValueError("duration_ms must be a positive integer")


class SqliteWatchStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _require_episode(conn: sqlite3.Connection, key: EpisodeKey) -> None:
        row = conn.execute("SELECT 1 FROM episode WHERE series_id = ? AND number = ?", (key.series_id, key.number))
        if row.fetchone() is None:
            raise UnknownRecordError(f"no episode {key.number:g} of series {key.series_id}").coded(
                "state.unknown_episode", series_id=key.series_id, number=key.number
            )

    @staticmethod
    def _get(conn: sqlite3.Connection, key: EpisodeKey) -> WatchState | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM watch_state WHERE series_id = ? AND number = ?",  # nosec B608
            (key.series_id, key.number),
        ).fetchone()
        return None if row is None else _state(row)

    @staticmethod
    def _existing(conn: sqlite3.Connection, key: EpisodeKey) -> WatchState:
        state = SqliteWatchStore._get(conn, key)
        if state is None:
            raise UnknownRecordError(f"no watch state for episode {key.number:g} of series {key.series_id}").coded(
                "state.unknown_episode", series_id=key.series_id, number=key.number
            )
        return state

    def record_opened(self, series: SeriesRef, episode: EpisodeRow, via: OpenedVia) -> WatchState:
        """Touches only the opening: a browser exit keeps the known position and the watched flag."""
        key = EpisodeKey(series.id, episode.number)
        with self._db.write() as conn:
            now = self._db.now()
            upsert_episode(conn, series, episode, now)
            conn.execute(
                """INSERT INTO watch_state (series_id, number, last_opened_at, opened_via) VALUES (?, ?, ?, ?)
                   ON CONFLICT(series_id, number) DO UPDATE SET
                     last_opened_at = excluded.last_opened_at, opened_via = excluded.opened_via,
                     revision = revision + 1""",
                (key.series_id, key.number, now, OpenedVia(via).value),
            )
            return self._existing(conn, key)

    @staticmethod
    def _new_session(conn: sqlite3.Connection, session: PlaybackSession) -> None:
        key = session.key
        conn.execute(
            """INSERT INTO watch_state (series_id, number, playback_session_id, playback_seq) VALUES (?, ?, ?, 0)
               ON CONFLICT(series_id, number) DO UPDATE SET
                 playback_session_id = excluded.playback_session_id, playback_seq = 0, revision = revision + 1""",
            (key.series_id, key.number, session.session_id),
        )

    def begin_playback(self, key: EpisodeKey) -> PlaybackSession:
        """Supersedes every earlier session of the episode; their later writes are refused."""
        session = PlaybackSession(key, uuid.uuid4().hex)
        with self._db.write() as conn:
            self._require_episode(conn, key)
            self._new_session(conn, session)
        return session

    def start_playback(self, series: SeriesRef, episode: EpisodeRow) -> tuple[PlaybackSession, WatchState]:
        """Opening in the player and a new session in one transaction, so no write can fall between them."""
        key = EpisodeKey(series.id, episode.number)
        session = PlaybackSession(key, uuid.uuid4().hex)
        with self._db.write() as conn:
            now = self._db.now()
            upsert_episode(conn, series, episode, now)
            conn.execute(
                """INSERT INTO watch_state (series_id, number, last_opened_at, opened_via) VALUES (?, ?, ?, ?)
                   ON CONFLICT(series_id, number) DO UPDATE SET
                     last_opened_at = excluded.last_opened_at, opened_via = excluded.opened_via""",
                (key.series_id, key.number, now, OpenedVia.PLAYER.value),
            )
            self._new_session(conn, session)
            return session, self._existing(conn, key)

    def record_position(self, session: PlaybackSession, seq: int, position_ms: int, duration_ms: int | None = None) -> bool:
        """False for a superseded session or a write older than one already kept; the value itself is never compared."""
        _seq(seq)
        _non_negative("position_ms", position_ms)
        _duration(duration_ms)
        with self._db.write() as conn:
            cur = conn.execute(
                """UPDATE watch_state SET position_ms = ?, duration_ms = COALESCE(?, duration_ms), position_at = ?,
                     playback_seq = ?, revision = revision + 1
                   WHERE series_id = ? AND number = ? AND playback_session_id = ? AND playback_seq < ?""",
                (position_ms, duration_ms, self._db.now(), seq, *self._where(session), seq),
            )
            return cur.rowcount == 1

    def record_playback_ended(self, session: PlaybackSession, seq: int, position_ms: int, duration_ms: int) -> bool:
        """The only automatic way to mark an episode watched: playback reached its end, no percentage threshold."""
        _seq(seq)
        _non_negative("position_ms", position_ms)
        _duration(duration_ms)
        with self._db.write() as conn:
            now = self._db.now()
            cur = conn.execute(
                """UPDATE watch_state SET position_ms = ?, duration_ms = ?, position_at = ?, playback_seq = ?,
                     watched = 1, watched_source = 'playback_ended', watched_at = ?, revision = revision + 1
                   WHERE series_id = ? AND number = ? AND playback_session_id = ? AND playback_seq < ?""",
                (position_ms, duration_ms, now, seq, now, *self._where(session), seq),
            )
            return cur.rowcount == 1

    @staticmethod
    def _where(session: PlaybackSession) -> tuple[str, float, str]:
        return session.key.series_id, session.key.number, session.session_id

    def set_watched(self, key: EpisodeKey, watched: bool, expected_revision: int | None = None) -> WatchState:
        """Manual change; leaves the position and the last opening as they are. No row yet is revision 0."""
        with self._db.write() as conn:
            self._require_episode(conn, key)
            if expected_revision is not None:
                current = self._get(conn, key)
                found = 0 if current is None else current.revision
                if found != expected_revision:
                    raise StateConflictError("the watch state changed since it was read").coded(
                        "state.conflict", series_id=key.series_id, number=key.number, expected=expected_revision, found=found
                    )
            now = self._db.now()
            conn.execute(
                """INSERT INTO watch_state (series_id, number, watched, watched_source, watched_at)
                   VALUES (?, ?, ?, 'manual', ?)
                   ON CONFLICT(series_id, number) DO UPDATE SET
                     watched = excluded.watched, watched_source = 'manual', watched_at = excluded.watched_at,
                     revision = revision + 1""",
                (key.series_id, key.number, int(watched), now),
            )
            return self._existing(conn, key)

    def get(self, key: EpisodeKey) -> WatchState | None:
        with self._db.read() as conn:
            return self._get(conn, key)

    def for_series(self, series_id: str) -> list[WatchState]:
        with self._db.read() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM watch_state WHERE series_id = ? ORDER BY number",  # nosec B608
                (series_id,),
            ).fetchall()
        return [_state(r) for r in rows]

    def last_opened(self, series_id: str) -> WatchState | None:
        """Where "Resume" starts and where the episode list is scrolled to."""
        with self._db.read() as conn:
            row = conn.execute(
                f"""SELECT {_COLUMNS} FROM watch_state WHERE series_id = ? AND last_opened_at IS NOT NULL
                    ORDER BY last_opened_at DESC, number DESC LIMIT 1""",  # nosec B608
                (series_id,),
            ).fetchone()
        return None if row is None else _state(row)

    def recent_series(self, limit: int = 50) -> list[tuple[SeriesRef, WatchState]]:
        """Series by their latest opening, each with that episode's state."""
        with self._db.read() as conn:
            rows = conn.execute(
                f"""SELECT s.slug, s.title, s.url, w.* FROM (
                      SELECT {_COLUMNS}, ROW_NUMBER() OVER (
                        PARTITION BY series_id ORDER BY last_opened_at DESC, number DESC) AS rn
                      FROM watch_state WHERE last_opened_at IS NOT NULL) w
                    JOIN series s ON s.id = w.series_id
                    WHERE w.rn = 1 ORDER BY w.last_opened_at DESC, w.series_id LIMIT ?""",  # nosec B608
                (limit,),
            ).fetchall()
        return [(SeriesRef(id=r["series_id"], slug=r["slug"], title=r["title"], url=r["url"]), _state(r)) for r in rows]
