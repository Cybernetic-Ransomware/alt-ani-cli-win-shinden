# Contributing

Development setup, test and lint workflows are described in the
[Development](README.md#development) section of the README. In short: `uv sync`,
then use the `justfile` recipes (`just test`, `just lint`, `just format`).

### Python 3.14 syntax note

This project intentionally uses PEP 758 bracketless `except` syntax, e.g.
`except ValueError, OSError:`. This is valid in Python 3.14+ when no `as`
clause is used. Do not "fix" it to Python 3.13-compatible syntax unless the
project drops the Python 3.14-only requirement.

Note that `ruff format` normalises parenthesised `except (E1, E2):` to the
bracketless form automatically, so re-adding parentheses will not survive
`just lint`.
