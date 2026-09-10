"""Continuous 2D geometry shared by sketch and GDS masks (units: micrometres).

CSG min/max preserves the zero boundary, not exact distance everywhere.
No rasterization or smoothing is used to construct these boundaries.
"""

import numpy as np


def segment_distance(xx, yy, start, end):
    vx, vy = end - start
    length2 = vx * vx + vy * vy
    t = np.zeros_like(xx) if length2 == 0 else np.clip(
        ((xx - start[0]) * vx + (yy - start[1]) * vy) / length2, 0, 1
    )
    return np.hypot(xx - start[0] - t * vx, yy - start[1] - t * vy)


def polygon_distance(xx, yy, points):
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
        raise ValueError("polygon needs at least three finite 2D vertices")
    inside = np.zeros(xx.shape, dtype=bool)
    distance = np.full(xx.shape, np.inf)
    for start, end in zip(points, np.roll(points, -1, axis=0), strict=True):
        distance = np.minimum(distance, segment_distance(xx, yy, start, end))
        if end[1] != start[1]:
            crossing_x = start[0] + (yy - start[1]) * (end[0] - start[0]) / (end[1] - start[1])
            inside ^= ((start[1] > yy) != (end[1] > yy)) & (xx < crossing_x)
    return np.where(inside, -distance, distance)


def path_distance(xx, yy, points, width):
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
        raise ValueError("path needs at least two finite 2D vertices")
    if not np.isfinite(width) or width <= 0:
        raise ValueError("path width must be positive and finite")
    distance = np.full(xx.shape, np.inf)
    for start, end in zip(points[:-1], points[1:], strict=True):
        distance = np.minimum(distance, segment_distance(xx, yy, start, end))
    return distance - width / 2
