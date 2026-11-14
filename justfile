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

# Apply ruff formatting to all Python sources
format:
    uv run ruff format .

# Run the full linting suite
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check apps packages/core/src
    uv run lint-imports
    # tracked files only, so local untracked notes and build output never fail the run
    uv run python -m codespell_lib (git ls-files apps packages docs)
    uv run bandit -c pyproject.toml -r apps packages/core/src -q

# Build both wheels and check them in clean virtual environments
package:
    uv run python tools/check_packaging.py

# Run tests
test:
    uv run pytest -v

# Run tests with coverage report
test-cov:
    uv run pytest --tb=short -q

# Opt-in checks against live shinden.pl (needs ALT_ANI_CLI_FLARESOLVERR_URL; not part of CI)
test-live:
    uv run pytest -m integration tests/live -v --no-cov

# Replay resolve_result failures from a diagnostics session log against live hosts
replay-failures LOG:
    uv run python tools/replay_failed_resolvers.py "{{LOG}}"
