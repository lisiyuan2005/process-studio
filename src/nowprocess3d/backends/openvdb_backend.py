"""OpenVDB 后端（OpenVDB 14 + 自写 C++ 扩展 _vdbcore）。

表示：每种材料一个窄带 SDF 网格（内部为负），各材料区域互不重叠。
体素中心 = (x0, y0, z_min) + (i+½, j+½, k+½)·g，与 ViennaPS 的 ToVoxelMesh 单元中心对齐。
x、y 方向周期；每个网格在 x、y 两侧各保留 M 个体素的周期一致边距。

各操作的几何模型（都是纯几何、无物理速率）：
- deposit_planar / deposit_stack：解析长方体 SDF 并入新材料（精确）。
- etch_masked / cmp：解析圆柱、长方体（含周期像）做 CSG 差（精确）。
- deposit_conformal：C++ offset_periodic，表面网格 → 欧氏距离场 → 外偏移 t（精确的 Minkowski 和）。
- etch_directional：从正上方可见的气体区域沿 −z 平移 [0, t] 后扣除（等价于速度 ∝ n_z 的方向性刻蚀；
  假设无悬垂，有悬垂时被遮挡的部分按不可见处理）。
- etch_isotropic：scikit-fmm 在“气体 ∪ 被刻材料”内求测地距离（周期边界），扣除距离 < t 的被刻材料。
"""
from __future__ import annotations

import math

import numpy as np

from ..voxel import MaterialGrid
from . import _vdbcore as vc

HW = 3.0       # 窄带半宽（体素）
MARGIN = 5     # x、y 两侧的周期边距（体素）
TIE = 0.0137   # 体素中心偏移（体素），见 __init__


class OpenVDBBackend:
    @staticmethod
    def version() -> dict:
        import skfmm
        return {"openvdb": vc.OPENVDB_VERSION, "scikit-fmm": getattr(skfmm, "__version__", "?")}

    def __init__(self, recipe, grid: float):
        self.r, self.g = recipe, float(grid)
        d = recipe.domain
        if d["boundary"] != "periodic":
            raise ValueError("OpenVDB 后端目前只实现周期边界")
        self.x, self.y = d["x"], d["y"]
        g = self.g
        self.nx = int(round((self.x[1] - self.x[0]) / g))
        self.ny = int(round((self.y[1] - self.y[0]) / g))
        sub = recipe.initial["substrate"]
        self.z_min = -float(sub["thickness"])
        # 体素中心整体偏移 TIE·g：整数坐标的界面不会恰好落在体素中心上，避免 SDF 恰为 0 的平局
        # （否则同种材料两次沉积的共享面成为 0 值“薄片”，后续偏移会从薄片继续生长）
        e = TIE * g
        self.o = (self.x[0] + g / 2 + e, self.y[0] + g / 2 + e, self.z_min + g / 2 + e)
        self.bg = HW * g
        self.mats: dict[str, vc.SDF] = {}
        self.top, self.flat = 0.0, True
        # 衬底：底面放在 z_min 以下，避免底面进入关心区域
        self._add(sub["material"], self._box_sdf(z_lo=self.z_min - (MARGIN + HW + 2) * g, z_hi=0.0))

    # ------------------------------------------------------------------ 坐标与稠密数组
    def _k(self, z):            # 世界 z → 索引（浮点）
        return (z - self.o[2]) / self.g

    def _axes(self, lo, shape):
        g = self.g
        return [self.o[a] + (lo[a] + np.arange(shape[a])) * g for a in range(3)]

    def _ext_xy(self):
        return (-MARGIN, -MARGIN), (self.nx + 2 * MARGIN, self.ny + 2 * MARGIN)

    def _from_dense(self, arr, k0):
        (i0, j0), _ = self._ext_xy()
        return vc.from_dense(np.ascontiguousarray(arr, dtype=np.float32), (i0, j0, int(k0)),
                             self.g, self.o, HW)

    def _zrange(self, z_lo, z_hi):
        k0 = int(math.floor(self._k(z_lo))) - int(HW) - 1
        k1 = int(math.ceil(self._k(z_hi))) + int(HW) + 1
        return k0, k1 - k0 + 1

    # ------------------------------------------------------------------ 解析形状
    def _box_sdf(self, z_lo, z_hi, xr=None, yr=None):
        """长方体 SDF，x/y 区间缺省为覆盖整个扩展区域（即无侧面），带周期像。"""
        k0, nz = self._zrange(z_lo, z_hi)
        (i0, j0), (nx, ny) = self._ext_xy()
        X, Y, Z = np.meshgrid(*self._axes((i0, j0, k0), (nx, ny, nz)), indexing="ij", sparse=True)
        big = 1e6
        xr = xr or (-big, big)
        yr = yr or (-big, big)
        Lx, Ly = self.x[1] - self.x[0], self.y[1] - self.y[0]
        best = None
        sx = range(-1, 2) if xr[0] > -big else [0]
        sy = range(-1, 2) if yr[0] > -big else [0]
        for a in sx:
            for b in sy:
                d = _box(X - a * Lx, Y - b * Ly, Z, xr, yr, (z_lo, z_hi))
                best = d if best is None else np.minimum(best, d)
        return self._from_dense(best, k0)

    def _cyl_sdf(self, cx, cy, r, z_lo, z_hi):
        k0, nz = self._zrange(z_lo, z_hi)
        (i0, j0), (nx, ny) = self._ext_xy()
        X, Y, Z = np.meshgrid(*self._axes((i0, j0, k0), (nx, ny, nz)), indexing="ij", sparse=True)
        Lx, Ly = self.x[1] - self.x[0], self.y[1] - self.y[0]
        best = None
        e = r + (MARGIN + HW) * self.g
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                ccx, ccy = cx + a * Lx, cy + b * Ly
                if not (self.x[0] - e <= ccx <= self.x[1] + e and self.y[0] - e <= ccy <= self.y[1] + e):
                    continue
                d = _cyl(X, Y, Z, ccx, ccy, r, z_lo, z_hi)
                best = d if best is None else np.minimum(best, d)
        return self._from_dense(best, k0)

    def _clip_box(self):
        """扩展区域内侧 ½ 体素处的长方体，用于裁掉周期像带来的区域外部分。"""
        if not hasattr(self, "_clip"):
            e = (MARGIN - 0.5) * self.g
            zr = (self.z_min - (MARGIN + HW + 1) * self.g, float(self.r.domain.get("z_top", 1000)))
            k0, nz = self._zrange(*zr)
            (i0, j0), (nx, ny) = self._ext_xy()
            X, Y, Z = np.meshgrid(*self._axes((i0, j0, k0), (nx, ny, nz)), indexing="ij", sparse=True)
            self._clip = self._from_dense(
                _box(X, Y, Z, (self.x[0] - e, self.x[1] + e), (self.y[0] - e, self.y[1] + e), zr), k0)
        return self._clip

    # ------------------------------------------------------------------ 材料集合
    def _add(self, name, region):
        self.mats[name] = vc.union(self.mats[name], region) if name in self.mats else region

    def _solid(self):
        it = iter(self.mats.values())
        s = next(it)
        for m in it:
            s = vc.union(s, m)
        return s

    def _top_from_bbox(self):
        """固体最高点 z：读取固体 SDF 的符号求最高界面（线性插值到亚体素）。

        不直接用活动包围盒：CSG 差之后，切割体边界附近会残留一些正值活动体素，
        它们不代表表面，但会把包围盒顶抬高（2026-11-16 在 CMP 后实测抬高 38 nm）。
        """
        S = self._solid()
        lo, hi, ok = vc.active_bbox(S)
        if not ok:
            return self.top
        k0, nz = lo[2], hi[2] - lo[2] + 2
        a = vc.to_dense(S, (0, 0, k0), (self.nx, self.ny, nz))
        inside = (a <= 0).any(axis=(0, 1))
        if not inside.any():
            return self.top
        k = int(np.nonzero(inside)[0].max())
        if k + 1 >= nz:
            return self.o[2] + (k0 + k) * self.g
        a0, a1 = a[:, :, k], a[:, :, k + 1]
        m = a0 <= 0
        frac = float(np.max(-a0[m] / np.maximum(a1[m] - a0[m], 1e-6)))
        return self.o[2] + (k0 + k + min(max(frac, 0.0), 1.0)) * self.g

    def _dense_region(self):
        """覆盖所有网格的 z 范围（索引）与 x/y 的域内范围。"""
        k0 = int(math.floor(self._k(self.z_min))) - MARGIN - int(HW) - 3
        k1 = int(math.ceil(self._k(self._top_from_bbox()))) + int(HW) + 3
        return k0, k1 - k0 + 1

    def _dense(self, sdf, k0, nz):
        return vc.to_dense(sdf, (0, 0, k0), (self.nx, self.ny, nz))

    def _write_back(self, name, arr, k0):
        pad = np.pad(arr, ((MARGIN, MARGIN), (MARGIN, MARGIN), (0, 0)), mode="wrap")
        self.mats[name] = self._from_dense(pad, k0)

    # ------------------------------------------------------------------ 操作
    def _planar(self, material, t):
        if not self.flat:
            raise ValueError("deposit_planar 只支持水平平整表面；当前表面有起伏，请改用 deposit_conformal")
        self._add(material, self._box_sdf(self.top, self.top + float(t)))
        self.top += float(t)

    def _cut_all(self, cutter):
        for k in list(self.mats):
            self.mats[k] = vc.difference(self.mats[k], cutter)

    def apply(self, step: dict) -> dict:
        top0 = self._top_from_bbox()
        info = self._apply(step)
        top1 = self._top_from_bbox()
        grow = step.get("thickness", 0) if step["op"] == "deposit_conformal" else 0
        if step["op"] in ("deposit_stack", "deposit_planar"):
            grow = self.top - top0
        if not np.isfinite(top1) or top1 > top0 + grow + 2 * self.g:
            raise RuntimeError(f"几何检查失败：步骤后表面最高点 z={top1}，步骤前 {top0}，允许增长 {grow} nm")
        return info

    def _apply(self, step):
        op, g, info = step["op"], self.g, {}
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
        elif op == "etch_masked":
            z_hi = self._top_from_bbox() + (HW + 2) * g
            z_lo = step["to_z"] if "to_z" in step else self.top - step["depth"]
            m = step["mask"]
            if m["shape"] == "circle":
                cutter = self._cyl_sdf(*m["center"], m["radius"], z_lo, z_hi)
            else:
                cutter = self._box_sdf(z_lo, z_hi, xr=tuple(m["x"]), yr=tuple(m["y"]))
            self._cut_all(cutter)
            info["etch_bottom_z"] = z_lo
            self.flat = False
        elif op == "cmp":
            z = float(step["z"])
            self._cut_all(self._box_sdf(z, self._top_from_bbox() + (HW + 2) * g))
            self.top, self.flat = z, True
        elif op == "deposit_conformal":
            t = float(step["thickness"])
            S = self._solid()
            R = (MARGIN + HW + 2) * g + t
            S2 = vc.offset_periodic(S, t, self.x[0], self.x[1], self.y[0], self.y[1], R)
            S2 = vc.intersection(S2, self._clip_box())   # 去掉扩展区域以外的周期像
            self._add(step["material"], vc.difference(S2, S))
            self.flat = False
        elif op == "etch_directional":
            self._directional(float(step["thickness"]))
            self.flat = False
        elif op == "etch_isotropic":
            self._isotropic(step["materials"], float(step["thickness"]))
            self.flat = False
        else:
            raise ValueError(f"OpenVDB 后端不支持操作 {op!r}")
        return info

    def _directional(self, t):
        g = self.g
        k0, nz = self._dense_region()
        nz += int(math.ceil(t / g)) + 2
        phiS = self._dense(self._solid(), k0, nz)
        gas = phiS > 0
        vis = np.logical_and.accumulate(gas[:, :, ::-1], axis=2)[:, :, ::-1]   # 正上方一路都是气体
        below = np.zeros_like(vis)
        below[:, :, :-1] = vis[:, :, 1:]                                       # 可见气体正下方的固体体素
        phi_vis = np.where(vis | below, -phiS, self.bg).astype(np.float32)
        n = int(math.floor(t / g + 1e-9))
        rem = t - n * g
        phiR = phi_vis.copy()
        for k in range(1, n + 1):                                              # 并集 = 逐层下移取最小
            sh = np.full_like(phi_vis, self.bg)
            sh[:, :, :-k] = phi_vis[:, :, k:]
            np.minimum(phiR, sh, out=phiR)
        if rem > 1e-9:
            phiR -= rem
        for name in list(self.mats):
            a = self._dense(self.mats[name], k0, nz)
            self._write_back(name, np.maximum(a, -phiR), k0)

    def _isotropic(self, materials, t):
        import skfmm
        k0, nz = self._dense_region()
        phiS = self._dense(self._solid(), k0, nz)
        target = None
        for m in materials:
            if m in self.mats:
                a = self._dense(self.mats[m], k0, nz)
                target = a if target is None else np.minimum(target, a)
        if target is None:
            return
        allowed = (phiS > 0) | (target < 0)
        phi0 = np.ma.MaskedArray(-phiS, mask=~allowed)
        d = skfmm.distance(phi0, dx=self.g, periodic=(True, True, False))
        d = np.ma.filled(d, fill_value=1e9).astype(np.float32)
        cut = (t - d).astype(np.float32)          # > 0 处被去除
        cut[~allowed] = -self.bg
        for m in materials:
            if m in self.mats:
                a = self._dense(self.mats[m], k0, nz)
                self._write_back(m, np.maximum(a, cut), k0)

    # ------------------------------------------------------------------ 状态与输出
    def stats(self) -> dict:
        return {"n_levelsets": len(self.mats),
                "n_points": int(sum(m.active_voxels for m in self.mats.values())),
                "vdb_mem_mb": round(sum(m.mem_bytes for m in self.mats.values()) / 2**20, 1),
                "surface_top_z": round(self._top_from_bbox(), 3)}

    def save_state(self, prefix):
        names = list(self.mats)
        vc.write_vdb([self.mats[n] for n in names], names, f"{prefix}.vdb")

    def material_grid(self) -> MaterialGrid:
        g = self.g
        k0 = int(round(self._k(self.z_min + g / 2)))         # 第一个体素中心 = z_min + g/2
        nz = int(math.ceil((self._top_from_bbox() - self.z_min) / g)) + 3
        names = list(self.r.materials)
        best = np.full((self.nx, self.ny, nz), self.bg, dtype=np.float32)
        grid = np.zeros((self.nx, self.ny, nz), dtype=np.uint8)
        for idx, n in enumerate(names, 1):
            if n not in self.mats:
                continue
            a = self._dense(self.mats[n], k0, nz)
            sel = (a < 0) & (a < best)
            grid[sel] = idx
            np.minimum(best, a, out=best)
        self.coverage = {"x": list(self.x), "y": list(self.y),
                         "z": [self.z_min, self.z_min + nz * g]}
        return MaterialGrid(grid, (self.x[0], self.y[0], self.z_min), g, names)

    def surface_meshes(self) -> dict:
        out = {}
        for n, s in self.mats.items():
            v, f = vc.surface_mesh(s)
            v = v.astype(np.float64)
            if len(f):
                c = v[f].mean(axis=1)
                keep = ((c[:, 0] >= self.x[0]) & (c[:, 0] <= self.x[1])
                        & (c[:, 1] >= self.y[0]) & (c[:, 1] <= self.y[1]))
                f = f[keep]
            out[n] = (v, f)
        return out


def _box(X, Y, Z, xr, yr, zr):
    cx, hx = (xr[0] + xr[1]) / 2, (xr[1] - xr[0]) / 2
    cy, hy = (yr[0] + yr[1]) / 2, (yr[1] - yr[0]) / 2
    cz, hz = (zr[0] + zr[1]) / 2, (zr[1] - zr[0]) / 2
    qx, qy, qz = np.abs(X - cx) - hx, np.abs(Y - cy) - hy, np.abs(Z - cz) - hz
    out = np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2 + np.maximum(qz, 0) ** 2)
    ins = np.minimum(np.maximum(np.maximum(qx, qy), qz), 0)
    return (out + ins).astype(np.float32)


def _cyl(X, Y, Z, cx, cy, r, z0, z1):
    dr = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2) - r
    dz = np.maximum(z0 - Z, Z - z1)
    out = np.sqrt(np.maximum(dr, 0) ** 2 + np.maximum(dz, 0) ** 2)
    ins = np.minimum(np.maximum(dr, dz), 0)
    return (out + ins).astype(np.float32)
