from importlib import resources

import yaml
from alt_ani_core.errors import ShindenError
from alt_ani_core.messages import MESSAGES
from alt_ani_core.redaction import redact_text


def load_content(file_name: str):
    """Load structured content from a YAML companion file shipped inside this package."""
    with resources.files(__package__).joinpath(file_name).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONTENT = load_content(file_name="ui_content_pl-pl.yaml")
# technical messages live in the engine; only the desktop-player texts are CLI-owned
EXCEPTIONS = {**MESSAGES, **load_content(file_name="exceptions_content_en-us.yaml")}
EXCEPTIONS_PL = load_content(file_name="exceptions_content_pl-pl.yaml")

# engine errors the CLI has always shown in Polish; every other one keeps the engine's technical text
_LOCALIZED_SECTIONS = frozenset({"download"})
_LOCALIZED_CODES = frozenset({"series.age_gate"})


def error_text(exc: ShindenError) -> str:
    code = exc.code
    if code is not None and "." in code:
        section, key = code.split(".", 1)
        if section in _LOCALIZED_SECTIONS or code in _LOCALIZED_CODES:
            template = EXCEPTIONS_PL.get(section, {}).get(key)
            if template is not None:
                return redact_text(template.format(**exc.params))
    return redact_text(exc)
