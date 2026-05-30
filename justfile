# alt-ani-cli-win — task runner
# Install: scoop install just  |  winget install Casey.Just

set shell := ["powershell", "-Command"]

# Run pre-commit on staged files
# Stage your changes first: git add <files>
commit:
    uv run pre-commit run

# Check formatting (report only — apply with: uv run ruff format src/)
format:
    uv run ruff format --check src/

# Run the full linting suite
lint:
    uv run ruff check src/
    uv run ty check src/
    uv run python -m codespell_lib src/
    uv run bandit -c pyproject.toml -r src/

# Run tests
test:
    uv run pytest -v

# Run tests with coverage report
test-cov:
    uv run pytest --tb=short -q
