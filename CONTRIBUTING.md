# Contributing

Development setup, test and lint workflows are described in the
[Development](README.md#development) section of the README. In short: `uv sync`,
then use the `justfile` recipes (`just test`, `just lint`, `just format`).

### Where code goes

The engine (`packages/core`, import `alt_ani_core`) must stay usable without a terminal: it does not import the
CLI, `rich`, `InquirerPy`, `argparse`, `platformdirs`, `yaml` or `subprocess`, does not read the environment and
does not print — it reports through events and typed errors. `curl_cffi`, `selectolax`, `yt_dlp`, `sqlite3`,
`fcntl` and `msvcrt` are each confined to their adapter modules. Anything about the terminal, desktop paths, players or Polish wording
belongs in `apps/cli`. `just lint` (import-linter) and `tests/architecture` enforce this; see
[ADR-0002](docs/adr/0002-core-cli-split.md).

### Python 3.14 syntax note

This project intentionally uses PEP 758 bracketless `except` syntax, e.g.
`except ValueError, OSError:`. This is valid in Python 3.14+ when no `as`
clause is used. Do not "fix" it to Python 3.13-compatible syntax unless the
project drops the Python 3.14-only requirement.

Note that `ruff format` normalises parenthesised `except (E1, E2):` to the
bracketless form automatically, so re-adding parentheses will not survive
`just lint`.
