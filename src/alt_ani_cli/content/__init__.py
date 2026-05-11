from pathlib import Path

import yaml


def load_content(file_name: str):
    """Load structured content from a YAML companion file."""
    p = Path(__file__).with_name(file_name)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONTENT = load_content(file_name="ui_content_pl-pl.yaml")
EXCEPTIONS = load_content(file_name="exceptions_content_en-us.yaml")
EXCEPTIONS_PL = load_content(file_name="exceptions_content_pl-pl.yaml")
