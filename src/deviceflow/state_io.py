"""Encode and decode a process state as a self-describing archive.

The archive stores geometry, not a recipe: decoding it reconstructs the state
directly and never re-runs a process operation. That is what makes restarting
cheap and what keeps a restored device independent of the steps that produced
it.

Layout of the archive, a plain ZIP written with the standard library:

``manifest.json``
    Format name and version, the kernel version that wrote it, units, the
    device window, grid, the material table, the z intervals with the
    material regions of each, and one entry per stored geometry with its
    size, digest and vertex count. Caller metadata is carried verbatim under
    its own key and is never interpreted here.
``geometry/<digest>.wkb``
    One well-known-binary polygon collection per distinct geometry. Identical
    geometry is stored once however many slabs and materials share it.

Decoding is defensive because an archive may come from another machine: every
declared limit is enforced before allocation, entry names are checked rather
than trusted, digests are verified, and coordinates must be finite. Pickle is
deliberately not used, so decoding cannot execute code.

Materials are rebuilt into a single registry, because the state keys its
regions by material identity: every slab that referred to one material before
encoding refers to one object after decoding.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import shapely
from shapely.geometry import MultiPolygon

from ._internal.geometry.state import ProcessState, Slab, znorm
from .exceptions import GeometryError
from .material import Material, MaterialRegistry

FORMAT = "deviceflow-state"

#: Incompatible changes raise the major version; an archive with a newer major
#: version is rejected rather than guessed at. Additive changes raise the minor
#: version and stay readable by an older decoder.
VERSION_MAJOR = 1
VERSION_MINOR = 0

_MANIFEST_NAME = "manifest.json"
_GEOMETRY_PREFIX = "geometry/"
_GEOMETRY_SUFFIX = ".wkb"
_DIGEST_LENGTH = 64


@dataclass(frozen=True)
class Limits:
    """Ceilings enforced while decoding, before anything large is allocated."""

    max_archive_bytes: int = 256 * 1024 * 1024
    max_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_entries: int = 100_000
    max_manifest_bytes: int = 64 * 1024 * 1024
    max_slabs: int = 100_000
    max_materials: int = 4_096
    max_geometries: int = 200_000
    max_vertices_per_geometry: int = 20_000_000
    max_total_vertices: int = 200_000_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if getattr(self, name) <= 0:
                raise ValueError(f"limit {name} must be positive")


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class RestoredState:
    """What :func:`decode_state` returns: geometry, its materials, metadata."""

    state: ProcessState
    materials: MaterialRegistry
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: tuple[int, int] = (VERSION_MAJOR, VERSION_MINOR)
    kernel_version: str = ""
    conformal_resolution: float | None = None


class StateFormatError(GeometryError):
    """An archive is malformed, truncated, altered, or outside the limits."""


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _material_entry(material: Material) -> dict[str, Any]:
    render = material.render or {}
    return {
        "id": int(material.id),
        "name": material.name,
        "role": material.role,
        "color": [float(channel) for channel in material.color],
        "roughness": float(render.get("roughness", 0.5)),
        "metallic": float(render.get("metallic", 0.0)),
        "transmission": float(render.get("transmission", 0.0)),
    }


def encode_state(
    state: ProcessState,
    materials: MaterialRegistry,
    *,
    metadata: Mapping[str, Any] | None = None,
    conformal_resolution: float | None = None,
) -> bytes:
    """Serialize ``state`` and its materials into an archive.

    ``metadata`` is stored verbatim for the caller's own bookkeeping and is
    never read back by the kernel. ``conformal_resolution`` is recorded when
    given so a restored device can be rebuilt with the same accuracy setting.
    """
    used: dict[Material, None] = {}
    for slab in state.slabs:
        for material in slab.regions:
            used[material] = None
    unknown = [material for material in used if material not in materials]
    if unknown:
        raise StateFormatError(f"state uses materials outside the registry: {unknown}")

    geometries: dict[str, bytes] = {}
    geometry_meta: dict[str, dict[str, Any]] = {}
    slabs: list[dict[str, Any]] = []
    for slab in state.slabs:
        regions: dict[str, str] = {}
        for material, geometry in slab.regions.items():
            payload = shapely.to_wkb(geometry, flavor="iso", include_srid=False)
            key = _digest(payload)
            if key not in geometries:
                geometries[key] = payload
                geometry_meta[key] = {
                    "bytes": len(payload),
                    "vertices": int(shapely.get_num_coordinates(geometry)),
                }
            regions[str(material.id)] = key
        slabs.append({"z0": float(slab.z0), "z1": float(slab.z1), "regions": regions})

    manifest: dict[str, Any] = {
        "format": FORMAT,
        "version": {"major": VERSION_MAJOR, "minor": VERSION_MINOR},
        "kernelVersion": _kernel_version(),
        "units": {"length": "um", "volume": "um3", "angle": "degree"},
        "bounds": [float(value) for value in state.bounds],
        "grid": float(state.grid),
        "materials": [_material_entry(materials.resolve(material)) for material in used],
        "slabs": slabs,
        "geometries": {
            key: {"file": f"{_GEOMETRY_PREFIX}{key}{_GEOMETRY_SUFFIX}", **meta}
            for key, meta in geometry_meta.items()
        },
        "metadata": dict(metadata or {}),
    }
    if conformal_resolution is not None:
        manifest["conformalResolution"] = float(conformal_resolution)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(_MANIFEST_NAME, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        for key, payload in sorted(geometries.items()):
            archive.writestr(f"{_GEOMETRY_PREFIX}{key}{_GEOMETRY_SUFFIX}", payload)
    return buffer.getvalue()


def _kernel_version() -> str:
    from . import __version__

    return str(__version__)


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise StateFormatError(message)


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exception:
        raise StateFormatError(f"{label} must be a number") from exception
    if not np.isfinite(number):
        raise StateFormatError(f"{label} must be finite")
    return number


def _check_entry_name(name: str) -> str:
    """Accept only the two shapes this format defines; never trust a path."""
    if name == _MANIFEST_NAME:
        return name
    _require(
        name.startswith(_GEOMETRY_PREFIX) and name.endswith(_GEOMETRY_SUFFIX),
        f"unexpected archive entry {name!r}",
    )
    key = name[len(_GEOMETRY_PREFIX) : -len(_GEOMETRY_SUFFIX)]
    _require(
        len(key) == _DIGEST_LENGTH and all(character in "0123456789abcdef" for character in key),
        f"archive entry {name!r} is not named by a digest",
    )
    return name


def decode_state(data: bytes, *, limits: Limits | None = None) -> RestoredState:
    """Rebuild a state from an archive without running any process operation.

    Raises :class:`StateFormatError` for anything malformed, altered, or over
    the limits. A successful decode leaves the state satisfying the kernel's
    own invariants, which are checked before it is returned.
    """
    limits = limits or DEFAULT_LIMITS
    _require(isinstance(data, (bytes, bytearray)), "archive must be bytes")
    _require(
        len(data) <= limits.max_archive_bytes,
        f"archive is {len(data)} bytes, over the {limits.max_archive_bytes} byte limit",
    )
    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except zipfile.BadZipFile as exception:
        raise StateFormatError("archive is not readable") from exception

    with archive:
        entries = archive.infolist()
        _require(
            len(entries) <= limits.max_entries,
            f"archive has {len(entries)} entries, over the {limits.max_entries} limit",
        )
        total = 0
        for entry in entries:
            _check_entry_name(entry.filename)
            _require(not entry.is_dir(), f"archive entry {entry.filename!r} is a directory")
            total += entry.file_size
            _require(
                total <= limits.max_uncompressed_bytes,
                "archive expands beyond the uncompressed size limit",
            )
        names = {entry.filename for entry in entries}
        _require(_MANIFEST_NAME in names, "archive has no manifest")

        manifest_info = archive.getinfo(_MANIFEST_NAME)
        _require(
            manifest_info.file_size <= limits.max_manifest_bytes,
            "manifest is larger than the limit",
        )
        try:
            manifest = json.loads(archive.read(_MANIFEST_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exception:
            raise StateFormatError("manifest is not valid JSON") from exception
        _require(isinstance(manifest, dict), "manifest must be an object")

        _require(manifest.get("format") == FORMAT, f"not a {FORMAT} archive")
        version = manifest.get("version")
        _require(isinstance(version, dict), "manifest version must be an object")
        major = version.get("major")
        minor = version.get("minor")
        _require(isinstance(major, int) and isinstance(minor, int), "version must be integers")
        _require(
            major == VERSION_MAJOR,
            f"archive major version {major} cannot be read by this kernel (expects {VERSION_MAJOR})",
        )

        bounds = manifest.get("bounds")
        _require(isinstance(bounds, list) and len(bounds) == 4, "bounds must be four numbers")
        x0, y0, x1, y1 = (_finite(value, "bounds") for value in bounds)
        _require(x1 > x0 and y1 > y0, "bounds must have positive extent")
        grid = _finite(manifest.get("grid"), "grid")
        _require(grid > 0, "grid must be positive")

        registry, by_id = _restore_materials(manifest.get("materials"), limits)
        geometries = _restore_geometries(archive, manifest.get("geometries"), limits)
        slabs = _restore_slabs(manifest.get("slabs"), by_id, geometries, limits)

    state = ProcessState(bounds=(x0, y0, x1, y1), grid=grid)
    state._slabs = slabs
    try:
        state.validate()
    except GeometryError as exception:
        raise StateFormatError(f"restored state is not valid: {exception}") from exception

    metadata = manifest.get("metadata")
    resolution = manifest.get("conformalResolution")
    if resolution is not None:
        resolution = _finite(resolution, "conformalResolution")
        _require(resolution > 0, "conformalResolution must be positive")
    return RestoredState(
        state=state,
        materials=registry,
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        version=(major, minor),
        kernel_version=str(manifest.get("kernelVersion", "")),
        conformal_resolution=resolution,
    )


def _restore_materials(
    entries: Any, limits: Limits
) -> tuple[MaterialRegistry, dict[str, Material]]:
    """Rebuild one registry so identical materials are one object again."""
    _require(isinstance(entries, list), "materials must be a list")
    _require(
        len(entries) <= limits.max_materials,
        f"archive declares {len(entries)} materials, over the {limits.max_materials} limit",
    )
    ordered = []
    for entry in entries:
        _require(isinstance(entry, dict), "each material must be an object")
        name = entry.get("name")
        role = entry.get("role")
        _require(isinstance(name, str) and name.strip(), "material name must be a non-empty string")
        _require(isinstance(role, str) and role, "material role must be a string")
        colour = entry.get("color")
        _require(
            isinstance(colour, list) and len(colour) == 4,
            f"material {name!r} must carry a four-channel colour",
        )
        ordered.append(
            {
                "id": entry.get("id"),
                "name": name,
                "role": role,
                "color": tuple(_finite(channel, f"material {name!r} colour") for channel in colour),
                "roughness": _finite(entry.get("roughness", 0.5), f"material {name!r} roughness"),
                "metallic": _finite(entry.get("metallic", 0.0), f"material {name!r} metallic"),
                "transmission": _finite(entry.get("transmission", 0.0), f"material {name!r} transmission"),
            }
        )
    names = [entry["name"] for entry in ordered]
    _require(len(set(names)) == len(names), "material names must be unique")

    # The table carries every appearance field, including transmission, which
    # add() only reads from a table. Adding in the recorded order reproduces
    # the original material ids.
    ordered.sort(key=lambda entry: (entry["id"] is None, entry["id"]))
    table = {
        entry["name"]: {
            "role": entry["role"],
            "color": list(entry["color"]),
            "roughness": entry["roughness"],
            "metallic": entry["metallic"],
            "transmission": entry["transmission"],
        }
        for entry in ordered
    }
    registry = MaterialRegistry(table)
    by_id: dict[str, Material] = {}
    for entry in ordered:
        material = registry.add(entry["name"])
        by_id[str(entry["id"])] = material
    return registry, by_id


def _restore_geometries(
    archive: zipfile.ZipFile, declared: Any, limits: Limits
) -> dict[str, MultiPolygon]:
    """Read, verify and parse every stored geometry exactly once."""
    _require(isinstance(declared, dict), "geometries must be an object")
    _require(
        len(declared) <= limits.max_geometries,
        f"archive declares {len(declared)} geometries, over the {limits.max_geometries} limit",
    )
    total_vertices = 0
    geometries: dict[str, MultiPolygon] = {}
    for key, entry in declared.items():
        _require(isinstance(entry, dict), f"geometry {key!r} entry must be an object")
        name = entry.get("file")
        _require(isinstance(name, str), f"geometry {key!r} has no file name")
        _check_entry_name(name)
        _require(
            name == f"{_GEOMETRY_PREFIX}{key}{_GEOMETRY_SUFFIX}",
            f"geometry {key!r} names a file that does not match its digest",
        )
        vertices = entry.get("vertices")
        _require(isinstance(vertices, int) and vertices >= 0, f"geometry {key!r} vertex count is invalid")
        _require(
            vertices <= limits.max_vertices_per_geometry,
            f"geometry {key!r} declares {vertices} vertices, over the per-geometry limit",
        )
        total_vertices += vertices
        _require(
            total_vertices <= limits.max_total_vertices,
            "archive declares more vertices in total than the limit allows",
        )
        try:
            payload = archive.read(name)
        except KeyError as exception:
            raise StateFormatError(f"geometry {key!r} is missing from the archive") from exception
        _require(_digest(payload) == key, f"geometry {key!r} does not match its digest")
        try:
            geometry = shapely.from_wkb(payload)
        except Exception as exception:  # shapely raises several types for bad input
            raise StateFormatError(f"geometry {key!r} is not readable") from exception
        _require(geometry is not None and not geometry.is_empty or geometry is not None, f"geometry {key!r} is empty")
        _require(
            isinstance(geometry, MultiPolygon),
            f"geometry {key!r} is {type(geometry).__name__}, expected MultiPolygon",
        )
        coordinates = shapely.get_coordinates(geometry)
        _require(bool(np.isfinite(coordinates).all()), f"geometry {key!r} has non-finite coordinates")
        _require(
            int(coordinates.shape[0]) == vertices,
            f"geometry {key!r} has {coordinates.shape[0]} vertices, manifest declares {vertices}",
        )
        geometries[key] = geometry
    return geometries


def _restore_slabs(
    declared: Any,
    by_id: Mapping[str, Material],
    geometries: Mapping[str, MultiPolygon],
    limits: Limits,
) -> list[Slab]:
    """Rebuild the z intervals, checking that they tile without gaps."""
    _require(isinstance(declared, list), "slabs must be a list")
    _require(
        len(declared) <= limits.max_slabs,
        f"archive declares {len(declared)} slabs, over the {limits.max_slabs} limit",
    )
    slabs: list[Slab] = []
    previous_top: float | None = None
    for index, entry in enumerate(declared):
        _require(isinstance(entry, dict), f"slab {index} must be an object")
        z0 = znorm(_finite(entry.get("z0"), f"slab {index} z0"))
        z1 = znorm(_finite(entry.get("z1"), f"slab {index} z1"))
        _require(z1 > z0, f"slab {index} must have positive thickness")
        if previous_top is not None:
            _require(
                z0 == previous_top,
                f"slab {index} starts at {z0} but the previous slab ends at {previous_top}",
            )
        previous_top = z1

        regions_entry = entry.get("regions")
        _require(isinstance(regions_entry, dict), f"slab {index} regions must be an object")
        regions: dict[Material, MultiPolygon] = {}
        for material_id, geometry_key in regions_entry.items():
            material = by_id.get(str(material_id))
            _require(material is not None, f"slab {index} names unknown material {material_id!r}")
            _require(
                isinstance(geometry_key, str) and geometry_key in geometries,
                f"slab {index} names unknown geometry {geometry_key!r}",
            )
            regions[material] = geometries[geometry_key]
        slabs.append(Slab(z0, z1, regions))
    return slabs
