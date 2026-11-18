"""ViennaPS 后端（ViennaPS 4.7 / ViennaLS 5.10）。

几何操作的实现方式：
- deposit_planar / deposit_stack：表面平整时用 MakePlane 直接加一层（精确、最快）。
- etch_masked：用圆柱或长方体 level set 做布尔差（理想方向性刻蚀，侧壁垂直）。
- cmp：用 z ≥ 截平面的长方体做布尔差。
- deposit_conformal：复制顶层 level set + IsotropicProcess 正速率推进。
- etch_directional：DirectionalProcess，方向 −z，负速率（只去除水平面）。
- etch_isotropic：IsotropicProcess，仅指定材料速率为 −1；只有与外界连通的表面会移动。

约定：ViennaPS 中速率为负表示刻蚀；推进速率取 1 nm/单位时间，时长 = 厚度。
"""
from __future__ import annotations

import os

import numpy as np

import viennals as ls
import viennaps as vps

from ..voxel import MaterialGrid

vps.setDimension(3)
D3, L3 = vps.d3, ls.d3
_BC = {"periodic": vps.BoundaryType.PERIODIC_BOUNDARY,
       "reflective": vps.BoundaryType.REFLECTIVE_BOUNDARY}
_SUB = vps.BooleanOperationType.RELATIVE_COMPLEMENT


def _mat(name: str):
    try:
        return getattr(vps.Material, name)
    except AttributeError:
        raise ValueError(f"ViennaPS 没有内建材料 {name!r}")


class ViennaPSBackend:
    @staticmethod
    def version() -> dict:
        return {"viennaps": vps.__version__, "viennals": ls.__version__}

    def __init__(self, recipe, grid: float):
        vps.Logger.setLogLevel("WARNING")
        n = int(os.environ.get("OMP_NUM_THREADS") or os.environ.get("NSLOTS") or 0)
        set_threads = getattr(vps, "setNumThreads", None) or getattr(vps._core, "setNumThreads", None)
        if n and set_threads:
            set_threads(n)
        self.threads = n
        self.r, self.g = recipe, grid
        d = recipe.domain
        self.x, self.y = d["x"], d["y"]
        sub = recipe.initial["substrate"]
        self.z_min = -float(sub["thickness"])
        self.z_top_out = float(d.get("z_top", 1000))
        bc = _BC[d["boundary"]]
        self.dom = D3.Domain(bounds=[self.x[0], self.x[1], self.y[0], self.y[1],
                                     self.z_min, self.z_top_out],
                             boundaryConditions=[bc, bc, vps.BoundaryType.INFINITE_BOUNDARY],
                             gridDelta=grid)
        D3.MakePlane(self.dom, 0.0, _mat(sub["material"]), False).apply()
        self.top = 0.0          # 当前最高面 z（表面平整时等于表面高度）
        self.flat = True        # 表面是否为水平平面

    # ------------------------------------------------------------------ 工具
    def _cut(self, geom):
        cutter = L3.Domain(self.dom.getGrid())
        mg = L3.MakeGeometry(cutter, geom)
        # 不加这一句时，贴着或跨越周期边界的长方体会让后续推进的顶层 level set 失效（2026-11-16 实测）
        mg.setIgnoreBoundaryConditions(True)
        mg.apply()
        self.dom.applyBooleanOperation(cutter, _SUB, True)

    def _process(self, model, duration):
        D3.Process(self.dom, model, float(duration)).apply()

    def _top_from_bbox(self):
        return float(self.dom.getBoundingBox()[1][2])

    def _wrap(self, lo, hi, axis):
        """把跨越周期边界的区间拆成区域内的若干段。"""
        a, b = (self.x if axis == 0 else self.y)
        L = b - a
        if hi - lo >= L:
            return [(a, b)]
        lo_w = a + (lo - a) % L
        hi_w = lo_w + (hi - lo)
        if hi_w <= b:
            return [(lo_w, hi_w)]
        return [(lo_w, b), (a, hi_w - L)]

    # ------------------------------------------------------------------ 操作
    def _planar(self, material, t):
        if not self.flat:
            raise ValueError("deposit_planar 只支持水平平整表面；当前表面有起伏，请改用 deposit_conformal")
        self.top += float(t)
        D3.MakePlane(self.dom, self.top, _mat(material), True).apply()

    def apply(self, step: dict) -> dict:
        top0 = self._top_from_bbox()
        info = self._apply(step)
        top1 = self._top_from_bbox()
        grow = step.get("thickness", 0) if step["op"].startswith("deposit_conformal") else 0
        if step["op"] == "deposit_stack" or step["op"] == "deposit_planar":
            grow = self.top - top0
        if not np.isfinite(top1) or top1 > top0 + grow + 2 * self.g or self.stats()["n_points"] == 0:
            raise RuntimeError(f"几何检查失败：步骤后表面最高点 z={top1}，步骤前 {top0}，"
                               f"允许增长 {grow} nm；level set 可能已损坏")
        return info

    def _apply(self, step: dict) -> dict:
        op = step["op"]
        info = {}
        if op == "deposit_planar":
            self._planar(step["material"], step["thickness"])
        elif op == "deposit_stack":
            n = 0
            for _ in range(step["repeat"]):
                for item in step["layers"]:
                    (m, t), = item.items()
                    self._planar(m, t)
                    n += 1
            if "cap" in step:
                (m, t), = step["cap"].items()
                self._planar(m, t)
                n += 1
            info["layers_added"] = n
        elif op == "deposit_conformal":
            self.dom.duplicateTopLevelSet(_mat(step["material"]))
            self._process(D3.IsotropicProcess(1.0), step["thickness"])
            self.flat = False
            self.top = self._top_from_bbox()
        elif op == "etch_masked":
            z_hi = self._top_from_bbox() + 4 * self.g
            z_lo = step["to_z"] if "to_z" in step else self.top - step["depth"]
            m = step["mask"]
            if m["shape"] == "circle":
                cx, cy = m["center"]
                self._cut(L3.Cylinder([cx, cy, z_lo], [0, 0, 1], z_hi - z_lo, m["radius"]))
            else:
                e = self.g   # 贴到区域边界的边向外延伸一格，避免边界处残留
                ext = lambda lo, hi, a, b: (lo - e if lo <= a else lo, hi + e if hi >= b else hi)
                for x0, x1 in self._wrap(*m["x"], 0):
                    for y0, y1 in self._wrap(*m["y"], 1):
                        x0, x1 = ext(x0, x1, *self.x)
                        y0, y1 = ext(y0, y1, *self.y)
                        self._cut(L3.Box([x0, y0, z_lo], [x1, y1, z_hi]))
            info["etch_bottom_z"] = z_lo
            self.flat = False
        elif op == "etch_directional":
            model = D3.DirectionalProcess([0.0, 0.0, -1.0], -1.0, 0.0, [], True)
            self._process(model, step["thickness"])
            self.flat = False
            self.top = self._top_from_bbox()
        elif op == "etch_isotropic":
            rates = {_mat(m): -1.0 for m in step["materials"]}
            self._process(D3.IsotropicProcess(rates, 0.0), step["thickness"])
            self.flat = False
            self.top = self._top_from_bbox()
        elif op == "cmp":
            z = float(step["z"])
            self._cut(L3.Box([self.x[0] - 4 * self.g, self.y[0] - 4 * self.g, z],
                             [self.x[1] + 4 * self.g, self.y[1] + 4 * self.g,
                              self._top_from_bbox() + 4 * self.g]))
            self.top, self.flat = z, True
        else:
            raise ValueError(f"ViennaPS 后端不支持操作 {op!r}")
        return info

    # ------------------------------------------------------------------ 状态
    def stats(self) -> dict:
        lsets = self.dom.getLevelSets()
        return {"n_levelsets": len(lsets),
                "n_points": int(sum(l.getNumberOfPoints() for l in lsets)),
                "surface_top_z": round(self._top_from_bbox(), 3)}

    def save_state(self, prefix):
        self.dom.saveLevelSets(str(prefix))
        self._state_files = [f"{prefix}_layer{i}.lvst" for i in range(len(self.dom.getLevelSets()))]

    def material_grid(self) -> MaterialGrid:
        """优先用自写 C++ 扩展 _lsvox 读取本步保存的 .lvst 文件做体素化；没有状态文件时退回 DenseCellSet。"""
        files = getattr(self, "_state_files", None)
        if files and len(files) == len(self.dom.getLevelSets()):
            import os
            if all(os.path.exists(f) for f in files):
                return self.material_grid_lsvox(files)
        return self.material_grid_densecellset()

    def material_grid_lsvox(self, files) -> MaterialGrid:
        """C++ 扩展 _lsvox：与 ToVoxelMesh 相同的判定规则（单元 8 个角点值之和 ≤ 0 为内部，
        自下而上第一层），按 z 层 OpenMP 并行，直接写入 uint8 数组，不经过 Python 列表。
        单元 (i,j,k) 的中心在 ((i+½)g, (j+½)g, (k+½)g)；周期边界下包含跨边界的最后一列/行。
        """
        from . import _lsvox
        g = self.g
        names = list(self.r.materials)
        id_of = {_builtin_id(m): k for k, m in enumerate(names, 1)}
        mm = self.dom.getMaterialMap()
        lut = np.zeros(len(files) + 1, dtype=np.uint8)
        for i in range(mm.size()):
            b = int(mm.getMaterialAtIdx(i).builtIn())
            if b not in id_of:
                raise RuntimeError(f"level set {i} 的材料 {mm.getMaterialAtIdx(i)} 不在配方中")
            lut[i + 1] = id_of[b]
        x0, y0, z0 = self.x[0], self.y[0], self.z_min
        nx = int(round((self.x[1] - x0) / g))
        ny = int(round((self.y[1] - y0) / g))
        nz = int(np.ceil((self._top_from_bbox() - z0) / g)) + 1
        lo = (int(round(x0 / g)), int(round(y0 / g)), int(round(z0 / g)))
        import os
        layer = _lsvox.voxelize([str(f) for f in files], lo, (nx, ny, nz),
                                int(os.environ.get("OMP_NUM_THREADS") or 0))
        grid = lut[layer]
        self.coverage = {"x": [x0, x0 + nx * g], "y": [y0, y0 + ny * g], "z": [z0, z0 + nz * g],
                         "method": "lsvox"}
        return MaterialGrid(grid, (x0, y0, z0), g, names)

    def material_grid_densecellset(self) -> MaterialGrid:
        """多层 level set → 材料体素网格，用 ViennaPS 的 DenseCellSet（单元中心判定）。

        DenseCellSet 设为“表面以上”模式：单元覆盖从最低窄带到给定高度的整个范围，
        表面以上（含孔内）标为覆盖材料 GAS，因此是稠密的，可直接 reshape。
        只返回一个材料编号的平铺数组；1 nm 下峰值约 1–2 GB。
        旧的 ToVoxelMesh 路径要把约 3000 万个六面体转成 Python 列表，峰值达 20 GB（见 Decisions.md R2），
        保留为 material_grid_voxelmesh() 供对照。
        - 最低窄带以下全部在每个 level set 内部，即衬底；
        - 周期边界下不含跨边界的最后一列/行，网格覆盖 [x0, x1 − g] × [y0, y1 − g]。
        """
        g = self.g
        top = self._top_from_bbox() + 2 * g
        cs = D3.DenseCellSet()
        cs.setCellSetPosition(True)
        gas = int(vps.Material.GAS.builtIn())
        cs.setCoverMaterial(gas)
        cs.fromLevelSets(self.dom.getLevelSets(), self.dom.getMaterialMap().getMaterialMap(), top)
        (bx0, by0, bz0), (bx1, by1, bz1) = cs.getBoundingBox()
        cnx, cny, cnz = (int(round((bx1 - bx0) / g)), int(round((by1 - by0) / g)), int(round((bz1 - bz0) / g)))
        ids = np.asarray(cs.getScalarData("Material"), dtype=np.float32)
        del cs
        if ids.size != cnx * cny * cnz:
            raise RuntimeError(f"DenseCellSet 单元数 {ids.size} ≠ {cnx}×{cny}×{cnz}")
        ids = ids.reshape(cnz, cny, cnx).transpose(2, 1, 0)       # x 最快 → (nx, ny, nz)

        names = list(self.r.materials)
        lut = np.zeros(256, dtype=np.uint8)
        for k, m in enumerate(names, 1):
            lut[_builtin_id(m)] = k
        known = {_builtin_id(m) for m in names} | {gas, -1}
        u = {int(v) for v in np.unique(ids)}
        if u - known:
            raise RuntimeError(f"体素中出现配方未定义的材料编号 {sorted(u - known)}")
        idsi = ids.astype(np.int64)
        idsi[(idsi < 0) | (idsi == gas)] = 0
        lab = lut[idsi]

        x0, y0, z0 = self.x[0], self.y[0], self.z_min
        nx = int(round((self.x[1] - x0) / g)) - 1
        ny = int(round((self.y[1] - y0) / g)) - 1
        nz = int(np.ceil((self._top_from_bbox() - z0) / g)) + 1
        grid = np.zeros((nx, ny, nz), dtype=np.uint8)
        k0 = int(round((bz0 + 1e-4 - z0) / g))                   # 单元集底面对应的 z 索引
        lo, hi = max(k0, 0), min(k0 + cnz, nz)
        if hi > lo:
            grid[:, :, lo:hi] = lab[:nx, :ny, lo - k0:hi - k0]
        sub = _builtin_id(self.r.initial["substrate"]["material"])
        grid[:, :, :max(lo, 0)] = lut[sub]                        # 最低窄带以下：衬底
        self.coverage = {"x": [x0, x0 + nx * g], "y": [y0, y0 + ny * g], "z": [z0, z0 + nz * g],
                         "method": "DenseCellSet"}
        return MaterialGrid(grid, (x0, y0, z0), g, names)

    def material_grid_voxelmesh(self) -> MaterialGrid:
        """旧方法：多层 level set → 材料体素网格，用 ViennaLS 的 ToVoxelMesh（单元中心判定）。

        - ToVoxelMesh 只输出各窄带 z 范围内的单元；其下方全部在每个 level set 内部，
          即最底层材料（衬底）；其上方为空。
        - 周期边界下，ToVoxelMesh 不输出跨越边界的最后一列/行单元，
          因此网格覆盖 [x0, x1 − g] × [y0, y1 − g]，记录在 coverage 中。
        """
        g = self.g
        mesh = ls.Mesh()
        L3.ToVoxelMesh(self.dom.getLevelSets(), mesh).apply()
        nodes = np.asarray(mesh.getNodes(), dtype=np.float64)
        hexas = np.asarray(mesh.getHexas(), dtype=np.int64)
        lsidx = np.asarray(mesh.getCellData().getScalarData(0), dtype=np.int64)
        centers = 0.5 * (nodes[hexas[:, 0]] + nodes[hexas[:, 6]])   # VTK 六面体 0 与 6 为对角
        del hexas, nodes

        names = list(self.r.materials)
        id_of = {_builtin_id(m): k for k, m in enumerate(names, 1)}
        mm = self.dom.getMaterialMap()
        lut = np.zeros(mm.size(), dtype=np.uint8)
        for i in range(mm.size()):
            b = int(mm.getMaterialAtIdx(i).builtIn())
            if b not in id_of:
                raise RuntimeError(f"level set {i} 的材料 {mm.getMaterialAtIdx(i)} 不在配方中")
            lut[i] = id_of[b]

        x0, y0, z0 = self.x[0], self.y[0], self.z_min
        nx = int(round((self.x[1] - x0) / g)) - 1
        ny = int(round((self.y[1] - y0) / g)) - 1
        nz = int(np.ceil((self._top_from_bbox() - z0) / g)) + 1
        idx = np.floor((centers - [x0, y0, z0]) / g).astype(np.int64)
        ok = ((idx[:, 0] >= 0) & (idx[:, 0] < nx) & (idx[:, 1] >= 0) & (idx[:, 1] < ny)
              & (idx[:, 2] >= 0) & (idx[:, 2] < nz))
        idx, lsidx = idx[ok], lsidx[ok]

        grid = np.zeros((nx, ny, nz), dtype=np.uint8)
        kmin = int(idx[:, 2].min()) if len(idx) else nz
        grid[:, :, :kmin] = lut[0]                     # 窄带以下：衬底
        grid[idx[:, 0], idx[:, 1], idx[:, 2]] = lut[lsidx]
        self.coverage = {"x": [x0, x0 + nx * g], "y": [y0, y0 + ny * g],
                         "z": [z0, z0 + nz * g], "voxel_cells_from_viennals": int(ok.sum())}
        return MaterialGrid(grid, (x0, y0, z0), g, names)


def _surface_meshes(self) -> dict:
    """每种材料区域的表面网格 {材料: (verts, faces)}。

    包裹表示中第 i 层材料区域 = LS_i ∖ LS_{i-1}；同种材料的各层取并集，
    因此同种材料之间（如多次沉积的 SiO2）不产生界面。
    """
    lsets = self.dom.getLevelSets()
    mm = self.dom.getMaterialMap()
    regions = {}
    for i, l in enumerate(lsets):
        r = L3.Domain(l)
        if i > 0:
            L3.BooleanOperation(r, lsets[i - 1], _SUB).apply()
        name = _name_of(int(mm.getMaterialAtIdx(i).builtIn()), self.r.materials)
        if name in regions:
            L3.BooleanOperation(regions[name], r, vps.BooleanOperationType.UNION).apply()
        else:
            regions[name] = r
    out = {}
    for name, r in regions.items():
        m = ls.Mesh()
        L3.ToSurfaceMesh(r, m).apply()
        out[name] = (np.asarray(m.getNodes(), dtype=np.float64).reshape(-1, 3),
                     np.asarray(m.getTriangles(), dtype=np.int64).reshape(-1, 3))
    return out


ViennaPSBackend.surface_meshes = _surface_meshes


def _name_of(builtin: int, materials) -> str:
    for m in materials:
        if _builtin_id(m) == builtin:
            return m
    raise RuntimeError(f"材料编号 {builtin} 不在配方中")


def _builtin_id(name: str) -> int:
    return int(_mat(name).builtIn())
