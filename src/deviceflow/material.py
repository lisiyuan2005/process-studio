"""Materials: a unique internal id, a name, a role and render metadata.

Geometry ownership inside the process state is keyed by :class:`Material`
objects (compared by id), never by bare strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .exceptions import MaterialError
from .export.palette import PALETTE, ROLE_DEFAULTS, load_material_table, lookup, parse_color

PALETTE_NAMES = tuple(PALETTE)

ROLES = ("substrate", "dielectric", "semiconductor", "metal", "resist", "other")

Color = tuple[float, float, float, float]


@dataclass(frozen=True, eq=False)
class Material:
    id: int
    name: str
    role: str
    color: Color
    render: dict  # render-only PBR parameters: roughness, metallic, transmission
    _registry_id: int

    def __hash__(self) -> int:
        return hash((self._registry_id, self.id))

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, Material)
            and other._registry_id == self._registry_id
            and other.id == self.id
        )

    def __repr__(self) -> str:
        return f"Material({self.name!r}, role={self.role!r}, id={self.id})"


class MaterialRegistry:
    """Owns the materials of one device; hands out unique ids."""

    _next_registry_id = 0

    def __init__(self, table=None) -> None:
        self._id = MaterialRegistry._next_registry_id
        MaterialRegistry._next_registry_id += 1
        self._by_name: dict[str, Material] = {}
        self._order: list[Material] = []
        try:
            self._table = load_material_table(table)
        except ValueError as e:
            raise MaterialError(str(e)) from e

    def add(self, name: str, role: str | None = None, color=None, roughness=None, metallic=None) -> Material:
        """Define a material. Role/look come from (in order): explicit arguments,
        the device's material table, the built-in palette, the role default."""
        if not isinstance(name, str) or not name.strip():
            raise MaterialError(f"material name must be a non-empty string, got {name!r}")
        if name in self._by_name:
            raise MaterialError(f"material {name!r} already defined")
        entry = self._table.get(name, {})
        builtin = lookup(name)
        if role is None:
            role = entry.get("role") or (builtin.role if builtin is not None else None)
        if role is None:
            known = sorted(set(self._table) | set(PALETTE_NAMES))
            raise MaterialError(
                f"material {name!r} is not in the material table or the built-in palette; "
                f"give role= (one of {ROLES}) or add it to the table. Known names: {known}"
            )
        if role not in ROLES:
            raise MaterialError(f"unknown material role {role!r}; expected one of {ROLES}")
        spec = builtin or ROLE_DEFAULTS[role]
        if color is None:
            color = entry.get("color", spec.color)
        try:
            color = parse_color(color)
        except ValueError as e:
            raise MaterialError(str(e)) from e
        render = {
            "roughness": float(roughness if roughness is not None else entry.get("roughness", spec.roughness)),
            "metallic": float(metallic if metallic is not None else entry.get("metallic", spec.metallic)),
            "transmission": float(entry.get("transmission", spec.transmission)),
        }
        material = Material(
            id=len(self._order), name=name, role=role, color=color, render=render, _registry_id=self._id
        )
        self._by_name[name] = material
        self._order.append(material)
        return material

    def resolve(self, ref: Material | str) -> Material:
        if isinstance(ref, Material):
            if ref._registry_id != self._id:
                raise MaterialError(f"{ref!r} belongs to a different device")
            return ref
        if isinstance(ref, str):
            try:
                return self._by_name[ref]
            except KeyError:
                raise MaterialError(
                    f"unknown material {ref!r}; defined: {list(self._by_name)}"
                ) from None
        raise MaterialError(f"expected a Material or material name, got {ref!r}")

    def __iter__(self) -> Iterator[Material]:
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, ref) -> bool:
        try:
            self.resolve(ref)
        except MaterialError:
            return False
        return True
