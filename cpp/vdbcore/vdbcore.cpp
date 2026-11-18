// The OpenVDB kernel's C++ core, called from Python (nanobind).
//
// Adapted from Nowprocess3D's vdbcore (nowprocess3d branch). Conventions:
// every grid shares one linear transform, so index (i,j,k) sits at
// origin + (i,j,k) * g (voxel centres); a signed distance field is negative
// inside; the narrow band is hw voxels wide on each side and values outside
// it are the inactive +-hw*g.
#include <nanobind/nanobind.h>
#include <nanobind/ndarray.h>
#include <nanobind/stl/array.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/string.h>

#include <openvdb/openvdb.h>
#include <openvdb/io/File.h>
#include <openvdb/tools/Activate.h>
#include <openvdb/tools/Composite.h>
#include <openvdb/tools/Merge.h>
#include <openvdb/tools/Dense.h>
#include <openvdb/tools/MeshToVolume.h>
#include <openvdb/tools/VolumeToMesh.h>
#include <openvdb/tools/Prune.h>
#include <openvdb/tools/SignedFloodFill.h>
#include <openvdb/tools/ValueTransformer.h>

#include <cmath>
#include <array>
#include <algorithm>

namespace nb = nanobind;
using namespace openvdb;

struct SDF {
    FloatGrid::Ptr grid;
    double g;
    float hw;
    float bg() const { return float(hw * g); }
};

using Arr3 = nb::ndarray<float, nb::ndim<3>, nb::c_contig, nb::device::cpu>;

static FloatGrid::Ptr new_grid(double g, std::array<double, 3> o, float hw) {
    auto gr = FloatGrid::create(float(hw * g));
    auto xf = math::Transform::createLinearTransform(g);
    xf->postTranslate(Vec3d(o[0], o[1], o[2]));
    gr->setTransform(xf);
    gr->setGridClass(GRID_LEVEL_SET);
    return gr;
}

// Values outside the band become the inactive +-background, then nodes whose
// values are all the same collapse into tiles. Not tools::pruneLevelSet: it
// folds a whole inactive node by the sign of its first voxel, which is wrong
// when a node holds inactive values from both sides.
static void normalize(SDF& s) {
    const float bg = s.bg();
    tools::foreach(s.grid->beginValueOn(), [bg](const FloatGrid::ValueOnIter& it) {
        const float v = *it;
        if (std::abs(v) >= bg) it.setValue(v < 0 ? -bg : bg);
    });
    tools::deactivate(s.grid->tree(), bg, 0.0f);
    tools::deactivate(s.grid->tree(), -bg, 0.0f);
    tools::prune(s.grid->tree());
}

// A numpy (nx, ny, nz) array into a grid; lo is the index of array[0, 0, 0].
static SDF from_dense(Arr3 a, std::array<int, 3> lo, double g, std::array<double, 3> o, float hw) {
    SDF s{new_grid(g, o, hw), g, hw};
    CoordBBox bb(Coord(lo[0], lo[1], lo[2]),
                 Coord(lo[0] + int(a.shape(0)) - 1, lo[1] + int(a.shape(1)) - 1, lo[2] + int(a.shape(2)) - 1));
    tools::Dense<float, tools::LayoutZYX> dense(bb, a.data());   // z fastest = numpy C order
    tools::copyFromDense(dense, *s.grid, /*tolerance=*/0.0f);
    normalize(s);
    return s;
}

static nb::ndarray<nb::numpy, float, nb::ndim<3>> to_dense(const SDF& s, std::array<int, 3> lo, std::array<int, 3> shape) {
    size_t n = size_t(shape[0]) * shape[1] * shape[2];
    float* buf = new float[n];
    // copyToDense leaves regions with no node untouched: fill the background first
    std::fill(buf, buf + n, s.grid->background());
    CoordBBox bb(Coord(lo[0], lo[1], lo[2]),
                 Coord(lo[0] + shape[0] - 1, lo[1] + shape[1] - 1, lo[2] + shape[2] - 1));
    tools::Dense<float, tools::LayoutZYX> dense(bb, buf);
    tools::copyToDense(*s.grid, dense);
    nb::capsule owner(buf, [](void* p) noexcept { delete[] (float*)p; });
    return nb::ndarray<nb::numpy, float, nb::ndim<3>>(buf, {size_t(shape[0]), size_t(shape[1]), size_t(shape[2])}, owner);
}

// CSG by value-wise min/max (union = min, intersection = max, difference =
// max(a, -b)), then the band is normalised. Not tools::csgUnionCopy and the
// like: on grids built here they gave untouched node regions the wrong
// inside sign.
static SDF csg(const SDF& a, const SDF& b, int op) {
    FloatGrid::Ptr r = a.grid->deepCopy();
    FloatGrid::Ptr o = b.grid->deepCopy();
    const float bg = a.bg();
    if (op == 2) {   // -b: every value (tiles too) negated; where b has no node, the complement is inside
        tools::foreach(o->beginValueAll(), [](const FloatGrid::ValueAllIter& it) { it.setValue(-*it); });
        o->tree().root().setBackground(-bg, /*updateChildNodes=*/false);
    }
    if (op == 0) tools::compMin(*r, *o);
    else tools::compMax(*r, *o);
    SDF out{r, a.g, a.hw};
    tools::foreach(r->beginValueAll(), [bg](const FloatGrid::ValueAllIter& it) {
        const float v = *it;
        if (std::abs(v) > bg) it.setValue(v < 0 ? -bg : bg);
    });
    normalize(out);   // compMin/compMax leave the union of the two active sets on
    return out;
}

static void mesh_raw(const SDF& s, std::vector<Vec3s>& pts, std::vector<Vec3I>& tris, std::vector<Vec4I>& quads) {
    tools::volumeToMesh(*s.grid, pts, tris, quads, 0.0, 0.0);
}

// The exact Euclidean outward offset S' = S (+) B(t), the round-cornered film:
//   1. the surface mesh of S (periodic: only faces whose centroid lies in
//      [x0,x1) x [y0,y1), copied to the 3 x 3 periodic images and kept
//      within R of the window; closed: every face as it is);
//   2. mesh -> unsigned distance field, its band covering t + hw;
//   3. outside S: d - t; inside S: -(d + t).
static SDF offset(const SDF& s, double t, double x0, double x1, double y0, double y1, double R, bool periodic) {
    std::vector<Vec3s> pts; std::vector<Vec3I> tris; std::vector<Vec4I> quads;
    mesh_raw(s, pts, tris, quads);
    const double Lx = x1 - x0, Ly = y1 - y0;
    std::vector<Vec3s> P; std::vector<Vec3I> T; std::vector<Vec4I> Q;
    auto inside_dom = [&](const Vec3d& c) { return c.x() >= x0 && c.x() < x1 && c.y() >= y0 && c.y() < y1; };
    auto near_ext = [&](const Vec3d& c) { return c.x() >= x0 - R && c.x() <= x1 + R && c.y() >= y0 - R && c.y() <= y1 + R; };
    const int images = periodic ? 1 : 0;
    for (int sx = -images; sx <= images; ++sx) for (int sy = -images; sy <= images; ++sy) {
        const Vec3s sh(float(sx * Lx), float(sy * Ly), 0.f);
        for (const auto& f : tris) {
            Vec3d c = (Vec3d(pts[f[0]]) + Vec3d(pts[f[1]]) + Vec3d(pts[f[2]])) / 3.0;
            if (periodic && (!inside_dom(c) || !near_ext(c + Vec3d(sh)))) continue;
            Index b = Index(P.size());
            for (int k = 0; k < 3; ++k) P.push_back(pts[f[k]] + sh);
            T.emplace_back(b, b + 1, b + 2);
        }
        for (const auto& f : quads) {
            Vec3d c = (Vec3d(pts[f[0]]) + Vec3d(pts[f[1]]) + Vec3d(pts[f[2]]) + Vec3d(pts[f[3]])) / 4.0;
            if (periodic && (!inside_dom(c) || !near_ext(c + Vec3d(sh)))) continue;
            Index b = Index(P.size());
            for (int k = 0; k < 4; ++k) P.push_back(pts[f[k]] + sh);
            Q.emplace_back(b, b + 1, b + 2, b + 3);
        }
    }
    const float band = float(t / s.g + s.hw + 2);
    auto ud = tools::meshToUnsignedDistanceField<FloatGrid>(s.grid->transform(), P, T, Q, band);
    // Start from a copy of S: beyond the distance band a point has the same
    // sign in S' as in S (S is inside S', and a point outside S farther than
    // t + hw from its surface stays outside), so no signed flood fill.
    SDF out{s.grid->deepCopy(), s.g, s.hw};
    auto sacc = s.grid->getConstAccessor();
    auto oacc = out.grid->getAccessor();
    const float bg = s.bg(), tf = float(t);
    for (auto it = ud->cbeginValueOn(); it; ++it) {
        const Coord c = it.getCoord();
        const float d = *it;
        const float v = sacc.getValue(c) < 0 ? -(d + tf) : (d - tf);
        if (std::abs(v) < bg) oacc.setValueOn(c, v);
        else oacc.setValueOff(c, v < 0 ? -bg : bg);
    }
    tools::prune(out.grid->tree());
    return out;
}

static std::tuple<nb::ndarray<nb::numpy, float, nb::ndim<2>>, nb::ndarray<nb::numpy, int64_t, nb::ndim<2>>>
surface_mesh(const SDF& s) {
    std::vector<Vec3s> pts; std::vector<Vec3I> tris; std::vector<Vec4I> quads;
    mesh_raw(s, pts, tris, quads);
    float* P = new float[pts.size() * 3];
    for (size_t i = 0; i < pts.size(); ++i) for (int k = 0; k < 3; ++k) P[3 * i + k] = pts[i][k];
    size_t nt = tris.size() + 2 * quads.size();
    int64_t* F = new int64_t[nt * 3];
    size_t j = 0;
    for (const auto& f : tris) { for (int k = 0; k < 3; ++k) F[3 * j + k] = f[k]; ++j; }
    for (const auto& q : quads) {
        F[3 * j] = q[0]; F[3 * j + 1] = q[1]; F[3 * j + 2] = q[2]; ++j;
        F[3 * j] = q[0]; F[3 * j + 1] = q[2]; F[3 * j + 2] = q[3]; ++j;
    }
    nb::capsule op(P, [](void* p) noexcept { delete[] (float*)p; });
    nb::capsule of(F, [](void* p) noexcept { delete[] (int64_t*)p; });
    return {nb::ndarray<nb::numpy, float, nb::ndim<2>>(P, {pts.size(), 3}, op),
            nb::ndarray<nb::numpy, int64_t, nb::ndim<2>>(F, {nt, 3}, of)};
}

static std::tuple<std::array<int, 3>, std::array<int, 3>, bool> active_bbox(const SDF& s) {
    CoordBBox bb = s.grid->evalActiveVoxelBoundingBox();
    return {{bb.min().x(), bb.min().y(), bb.min().z()}, {bb.max().x(), bb.max().y(), bb.max().z()}, !bb.empty()};
}

static void write_vdb(const std::vector<SDF*>& grids, const std::vector<std::string>& names, const std::string& path) {
    GridPtrVec v;
    for (size_t i = 0; i < grids.size(); ++i) {
        auto g = grids[i]->grid->deepCopy();
        g->setName(names[i]);
        // what read_vdb needs to rebuild the SDF: the spacing is in the
        // transform, the band's half-width is not
        g->insertMeta("ps_hw", FloatMetadata(grids[i]->hw));
        v.push_back(g);
    }
    io::File f(path);
    f.setCompression(io::COMPRESS_BLOSC | io::COMPRESS_ACTIVE_MASK);
    f.write(v);
    f.close();
}

// The grids write_vdb stored, by name, in the order written.
static std::vector<std::tuple<std::string, SDF>> read_vdb(const std::string& path) {
    io::File f(path);
    f.open();
    std::vector<std::tuple<std::string, SDF>> out;
    GridPtrVecPtr grids = f.getGrids();
    f.close();
    for (auto& base : *grids) {
        FloatGrid::Ptr grid = gridPtrCast<FloatGrid>(base);
        if (!grid) continue;
        const double g = grid->transform().voxelSize()[0];
        float hw = 3.0f;
        if (auto meta = grid->getMetadata<FloatMetadata>("ps_hw")) hw = meta->value();
        grid->removeMeta("ps_hw");
        out.emplace_back(grid->getName(), SDF{grid, g, hw});
    }
    return out;
}

NB_MODULE(_vdbcore, m) {
    openvdb::initialize();
    nb::class_<SDF>(m, "SDF")
        .def_prop_ro("active_voxels", [](const SDF& s) { return s.grid->activeVoxelCount(); })
        .def_prop_ro("mem_bytes", [](const SDF& s) { return s.grid->memUsage(); })
        .def_prop_ro("g", [](const SDF& s) { return s.g; })
        .def_prop_ro("hw", [](const SDF& s) { return s.hw; });
    m.def("from_dense", &from_dense, nb::arg("array"), nb::arg("lo"), nb::arg("g"), nb::arg("origin"), nb::arg("hw"));
    m.def("to_dense", &to_dense, nb::arg("sdf"), nb::arg("lo"), nb::arg("shape"));
    m.def("union", [](const SDF& a, const SDF& b) { return csg(a, b, 0); });
    m.def("intersection", [](const SDF& a, const SDF& b) { return csg(a, b, 1); });
    m.def("difference", [](const SDF& a, const SDF& b) { return csg(a, b, 2); });
    m.def("offset", &offset, nb::arg("sdf"), nb::arg("t"), nb::arg("x0"), nb::arg("x1"),
          nb::arg("y0"), nb::arg("y1"), nb::arg("R"), nb::arg("periodic"));
    m.def("surface_mesh", &surface_mesh);
    m.def("active_bbox", &active_bbox);
    m.def("write_vdb", &write_vdb);
    m.def("read_vdb", &read_vdb);
    m.attr("OPENVDB_VERSION") = OPENVDB_LIBRARY_VERSION_STRING;
}
