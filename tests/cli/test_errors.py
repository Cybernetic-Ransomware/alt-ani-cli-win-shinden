"""Global error handlers and start-up checks in main()."""

import json
import sqlite3

import pytest

from alt_ani_cli import legacy_history, paths
from alt_ani_cli.content import error_text
from alt_ani_core.errors import (
    AccessBlockedError,
    DownloadFailedError,
    DownloadTargetError,
    FilterMismatchError,
    PlayerNotFoundError,
    ShindenError,
)
from alt_ani_core.history.local import open_local_state
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.history.sqlite import SCHEMA_VERSION, SqliteHistory
from alt_ani_core.messages import MESSAGES
from alt_ani_core.state import (
    DownloadCriteria,
    EpisodeKey,
    JobState,
    NetworkKind,
    OfflineItemSpec,
    OpenedVia,
    StorageLocator,
)
from alt_ani_core.testing import FakeResponse
from alt_ani_core.transport.api import HttpStatusError, NetworkError

from .support import REF, SERIES_URL, episode, player

_ARGV = ["--url", SERIES_URL]
_AGE_GATE_PL = "Shinden wymaga potwierdzenia wieku (18+) dla tej serii."


def _raises(exc: BaseException):
    def _run(*_args):
        raise exc

    return _run


def _http_error(status: int, url: str = SERIES_URL) -> HttpStatusError:
    return HttpStatusError(status, url=url)


@pytest.mark.unit
class TestHTTPStatusErrorHandler:
    def test_503_prints_status_and_exits_1(self, run_main, console):
        result = run_main(_ARGV, run=_raises(_http_error(503)))

        assert result.code == 1
        assert "503" in console.of("error")[-1]
        assert console.of("warn") == []

    def test_404_prints_url_and_exits_1(self, run_main, console):
        url = "https://shinden.pl/series/99-missing"
        result = run_main(["--url", url], run=_raises(_http_error(404, url)))

        assert result.code == 1
        assert console.of("error") == [f"Błąd HTTP 404 — {url}"]

    def test_403_shinden_no_flaresolverr_shows_cloudflare_hint(self, run_main, console):
        result = run_main(_ARGV, run=_raises(_http_error(403)))

        assert result.code == 1
        (hint,) = console.of("warn")
        assert "Cloudflare" in hint
        assert "ALT_ANI_CLI_FLARESOLVERR_URL" in hint

    def test_403_shinden_with_flaresolverr_shows_unreachable_hint(self, run_main, console):
        env = {paths.ENV_FLARESOLVERR_URL: "http://localhost:8191"}
        result = run_main(_ARGV, env=env, run=_raises(_http_error(403)))

        assert result.code == 1
        (hint,) = console.of("warn")
        assert "localhost:8191" in hint
        assert "niedostępny" in hint

    def test_403_elsewhere_gets_no_cloudflare_hint(self, run_main, console):
        result = run_main(_ARGV, run=_raises(_http_error(403, "https://cdn.example/video.mp4")))

        assert result.code == 1
        assert console.of("warn") == []

    def test_error_without_url_still_reports_the_status(self, run_main, console):
        result = run_main(_ARGV, run=_raises(HttpStatusError(500)))

        assert result.code == 1
        assert "500" in console.of("error")[-1]

    def test_real_403_on_the_episode_list_is_reported_end_to_end(self, run_main, console, transport):
        transport.route(f"{SERIES_URL}/episodes", FakeResponse("blocked", 403))

        result = run_main(_ARGV)

        assert result.code == 1
        assert console.of("error") == [f"Błąd HTTP 403 — {SERIES_URL}/episodes"]
        assert "Cloudflare" in console.of("warn")[-1]
        assert result.engine.closed
        assert all(session.closed for session in transport.sessions)


@pytest.mark.unit
class TestFilterMismatchErrorHandler:
    def test_filter_mismatch_exits_1_with_message(self, run_main, console):
        exc = FilterMismatchError("No players match the requested filters (--lang=xx).")

        result = run_main([*_ARGV, "-S", "1", "-e", "1"], run=_raises(exc))

        assert result.code == 1
        assert console.of("error") == ["No players match the requested filters (--lang=xx)."]


@pytest.mark.unit
class TestDownloadErrorHandlers:
    def test_download_failure_exits_1_with_plain_polish_message(self, run_main, console):
        exc = DownloadFailedError(MESSAGES["download"]["failed"]).coded("download.failed")

        result = run_main([*_ARGV, "-S", "1", "-d"], run=_raises(exc))

        assert result.code == 1
        assert console.of("error") == ["Pobieranie nie powiodło się."]
        assert "Błąd shinden" not in console.text

    def test_download_target_error_is_rendered_in_polish_with_its_path(self, run_main, console):
        exc = DownloadTargetError("could not replace").coded("download.replace_failed", path="Fate - 01.mp4")

        result = run_main([*_ARGV, "-d"], run=_raises(exc))

        assert result.code == 1
        (error,) = console.of("error")
        assert error.startswith("Nie udało się zastąpić pliku Fate - 01.mp4")


@pytest.mark.unit
class TestHTTPErrorUrlRedaction:
    def test_signed_query_value_not_shown(self, run_main, console):
        result = run_main(_ARGV, run=_raises(_http_error(403, "https://example.test/path?token=SECRET")))

        assert result.code == 1
        assert "SECRET" not in console.text
        assert "example.test/path" in console.text
        assert "token=<redacted>" in console.text

    def test_shinden_detection_still_uses_full_url(self, run_main, console):
        result = run_main(_ARGV, run=_raises(_http_error(403, "https://shinden.pl/episode/1/view?sig=SECRET")))

        assert result.code == 1
        assert "Cloudflare" in console.text
        assert "SECRET" not in console.text

    def test_flaresolverr_url_credentials_are_redacted(self, run_main, console):
        env = {paths.ENV_FLARESOLVERR_URL: "http://localhost:8191/?token=SOLVERSECRET"}

        result = run_main(_ARGV, env=env, run=_raises(_http_error(403)))

        assert result.code == 1
        assert "localhost:8191" in console.text
        assert "SOLVERSECRET" not in console.text


@pytest.mark.unit
class TestOtherErrorHandlers:
    def test_keyboard_interrupt_exits_130(self, run_main, console, diag_log):
        result = run_main(_ARGV, run=_raises(KeyboardInterrupt()))

        assert result.code == 130
        assert console.of("warn") == ["Przerwano przez użytkownika."]
        assert "outcome=interrupted" in diag_log()
        assert result.engine.closed

    def test_network_error_is_rendered_redacted(self, run_main, console, diag_log):
        exc = NetworkError("connection reset: https://shinden.pl/x?auth=SECRET")

        result = run_main(_ARGV, run=_raises(exc))

        assert result.code == 1
        (error,) = console.of("error")
        assert error.startswith("Błąd sieci: connection reset")
        assert "SECRET" not in error
        assert "exc=NetworkError" in diag_log()

    def test_player_not_found_shows_the_install_hint_verbatim(self, run_main, console):
        result = run_main(_ARGV, run=_raises(PlayerNotFoundError("mpv / mpv.net nie znaleziony.")))

        assert result.code == 1
        assert console.of("error") == ["mpv / mpv.net nie znaleziony."]

    def test_challenge_page_is_reported_without_the_shinden_prefix(self, run_main, console):
        exc = AccessBlockedError("shinden returned a Cloudflare challenge page", kind="cloudflare_challenge")

        result = run_main(_ARGV, run=_raises(exc))

        assert result.code == 1
        assert console.of("error") == ["shinden returned a Cloudflare challenge page"]

    def test_other_engine_error_gets_the_shinden_prefix(self, run_main, console, diag_log):
        result = run_main(_ARGV, run=_raises(ShindenError("something else")))

        assert result.code == 1
        assert console.of("error") == ["Błąd shinden: something else"]
        log = diag_log()
        assert "outcome=error" in log
        assert "exc=ShindenError" in log

    @pytest.mark.parametrize(
        "exc",
        [None, KeyboardInterrupt(), HttpStatusError(500), NetworkError(), ShindenError("x"), FilterMismatchError("x")],
        ids=lambda exc: type(exc).__name__,
    )
    def test_engine_is_always_closed(self, exc, run_main):
        result = run_main(_ARGV, run=_raises(exc) if exc is not None else (lambda *args: None))

        assert result.code == (None if exc is None else 130 if isinstance(exc, KeyboardInterrupt) else 1)
        assert result.engine.closed

    def test_unexpected_exception_still_closes_the_engine(self, run_main):
        engines = []

        def boom(_options, engine, _paths):
            engines.append(engine)
            raise RuntimeError("bug")

        with pytest.raises(RuntimeError, match="bug"):
            run_main(_ARGV, run=boom)

        assert engines[0].closed


@pytest.mark.unit
class TestAgeGate:
    def test_error_text_renders_the_age_gate_in_polish(self):
        exc = ShindenError(MESSAGES["series"]["age_gate"]).coded("series.age_gate")

        assert error_text(exc).startswith(_AGE_GATE_PL)
        assert "age verification" not in error_text(exc)

    def test_error_text_keeps_the_engine_text_for_other_codes(self):
        exc = ShindenError("no source of episode 3 produced a stream").coded("episode.sources_exhausted", number=3.0)

        assert error_text(exc) == "no source of episode 3 produced a stream"

    def test_error_text_redacts_uncoded_messages(self):
        assert "SECRET" not in error_text(ShindenError("failed for https://x.example/a?token=SECRET"))

    def test_age_gated_series_page_exits_1_with_the_polish_text(self, run_main, console, transport, capsys):
        transport.route(f"{SERIES_URL}/episodes", "<html><body><p>Potwierdź wiek, aby kontynuować.</p></body></html>")

        result = run_main(_ARGV)

        assert result.code == 1
        (error,) = console.of("error")
        assert error.startswith(f"Błąd shinden: {_AGE_GATE_PL}")
        assert "Traceback" not in capsys.readouterr().err
        assert result.engine.closed

    def test_age_gated_episode_page_exits_1_with_the_polish_text(self, run_main, console, site):
        site.series([episode(1)])
        site.transport.route(episode(1).url, "<html><body>Treści dla dorosłych — potwierdź wiek</body></html>")

        result = run_main(_ARGV)

        assert result.code == 1
        assert console.of("error")[-1].startswith(f"Błąd shinden: {_AGE_GATE_PL}")
        assert site.xhr_urls == []


@pytest.mark.unit
class TestEnvironmentChecks:
    def test_invalid_backend_value_exits_before_any_request(self, run_main, console, transport):
        result = run_main(_ARGV, env={paths.ENV_XHR_BACKEND: "legacy"})

        assert result.code == 1
        assert result.engines == []
        assert transport.sessions == []
        assert transport.requests == []
        (error,) = console.of("error")
        assert "ALT_ANI_CLI_XHR_BACKEND" in error
        assert "'legacy'" in error

    def test_invalid_antibot_delay_exits_before_any_request(self, run_main, console, transport):
        result = run_main(_ARGV, env={paths.ENV_ANTIBOT_DELAY: "soon"})

        assert result.code == 1
        assert result.engines == []
        assert transport.sessions == []
        assert "ALT_ANI_CLI_ANTIBOT_DELAY" in console.of("error")[-1]

    def test_valid_settings_reach_the_engine_config(self, run_main, cli_paths):
        env = {paths.ENV_XHR_BACKEND: "api4", paths.ENV_ANTIBOT_DELAY: "0"}

        result = run_main(_ARGV, env=env, run=lambda *args: None)

        config = result.engine.config
        assert (config.xhr_backend, config.antibot_delay_sec, config.cache_dir) == ("api4", 0.0, cli_paths.cache_dir)

    def test_no_session_is_opened_until_the_first_request(self, run_main, transport):
        result = run_main(_ARGV, run=lambda *args: None)

        assert result.code is None
        assert transport.sessions == []


def _write_legacy(cli_paths, payload: object) -> None:
    cli_paths.state_dir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    cli_paths.legacy_history.write_text(text, encoding="utf-8")


_LEGACY = {
    "series": {
        "1": {
            "slug": "fate",
            "title": "Fate",
            "url": SERIES_URL,
            "last_ep": 4.0,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "downloaded_eps": [1.0, 2.0],
        }
    }
}


@pytest.mark.unit
class TestHistoryStartup:
    def test_delete_history_exits_0_without_an_engine(self, run_main, console, transport, cli_paths):
        store = SqliteHistory(cli_paths.history_db)
        store.record_watch(REF, 3.0)
        store.record_download(REF, 1.0)

        result = run_main(["-D"])

        assert result.code == 0
        assert console.of("success") == ["Historia wyczyszczona."]
        assert result.engines == []
        assert transport.sessions == []
        assert store.watch_entries() == []
        assert store.download_entries() == []

    def test_delete_history_keeps_watch_state_queue_and_library(self, run_main, cli_paths):
        local = open_local_state(cli_paths.history_db)
        local.history.record_watch(REF, 3.0)
        local.history.record_download(REF, 1.0)
        local.watch.record_opened(REF, episode(2), OpenedVia.PLAYER)
        session = local.watch.begin_playback(EpisodeKey(REF.id, 2))
        local.watch.record_position(session, 1, 90_000)
        local.queue.enqueue(REF, [episode(4)], DownloadCriteria())
        local.queue.enqueue(REF, [episode(5)], DownloadCriteria())
        owner = ExecutorOwnership.try_acquire(cli_paths.history_db)
        claim = local.queue.claim_next(NetworkKind.WIFI, owner)
        owner.release()
        local.queue.begin_finalize(claim.attempt_id)
        local.queue.complete(claim.attempt_id, OfflineItemSpec(StorageLocator("fs", "/videos/ep4.mp4")))

        result = run_main(["-D"])

        assert result.code == 0
        assert local.history.watch_entries() == []
        assert local.history.download_entries() == []
        assert local.watch.get(EpisodeKey(REF.id, 2)).position_ms == 90_000
        assert [(j.key.number, j.state) for j in local.queue.jobs()] == [(4.0, JobState.COMPLETED), (5.0, JobState.QUEUED)]
        assert len(local.library.items()) == 1

    def test_delete_history_runs_after_the_legacy_import_and_clears_it(self, run_main, console, cli_paths):
        _write_legacy(cli_paths, _LEGACY)

        result = run_main(["-D"])

        assert result.code == 0
        store = SqliteHistory(cli_paths.history_db)
        assert cli_paths.history_db.is_file()
        assert store.watch_entries() == []
        assert store.download_entries() == []
        # the import is marked done, so the legacy file does not come back on the next start
        assert store.meta(legacy_history.META_STATUS) == "done"
        assert cli_paths.legacy_history.is_file()

        again = run_main(_ARGV, run=lambda *args: None)

        assert again.code is None
        assert store.watch_entries() == []
        assert store.download_entries() == []

    def test_legacy_history_is_imported_on_a_normal_start(self, run_main, cli_paths):
        _write_legacy(cli_paths, _LEGACY)
        seen = []

        run_main(_ARGV, run=lambda _options, engine, _paths: seen.append(engine.history.watch_entries()))

        ((entry,),) = seen
        assert (entry.series.id, entry.series.title, entry.last_ep) == ("1", "Fate", 4.0)
        (downloads,) = SqliteHistory(cli_paths.history_db).download_entries()
        assert downloads.episodes == frozenset({1.0, 2.0})

    def test_corrupt_legacy_history_warns_and_starts_empty(self, run_main, console, cli_paths):
        _write_legacy(cli_paths, "{not json")

        result = run_main(_ARGV, run=lambda *args: None)

        assert result.code is None
        (warning,) = console.of("warn")
        assert str(cli_paths.legacy_history) in warning
        assert SqliteHistory(cli_paths.history_db).watch_entries() == []
        assert cli_paths.legacy_history.read_text(encoding="utf-8") == "{not json"

    def test_skipped_legacy_entries_are_counted_in_a_warning(self, run_main, console, cli_paths):
        _write_legacy(cli_paths, {"series": {**_LEGACY["series"], "2": {"last_ep": "four"}}})

        run_main(_ARGV, run=lambda *args: None)

        (warning,) = console.of("warn")
        assert "pominięto 1" in warning

    @pytest.fixture
    def newer_schema_db(self, cli_paths) -> None:
        cli_paths.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(cli_paths.history_db)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()

    def _assert_schema_error(self, result, console, capsys) -> None:
        assert result.code == 1
        (error,) = console.of("error")
        assert error.startswith("Błąd shinden:")
        assert f"schema {SCHEMA_VERSION + 1} is newer" in error
        assert "Traceback" not in capsys.readouterr().err

    @pytest.mark.parametrize("argv", [["-D"], ["-c"], ["-c", "-d"]], ids=["delete-history", "continue", "continue-download"])
    def test_newer_schema_database_exits_1_with_an_error_not_a_traceback(
        self, argv, newer_schema_db, run_main, console, transport, capsys
    ):
        result = run_main(argv)

        self._assert_schema_error(result, console, capsys)
        assert transport.requests == []
        assert console.of("success") == []
        assert all(engine.closed for engine in result.engines)

    def test_newer_schema_database_stops_a_watch_run_before_any_request(
        self, newer_schema_db, site, actions, run_main, console, capsys, cli_paths, transport
    ):
        site.series([episode(1)])
        site.episode(episode(1), [player("11", "PlayerA")])

        result = run_main(_ARGV)

        self._assert_schema_error(result, console, capsys)
        assert transport.requests == []
        actions.play.assert_not_called()
        conn = sqlite3.connect(cli_paths.history_db)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        conn.close()

    def test_legacy_import_into_a_newer_schema_database_exits_1(self, newer_schema_db, run_main, console, capsys, cli_paths):
        _write_legacy(cli_paths, _LEGACY)

        result = run_main(_ARGV, run=lambda *args: None)

        self._assert_schema_error(result, console, capsys)
        assert result.engines == []
