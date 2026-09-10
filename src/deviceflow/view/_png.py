"""Optional PNG export of filled polygons through matplotlib."""

from __future__ import annotations

from pathlib import Path

from ..exceptions import ProcessError


def write_png(path, shapes, bounds, title="", x_label="", y_label="", dpi=200, figsize=(7, 5)) -> Path:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import PathPatch
        from matplotlib.path import Path as MPath
    except ImportError as e:  # pragma: no cover
        raise ProcessError("export_png needs matplotlib (pip install matplotlib); use export_svg otherwise") from e

    fig, ax = plt.subplots(figsize=figsize)
    handles = {}
    for rings, rgba, label in shapes:
        verts, codes = [], []
        for ring in rings:
            pts = list(ring) + [ring[0]]
            verts.extend(pts)
            codes.extend([MPath.MOVETO] + [MPath.LINETO] * (len(pts) - 2) + [MPath.CLOSEPOLY])
        patch = PathPatch(MPath(verts, codes), facecolor=rgba[:3], alpha=rgba[3], edgecolor="black", linewidth=0.6)
        ax.add_patch(patch)
        handles.setdefault(label, patch)
    x0, y0, x1, y1 = bounds
    pad = 0.03 * max(x1 - x0, y1 - y0, 1e-9)
    ax.set_xlim(x0 - pad, x1 + pad)
    ax.set_ylim(y0 - pad, y1 + pad)
    ax.set_aspect("equal")
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title, fontsize=10)
    if handles:
        ax.legend(handles.values(), handles.keys(), loc="upper right", fontsize=8)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path
