"""Minimal SVG writer for filled polygons (no external dependency)."""

from __future__ import annotations

from pathlib import Path

Ring = list[tuple[float, float]]


def rgba_to_svg(color) -> tuple[str, float]:
    r, g, b, a = color
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}", float(a)


def write_svg(
    path,
    shapes: list[tuple[list[Ring], tuple, str]],
    bounds: tuple[float, float, float, float],
    title: str = "",
    note: str = "",
    x_label: str = "",
    y_label: str = "",
    px_per_unit: float = 300.0,
    margin: float = 60.0,
) -> Path:
    """``shapes``: [(rings, rgba, label)], rings in data units (y up).

    Each ring is one polygon; the first ring of a shape is drawn with the
    shape's fill, later rings (holes) are drawn with white so the result
    looks right without relying on fill-rule support.
    """
    x0, y0, x1, y1 = bounds
    w, h = (x1 - x0) * px_per_unit, (y1 - y0) * px_per_unit
    W, H = w + 2 * margin, h + 2 * margin + 30

    def tx(x):
        return margin + (x - x0) * px_per_unit

    def ty(y):
        return margin + (y1 - y) * px_per_unit

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    if title:
        lines.append(f'<text x="{margin}" y="{margin * 0.6:.0f}" font-family="sans-serif" font-size="16">{title}</text>')
    seen = {}
    for rings, rgba, label in shapes:
        fill, alpha = rgba_to_svg(rgba)
        seen.setdefault(label, fill)
        for i, ring in enumerate(rings):
            pts = " ".join(f"{tx(x):.3f},{ty(y):.3f}" for x, y in ring)
            color = fill if i == 0 else "white"
            lines.append(
                f'<polygon points="{pts}" fill="{color}" fill-opacity="{alpha if i == 0 else 1.0}" '
                f'stroke="#222" stroke-width="0.8" data-material="{label}"/>'
            )
    # frame + axis labels
    lines.append(
        f'<rect x="{margin}" y="{margin}" width="{w:.1f}" height="{h:.1f}" fill="none" stroke="#888" stroke-width="0.5"/>'
    )
    lines.append(
        f'<text x="{margin}" y="{margin + h + 18:.0f}" font-family="sans-serif" font-size="11">{x_label} {x0:g} .. {x1:g} um</text>'
    )
    lines.append(
        f'<text x="{margin + w / 2:.0f}" y="{margin + h + 18:.0f}" font-family="sans-serif" font-size="11">{y_label} {y0:g} .. {y1:g} um {note}</text>'
    )
    # legend
    lx, ly = margin + w + 8, margin
    for label, fill in seen.items():
        lines.append(f'<rect x="{lx:.0f}" y="{ly:.0f}" width="12" height="12" fill="{fill}" stroke="#222" stroke-width="0.5"/>')
        lines.append(f'<text x="{lx + 16:.0f}" y="{ly + 11:.0f}" font-family="sans-serif" font-size="11">{label}</text>')
        ly += 18
    lines.append("</svg>")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path
