"""Read-only report objects returned by validation calls."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GeometryReport:
    valid: bool
    errors: tuple[str, ...] = ()
    num_slabs: int = 0
    z_planes: tuple[float, ...] = ()
    volumes: dict[str, float] = field(default_factory=dict)

    def __str__(self) -> str:
        head = "geometry OK" if self.valid else "geometry INVALID"
        lines = [f"{head}: {self.num_slabs} slabs, z planes {list(self.z_planes)}"]
        for name, v in self.volumes.items():
            lines.append(f"  {name}: {v:.6g} um^3")
        lines.extend(f"  ! {e}" for e in self.errors)
        return "\n".join(lines)


@dataclass(frozen=True)
class MaterialMeshReport:
    name: str
    watertight: bool
    winding_consistent: bool
    volume: float
    process_volume: float
    vertices: int
    faces: int
    euler_number: int
    duplicate_faces: int
    degenerate_faces: int
    vertex_manifold: bool = True
    self_intersections: int = 0
    components: int = 1
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def __str__(self) -> str:
        state = "OK" if self.valid else "INVALID"
        line = (
            f"{self.name}: {state} v={self.vertices} f={self.faces} "
            f"volume={self.volume:.9g} (process {self.process_volume:.9g}) euler={self.euler_number}"
        )
        return "\n".join([line] + [f"    ! {e}" for e in self.errors])


@dataclass(frozen=True)
class MeshReport:
    valid: bool
    materials: dict[str, MaterialMeshReport] = field(default_factory=dict)

    @property
    def watertight(self) -> bool:
        return all(m.watertight for m in self.materials.values())

    @property
    def manifold(self) -> bool:
        return all(
            m.watertight and m.winding_consistent and m.vertex_manifold and m.self_intersections == 0
            for m in self.materials.values()
        )

    def __str__(self) -> str:
        head = "mesh OK" if self.valid else "mesh INVALID"
        return "\n".join([head] + [f"  {m}" for m in self.materials.values()])
