"""Write flow.json: a gate-last (replacement-gate) 3D NAND block for the slab kernel.

The block is six ON pairs tall and sits between two slits, with six channel
holes, a three-step staircase and word-line contacts. Dimensions are a few
times coarser than a real device so the flow runs in minutes; the sequence
and the materials are the real ones. Run it with

    process-studio --root nand flow apply examples/3d-nand/flow.json
    process-studio --root nand run

and open the workspace in the desktop, or ``view section --named 1``.
"""

from __future__ import annotations

import json
from pathlib import Path

PAIRS = 6
OXIDE, NITRIDE = 0.025, 0.030          # one ON pair: 55 nm
SOURCE, ETCH_STOP, CAP = 0.10, 0.02, 0.05
BLOCK, TRAP, TUNNEL, CHANNEL, CORE = 0.006, 0.006, 0.005, 0.008, 0.05
HOLE_RADIUS = 0.055                    # 110 nm holes
X0, X1 = -0.35, 0.55
SLITS = [-0.04, 0.55]                  # the right slit is half outside the window
HOLES = [(-0.24, -0.17), (-0.24, 0.0), (-0.24, 0.17), (0.16, -0.17), (0.16, 0.0), (0.16, 0.17)]
STAIRS = [0.30, 0.36, 0.42]            # beyond each x one more pair is removed
STACK_TOP = SOURCE + ETCH_STOP + PAIRS * (OXIDE + NITRIDE) + CAP


def deposit(name, material, thickness, mode="planar", **extra):
    step = {"name": name, "type": "deposit", "material": material,
            "parameters": {"target": round(thickness, 4), "mode": mode}}
    step.update(extra)
    return step


def etch(name, target, rates, mask=None, directional=1.0, **extra):
    step = {"name": name, "type": "etch",
            "parameters": {"target": round(target, 4), "directional_fraction": directional}, "rates": rates}
    if mask:
        step["mask"] = mask
    step.update(extra)
    return step


def cmp(name, z):
    return {"name": name, "type": "cmp", "parameters": {"target_z": round(z, 4)}}


def circle(x, y, r):
    return {"kind": "circle", "operation": "merge", "parameters": {"center": [x, y], "radius": r}}


def rectangle(cx, cy, w, h):
    return {"kind": "rectangle", "operation": "merge", "parameters": {"center": [cx, cy], "size": [w, h]}}


steps = [
    deposit("Source plate (n+ poly)", "Poly-Si", SOURCE, tool="LPCVD"),
    deposit("Etch-stop oxide", "SiO2", ETCH_STOP, tool="CVD"),
]
for pair in range(1, PAIRS + 1):
    steps.append(deposit(f"ON pair {pair}: SiO2", "SiO2", OXIDE, tool="PECVD"))
    steps.append(deposit(f"ON pair {pair}: SiN (sacrificial)", "SiN", NITRIDE, tool="PECVD"))
steps.append(deposit("Cap oxide", "SiO2", CAP, tool="PECVD"))
steps += [
    # Through the whole stack; the source plate is not in the rate table, so it stops there.
    etch("Channel hole etch", STACK_TOP - SOURCE + 0.01, {"SiO2": 1, "SiN": 1}, mask="sketch:holes", tool="HAR etcher"),
    deposit("Blocking oxide", "SiO2", BLOCK, "conformal", tool="ALD"),
    # Its own material name, so the sacrificial-nitride removal later leaves it alone.
    deposit("Charge-trap nitride", "SiN-trap", TRAP, "conformal", tool="ALD"),
    deposit("Tunnel oxide", "SiO2", TUNNEL, "conformal", tool="ALD"),
    deposit("Poly-Si channel", "Poly-Si", CHANNEL, "conformal", tool="LPCVD"),
    deposit("Core oxide fill", "SiO2", CORE, "conformal", tool="ALD"),
    cmp("CMP to cap", STACK_TOP),
]
for index, x in enumerate(STAIRS, start=1):
    depth = (CAP if index == 1 else 0.0) + OXIDE + NITRIDE
    steps.append(etch(f"Staircase trim-etch {index}", depth, {"SiO2": 1, "SiN": 1},
                      mask=f"sketch:stair{index}", tool="Trim etcher"))
steps += [
    deposit("Staircase oxide fill", "SiO2", 0.25, tool="HDP CVD"),
    cmp("CMP after fill", STACK_TOP),
    etch("Slit etch", STACK_TOP - SOURCE + 0.01, {"SiO2": 1, "SiN": 1}, mask="sketch:slits", tool="HAR etcher"),
    # Isotropic, from the slit walls, half a block wide: only SiN is a target,
    # the oxides are barriers and the trap nitride is a different material.
    etch("SiN removal (hot H3PO4)", 0.27, {"SiN": 1}, directional=0.0, tool="Wet bench"),
    deposit("High-k blocking Al2O3", "Al2O3", 0.003, "conformal", tool="ALD"),
    deposit("TiN barrier", "TiN", 0.003, "conformal", tool="ALD"),
    deposit("W word-line fill", "W", 0.015, "conformal", tool="CVD W"),
    # Recess the W from the slit walls so the word lines are cut from each other.
    etch("W etch-back", 0.04, {"W": 1}, directional=0.0, tool="Dry etcher"),
    deposit("Slit liner oxide", "SiO2", 0.03, "conformal", tool="ALD"),
    deposit("Slit core (source line)", "Poly-Si", 0.03, "conformal", tool="LPCVD"),
    cmp("CMP after slit fill", STACK_TOP),
    # One etch for contacts of three depths; W is not in the table, so each lands on its word line.
    etch("Word-line contact etch", 0.25, {"SiO2": 1, "Al2O3": 1, "TiN": 1}, mask="sketch:wlc", tool="Contact etcher"),
    deposit("Contact W fill", "W", 0.03, "conformal", tool="CVD W"),
    cmp("Final CMP", STACK_TOP),
]

sketches = {
    "holes": {"name": "Channel holes", "shapes": [circle(x, y, HOLE_RADIUS) for x, y in HOLES]},
    "slits": {"name": "Slits", "shapes": [rectangle(x, 0.0, 0.12, 0.8) for x in SLITS]},
    "wlc": {"name": "Word-line contacts", "shapes": [circle(x + 0.03, 0.0, 0.02) for x in STAIRS]},
}
for index, x in enumerate(STAIRS, start=1):
    sketches[f"stair{index}"] = {"name": f"Staircase mask {index}",
                                 "shapes": [rectangle((x + X1) / 2, 0.0, X1 - x, 0.8)]}

flow = {
    "name": "3D NAND (gate-last, 6 pairs)",
    "kernel": "slab",
    "window": {"x": [X0, X1], "y": [-0.35, 0.35], "z": [-0.3, 0.85]},
    "resolution_nm": 4,
    "resolution_xy_nm": 4,
    "materials": [
        {"name": "Poly-Si", "category": "Semiconductor", "color": "#a9563e", "opacity": 1.0},
        {"name": "SiN-trap", "category": "Dielectric", "color": "#2f7f7f", "opacity": 0.9},
    ],
    "section_lines": [
        {"name": "Through holes and slit", "start": [X0, 0.17], "end": [X1, 0.17]},
        {"name": "Along the slit", "start": [-0.04, -0.35], "end": [-0.04, 0.35]},
        {"name": "Across the staircase", "start": [0.2, 0.0], "end": [X1, 0.0]},
    ],
    "sketches": sketches,
    "steps": steps,
}

if __name__ == "__main__":
    target = Path(__file__).with_name("flow.json")
    target.write_text(json.dumps(flow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{target}: {len(steps)} steps, stack top at z = {STACK_TOP:g} µm")
