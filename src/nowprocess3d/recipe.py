"""配方读取与校验。校验只检查配方本身，不做几何计算。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FORMAT = "nowprocess3d.recipe/0.1"

# 每种操作：必填字段 -> 类型检查函数；可选字段同理
_num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
_pos = lambda v: _num(v) and v > 0
_str = lambda v: isinstance(v, str) and v != ""

OPS = {
    "deposit_planar":    {"req": {"material": _str, "thickness": _pos}, "opt": {}},
    "deposit_stack":     {"req": {"layers": list, "repeat": int}, "opt": {"cap": dict}},
    "deposit_conformal": {"req": {"material": _str, "thickness": _pos}, "opt": {}},
    "etch_masked":       {"req": {"mask": dict}, "opt": {"depth": _pos, "to_z": _num}},
    "etch_directional":  {"req": {"thickness": _pos}, "opt": {}},
    "etch_isotropic":    {"req": {"materials": list, "thickness": _pos}, "opt": {}},
    "cmp":               {"req": {"z": _num}, "opt": {}},
}


class RecipeError(Exception):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__("\n".join(diagnostics))


@dataclass
class Recipe:
    name: str
    domain: dict
    materials: dict
    initial: dict
    steps: list
    source: dict = field(repr=False)

    def summary(self) -> str:
        d = self.domain
        lines = [f"recipe  {self.name}",
                 f"domain  x={d['x']} y={d['y']} boundary={d['boundary']} nm",
                 f"materials {', '.join(self.materials)}",
                 f"initial {self.initial['substrate']['material']} "
                 f"{self.initial['substrate']['thickness']} nm",
                 f"steps   {len(self.steps)}"]
        for i, s in enumerate(self.steps, 2):  # 1 = 初始衬底
            params = {k: v for k, v in s.items() if k not in ("id", "op")}
            lines.append(f"  {i:2d} {s['id']:<18} {s['op']:<18} {params}")
        return "\n".join(lines)


def _layer_pair(item, where, mats, diag):
    if not (isinstance(item, dict) and len(item) == 1):
        diag.append(f"{where}: 应写成 {{材料: 厚度}}")
        return None
    (m, t), = item.items()
    if m not in mats:
        diag.append(f"{where}: 未知材料 '{m}'")
    if not _pos(t):
        diag.append(f"{where}: 厚度必须为正数，得到 {t!r}")
    return m, t


def _check_mask(mask, where, diag):
    shape = mask.get("shape")
    if shape == "circle":
        c, r = mask.get("center"), mask.get("radius")
        if not (isinstance(c, list) and len(c) == 2 and all(map(_num, c))):
            diag.append(f"{where}.center: 应为 [x, y]")
        if not _pos(r):
            diag.append(f"{where}.radius: 必须为正数")
    elif shape == "rect":
        for k in ("x", "y"):
            v = mask.get(k)
            if not (isinstance(v, list) and len(v) == 2 and all(map(_num, v)) and v[0] < v[1]):
                diag.append(f"{where}.{k}: 应为 [min, max] 且 min < max")
    else:
        diag.append(f"{where}.shape: 支持 circle / rect，得到 {shape!r}")


def validate(data: dict) -> Recipe:
    diag = []
    if data.get("format") != FORMAT:
        diag.append(f"format: 应为 {FORMAT}，得到 {data.get('format')!r}")
    if data.get("units", "nm") != "nm":
        diag.append("units: 目前只支持 nm")

    dom = data.get("domain") or {}
    for k in ("x", "y"):
        v = dom.get(k)
        if not (isinstance(v, list) and len(v) == 2 and all(map(_num, v)) and v[0] < v[1]):
            diag.append(f"domain.{k}: 应为 [min, max] 且 min < max")
    if dom.get("boundary", "periodic") not in ("periodic", "reflective"):
        diag.append("domain.boundary: 支持 periodic / reflective")
    dom.setdefault("boundary", "periodic")

    mats = data.get("materials") or {}
    if not mats:
        diag.append("materials: 至少定义一种材料")

    sub = (data.get("initial") or {}).get("substrate") or {}
    if sub.get("material") not in mats:
        diag.append(f"initial.substrate.material: 未知材料 {sub.get('material')!r}")
    if not _pos(sub.get("thickness")):
        diag.append("initial.substrate.thickness: 必须为正数")

    steps = data.get("steps") or []
    seen = set()
    for i, s in enumerate(steps, 1):
        sid = s.get("id", f"#{i}")
        w = f"steps[{i}] ({sid})"
        if "id" not in s:
            diag.append(f"{w}: 缺少 id")
        elif sid in seen:
            diag.append(f"{w}: id 重复")
        seen.add(sid)
        op = s.get("op")
        if op not in OPS:
            diag.append(f"{w}.op: 不支持的操作 {op!r}，可用 {sorted(OPS)}")
            continue
        spec = OPS[op]
        for k, chk in spec["req"].items():
            if k not in s:
                diag.append(f"{w}.{k}: 缺少必填字段")
            elif chk in (list, dict, int):
                if not isinstance(s[k], chk) or isinstance(s[k], bool):
                    diag.append(f"{w}.{k}: 类型应为 {chk.__name__}")
            elif not chk(s[k]):
                diag.append(f"{w}.{k}: 取值不合法 {s[k]!r}")
        for k in s:
            if k not in ("id", "op") and k not in spec["req"] and k not in spec["opt"]:
                diag.append(f"{w}.{k}: 未知字段")
        if "material" in s and s["material"] not in mats:
            diag.append(f"{w}.material: 未知材料 {s['material']!r}")
        if op == "etch_isotropic":
            for m in s.get("materials", []):
                if m not in mats:
                    diag.append(f"{w}.materials: 未知材料 {m!r}")
        if op == "deposit_stack":
            for j, it in enumerate(s.get("layers", []), 1):
                _layer_pair(it, f"{w}.layers[{j}]", mats, diag)
            if "cap" in s:
                _layer_pair(s["cap"], f"{w}.cap", mats, diag)
            if isinstance(s.get("repeat"), int) and s["repeat"] < 1:
                diag.append(f"{w}.repeat: 必须 ≥ 1")
        if op == "etch_masked":
            _check_mask(s["mask"], f"{w}.mask", diag) if isinstance(s.get("mask"), dict) else None
            if ("depth" in s) == ("to_z" in s):
                diag.append(f"{w}: depth 与 to_z 必须且只能给一个")
    if diag:
        raise RecipeError(diag)
    return Recipe(name=data.get("name", "unnamed"), domain=dom, materials=mats,
                  initial=data["initial"], steps=steps, source=copy.deepcopy(data))


def load(path) -> Recipe:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise RecipeError([f"{path}: 不是 YAML 映射"])
    return validate(data)
