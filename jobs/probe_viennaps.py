"""在小区域上确认 ViennaPS 4.7 的约定：刻蚀符号、DenseCellSet 排列、周期边界、布尔运算。"""
import time
import numpy as np
import viennaps as vps
import viennals as ls

vps.setDimension(3)
d3, l3 = vps.d3, ls.d3
P, INF = vps.BoundaryType.PERIODIC_BOUNDARY, vps.BoundaryType.INFINITE_BOUNDARY
g = 2.0
dom = d3.Domain(bounds=[0, 40, 0, 20, -10, 60], boundaryConditions=[P, P, INF], gridDelta=g)
d3.MakePlane(dom, 0.0, vps.Material.Si, False).apply()
d3.MakePlane(dom, 10.0, vps.Material.SiO2, True).apply()
d3.MakePlane(dom, 20.0, vps.Material.SiN, True).apply()
print("levelsets", len(dom.getLevelSets()), "mats", dom.getMaterialsInDomain())
print("bbox", dom.getBoundingBox())

def cells(dom, label):
    t = time.perf_counter()
    cs = d3.DenseCellSet()
    cs.fromLevelSets(dom.getLevelSets(), dom.getMaterialMap().getMaterialMap(), 10.0)
    n = cs.getNumberOfCells()
    mat = np.array(cs.getScalarData("Material"))
    print(f"[{label}] cells={n} labels={cs.getScalarDataLabels()} bbox={cs.getBoundingBox()} "
          f"t={time.perf_counter()-t:.3f}s")
    for i in (0, 1, 2, n - 1):
        print("   center", i, cs.getCellCenter(i), "mat", mat[i])
    vals, cnt = np.unique(mat, return_counts=True)
    print("   material ids/counts", dict(zip(vals.tolist(), cnt.tolist())))
    return cs, mat

cells(dom, "stack")
mm = dom.getMaterialMap()
print("matmap", [(i, str(mm.getMaterialAtIdx(i))) for i in range(mm.size())])

# 布尔：穿过周期边界的盒子 x∈[-5,5]，分两段
for x0, x1 in ((35, 40), (0, 5)):
    box = l3.Domain(dom.getGrid())
    l3.MakeGeometry(box, l3.Box([x0, -5, 15], [x1, 25, 100])).apply()
    dom.applyBooleanOperation(box, vps.BooleanOperationType.RELATIVE_COMPLEMENT, True)
cells(dom, "after slit cut z>15")

# 方向性刻蚀符号
dom2 = d3.Domain(bounds=[0, 40, 0, 20, -10, 60], boundaryConditions=[P, P, INF], gridDelta=g)
d3.MakePlane(dom2, 0.0, vps.Material.Si, False).apply()
d3.MakePlane(dom2, 20.0, vps.Material.SiO2, True).apply()
for vel in (1.0, -1.0):
    tmp = d3.Domain(dom2)  # 拷贝
    tmp.deepCopy(dom2)
    model = d3.DirectionalProcess([0, 0, -1], vel, 0.0, [], True)
    d3.Process(tmp, model, 4.0).apply()
    _, m = cells(tmp, f"directional dir=-z vel={vel}")

# 各向同性选择性刻蚀与共形沉积
tmp = d3.Domain(dom2); tmp.deepCopy(dom2)
tmp.duplicateTopLevelSet(vps.Material.W)
d3.Process(tmp, d3.IsotropicProcess(1.0), 6.0).apply()
cells(tmp, "conformal W 6")
d3.Process(tmp, d3.IsotropicProcess({vps.Material.W: -1.0}, 0.0), 10.0).apply()
cells(tmp, "isotropic W etch 10")
lsets = tmp.getLevelSets()
print("npoints", [l.getNumberOfPoints() for l in lsets])
tmp.saveLevelSets("/tmp/np3d_probe")
import glob; print(glob.glob("/tmp/np3d_probe*"))
print("PROBE_OK")
