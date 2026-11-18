"""与后端无关的材料体素网格：保存、统计、截面图、GLB 导出。

约定：grid[i, j, k] 是材料编号，0 表示空（空气/真空），1..N 对应 materials 列表顺序。
体素 (i, j, k) 的中心坐标 = origin + (i+0.5, j+0.5, k+0.5) * spacing，单位 nm。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FORMAT = "nowprocess3d.voxel/0.1"


@dataclass
class MaterialGrid:
    grid: np.ndarray          # uint8 (nx, ny, nz)
    origin: tuple             # nm，网格角点
    spacing: float            # nm
    materials: list           # 名称，编号 = 下标 + 1

    def volumes(self) -> dict:
        counts = np.bincount(self.grid.ravel(), minlength=len(self.materials) + 1)
        v = self.spacing ** 3
        return {m: float(counts[i + 1] * v) for i, m in enumerate(self.materials)}

    def save(self, path: Path):
        np.savez_compressed(path, grid=self.grid, origin=np.array(self.origin),
                            spacing=self.spacing,
                            meta=json.dumps({"format": FORMAT, "materials": self.materials}))

    @classmethod
    def load(cls, path: Path) -> "MaterialGrid":
        d = np.load(path, allow_pickle=False)
        meta = json.loads(str(d["meta"]))
        return cls(d["grid"], tuple(d["origin"]), float(d["spacing"]), meta["materials"])

    def axis(self, a: int) -> np.ndarray:
        return self.origin[a] + (np.arange(self.grid.shape[a]) + 0.5) * self.spacing


def _hex_rgba(c: str, alpha: float = 1.0):
    c = c.lstrip("#")
    return [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)] + [alpha]


def plot_sections(mg: MaterialGrid, colors: dict, path: Path, title: str,
                  y_cut: float, z_cuts=(), z_range=None):
    """xz 截面（y = y_cut）+ 若干 xy 截面（z = z_cuts），保存为 PNG。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    cmap = ListedColormap([[1, 1, 1, 1]] + [_hex_rgba(colors[m]["color"]) for m in mg.materials])
    vmax = len(mg.materials)
    x, y, z = mg.axis(0), mg.axis(1), mg.axis(2)
    s = mg.spacing
    n = 1 + len(z_cuts)
    fig, axes = plt.subplots(1, n, figsize=(3.2 + 2.4 * len(z_cuts), 4.2),
                             gridspec_kw={"width_ratios": [1.4] + [1] * len(z_cuts)})
    axes = np.atleast_1d(axes)
    j = int(np.clip(np.searchsorted(y, y_cut), 0, len(y) - 1))
    ext = [x[0] - s / 2, x[-1] + s / 2, z[0] - s / 2, z[-1] + s / 2]
    axes[0].imshow(mg.grid[:, j, :].T, origin="lower", extent=ext, cmap=cmap,
                   vmin=0, vmax=vmax, interpolation="nearest", aspect="equal")
    axes[0].set(xlabel="x (nm)", ylabel="z (nm)", title=f"xz @ y={y[j]:.0f}")
    if z_range:
        axes[0].set_ylim(*z_range)
    for ax, zc in zip(axes[1:], z_cuts):
        k = int(np.clip(np.searchsorted(z, zc), 0, len(z) - 1))
        ax.imshow(mg.grid[:, :, k].T, origin="lower",
                  extent=[x[0] - s / 2, x[-1] + s / 2, y[0] - s / 2, y[-1] + s / 2],
                  cmap=cmap, vmin=0, vmax=vmax, interpolation="nearest", aspect="equal")
        ax.set(xlabel="x (nm)", ylabel="y (nm)", title=f"xy @ z={z[k]:.0f}")
    for ax in axes:
        ax.tick_params(labelsize=7)
        ax.title.set_fontsize(8)
        ax.xaxis.label.set_fontsize(7.5)
        ax.yaxis.label.set_fontsize(7.5)
    present = [m for m in mg.materials if (mg.grid == mg.materials.index(m) + 1).any()]
    axes[0].legend(handles=[Patch(color=colors[m]["color"], label=m) for m in present],
                   fontsize=7, loc="upper right", framealpha=0.85)
    fig.suptitle(title, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def export_glb(mg: MaterialGrid, colors: dict, path: Path, max_faces: int = 60000,
               scale: float = 0.01) -> dict:
    """每种材料一个网格（marching cubes 提取体素区域表面），写成 GLB。

    glTF 为 y 轴向上：模型坐标 (x, y, z)_nm → (x, z, -y) * scale。
    返回每种材料的面数。
    """
    import trimesh
    from skimage.measure import marching_cubes

    scene = trimesh.Scene()
    faces_out = {}
    g = mg.grid
    for idx, name in enumerate(mg.materials, 1):
        mask = g == idx
        if not mask.any():
            continue
        vol = np.pad(mask.astype(np.float32), 1)       # 计算区域边界处封闭
        verts, faces, _, _ = marching_cubes(vol, level=0.5)
        verts = (verts - 1 + 0.5) * mg.spacing + np.asarray(mg.origin)
        mesh = trimesh.Trimesh(vertices=verts, faces=faces[:, ::-1], process=True)
        if len(mesh.faces) > max_faces:
            try:
                mesh = mesh.simplify_quadric_decimation(face_count=max_faces)
            except Exception:
                pass
        v = mesh.vertices
        mesh.vertices = np.column_stack([v[:, 0], v[:, 2], -v[:, 1]]) * scale
        c = colors[name]
        alpha = float(c.get("opacity", 1.0))
        mat = trimesh.visual.material.PBRMaterial(
            name=name, baseColorFactor=_hex_rgba(c["color"], alpha),
            metallicFactor=0.1, roughnessFactor=0.7,
            alphaMode="BLEND" if alpha < 1 else "OPAQUE", doubleSided=True)
        mesh.visual = trimesh.visual.TextureVisuals(material=mat)
        scene.add_geometry(mesh, node_name=name, geom_name=name)
        faces_out[name] = int(len(mesh.faces))
    scene.export(path)
    return faces_out
