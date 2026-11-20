from collections.abc import Generator
from contextlib import contextmanager

from rich.console import Console
from rich.text import Text

_console: Console | None = None
_err_console: Console | None = None


def _get() -> Console:
    global _console
    if _console is None:
        _console = Console(legacy_windows=False, highlight=False)
    return _console


def _get_err() -> Console:
    global _err_console
    if _err_console is None:
        _err_console = Console(stderr=True, legacy_windows=False, highlight=False)
    return _err_console


@contextmanager
def spinner(message: str) -> Generator[None]:
    with _get().status(Text(message, style="bold green")):
        yield


def info(msg: str) -> None:
    _get().print(Text.assemble((">", "bold blue"), " ", msg))


def success(msg: str) -> None:
    _get().print(Text.assemble(("OK", "bold green"), " ", msg))


def error(msg: str) -> None:
    _get_err().print(Text.assemble("\n", ("ERR", "bold red"), " ", msg))


def warn(msg: str) -> None:
    _get_err().print(Text.assemble("\n", ("WARN", "bold yellow"), " ", msg))


def rule(title: str) -> None:
    _get().rule(Text(title))


def output(msg: str | Text) -> None:
    _get().print(msg if isinstance(msg, Text) else Text(msg))
