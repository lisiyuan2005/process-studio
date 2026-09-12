"""Device: the public modelling interface.

Users describe fabrication operations (deposit, etch); the device keeps an
internal :class:`ProcessState` and never exposes polygon objects.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ._internal.geometry.state import ProcessState
from ._internal.mesh.builder import build_material_meshes
from ._internal.mesh.validate import validate_material_mesh
from .exceptions import GeometryError, MaskError, MeshError, ProcessError
from .export.blender import VIEWS, render_glb
from .export.glb import export_glb
from .mask import Mask, MaskFactory
from .material import Material, MaterialRegistry
from .process.cmp import planarize
from .process.conformal import deposit_conformal
from .process.isotropic_etch import etch_isotropic
from .process.planar import deposit_planar
from .process.square import deposit_square
from .process.vertical_etch import etch_vertical
from .reports import GeometryReport, MeshReport
from .units import format_length, parse_length, parse_rate, parse_time
from .view import CrossSection, TopView

DEFAULT_GRID = "1e-6um"
DEFAULT_CONFORMAL_RESOLUTION = "2nm"


class Device:
    def __init__(
        self,
        name: str,
        bounds,
        units: str = "um",
        grid=DEFAULT_GRID,
        conformal_resolution=DEFAULT_CONFORMAL_RESOLUTION,
        xy_resolution=None,
        materials=None,
        record_steps=False,
        step_options: dict | None = None,
        verbose: bool = True,
    ):
        """``materials``: optional table {name: {role, color, roughness, metallic}}
        (dict or .json/.toml path) consulted by :meth:`material`.
        ``record_steps``: ``True`` keeps a snapshot of the geometry after every
        process step (see :meth:`snapshot`, :meth:`export_steps`); a directory
        path additionally exports each snapshot into ``<dir>/<NN_op_material>/``
        as soon as the step is done, with ``step_options`` (the
        :meth:`export_all` options; default: sections, top view and GLB, no
        renders). ``verbose``: print a line when each process step and export
        stage starts."""
        if units != "um":
            raise ProcessError(f"only units='um' is supported, got {units!r}")
        self.name = str(name)
        self.units = "um"
        try:
            x0, y0, x1, y1 = (parse_length(v) for v in bounds)
        except (TypeError, ValueError) as e:
            raise ProcessError(f"bounds must be (x0, y0, x1, y1), got {bounds!r}") from e
        if not (x1 > x0 and y1 > y0):
            raise ProcessError(f"bounds must satisfy x1 > x0 and y1 > y0, got {bounds!r}")
        self.grid = parse_length(grid)
        if self.grid <= 0:
            raise ProcessError(f"grid must be positive, got {grid!r}")
        self.conformal_resolution = parse_length(conformal_resolution)
        if self.conformal_resolution <= 0:
            raise ProcessError(f"conformal_resolution must be positive, got {conformal_resolution!r}")
        # The XY arc sagitta, when it is not simply the z step.
        self.xy_resolution = None if xy_resolution is None else parse_length(xy_resolution)
        if self.xy_resolution is not None and self.xy_resolution <= 0:
            raise ProcessError(f"xy_resolution must be positive, got {xy_resolution!r}")

        self._materials = MaterialRegistry(materials)
        self.masks = MaskFactory(grid=self.grid)
        self._state = ProcessState(bounds=(x0, y0, x1, y1), grid=self.grid)
        self._history: list[dict] = []
        self._meshes = None  # built lazily, invalidated by every process op
        self.record_steps = bool(record_steps)
        self._step_dir = None if isinstance(record_steps, bool) or not record_steps else Path(record_steps)
        self._step_options = {"render": False, **(step_options or {})}
        self.verbose = bool(verbose)
        self._snapshots: list[tuple[str, ProcessState, list[dict]]] = []

    # -- setup ------------------------------------------------------------

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self._state.bounds

    def material(self, name: str, role: str | None = None, color=None, roughness=None, metallic=None) -> Material:
        """Define a material; role/colour default from the device's material
        table or the built-in palette (``role`` is required for unknown names)."""
        return self._materials.add(name, role=role, color=color, roughness=roughness, metallic=metallic)

    @property
    def materials(self) -> list[Material]:
        return list(self._materials)

    # -- process operations ----------------------------------------------

    def deposit(self, material, thickness, mode: str, *, square: bool = False) -> None:
        """Deposit ``thickness`` of ``material``. ``mode`` is "planar" or "conformal".

        ``square`` picks the simplified film: square corners, one sample
        between the planes of the stack, no rounding; see ``process.square``.
        """
        mat = self._materials.resolve(material)
        t = parse_length(thickness)
        if t <= 0:
            raise ProcessError(f"deposition thickness must be positive, got {thickness!r}")
        if mode not in ("planar", "conformal"):
            raise ProcessError(f"unknown deposition mode {mode!r}; expected 'planar' or 'conformal'")
        t_start = self._begin(f"deposit {mat.name} {format_length(t)} {mode}")
        # transactional: work on a copy, commit only after validation succeeded
        state = self._state.copy()
        if mode == "planar":
            before = state.volume(mat)
            z0, z1 = deposit_planar(
                state, mat, t, self.conformal_resolution, self.xy_resolution, square=square
            )
            volume = state.volume(mat) - before
        elif square:
            z0, z1, volume = deposit_square(
                state, mat, t, self.xy_resolution or self.conformal_resolution
            )
        else:
            z0, z1, volume = deposit_conformal(
                state, mat, t, self.conformal_resolution, self.xy_resolution
            )
        self._state = state
        self._meshes = None
        self._history.append(
            {
                "op": "deposit",
                "mode": mode,
                "square": bool(square),
                "material": mat.name,
                "thickness": format_length(t),
                "z0": z0,
                "z1": z1,
                "volume": volume,
            }
        )
        self._end(t_start)
        self._after_step()

    def etch(
        self,
        mask=None,
        target=None,
        depth=None,
        *,
        rates=None,
        time=None,
        selectivity=None,
        reference=None,
        overetch=None,
    ) -> None:
        """Vertical etch through ``mask`` (``None``: the whole window, a
        blanket etch-back). Three equivalent forms:

        * ``etch(mask, target=M or [M, N], depth="500nm")`` — listed materials
          etch at equal rate; ``depth`` is removed from each (in sequence).
        * ``etch(mask, rates={M: "10nm/s", N: "2nm/s"}, time="50s")``.
        * ``etch(mask, depth="500nm", selectivity={M: 1.0, N: 0.2}, reference=M)``
          — ``depth`` refers to ``reference`` (default: first key).

        Unlisted materials have rate 0 and act as etch stops. ``overetch``
        extends the time budget: ``"20%"``, a time (``"5s"``) or, for the
        depth forms, a length.
        """
        mask = self._mask_or_window(mask, "etch")
        rate_map, budget, unit, ref = self._resolve_etch(target, depth, rates, time, selectivity, reference, overetch)
        opening = mask.clip(self.bounds)
        if opening.is_empty:
            raise MaskError("mask does not overlap the device bounds")
        t_start = self._begin(f"etch {self._etch_text(rate_map, budget, ref, target, depth, rates, selectivity)}, {self._mask_text(mask, opening)}")
        state = self._state.copy()  # transactional: commit only after success
        removed = etch_vertical(state, opening._geom, rate_map, budget)
        self._state = state
        self._meshes = None
        step = {"op": "etch", "profile": "vertical", "mask_area": round(opening.area, 9)}
        step.update(self._etch_record(removed, rate_map, budget, ref, target, depth, rates, selectivity, overetch))
        self._history.append(step)
        self._end(t_start)
        self._after_step()

    def wet_etch(
        self,
        mask=None,
        target=None,
        depth=None,
        *,
        rates=None,
        time=None,
        selectivity=None,
        reference=None,
        overetch=None,
        square: bool = False,
    ) -> None:
        """Isotropic (wet) etch: every exposed surface of the listed materials
        recedes by the etch depth in all directions (undercut included).
        Same parameter table as :meth:`etch`: ``mask`` (``None``: the whole
        window, i.e. every exposed surface), ``target``+``depth``,
        ``rates``+``time``, ``selectivity``+``depth``, ``overetch``. Unlisted
        materials are not etched. The window boundaries and the device floor
        are not surfaces (the wafer and the inert substrate continue there);
        a mask restricts where the etch starts to the mask column.
        """
        blanket = mask is None
        mask = self._mask_or_window(mask, "wet_etch")
        rate_map, budget, unit, ref = self._resolve_etch(target, depth, rates, time, selectivity, reference, overetch)
        opening = mask.clip(self.bounds)
        if opening.is_empty:
            raise MaskError("mask does not overlap the device bounds")
        depths = {m: r * budget for m, r in rate_map.items() if r > 0}
        t_start = self._begin(f"wet_etch {self._etch_text(rate_map, budget, ref, target, depth, rates, selectivity)}, {self._mask_text(None if blanket else mask, opening)}")
        state = self._state.copy()  # transactional: commit only after success
        removed = etch_isotropic(
            state, depths, self.conformal_resolution, None if blanket else opening._geom,
            self.xy_resolution, square=square,
        )
        removed = {m: removed.get(m, 0.0) for m in rate_map}
        self._state = state
        self._meshes = None
        step = {
            "op": "wet_etch", "profile": "isotropic", "square": bool(square),
            "mask_area": round(opening.area, 9),
        }
        step.update(self._etch_record(removed, rate_map, budget, ref, target, depth, rates, selectivity, overetch))
        self._history.append(step)
        self._end(t_start)
        self._after_step()

    def cmp(self, height) -> None:
        """Chemical-mechanical polishing: remove everything above ``height``
        (measured from the device floor, z = 0), whatever the material."""
        h = parse_length(height)
        if self._state.floor is None:
            raise ProcessError("cmp on an empty device: nothing to polish")
        if h <= self._state.floor:
            raise ProcessError(f"cmp height {height!r} must lie above the device floor")
        t_start = self._begin(f"cmp to {format_length(h)}")
        state = self._state.copy()  # transactional
        removed = planarize(state, h)
        self._state = state
        self._meshes = None
        self._history.append({
            "op": "cmp",
            "height": format_length(h),
            "removed": {m.name: round(v, 12) for m, v in removed.items()},
            "removed_volume": round(sum(removed.values()), 12),
        })
        self._end(t_start)
        self._after_step()

    def _mask_or_window(self, mask, verb: str) -> Mask:
        """``None`` means the whole device window."""
        if mask is None:
            x0, y0, x1, y1 = self.bounds
            return self.masks.rectangle(corner=(x0, y0), size=(x1 - x0, y1 - y0))
        if not isinstance(mask, Mask):
            raise MaskError(f"{verb} needs a Mask or None, got {mask!r}")
        return mask

    def _resolve_etch(self, target, depth, rates, time, selectivity, reference, overetch):
        """Turn one of the three etch forms into (rate_map, budget, unit, reference)."""
        forms = sum(x is not None for x in (target, rates, selectivity))
        if forms != 1:
            raise ProcessError("etch needs exactly one of target=, rates= or selectivity=")

        if target is not None:
            if depth is None or time is not None:
                raise ProcessError("target= needs depth= (and no time=)")
            names = list(target) if isinstance(target, (list, tuple)) else [target]
            if not names:
                raise ProcessError("etch needs at least one target material")
            rate_map = {self._materials.resolve(n): 1.0 for n in names}
            budget = parse_length(depth)
            ref = None
            unit = "depth"
        elif rates is not None:
            if time is None or depth is not None:
                raise ProcessError("rates= needs time= (and no depth=)")
            rate_map = {self._materials.resolve(n): parse_rate(r) for n, r in rates.items()}
            budget = parse_time(time)
            ref = None
            unit = "time"
        else:
            if depth is None or time is not None:
                raise ProcessError("selectivity= needs depth= (and no time=)")
            rate_map = {self._materials.resolve(n): float(s) for n, s in selectivity.items()}
            if any(s < 0 for s in rate_map.values()):
                raise ProcessError("selectivity values must be non-negative")
            keys = list(rate_map)
            if not keys:
                raise ProcessError("selectivity= needs at least one material")
            ref = self._materials.resolve(reference) if reference is not None else keys[0]
            if ref not in rate_map or rate_map[ref] <= 0:
                raise ProcessError(f"reference material {ref.name} needs a positive selectivity")
            budget = parse_length(depth) / rate_map[ref]
            unit = "depth"

        if not rate_map:
            raise ProcessError("etch needs at least one material")
        if all(r <= 0 for r in rate_map.values()):
            raise ProcessError("etch needs at least one material with a positive rate")
        if budget <= 0:
            raise ProcessError("etch depth/time must be positive")
        budget = self._apply_overetch(budget, overetch, unit, rate_map, ref)

        return rate_map, budget, unit, ref

    @staticmethod
    def _etch_record(removed, rate_map, budget, ref, target, depth, rates, selectivity, overetch) -> dict:
        step = {
            "removed": {m.name: round(v, 12) for m, v in removed.items()},
            "removed_volume": round(sum(removed.values()), 12),
        }
        if target is not None:
            step["target"] = [m.name for m in rate_map]
            step["depth"] = format_length(parse_length(depth))
        elif rates is not None:
            step["rates"] = {m.name: r * 1e3 for m, r in rate_map.items()}  # nm/s
            step["time"] = budget  # s
        else:
            step["selectivity"] = {m.name: r for m, r in rate_map.items()}
            step["reference"] = ref.name
            step["depth"] = format_length(parse_length(depth))
            step["budget"] = budget  # depth-equivalent in the reference material, incl. overetch
        if overetch is not None:
            step["overetch"] = overetch
        return step

    # -- snapshots (process-step outputs) --------------------------------

    def _after_step(self) -> None:
        if not self.record_steps:
            return
        name = self.snapshot()
        if self._step_dir is not None:
            out = self._step_dir / name
            self._log(f"snapshot {name} -> {out}")
            self.at(name).export_all(out, **self._step_options)

    def snapshot(self, name: str | None = None) -> str:
        """Keep the current geometry under ``name`` (default: step number,
        operation and materials, e.g. ``03_etch_W_SiO2``). Returns the name."""
        if name is None:
            n = len(self._history)
            if n == 0:
                name = "00_empty"
            else:
                step = self._history[-1]
                mats = [step["material"]] if step["op"] == "deposit" else list(step.get("removed", {}))
                name = "_".join([f"{n:02d}", step["op"], *mats])
        name = str(name)
        if name in self.snapshots:
            raise ProcessError(f"snapshot {name!r} already exists")
        self._snapshots.append((name, self._state.copy(), [dict(h) for h in self._history]))
        return name

    @property
    def snapshots(self) -> list[str]:
        return [n for n, _, _ in self._snapshots]

    def at(self, name: str) -> "Device":
        """A device holding the geometry of snapshot ``name`` (same materials,
        bounds and settings), usable for views and exports."""
        for n, state, history in self._snapshots:
            if n == name:
                return self._clone(state, history)
        raise ProcessError(f"unknown snapshot {name!r}; have {self.snapshots}")

    def _clone(self, state: ProcessState, history: list[dict]) -> "Device":
        other = Device.__new__(Device)
        other.name = self.name
        other.units = self.units
        other.grid = self.grid
        other.conformal_resolution = self.conformal_resolution
        other.xy_resolution = getattr(self, "xy_resolution", None)
        other._materials = self._materials
        other.masks = self.masks
        other._state = state.copy()
        other._history = [dict(h) for h in history]
        other._meshes = None
        other.record_steps = False
        other._step_dir = None
        other._step_options = dict(self._step_options)
        other.verbose = self.verbose
        other._snapshots = []
        return other

    def export_steps(self, out_dir, *, steps=None, **export_all_options) -> dict[str, dict[str, Path]]:
        """Export every snapshot (or the ones named in ``steps``) like a
        finished device, each into ``out_dir/<snapshot name>/``; options are
        those of :meth:`export_all`. Returns {snapshot name: {file: path}}."""
        names = self.snapshots if steps is None else [str(s) for s in steps]
        if not names:
            raise ProcessError("no snapshots to export: use record_steps=True or snapshot()")
        out = Path(out_dir)
        files: dict[str, dict[str, Path]] = {}
        for name in names:
            self._log(f"snapshot {name} -> {out / name}")
            files[name] = self.at(name).export_all(out / name, **export_all_options)
        return files

    # -- progress output ------------------------------------------------

    def _log(self, text: str) -> None:
        if self.verbose:
            print(f"[deviceflow] {text}", flush=True)

    def _begin(self, text: str) -> float:
        """Announce a process step (numbered as it will appear in the history)."""
        self._log(f"step {len(self._history) + 1}: {text}")
        return time.perf_counter()

    def _end(self, t_start: float) -> None:
        self._log(f"  done {time.perf_counter() - t_start:.1f} s")

    @staticmethod
    def _etch_text(rate_map, budget, ref, target, depth, rates, selectivity) -> str:
        names = ", ".join(m.name for m in rate_map)
        if target is not None:
            return f"{names} depth {format_length(parse_length(depth))}"
        if rates is not None:
            return f"{names} rates " + ", ".join(f"{m.name} {r * 1e3:g}nm/s" for m, r in rate_map.items()) + f" time {budget:g}s"
        return f"{names} depth {format_length(parse_length(depth))} in {ref.name} (selectivity)"

    @staticmethod
    def _mask_text(mask, opening) -> str:
        if mask is None:
            return f"whole window {opening.area:g} um^2"
        return f"mask {opening.area:g} um^2"

    @staticmethod
    def _apply_overetch(budget, overetch, unit, rate_map, ref):
        if overetch is None:
            return budget
        if isinstance(overetch, str) and overetch.strip().endswith("%"):
            pct = float(overetch.strip()[:-1])
            if pct < 0:
                raise ProcessError("overetch percentage must be non-negative")
            return budget * (1 + pct / 100)
        if unit == "time":
            return budget + parse_time(overetch)
        extra = parse_length(overetch)
        if extra < 0:
            raise ProcessError("overetch must be non-negative")
        return budget + (extra / rate_map[ref] if ref is not None else extra)

    # -- mesh / export ----------------------------------------------------

    def build_mesh(self) -> dict[str, "trimesh.Trimesh"]:
        """Build one triangle mesh per material from the finished geometry."""
        self._state.validate()
        meshes = build_material_meshes(self._state)
        if not meshes:
            raise MeshError("device has no geometry to mesh")
        self._meshes = meshes
        return {m.name: mesh for m, mesh in meshes.items()}

    def validate_mesh(self) -> MeshReport:
        if self._meshes is None:
            self.build_mesh()
        zs = self._state.z_planes
        reports = {
            m.name: validate_material_mesh(
                m.name, mesh, self._state.volume(m), grid=self.grid, bounds=self.bounds,
                z_range=(zs[0], zs[-1]) if zs else None,
            )
            for m, mesh in self._meshes.items()
        }
        return MeshReport(valid=all(r.valid for r in reports.values()), materials=reports)

    def export_glb(self, path) -> MeshReport:
        """Validate the mesh and write a GLB; refuses to export an invalid mesh."""
        report = self.validate_mesh()
        if not report.valid:
            raise MeshError(f"refusing to export an invalid mesh:\n{report}")
        export_glb(path, self._meshes, self.name, self._history)
        return report

    def export_blender(
        self,
        blend=None,
        png=None,
        *,
        view: str = "iso",
        samples: int = 32,
        resolution=(1200, 900),
        hide=(),
        transparent=None,
        background=(0.85, 0.85, 0.85),
        film_transparent: bool = False,
        hdri=None,
        outline: bool = False,
        debug_normals: bool = False,
        view_transform: str = "Khronos PBR Neutral",
        exposure: float = 0.0,
        lights: dict | None = None,
        blender=None,
        glb=None,
    ) -> dict:
        """Render the validated GLB in headless Blender (materials, camera,
        lights, PNG, .blend). Blender performs no geometry operation; the
        returned report lists each object's (empty) modifier stack.

        ``hide``: material names not rendered; ``transparent``: {material: alpha}
        (back faces of transparent shells are invisible, so interfaces resolve to
        the opaque neighbour); ``outline``: compositor edge lines; ``debug_normals``:
        red = back face seen by the camera; ``hdri``: optional environment image;
        ``lights``: {"key", "fill", "headlight", "ambient"} strengths.
        ``glb``: where to keep the intermediate GLB (default: temporary).
        """
        if blend is None and png is None:
            raise ProcessError("export_blender needs blend= and/or png=")
        if view not in VIEWS:
            raise ProcessError(f"unknown view {view!r}; expected one of {VIEWS}")
        for name in list(hide) + list((transparent or {}).keys()):
            self._materials.resolve(name)
        if glb is None:
            tmp = tempfile.TemporaryDirectory(prefix="deviceflow_glb_")
            glb_path = Path(tmp.name) / f"{self.name}.glb"
        else:
            tmp = None
            glb_path = Path(glb)
        try:
            report = self.validate_mesh()
            if not report.valid:
                raise MeshError(f"refusing to render an invalid mesh:\n{report}")
            export_glb(glb_path, self._meshes, self.name, self._history, xray=tuple((transparent or {}).keys()))
            return render_glb(
                glb_path,
                blend=blend,
                png=png,
                view=view,
                samples=samples,
                resolution=resolution,
                hide=hide,
                transparent=transparent,
                background=background,
                film_transparent=film_transparent,
                hdri=hdri,
                outline=outline,
                debug_normals=debug_normals,
                view_transform=view_transform,
                exposure=exposure,
                lights=lights,
                blender=blender,
            )
        finally:
            if tmp is not None:
                tmp.cleanup()

    def export_all(
        self,
        out_dir,
        *,
        sections: dict | None = None,
        top_view: bool = True,
        glb: bool = True,
        renders: dict | None = None,
        render: bool = True,
        samples: int = 64,
        resolution=(1200, 900),
        z_scale: float = 1.0,
    ) -> dict[str, Path]:
        """Write every deliverable into ``out_dir`` and return {name: path}.

        Default sections: A-A' along x and B-B' along y through the device
        centre; pass ``sections={"CC": ((x0, y0), (x1, y1)), ...}`` to choose.
        ``renders``: {name: options} -> ``render_<name>.png`` each, where
        options are ``export_blender`` keywords (view, hide, transparent,
        outline, background, ...); default one iso render. ``render=False``
        skips Blender entirely. ``report.txt`` holds the geometry + mesh
        validation and the full process history.
        """
        out = Path(out_dir)
        geometry = self.validate_geometry()
        if not geometry.valid:
            raise GeometryError(str(geometry))
        if renders is None:
            renders = {"iso": {"view": "iso"}}
        if glb or (render and renders):
            self._log(f"mesh: building and validating ({len(self._materials)} materials)")
            mesh = self.validate_mesh()
        else:
            mesh = None
        if mesh is not None and not mesh.valid:
            raise MeshError(str(mesh))
        out.mkdir(parents=True, exist_ok=True)
        files: dict[str, Path] = {}

        x0, y0, x1, y1 = self.bounds
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if sections is None:
            sections = {"AA": ((x0, cy), (x1, cy)), "BB": ((cx, y0), (cx, y1))}
        lines = []
        for label, (start, end) in sections.items():
            self._log(f"section {label}: {start} -> {end}")
            section = self.cross_section(start=start, end=end)
            files[f"{label}.svg"] = section.export_svg(out / f"{label}.svg", z_scale=z_scale)
            try:
                files[f"{label}.png"] = section.export_png(out / f"{label}.png", z_scale=z_scale)
            except ProcessError:
                pass  # no matplotlib: SVG only
            lines.append(f"section {label}: {section.start} -> {section.end}, length {section.length:.6g} um")
        if top_view:
            self._log("top view")
            top = self.top_view()
            files["top.svg"] = top.export_svg(out / "top.svg")
            try:
                files["top.png"] = top.export_png(out / "top.png")
            except ProcessError:
                pass
            lines.append("top view areas: " + ", ".join(f"{m} {top.area(m):.6g} um^2" for m in top.materials))
        if glb:
            self._log(f"glb: {out / (self.name + '.glb')}")
            files["glb"] = Path(export_glb(out / f"{self.name}.glb", self._meshes, self.name, self._history))
        if render:
            for label, options in renders.items():
                name = f"render_{label}.png"
                self._log(f"render {label}: {options}")
                opts = {"samples": samples, "resolution": resolution, **options}
                self.export_blender(png=out / name, **opts)
                files[name] = out / name

        report = [str(geometry), "", str(mesh) if mesh is not None else "mesh: not built", "", *lines, "", "history:"]
        report += [f"  {i}: {step}" for i, step in enumerate(self._history)]
        self._log(f"report: {out / 'report.txt'}")
        files["report.txt"] = out / "report.txt"
        files["report.txt"].write_text("\n".join(report) + "\n")
        return files

    # -- scientific views -------------------------------------------------

    def cross_section(self, start, end) -> CrossSection:
        """Section along the line start -> end (XY, um or unit strings)."""
        s = tuple(parse_length(v) for v in start)
        e = tuple(parse_length(v) for v in end)
        return CrossSection(self._state, s, e, list(self._materials))

    def top_view(self) -> TopView:
        return TopView(self._state, list(self._materials))

    # -- inspection -------------------------------------------------------

    def material_at(self, x, y, z) -> Material | None:
        """Solid material at a point, or None for void."""
        return self._state.material_at(parse_length(x), parse_length(y), parse_length(z))

    @property
    def history(self) -> list[dict]:
        return [dict(step) for step in self._history]

    @property
    def top(self) -> float | None:
        return self._state.top

    def volume(self, material) -> float:
        return self._state.volume(self._materials.resolve(material))

    def validate_geometry(self) -> GeometryReport:
        errors: list[str] = []
        try:
            self._state.validate()
        except GeometryError as e:
            errors.append(str(e))
        return GeometryReport(
            valid=not errors,
            errors=tuple(errors),
            num_slabs=len(self._state.slabs),
            z_planes=tuple(self._state.z_planes),
            volumes={m.name: self._state.volume(m) for m in self._materials},
        )

    def __repr__(self) -> str:
        return f"Device({self.name!r}, bounds={self.bounds}, steps={len(self._history)})"
