// 读取 ViennaLS 的 .lvst 文件（多层包裹 level set），按 ToVoxelMesh 的同一规则生成稠密材料体素：
//   单元 (i,j,k) 的 8 个角点为网格节点 (i..i+1, j..j+1, k..k+1)；角点值之和 ≤ 0 视为在该层内部；
//   单元的层号 = 自下而上第一个满足条件的层（包裹表示中第 i 层包含第 0..i 层材料）。
// 与 ViennaPS 的 Python 绑定之间不共享任何 C++ 对象，只通过文件交换，避免二进制兼容问题。
#include <nanobind/nanobind.h>
#include <nanobind/ndarray.h>
#include <nanobind/stl/array.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/tuple.h>

#include <lsDomain.hpp>
#include <lsReader.hpp>
#include <hrleDenseCellIterator.hpp>

#include <omp.h>
#include <array>
#include <vector>

namespace nb = nanobind;
using T = double;
constexpr int D = 3;
using LS = viennals::Domain<T, D>;
using Index = viennahrle::Index<D>;

static nb::ndarray<nb::numpy, uint8_t, nb::ndim<3>>
voxelize(const std::vector<std::string>& files, std::array<int, 3> lo, std::array<int, 3> shape, int nthreads) {
    if (files.empty() || files.size() > 254) throw std::runtime_error("层数必须在 1..254");
    std::vector<viennacore::SmartPointer<LS>> sets;
    for (const auto& f : files) {
        auto ls = viennacore::SmartPointer<LS>::New();
        viennals::Reader<T, D>(ls, f).apply();
        sets.push_back(ls);
    }
    const size_t nx = shape[0], ny = shape[1], nz = shape[2];
    uint8_t* out = new uint8_t[nx * ny * nz]();
    if (nthreads > 0) omp_set_num_threads(nthreads);

    // 按 z 层并行：每个线程处理若干完整的 z 层，各自构造迭代器
#pragma omp parallel for schedule(dynamic, 1)
    for (long kz = 0; kz < long(nz); ++kz) {
        Index start, stop;
        start[0] = lo[0]; start[1] = lo[1]; start[2] = lo[2] + int(kz);
        stop[0] = lo[0] + int(nx) - 1; stop[1] = lo[1] + int(ny) - 1; stop[2] = lo[2] + int(kz);
        std::vector<viennahrle::ConstDenseCellIterator<typename LS::DomainType>> its;
        its.reserve(sets.size());
        for (auto& s : sets) its.emplace_back(s->getDomain(), start);
        auto& lead = its.front();
        for (; !(lead.getIndices() > stop); lead.next()) {
            const Index& c = lead.getIndices();
            const long i = c[0] - lo[0], j = c[1] - lo[1];
            if (i < 0 || j < 0 || i >= long(nx) || j >= long(ny)) continue;
            for (size_t l = 0; l < its.size(); ++l) {
                auto& it = its[l];
                if (l > 0) it.goToIndicesSequential(c);
                T sum = 0;
                for (int q = 0; q < (1 << D); ++q) sum += it.getCorner(q).getValue();
                if (sum <= 0) {
                    out[(i * ny + j) * nz + kz] = uint8_t(l + 1);
                    break;
                }
            }
        }
    }
    nb::capsule owner(out, [](void* p) noexcept { delete[] (uint8_t*)p; });
    return nb::ndarray<nb::numpy, uint8_t, nb::ndim<3>>(out, {nx, ny, nz}, owner);
}

// 网格信息：最小/最大网格点、网格间距、边界类型（供 Python 端核对对齐）
static std::tuple<std::array<int, 3>, std::array<int, 3>, double> grid_info(const std::string& f) {
    auto ls = viennacore::SmartPointer<LS>::New();
    viennals::Reader<T, D>(ls, f).apply();
    auto& g = ls->getGrid();
    return {{g.getMinGridPoint(0), g.getMinGridPoint(1), g.getMinGridPoint(2)},
            {g.getMaxGridPoint(0), g.getMaxGridPoint(1), g.getMaxGridPoint(2)}, g.getGridDelta()};
}

NB_MODULE(_lsvox, m) {
    m.def("voxelize", &voxelize, nb::arg("files"), nb::arg("lo"), nb::arg("shape"), nb::arg("nthreads") = 0);
    m.def("grid_info", &grid_info);
}
