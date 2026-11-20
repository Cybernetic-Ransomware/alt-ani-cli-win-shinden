import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from alt_ani_cli import __version__
from alt_ani_cli.content import CONTENT
from alt_ani_core.config import ExtractOptions
from alt_ani_core.models import SourceFilter

_CLI = CONTENT["cli"]


@dataclass(frozen=True)
class CliOptions:
    query: tuple[str, ...] = ()
    url: str | None = None
    resume: bool = False
    download: bool = False
    delete_history: bool = False
    episode: str | None = None
    quality: str | None = None
    select_nth: int | None = None
    vlc: bool = False
    no_detach: bool = False
    debug: bool = False
    player_name: str | None = None
    lang: str | None = None
    subs: str | None = None
    allow_fallback: bool = False
    show_sources: bool = False
    cookies_file: str | None = None
    cookies_browser: str | None = None

    @property
    def source_filter(self) -> SourceFilter:
        return SourceFilter(lang=self.lang, subs=self.subs, player_name=self.player_name)

    @property
    def extract_options(self) -> ExtractOptions:
        return ExtractOptions(cookies_file=self.cookies_file, cookies_browser=self.cookies_browser)

    @property
    def player_kind(self) -> Literal["mpv", "vlc"]:
        return "vlc" if self.vlc else "mpv"


def build_parser() -> argparse.ArgumentParser:
    h = _CLI["help"]
    p = argparse.ArgumentParser(
        prog="alt-ani-cli",
        description=_CLI["description"],
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=_CLI["epilog"],
    )

    p.add_argument("query", nargs="*", metavar="QUERY", help=h["query"])

    p.add_argument("--url", metavar="URL", help=h["url"])

    p.add_argument("-c", "--continue", dest="resume", action="store_true", help=h["resume"])
    p.add_argument("-d", "--download", action="store_true", help=h["download"])
    p.add_argument("-D", "--delete-history", action="store_true", help=h["delete_history"])

    p.add_argument("-e", "--episode", metavar="RANGE", help=h["episode"])
    p.add_argument("-q", "--quality", default=None, metavar="QUALITY", help=h["quality"])
    p.add_argument("-S", "--select-nth", type=int, metavar="N", help=h["select_nth"])

    p.add_argument("-v", "--vlc", action="store_true", help=h["vlc"])
    p.add_argument("--no-detach", action="store_true", help=h["no_detach"])
    p.add_argument("--debug", action="store_true", help=h["debug"])

    p.add_argument("--player-name", metavar="NAME", help=h["player_name"])
    p.add_argument("--lang", metavar="{pl,jp,en}", help=h["lang"])
    p.add_argument("--subs", metavar="{pl,en,none}", help=h["subs"])
    p.add_argument("--allow-fallback", action="store_true", help=h["allow_fallback"])
    p.add_argument("--show-sources", action="store_true", help=h["show_sources"])

    p.add_argument("--cookies-file", metavar="PATH", help=h["cookies_file"])
    p.add_argument("--cookies-browser", metavar="{chrome,firefox,edge,opera,brave}", help=h["cookies_browser"])

    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    return p


def parse_args(argv: Sequence[str] | None = None) -> CliOptions:
    ns = build_parser().parse_args(argv)
    values = vars(ns)
    values["query"] = tuple(values["query"])
    return CliOptions(**values)


def missing_input_error() -> None:
    argparse.ArgumentParser(prog="alt-ani-cli").error(_CLI["errors"]["missing_input"])
