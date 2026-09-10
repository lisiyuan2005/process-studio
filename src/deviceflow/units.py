"""Length parsing. Internal geometry unit is micrometres (um).

Public API accepts strings such as ``"20nm"``, ``"1um"``, ``"0.1mm"`` or plain
numbers (already in um). All parsing goes through :func:`parse_length`.
"""

from __future__ import annotations

import math
import re

from .exceptions import UnitError

_TO_UM = {
    "a": 1e-4,
    "nm": 1e-3,
    "um": 1.0,
    "µm": 1.0,
    "μm": 1.0,  # greek mu, in case of copy-paste
    "mm": 1e3,
}

_PATTERN = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Zµμ]+)\s*$")


def parse_length(value) -> float:
    """Convert a length to micrometres.

    ``value`` may be a number (interpreted as um) or a string with a unit
    suffix among ``A``, ``nm``, ``um``/``µm``, ``mm``.
    """
    if isinstance(value, bool):
        raise UnitError(f"cannot interpret {value!r} as a length")
    if isinstance(value, (int, float)):
        um = float(value)
        if not math.isfinite(um):
            raise UnitError(f"length must be finite, got {value!r}")
        return um
    if not isinstance(value, str):
        raise UnitError(f"cannot interpret {value!r} as a length")
    match = _PATTERN.match(value)
    if match is None:
        raise UnitError(
            f"cannot parse length {value!r}; expected e.g. '20nm', '1um', '0.1mm'"
        )
    number, unit = match.groups()
    unit_key = unit if unit in ("µm", "μm") else unit.lower()
    if unit_key not in _TO_UM:
        raise UnitError(f"unknown length unit {unit!r} in {value!r}")
    um = float(number) * _TO_UM[unit_key]
    if not math.isfinite(um):
        raise UnitError(f"length must be finite, got {value!r}")
    return um


_TO_S = {"s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0, "hr": 3600.0}

_TIME_PATTERN = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z]+)\s*$")


def parse_time(value) -> float:
    """Convert a duration ("50s", "2min", "1.5h", or a number of seconds) to seconds."""
    if isinstance(value, bool):
        raise UnitError(f"cannot interpret {value!r} as a time")
    if isinstance(value, (int, float)):
        t = float(value)
    elif isinstance(value, str):
        match = _TIME_PATTERN.match(value)
        if match is None:
            raise UnitError(f"cannot parse time {value!r}; expected e.g. '50s', '2min', '1h'")
        number, unit = match.groups()
        if unit.lower() not in _TO_S:
            raise UnitError(f"unknown time unit {unit!r} in {value!r}")
        t = float(number) * _TO_S[unit.lower()]
    else:
        raise UnitError(f"cannot interpret {value!r} as a time")
    if not math.isfinite(t) or t < 0:
        raise UnitError(f"time must be finite and non-negative, got {value!r}")
    return t


def parse_rate(value) -> float:
    """Convert a rate ("10nm/s", "0.6um/min", "2A/s") to um per second."""
    if not isinstance(value, str) or value.count("/") != 1:
        raise UnitError(f"cannot parse rate {value!r}; expected e.g. '10nm/s' or '0.6um/min'")
    length, per = value.split("/")
    if not per.strip() or not per.strip().isalpha():
        raise UnitError(f"cannot parse rate {value!r}; expected a time unit after '/'")
    um = parse_length(length)
    seconds = parse_time("1" + per.strip())
    if um < 0:
        raise UnitError(f"rate must be non-negative, got {value!r}")
    return um / seconds


def format_length(um: float) -> str:
    """Render a length in um as a compact string ("20nm", "1.5um")."""
    if um == 0:
        return "0nm"
    nm = um * 1e3
    if abs(nm) < 1000 - 1e-9:
        return _fmt(nm) + "nm"
    return _fmt(um) + "um"


def _fmt(x: float) -> str:
    text = f"{x:.6g}"
    if "." in text and "e" not in text:
        text = text.rstrip("0").rstrip(".")
    return text
