"""CLI argument parser, the options derived from it, and environment-driven engine configuration."""

import dataclasses

import pytest
from alt_ani_core.config import DEFAULT_ANTIBOT_DELAY_SEC, AccessConfig, EngineConfig, ExtractOptions
from alt_ani_core.models import SourceFilter

from alt_ani_cli import paths
from alt_ani_cli.args import CliOptions, build_parser, parse_args


@pytest.mark.unit
class TestArgParser:
    def test_url_flag(self):
        options = parse_args(["--url", "https://shinden.pl/series/123-slug"])
        assert options.url == "https://shinden.pl/series/123-slug"

    def test_episode_short(self):
        options = parse_args(["-e", "5", "--url", "u"])
        assert options.episode == "5"

    def test_episode_long(self):
        options = parse_args(["--episode", "1-5", "--url", "u"])
        assert options.episode == "1-5"

    def test_vlc_flag(self):
        options = parse_args(["--vlc", "--url", "u"])
        assert options.vlc is True

    def test_debug_flag(self):
        options = parse_args(["--debug", "--url", "u"])
        assert options.debug is True

    def test_query_positional(self):
        options = parse_args(["fate", "strange", "fake"])
        assert options.query == ("fate", "strange", "fake")

    def test_allow_fallback_flag(self):
        options = parse_args(["--allow-fallback", "--url", "u"])
        assert options.allow_fallback is True

    def test_allow_fallback_default_false(self):
        options = parse_args(["--url", "u"])
        assert options.allow_fallback is False

    def test_show_sources_flag(self):
        options = parse_args(["--show-sources", "--url", "u"])
        assert options.show_sources is True

    def test_show_sources_default_false(self):
        options = parse_args(["--url", "u"])
        assert options.show_sources is False

    def test_flags_before_query(self):
        a1 = parse_args(["-d", "-e", "5", "fate", "strange"])
        a2 = parse_args(["fate", "strange", "-d", "-e", "5"])
        assert a1.download == a2.download
        assert a1.episode == a2.episode
        assert a1.query == a2.query

    def test_continue_flag_is_stored_as_resume(self):
        assert parse_args(["-c"]).resume is True
        assert parse_args(["--continue"]).resume is True

    def test_select_nth_is_an_integer(self):
        assert parse_args(["-S", "3", "fate"]).select_nth == 3

    def test_no_arguments_gives_the_defaults(self):
        assert parse_args([]) == CliOptions()

    def test_parser_and_options_agree_on_every_field(self):
        namespace = vars(build_parser().parse_args([]))
        assert set(namespace) == {f.name for f in dataclasses.fields(CliOptions)}


@pytest.mark.unit
class TestCliOptions:
    def test_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            parse_args(["--url", "u"]).url = "other"  # ty: ignore[invalid-assignment]

    def test_source_filter_carries_the_three_criteria(self):
        options = parse_args(["--lang", "jp", "--subs", "pl", "--player-name", "CDA", "--url", "u"])
        assert options.source_filter == SourceFilter(lang="jp", subs="pl", player_name="CDA")

    def test_source_filter_is_inactive_by_default(self):
        assert parse_args(["--url", "u"]).source_filter.active is False

    def test_extract_options_carry_the_cookie_sources(self):
        options = parse_args(["--cookies-file", "c.txt", "--cookies-browser", "firefox", "--url", "u"])
        assert options.extract_options == ExtractOptions(cookies_file="c.txt", cookies_browser="firefox")

    def test_extract_options_default_to_no_cookies(self):
        assert parse_args(["--url", "u"]).extract_options == ExtractOptions()

    def test_player_kind_defaults_to_mpv(self):
        assert parse_args(["--url", "u"]).player_kind == "mpv"

    @pytest.mark.parametrize("flag", ["-v", "--vlc"])
    def test_player_kind_is_vlc_with_the_flag(self, flag):
        assert parse_args([flag, "--url", "u"]).player_kind == "vlc"


@pytest.mark.unit
class TestLoadEngineConfig:
    def test_defaults(self, cli_paths):
        config = paths.load_engine_config(cli_paths, {})
        assert config == EngineConfig(
            cache_dir=cli_paths.cache_dir,
            xhr_backend="new",
            antibot_delay_sec=DEFAULT_ANTIBOT_DELAY_SEC,
            access=AccessConfig(flaresolverr_url=None),
        )

    @pytest.mark.parametrize(("raw", "expected"), [("api4", "api4"), ("new", "new"), (" API4 ", "api4")])
    def test_backend_from_environment(self, cli_paths, raw, expected):
        assert paths.load_engine_config(cli_paths, {paths.ENV_XHR_BACKEND: raw}).xhr_backend == expected

    @pytest.mark.parametrize("raw", ["newest", "", "new_xhr"])
    def test_invalid_backend_raises_value_error(self, cli_paths, raw):
        with pytest.raises(ValueError, match="ALT_ANI_CLI_XHR_BACKEND"):
            paths.load_engine_config(cli_paths, {paths.ENV_XHR_BACKEND: raw})

    def test_antibot_delay_from_environment(self, cli_paths):
        assert paths.load_engine_config(cli_paths, {paths.ENV_ANTIBOT_DELAY: "7.5"}).antibot_delay_sec == 7.5

    def test_invalid_antibot_delay_raises_value_error_naming_the_variable(self, cli_paths):
        with pytest.raises(ValueError, match=r"ALT_ANI_CLI_ANTIBOT_DELAY.*'soon'"):
            paths.load_engine_config(cli_paths, {paths.ENV_ANTIBOT_DELAY: "soon"})

    def test_flaresolverr_url_becomes_access_config(self, cli_paths):
        config = paths.load_engine_config(cli_paths, {paths.ENV_FLARESOLVERR_URL: "http://localhost:8191"})
        assert config.access == AccessConfig(flaresolverr_url="http://localhost:8191")
        assert config.access.solver_url == "http://localhost:8191"

    def test_empty_flaresolverr_url_means_no_solver(self, cli_paths):
        config = paths.load_engine_config(cli_paths, {paths.ENV_FLARESOLVERR_URL: ""})
        assert config.access.flaresolverr_url is None

    def test_reads_the_process_environment_by_default(self, cli_paths, monkeypatch):
        monkeypatch.setenv(paths.ENV_XHR_BACKEND, "api4")
        monkeypatch.setenv(paths.ENV_FLARESOLVERR_URL, "http://solver:8191")
        config = paths.load_engine_config(cli_paths)
        assert config.xhr_backend == "api4"
        assert config.access.flaresolverr_url == "http://solver:8191"
        assert paths.flaresolverr_url() == "http://solver:8191"

    def test_cache_dir_comes_from_the_paths(self, cli_paths):
        assert paths.load_engine_config(cli_paths, {}).cache_dir == cli_paths.cache_dir


@pytest.mark.unit
class TestCliPaths:
    def test_derived_locations(self, cli_paths):
        assert cli_paths.history_db == cli_paths.state_dir / "history.db"
        assert cli_paths.legacy_history == cli_paths.state_dir / "history.json"
        assert cli_paths.diagnostics_dir == cli_paths.cache_dir / "diagnostics"
        assert cli_paths.mpv_log == cli_paths.cache_dir / "mpv-debug.log"
