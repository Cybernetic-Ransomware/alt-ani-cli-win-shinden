from contextlib import nullcontext

import pytest

from alt_ani_cli.ui import progress

_DYNAMIC = [
    "Show [/x]",
    "[director's cut]",
    "Anime [2026]",
    "[bold]Naruto",
    "Re[Zero] [b]x[/b] [/b]",
    "x [link=https://example.com]y[/link]",
    "Łódź [red]żółw[/red] 進撃の巨人",
    "Ep :joy: :smile:",
    r"C:\dl\[Oshi no Ko]\01.mp4",
    "trailing backslash\\",
    "Błąd: HTTP 404 dla https://cdn.example/[cut]/x.mp4?a=[/x]",
]


@pytest.mark.unit
class TestPrefixedMessagesRenderLiterally:
    @pytest.mark.parametrize("text", _DYNAMIC)
    @pytest.mark.parametrize(
        ("fn", "expected"),
        [
            (progress.info, "> {}\n"),
            (progress.success, "OK {}\n"),
            (progress.error, "\nERR {}\n"),
            (progress.warn, "\nWARN {}\n"),
        ],
    )
    def test_dynamic_text_is_kept_verbatim(self, rich_plain, fn, expected, text):
        fn(text)
        assert rich_plain.text == expected.format(text)

    def test_info_and_success_go_to_stdout(self, rich_plain):
        progress.info("a")
        progress.success("b")
        assert rich_plain.err.getvalue() == ""

    def test_error_and_warn_go_to_stderr(self, rich_plain):
        progress.error("a")
        progress.warn("b")
        assert rich_plain.out.getvalue() == ""


@pytest.mark.unit
class TestPrefixStylesArePreserved:
    @pytest.mark.parametrize(
        ("fn", "expected"),
        [
            (progress.info, "\x1b[1;34m>\x1b[0m [bold]x\n"),
            (progress.success, "\x1b[1;32mOK\x1b[0m [bold]x\n"),
            (progress.error, "\n\x1b[1;31mERR\x1b[0m [bold]x\n"),
            (progress.warn, "\n\x1b[1;33mWARN\x1b[0m [bold]x\n"),
        ],
    )
    def test_only_the_prefix_is_styled(self, rich_ansi, fn, expected):
        fn("[bold]x")
        assert rich_ansi.text == expected


@pytest.mark.unit
class TestRule:
    @pytest.mark.parametrize("text", _DYNAMIC)
    def test_title_is_kept_verbatim(self, rich_plain, text):
        progress.rule(text)
        line = rich_plain.text
        assert f" {text} " in line
        assert line.startswith("─") and line.rstrip("\n").endswith("─")


@pytest.mark.unit
class TestSpinner:
    @pytest.mark.parametrize("text", _DYNAMIC)
    def test_status_text_is_kept_verbatim(self, rich_plain, monkeypatch, text):
        shown = []
        monkeypatch.setattr(rich_plain.out_console, "status", lambda status, **_kw: shown.append(status) or nullcontext())
        with progress.spinner(text):
            pass
        rich_plain.out_console.print(shown[0])
        assert rich_plain.text == f"{text}\n"

    def test_status_text_keeps_its_style(self, rich_ansi, monkeypatch):
        shown = []
        monkeypatch.setattr(rich_ansi.out_console, "status", lambda status, **_kw: shown.append(status) or nullcontext())
        with progress.spinner("[i]x"):
            pass
        rich_ansi.out_console.print(shown[0])
        assert rich_ansi.text == "\x1b[1;32m[i]x\x1b[0m\n"


@pytest.mark.unit
class TestOutput:
    @pytest.mark.parametrize("text", _DYNAMIC)
    def test_plain_string_is_kept_verbatim(self, rich_plain, text):
        progress.output(text)
        assert rich_plain.text == f"{text}\n"
