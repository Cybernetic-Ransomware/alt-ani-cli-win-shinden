"""Typed readers for request fields; only these errors (and unparsable JSON) are reported as ``bad_request``."""

import json
import math
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from alt_ani_bridge.contract import LIMITS
from alt_ani_bridge.errors import BridgeError

_MISSING: Any = object()


class InputError(BridgeError):
    def __init__(self, field: str, problem: str, **params: Any) -> None:
        super().__init__(
            "bad_request", f"invalid request field {field or '<request>'}: {problem}", field=field, problem=problem, **params
        )


def _reject_constant(name: str) -> float:
    raise InputError("", "not_finite")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise InputError("", "not_finite")
    return value


def parse(payload_json: str) -> dict[str, Any]:
    if not isinstance(payload_json, str):
        raise InputError("", "not_a_string")
    if len(payload_json.encode("utf-8", "surrogatepass")) > LIMITS["request_bytes"]:
        raise InputError("", "too_large", limit=LIMITS["request_bytes"])
    try:
        payload = json.loads(payload_json, parse_constant=_reject_constant, parse_float=_finite_float)
    except json.JSONDecodeError, RecursionError:
        raise InputError("", "malformed_json") from None
    if not isinstance(payload, dict):
        raise InputError("", "not_an_object")
    return payload


class Reader:
    """One JSON object; ``only`` rejects fields no reader of this object knows."""

    def __init__(self, data: dict[str, Any], path: str = "") -> None:
        self.data = data
        self.path = path

    def field(self, name: str) -> str:
        return f"{self.path}.{name}" if self.path else name

    def has(self, name: str) -> bool:
        return name in self.data

    def only(self, allowed: Iterable[str]) -> None:
        unknown = sorted(set(self.data) - set(allowed))
        if unknown:
            raise InputError(self.field(unknown[0]), "unknown_field")

    def _get(self, name: str, default: Any, nullable: bool) -> Any:
        if name not in self.data:
            if default is _MISSING:
                raise InputError(self.field(name), "missing")
            return default
        value = self.data[name]
        if value is None and not nullable:
            raise InputError(self.field(name), "null")
        return value

    def _string(self, field: str, value: Any, nonempty: bool) -> str:
        if not isinstance(value, str):
            raise InputError(field, "wrong_type", expected="string")
        if len(value) > LIMITS["string_chars"]:
            raise InputError(field, "too_large", limit=LIMITS["string_chars"])
        if nonempty and not value.strip():
            raise InputError(field, "empty")
        return value

    def text(self, name: str, default: Any = _MISSING, *, nullable: bool = False, nonempty: bool = True) -> Any:
        value = self._get(name, default, nullable)
        if value is None or name not in self.data:
            return value
        return self._string(self.field(name), value, nonempty)

    def integer(
        self,
        name: str,
        default: Any = _MISSING,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        nullable: bool = False,
    ) -> Any:
        value = self._get(name, default, nullable)
        if value is None or name not in self.data:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputError(self.field(name), "wrong_type", expected="integer")
        if minimum is not None and value < minimum:
            raise InputError(self.field(name), "out_of_range", minimum=minimum)
        if maximum is not None and value > maximum:
            raise InputError(self.field(name), "too_large", limit=maximum)
        return value

    def number(self, name: str, default: Any = _MISSING, *, minimum: float | None = None, nullable: bool = False) -> Any:
        value = self._get(name, default, nullable)
        if value is None or name not in self.data:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise InputError(self.field(name), "wrong_type", expected="number")
        if not math.isfinite(value):
            raise InputError(self.field(name), "not_finite")
        if minimum is not None and value < minimum:
            raise InputError(self.field(name), "out_of_range", minimum=minimum)
        return float(value)

    def flag(self, name: str, default: Any = _MISSING) -> Any:
        value = self._get(name, default, False)
        if name not in self.data:
            return value
        if not isinstance(value, bool):
            raise InputError(self.field(name), "wrong_type", expected="boolean")
        return value

    def enum[E: StrEnum](self, name: str, cls: type[E], default: Any = _MISSING, *, nullable: bool = False) -> Any:
        value = self._get(name, default, nullable)
        if value is None or name not in self.data:
            return value
        try:
            return cls(self._string(self.field(name), value, True))
        except ValueError:
            raise InputError(self.field(name), "unknown_value", expected=[m.value for m in cls]) from None

    def choice(self, name: str, choices: tuple[str, ...], default: Any = _MISSING) -> Any:
        value = self._get(name, default, False)
        if name not in self.data:
            return value
        if self._string(self.field(name), value, True) not in choices:
            raise InputError(self.field(name), "unknown_value", expected=list(choices))
        return value

    def obj(self, name: str, allowed: Iterable[str] | None = None, default: Any = _MISSING, *, nullable: bool = False) -> Any:
        value = self._get(name, default, nullable)
        if value is None or name not in self.data:
            return value
        if not isinstance(value, dict):
            raise InputError(self.field(name), "wrong_type", expected="object")
        reader = Reader(value, self.field(name))
        if allowed is not None:
            reader.only(allowed)
        return reader

    def array(self, name: str, limit: str = "list_items", default: Any = _MISSING) -> Any:
        value = self._get(name, default, False)
        if name not in self.data:
            return value
        if not isinstance(value, list):
            raise InputError(self.field(name), "wrong_type", expected="array")
        if len(value) > LIMITS[limit]:
            raise InputError(self.field(name), "too_large", limit=LIMITS[limit])
        return value

    def strs(self, name: str, limit: str = "list_items", default: Any = _MISSING) -> Any:
        values = self.array(name, limit, default)
        if name not in self.data:
            return values
        return [self._string(f"{self.field(name)}[{i}]", v, True) for i, v in enumerate(values)]

    def numbers(self, name: str, limit: str = "episodes", default: Any = _MISSING) -> Any:
        values = self.array(name, limit, default)
        if name not in self.data:
            return values
        wrapper = Reader({str(i): v for i, v in enumerate(values)}, self.field(name))
        return [wrapper.number(str(i)) for i in range(len(values))]

    def objs(self, name: str, allowed: Iterable[str], limit: str = "list_items", default: Any = _MISSING) -> Any:
        values = self.array(name, limit, default)
        if name not in self.data:
            return values
        wrapper = Reader({str(i): v for i, v in enumerate(values)}, self.field(name))
        return [wrapper.obj(str(i), allowed) for i in range(len(values))]
