from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Generator

from rich.console import Console

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
def spinner(message: str) -> Generator[None, None, None]:
    with _get().status(f"[bold green]{message}[/bold green]"):
        yield


def info(msg: str) -> None:
    _get().print(f"[bold blue]>[/bold blue] {msg}")


def success(msg: str) -> None:
    _get().print(f"[bold green]OK[/bold green] {msg}")


def error(msg: str) -> None:
    _get_err().print(f"[bold red]ERR[/bold red] {msg}")


def warn(msg: str) -> None:
    _get_err().print(f"[bold yellow]WARN[/bold yellow] {msg}")
