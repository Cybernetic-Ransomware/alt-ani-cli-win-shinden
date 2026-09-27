"""Tests for noninteractive diagnostics wiring — session lifecycle, episode/player context."""

import argparse
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli import diagnostics
from alt_ani_cli.cli import _run_noninteractive, main
from alt_ani_cli.errors import AntiBotError, NoStreamError
from alt_ani_cli.models import EmbedURL, EpisodeRow, PlayerEntry, SeriesRef

_REF = SeriesRef(id="1", slug="fate", title="Fate", url="http://shinden.pl/series/1-fate")
_EP1 = EpisodeRow(number=1.0, title="Ep 1", url="http://shinden.pl/ep/1")
_PLAYER_A = PlayerEntry(online_id="a1", player="PlayerA", lang_audio="jp", lang_subs="pl")
_PLAYER_B = PlayerEntry(online_id="b2", player="PlayerB", lang_audio="jp", lang_subs="pl")


def _make_args(**overrides):
    defaults = dict(
        query=[],
        url="http://shinden.pl/series/1-fate",
        resume=False,
        download=False,
        delete_history=False,
        episode=None,
        quality=None,
        select_nth=None,
        vlc=False,
        no_detach=False,
        debug=False,
        player_name=None,
        lang=None,
        subs=None,
        allow_fallback=False,
        show_sources=False,
        cookies_file=None,
        cookies_browser=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _events(content: str) -> list[str]:
    return [next(tok for tok in line.split() if tok.startswith("event=")) for line in content.splitlines()]


@pytest.mark.unit
class TestNoninteractiveDiagnosticsContext:
    """Runs _run_noninteractive against a real, tmp_path-isolated diagnostics log."""

    def test_episode_and_player_context_precede_each_resolve_result(self):
        diagnostics.configure()
        args = _make_args()
        client = MagicMock()
        client.get.return_value = MagicMock(raise_for_status=MagicMock(), text="")

        with (
            patch("alt_ani_cli.cli.shinden_series.parse_series_url", return_value=_REF),
            patch("alt_ani_cli.cli.shinden_series.list_episodes", return_value=(_REF, [_EP1])),
            patch("alt_ani_cli.cli.shinden_episode.parse_players", return_value=[_PLAYER_A, _PLAYER_B]),
            patch("alt_ani_cli.cli.shinden_episode.sort_players", return_value=[_PLAYER_A, _PLAYER_B]),
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", side_effect=AntiBotError("blocked")),
        ):
            _run_noninteractive(args, client)

        (log_file,) = diagnostics.DIAG_DIR.glob("session_*.log")
        content = log_file.read_text(encoding="utf-8")

        assert _events(content) == [
            "event=series_selected",
            "event=episode_selected",
            "event=player_selected",
            "event=resolve_result",
            "event=player_selected",
            "event=resolve_result",
        ]
        assert f"online_id={_PLAYER_A.online_id}" in content.splitlines()[2]
        assert f"online_id={_PLAYER_B.online_id}" in content.splitlines()[4]

    def test_episode_selected_uses_final_episode_number_and_title(self):
        diagnostics.configure()
        args = _make_args()
        client = MagicMock()
        client.get.return_value = MagicMock(raise_for_status=MagicMock(), text="")

        with (
            patch("alt_ani_cli.cli.shinden_series.parse_series_url", return_value=_REF),
            patch("alt_ani_cli.cli.shinden_series.list_episodes", return_value=(_REF, [_EP1])),
            patch("alt_ani_cli.cli.shinden_episode.parse_players", return_value=[]),
        ):
            _run_noninteractive(args, client)

        (log_file,) = diagnostics.DIAG_DIR.glob("session_*.log")
        content = log_file.read_text(encoding="utf-8")
        assert f"number={_EP1.number}" in content
        assert _EP1.title in content


@pytest.mark.unit
class TestMainConfiguresDiagnosticsInBothModes:
    def test_noninteractive_run_records_mode_and_session_end(self):
        with (
            patch("sys.argv", ["alt-ani-cli", "--url", "http://shinden.pl/series/1-fate"]),
            patch("alt_ani_cli.cli.shinden_http.make_client"),
            patch("alt_ani_cli.cli._run_noninteractive"),
        ):
            main()

        (log_file,) = diagnostics.DIAG_DIR.glob("session_*.log")
        content = log_file.read_text(encoding="utf-8")
        assert "event=session_start" in content
        assert "mode=noninteractive" in content
        assert "event=session_end" in content
        assert "outcome=ok" in content


@pytest.mark.unit
class TestNoninteractiveHealthDeferAcrossEpisodes:
    """Health persists across the episode loop and defers a known-unavailable host — order never changes."""

    def test_host_marked_unavailable_in_episode_one_is_deferred_in_episode_two(self):
        embeds = {
            "p1": EmbedURL(url="https://hostx.example/e/p1", referer="https://shinden.pl/"),
            "p2": EmbedURL(url="https://hostx.example/e/p2", referer="https://shinden.pl/"),
            "p3": EmbedURL(url="https://hosty.example/e/p3", referer="https://shinden.pl/"),
            "p4": EmbedURL(url="https://hostx.example/e/p4", referer="https://shinden.pl/"),
            "p5": EmbedURL(url="https://hosty.example/e/p5", referer="https://shinden.pl/"),
        }
        players_ep1 = [
            PlayerEntry(online_id="p1", player="P1", lang_audio="pl", lang_subs="pl"),
            PlayerEntry(online_id="p2", player="P2", lang_audio="pl", lang_subs="pl"),
            PlayerEntry(online_id="p3", player="P3", lang_audio="pl", lang_subs="pl"),
        ]
        players_ep2 = [
            PlayerEntry(online_id="p4", player="P4", lang_audio="pl", lang_subs="pl"),
            PlayerEntry(online_id="p5", player="P5", lang_audio="pl", lang_subs="pl"),
        ]
        ep2 = EpisodeRow(number=2.0, title="Ep 2", url="http://shinden.pl/ep/2")

        stream_success = MagicMock(qualities={})

        def _extract_side_effect(url, _referer, **_kwargs):
            if "hostx" in url:
                exc = NoStreamError("transient")
                exc.layer = "custom"
                exc.category = "network_error"
                exc.http_status = None
                exc.used_fallback = False
                exc.fallback_category = None
                exc.fallback_http_status = None
                raise exc
            return stream_success

        resolve_embed_ids: list[str] = []

        def _resolve_embed_side_effect(_client, online_id):
            resolve_embed_ids.append(online_id)
            return embeds[online_id]

        client = MagicMock()
        client.get.return_value = MagicMock(raise_for_status=MagicMock(), text="")

        with (
            patch("alt_ani_cli.cli.shinden_series.parse_series_url", return_value=_REF),
            patch("alt_ani_cli.cli.shinden_series.list_episodes", return_value=(_REF, [_EP1, ep2])),
            patch("alt_ani_cli.cli.shinden_episode.parse_players", side_effect=[players_ep1, players_ep2]),
            patch("alt_ani_cli.cli.shinden_episode.sort_players", side_effect=lambda players, download: players),
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", side_effect=_resolve_embed_side_effect),
            patch("alt_ani_cli.cli.extract.resolve", side_effect=_extract_side_effect) as mock_extract,
            patch("alt_ani_cli.cli.diagnostics.health_defer") as mock_defer,
            patch("alt_ani_cli.cli.player_runner.play", return_value=MagicMock(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.cli.history.upsert"),
        ):
            args = _make_args(episode="1-2")
            _run_noninteractive(args, client)

        # Only p4 (episode 2) is deferred — host X only reaches UNAVAILABLE after p2's failure in episode 1.
        mock_defer.assert_called_once()
        _, kwargs = mock_defer.call_args
        assert kwargs["action"] == "deferred"
        assert kwargs["host"] == "hostx.example"
        assert kwargs["online_id"] == "p4"

        # resolve_embed still runs for the deferred candidate — only its extraction is skipped
        assert resolve_embed_ids.count("p4") == 1

        extracted_urls = [c.args[0] for c in mock_extract.call_args_list]
        assert extracted_urls == [
            embeds["p1"].url,
            embeds["p2"].url,
            embeds["p3"].url,
            embeds["p5"].url,
        ]


@pytest.mark.unit
class TestNoninteractiveReorder:
    """health.order() tie-breaks auto-mode candidates within a (lang_audio, lang_subs, max_res) group."""

    def test_no_reorder_event_on_first_episode_without_prior_evidence(self):
        client = MagicMock()
        client.get.return_value = MagicMock(raise_for_status=MagicMock(), text="")

        with (
            patch("alt_ani_cli.cli.shinden_series.parse_series_url", return_value=_REF),
            patch("alt_ani_cli.cli.shinden_series.list_episodes", return_value=(_REF, [_EP1])),
            patch("alt_ani_cli.cli.shinden_episode.parse_players", return_value=[_PLAYER_A, _PLAYER_B]),
            patch("alt_ani_cli.cli.shinden_episode.sort_players", return_value=[_PLAYER_A, _PLAYER_B]),
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", side_effect=AntiBotError("blocked")),
            patch("alt_ani_cli.cli.diagnostics.health_reorder") as mock_reorder,
        ):
            _run_noninteractive(_make_args(), client)

        mock_reorder.assert_not_called()

    def test_reorder_from_prior_episode_evidence_respects_group_boundary(self):
        embeds = {
            "pa1": EmbedURL(url="https://a-host.example/e/pa1", referer="https://shinden.pl/"),
            "pb1": EmbedURL(url="https://b-host.example/e/pb1", referer="https://shinden.pl/"),
            "qa2": EmbedURL(url="https://a-host.example/e/qa2", referer="https://shinden.pl/"),
            "qb2": EmbedURL(url="https://b-host.example/e/qb2", referer="https://shinden.pl/"),
            "qc2": EmbedURL(url="https://c-host.example/e/qc2", referer="https://shinden.pl/"),
        }
        # Episode 1: A (1080p) soft-fails -> DEGRADED, B (same group) succeeds -> HEALTHY, C (720p) is never tried.
        pa1 = PlayerEntry(online_id="pa1", player="A", lang_audio="jp", lang_subs="pl", max_res="1080p")
        pb1 = PlayerEntry(online_id="pb1", player="B", lang_audio="jp", lang_subs="pl", max_res="1080p")
        pc1 = PlayerEntry(online_id="pc1", player="C", lang_audio="jp", lang_subs="pl", max_res="720p")
        players_ep1 = [pa1, pb1, pc1]

        qa2 = PlayerEntry(online_id="qa2", player="A", lang_audio="jp", lang_subs="pl", max_res="1080p")
        qb2 = PlayerEntry(online_id="qb2", player="B", lang_audio="jp", lang_subs="pl", max_res="1080p")
        qc2 = PlayerEntry(online_id="qc2", player="C", lang_audio="jp", lang_subs="pl", max_res="720p")
        players_ep2 = [qa2, qb2, qc2]
        ep2 = EpisodeRow(number=2.0, title="Ep 2", url="http://shinden.pl/ep/2")

        stream_success = MagicMock(qualities={})

        def _extract_side_effect(url, _referer, **_kwargs):
            if "a-host" in url and "qa2" not in url:
                exc = NoStreamError("forbidden")
                exc.layer = "custom"
                exc.category = "http_error"
                exc.http_status = 403
                exc.used_fallback = False
                exc.fallback_category = None
                exc.fallback_http_status = None
                raise exc
            return stream_success

        resolve_embed_ids: list[str] = []

        def _resolve_embed_side_effect(_client, online_id):
            resolve_embed_ids.append(online_id)
            return embeds[online_id]

        client = MagicMock()
        client.get.return_value = MagicMock(raise_for_status=MagicMock(), text="")

        with (
            patch("alt_ani_cli.cli.shinden_series.parse_series_url", return_value=_REF),
            patch("alt_ani_cli.cli.shinden_series.list_episodes", return_value=(_REF, [_EP1, ep2])),
            patch("alt_ani_cli.cli.shinden_episode.parse_players", side_effect=[players_ep1, players_ep2]),
            patch("alt_ani_cli.cli.shinden_episode.sort_players", side_effect=lambda players, download: players),
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", side_effect=_resolve_embed_side_effect),
            patch("alt_ani_cli.cli.extract.resolve", side_effect=_extract_side_effect),
            patch("alt_ani_cli.cli.diagnostics.health_reorder") as mock_reorder,
            patch("alt_ani_cli.cli.player_runner.play", return_value=MagicMock(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.cli.history.upsert"),
        ):
            args = _make_args(episode="1-2")
            _run_noninteractive(args, client)

        # No reorder in episode 1 — no evidence existed yet when it started.
        mock_reorder.assert_called_once()
        _, kwargs = mock_reorder.call_args
        assert kwargs["number"] == ep2.number

        before_ids = kwargs["before"].split(",")
        after_ids = kwargs["after"].split(",")
        states = kwargs["states"].split(",")

        assert set(before_ids) == set(after_ids) == {"qa2", "qb2", "qc2"}
        assert before_ids == ["qa2", "qb2", "qc2"]

        # B (healthy) is promoted ahead of A (degraded) within their shared 1080p group...
        assert after_ids == ["qb2", "qa2", "qc2"]
        # ...but C (a separate, 720p group) never moves — the group boundary is preserved.
        assert after_ids[-1] == "qc2"

        # states is positionally aligned with after.
        assert states == ["healthy", "degraded", "unknown"]

        # chosen == ordered[0]: episode 2's first extraction attempt is for B, not A (original order).
        # Episode 1 only ever resolves pa1 (fails) then pb1 (succeeds and returns) — pc1 is never reached.
        assert resolve_embed_ids[:2] == ["pa1", "pb1"]
        second_episode_resolve_ids = resolve_embed_ids[2:]
        assert second_episode_resolve_ids[0] == "qb2"
