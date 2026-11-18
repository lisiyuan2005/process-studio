"""精度检查：沿轴向探测线与各材料表面网格求交，得到亚体素精度的界面位置，并与解析值比较。"""
from __future__ import annotations

import numpy as np
import yaml

_AX = {"x": 0, "y": 1, "z": 2}


def line_hits(verts: np.ndarray, faces: np.ndarray, axis: str, at: dict) -> np.ndarray:
    """轴向直线与三角网格的所有交点坐标（沿 axis）。"""
    a = _AX[axis]
    u, v = [i for i in range(3) if i != a]
    pu, pv = at["xyz"[u]], at["xyz"[v]]
    tri = verts[faces]                                    # (n, 3, 3)
    A, B, C = tri[:, 0], tri[:, 1], tri[:, 2]
    # 先按包围盒粗筛
    lo, hi = tri.min(axis=1), tri.max(axis=1)
    m = (lo[:, u] <= pu) & (hi[:, u] >= pu) & (lo[:, v] <= pv) & (hi[:, v] >= pv)
    A, B, C = A[m], B[m], C[m]
    d = (B[:, u] - A[:, u]) * (C[:, v] - A[:, v]) - (C[:, u] - A[:, u]) * (B[:, v] - A[:, v])
    ok = np.abs(d) > 1e-12
    A, B, C, d = A[ok], B[ok], C[ok], d[ok]
    l1 = ((B[:, u] - pu) * (C[:, v] - pv) - (C[:, u] - pu) * (B[:, v] - pv)) / d
    l2 = ((C[:, u] - pu) * (A[:, v] - pv) - (A[:, u] - pu) * (C[:, v] - pv)) / d
    l3 = 1 - l1 - l2
    eps = -1e-9
    inside = (l1 >= eps) & (l2 >= eps) & (l3 >= eps)
    return (l1 * A[:, a] + l2 * B[:, a] + l3 * C[:, a])[inside]


def interfaces(surfaces: dict, probe: dict, merge: float = 1.0) -> list:
    """surfaces: {材料: (verts, faces)}。返回去重后的界面坐标（相邻材料各贡献一次，合并）。"""
    hits = []
    for verts, faces in surfaces.values():
        if len(faces):
            hits.append(line_hits(verts, faces, probe["axis"], probe["at"]))
    if not hits:
        return []
    h = np.sort(np.concatenate(hits))
    lo, hi = probe["range"]
    h = h[(h >= lo) & (h <= hi)]
    out = []
    for x in h:
        if out and x - out[-1][-1] <= merge:
            out[-1].append(x)
        else:
            out.append([x])
    return [float(np.mean(g)) for g in out]


def compare(measured: list, expected: list, tol: float = 3.0) -> dict:
    """逐个期望值找最近的测量值；超过 tol 视为缺失。多余的测量值单独列出。"""
    meas = np.asarray(measured, dtype=float)
    used, errs, missing = set(), [], []
    for e in expected:
        if len(meas):
            j = int(np.argmin(np.abs(meas - e)))
            if abs(meas[j] - e) <= tol and j not in used:
                used.add(j)
                errs.append(float(meas[j] - e))
                continue
        missing.append(e)
    extra = [float(m) for j, m in enumerate(meas) if j not in used]
    return {"n_expected": len(expected), "errors": [round(x, 3) for x in errs],
            "max_abs_err": round(max(map(abs, errs)), 3) if errs else None,
            "rms_err": round(float(np.sqrt(np.mean(np.square(errs)))), 3) if errs else None,
            "missing": missing, "extra": [round(x, 2) for x in extra]}


def _flat(v):
    out = []
    for x in v:
        out.extend(_flat(x) if isinstance(x, list) else [x])
    return out


def load_checks(path):
    """读取检查文件；YAML 别名 [*layers, 575] 会产生嵌套列表，这里拍平。"""
    with open(path) as f:
        c = yaml.safe_load(f)
    for step, probes in (c.get("expect") or {}).items():
        for k, v in probes.items():
            probes[k] = [float(x) for x in _flat(v)]
    return c


def evaluate(checks: dict, step_id: str, surfaces: dict) -> dict | None:
    exp = (checks.get("expect") or {}).get(step_id)
    if not exp:
        return None
    out = {}
    for name, expected in exp.items():
        pr = checks["probes"][name]
        meas = interfaces(surfaces, pr)
        r = compare(meas, expected)
        r["measured"] = [round(x, 3) for x in meas]
        out[name] = r
    return out
