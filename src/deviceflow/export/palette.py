"""Render palette for common semiconductor materials.

Render-only: colours and PBR parameters never enter the geometry engine.
Names are matched case-insensitively and with common suffixes stripped
(``W_TE`` -> ``W``, ``SiO2_base`` -> ``SiO2``) so process-flow naming maps
onto one consistent look per material across a paper or project.

Style: vivid, mutually distinct hues; dielectrics opaque (not glass);
metals metallic with moderate roughness so reflections stay limited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Color = tuple[float, float, float, float]


@dataclass(frozen=True)
class RenderSpec:
    color: Color
    roughness: float
    metallic: float
    transmission: float = 0.0
    role: str = "other"


def _dielectric(rgb, roughness=0.45, role="dielectric") -> RenderSpec:
    return RenderSpec((*rgb, 1.0), roughness, 0.0, role=role)


def _metal(rgb, roughness=0.3) -> RenderSpec:
    return RenderSpec((*rgb, 1.0), roughness, 1.0, role="metal")


def _semiconductor(rgb, roughness=0.5) -> RenderSpec:
    return RenderSpec((*rgb, 1.0), roughness, 0.0, role="semiconductor")


PALETTE: dict[str, RenderSpec] = {
    # substrate / bulk
    "Si": _dielectric((0.55, 0.45, 0.38), 0.65, role="substrate"),  # dark warm grey
    # dielectrics
    "SiO2": _dielectric((0.65, 0.92, 1.00), 0.40),  # pale cyan-blue
    "SiN": _dielectric((0.80, 0.85, 0.55), 0.45),  # olive
    "Al2O3": _dielectric((0.85, 0.75, 0.95), 0.45),  # lilac
    "HfO2": _dielectric((0.20, 0.85, 0.85), 0.45),  # vivid cyan
    "HZO": _dielectric((0.95, 0.30, 0.55), 0.40),  # magenta
    "In2O3": _dielectric((0.70, 0.50, 0.85), 0.50),  # purple (hard mask)
    "PR": _dielectric((1.00, 0.30, 0.10), 0.65, role="resist"),  # photoresist red-orange
    # metals
    "W": _metal((0.55, 0.62, 0.78), 0.70),  # cool silver-blue, matte
    "TiN": _metal((1.00, 0.80, 0.20), 0.35),  # gold
    "NiPd": _metal((1.00, 0.55, 0.20), 0.22),  # copper-orange
    "Cr": _metal((0.33, 0.42, 0.40), 0.30),  # dark grey-green
    "Al": _metal((0.85, 0.87, 0.90), 0.25),
    "Cu": _metal((0.95, 0.55, 0.35), 0.30),
    "Au": _metal((1.00, 0.85, 0.35), 0.25),
    "Pt": _metal((0.80, 0.80, 0.85), 0.30),
    "Mo": _metal((0.60, 0.60, 0.65), 0.45),
    # semiconductors
    "InGeO": _semiconductor((0.10, 0.35, 0.95)),  # deep blue
    "InSiO": _semiconductor((0.15, 0.75, 0.35)),  # green
    "IGZO": _semiconductor((0.25, 0.60, 0.90)),
    "poly-Si": _semiconductor((0.75, 0.55, 0.30)),
}

ROLE_DEFAULTS: dict[str, RenderSpec] = {
    "substrate": _dielectric((0.55, 0.55, 0.58), 0.6, role="substrate"),
    "dielectric": _dielectric((0.62, 0.82, 0.92), 0.45),
    "semiconductor": _semiconductor((0.20, 0.35, 0.75)),
    "metal": _metal((0.75, 0.78, 0.85), 0.35),
    "resist": _dielectric((0.90, 0.35, 0.25), 0.65, role="resist"),
    "other": _dielectric((0.70, 0.70, 0.70), 0.5, role="other"),
}

_SUFFIX = re.compile(r"[_\-\s]+(te|be|top|bottom|base|cap|liner|fill|plug|\d+)$", re.IGNORECASE)
_BY_LOWER = {k.lower(): v for k, v in PALETTE.items()}


def lookup(name: str) -> RenderSpec | None:
    """Palette entry for a material name, or None if unknown."""
    key = name.strip().lower()
    while True:
        if key in _BY_LOWER:
            return _BY_LOWER[key]
        stripped = _SUFFIX.sub("", key)
        if stripped == key:
            return None
        key = stripped


# ------------------------------------------------------- user material tables --


def parse_color(value) -> Color:
    """'#RRGGBB' / '#RRGGBBAA' / (r, g, b) / (r, g, b, a) in 0..1 -> RGBA tuple."""
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) not in (6, 8):
            raise ValueError(f"color must be '#RRGGBB' or an (r, g, b[, a]) tuple in 0..1, got {value!r}")
        try:
            parts = [int(text[i : i + 2], 16) / 255 for i in range(0, len(text), 2)]
        except ValueError:
            raise ValueError(f"color must be '#RRGGBB' or an (r, g, b[, a]) tuple in 0..1, got {value!r}") from None
        if len(parts) == 3:
            parts.append(1.0)
        return tuple(parts)
    try:
        parts = [float(c) for c in value]
    except (TypeError, ValueError):
        raise ValueError(f"color must be '#RRGGBB' or an (r, g, b[, a]) tuple in 0..1, got {value!r}") from None
    if len(parts) == 3:
        parts.append(1.0)
    if len(parts) != 4 or not all(0.0 <= c <= 1.0 for c in parts):
        raise ValueError(f"color must be '#RRGGBB' or an (r, g, b[, a]) tuple in 0..1, got {value!r}")
    return tuple(parts)


def load_material_table(source) -> dict[str, dict]:
    """A user material table: {name: {role, color?, roughness?, metallic?}}.

    ``source`` may be a dict, or a path to a .json / .toml file with the same
    structure. Every entry needs a role; look parameters are optional and
    fall back to the built-in palette or the role default.
    """
    if source is None:
        return {}
    if isinstance(source, dict):
        table = source
    else:
        from pathlib import Path

        path = Path(source)
        text = path.read_text()
        if path.suffix.lower() == ".json":
            import json

            table = json.loads(text)
        elif path.suffix.lower() == ".toml":
            try:
                import tomllib
            except ImportError:  # Python 3.10
                import tomli as tomllib  # type: ignore
            table = tomllib.loads(text)
        else:
            raise ValueError(f"material table must be a dict, .json or .toml, got {path}")
    out: dict[str, dict] = {}
    for name, spec in table.items():
        if not isinstance(spec, dict) or "role" not in spec:
            raise ValueError(f"material {name!r}: every entry needs a role (substrate/dielectric/semiconductor/metal/resist/other)")
        entry = {"role": spec["role"]}
        if "color" in spec:
            entry["color"] = parse_color(spec["color"])
        for key in ("roughness", "metallic", "transmission"):
            if key in spec:
                entry[key] = float(spec[key])
        out[str(name)] = entry
    return out
