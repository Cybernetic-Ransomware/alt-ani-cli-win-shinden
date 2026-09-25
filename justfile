# alt-ani-cli-win — task runner
# Install: scoop install just  |  winget install Casey.Just

set shell := ["powershell", "-Command"]

# Run pre-commit on staged files, then open Commitizen
# Stage your changes first: git add <files>
commit:
    uv run pre-commit run
    uv run cz commit

# Bump version on release branches (auto-tags vX.Y.Z, updates pyproject.toml)
bump:
    uv run cz bump

# Apply ruff formatting to src/
format:
    uv run ruff format src/

# Run the full linting suite
lint:
    uv run ruff check src/
    uv run ty check src/
    uv run python -m codespell_lib src/
    uv run bandit -c pyproject.toml -r src/ -q

# Run tests
test:
    uv run pytest -v

# Run tests with coverage report
test-cov:
    uv run pytest --tb=short -q

# Replay resolve_result failures from a diagnostics session log against live hosts
replay-failures LOG:
    uv run python tools/replay_failed_resolvers.py "{{LOG}}"
