"""The wet etch's cost, and the two things that were paying for it.

A step handed its successor one piece of front per slab it had cut, so a
stack etch carried 156 pieces that were four regions; every piece cost a
buffer at every z sample, and its two z planes made the sampling around
them fine. The same reach was then cut out of each sampled slab
separately although neighbouring samples deliberately share one. Folding
both is arithmetic, not approximation, and the geometry that comes out is
the same.

The multi-depth sampling this uncovered is a correctness fix: the sample
intervals were bounded by the planes shifted by the *largest* depth only,
so an etch with a slower second material had a kink where no sample
looked, and its answer never settled as the resolution was refined.
"""

from __future__ import annotations

import pytest
import shapely
from shapely.geometry import MultiPolygon, Point, box

from deviceflow._internal.geometry import polygons as P

from deviceflow import Device
from deviceflow.process.conformal import _nearly_same, _sample_intervals
from deviceflow.process.isotropic_etch import _accessible_reach, _fold_runs, _merge_front


def _mp(x0, x1):
    return MultiPolygon([box(x0, 0.0, x1, 1.0)])


def test_touching_pieces_of_one_region_become_a_single_tall_piece():
    a, b = _mp(0, 1), _mp(2, 3)
    front = [(0.0, 0.1, a), (0.1, 0.2, a), (0.2, 0.3, a), (0.0, 0.3, b)]
    merged = _merge_front(front)
    assert sorted((z0, z1) for z0, z1, _ in merged) == [(0.0, 0.3), (0.0, 0.3)]
    # A gap in z is a real boundary of the void and stays one.
    apart = _merge_front([(0.0, 0.1, a), (0.5, 0.6, a)])
    assert sorted((z0, z1) for z0, z1, _ in apart) == [(0.0, 0.1), (0.5, 0.6)]
    # Nothing to fold, nothing changed.
    assert _merge_front([(0.0, 0.1, a)]) == [(0.0, 0.1, a)]


def test_neighbouring_samples_that_share_a_reach_are_applied_once():
    a, b = _mp(0, 1), _mp(2, 3)
    assert _fold_runs([(0.0, 0.1, a), (0.1, 0.2, a), (0.2, 0.3, b), (0.3, 0.4, a)]) == [
        (0.0, 0.2, a), (0.2, 0.3, b), (0.3, 0.4, a),
    ]
    # Equal but distinct regions are not the shared object and are left be:
    # only the sharing the sampling itself creates is folded.
    assert len(_fold_runs([(0.0, 0.1, _mp(0, 1)), (0.1, 0.2, _mp(0, 1))])) == 2


def test_a_region_that_moved_further_than_the_tolerance_is_not_nearly_the_same():
    # Settled by the bounding boxes, without the quadratic distance.
    assert not _nearly_same(_mp(0, 1), _mp(0.5, 1.5), 0.01)
    assert _nearly_same(_mp(0, 1), _mp(0, 1.001), 0.01)


def test_every_depth_bounds_the_sample_intervals_not_only_the_largest():
    planes = [0.0, 1.0]
    one = _sample_intervals(planes, -1.0, 2.0, 0.5, 10.0)
    edges = {round(z, 9) for a, b in one for z in (a, b)}
    assert 0.75 not in edges  # the smaller reach kinks here and is missed
    both = _sample_intervals(planes, -1.0, 2.0, 0.5, 10.0, offsets=(0.5, 0.25))
    edges = {round(z, 9) for a, b in both for z in (a, b)}
    assert {-0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5} <= edges


def _undercut(resolution: float, split: bool = False) -> dict[str, float]:
    """Undercut two materials at different rates through a slit."""
    device = Device("two rates", (-0.6, -0.6, 0.6, 0.6), conformal_resolution=resolution, verbose=False)
    for name in ("Si", "SiO2", "SiN"):
        device.material(name)
    device.deposit("Si", 0.2, mode="planar")
    device.deposit("SiO2", 0.12, mode="planar")
    device.deposit("SiN", 0.12, mode="planar")
    device.etch(device.masks.circle((0, 0), 0.5), target=["SiO2", "SiN"], depth=0.24)
    if split:
        # What a film's sampling planes elsewhere would have done to the
        # stack: the same solid, cut into many more slabs. The front the
        # etch starts from is that much more fragmented.
        from deviceflow._internal.geometry.state import znorm

        state = device._state
        for i in range(1, 40):
            state.split_at(znorm(0.2 + 0.24 * i / 40))
    device.wet_etch(depth=0.1, selectivity={"SiO2": 1.0, "SiN": 0.5}, square=True)
    return {m.name: device._state.volume(m) for m in device.materials}


def test_the_square_front_does_not_depend_on_how_finely_the_stack_is_sliced():
    """The simplified front is exact between the critical planes, so the
    answer may not move when the same solid arrives as more slabs, nor
    when the resolution changes. Both were true only of the material with
    the largest depth before the sample intervals took every depth."""
    plain = _undercut(0.01)
    assert _undercut(0.01, split=True) == pytest.approx(plain, rel=1e-9)
    assert _undercut(0.004) == pytest.approx(plain, rel=1e-9)


def _slower_material_volume(resolution: float) -> float:
    """The half-rate material's volume after an undercut through a slit."""
    device = Device("two rates", (-0.6, -0.6, 0.6, 0.6), conformal_resolution=resolution, verbose=False)
    for name in ("Si", "SiO2", "SiN"):
        device.material(name)
    device.deposit("Si", 0.2, mode="planar")
    device.deposit("SiO2", 0.12, mode="planar")
    device.deposit("SiN", 0.12, mode="planar")
    device.etch(device.masks.circle((0, 0), 0.5), target=["SiO2", "SiN"], depth=0.24)
    device.wet_etch(depth=0.1, selectivity={"SiO2": 1.0, "SiN": 0.5})
    return device._state.volume(next(m for m in device.materials if m.name == "SiN"))


def test_the_slower_material_settles_as_the_resolution_is_refined():
    """The round front is sampled, so its answer moves a little with the
    resolution and should move less as the resolution gets finer. The
    slower material's did not: its reach kinks half a depth from each
    plane, which was not a sample boundary, so refining moved the answer
    by percent in no particular direction."""
    coarse = _slower_material_volume(0.01)
    fine = _slower_material_volume(0.004)
    assert abs(fine - coarse) / coarse < 0.005


def test_a_step_hands_on_a_front_of_regions_not_of_slabs(monkeypatch):
    """The regression the folding is for. The void of a structure is a
    handful of regions, but it reaches a step cut into slabs -- one per
    slab of the stack to begin with, then one per sample the previous step
    took. Each piece costs a buffer at every sample within reach of it and
    makes the sampling around its own two z planes fine, so a front that
    grows with the sampling is quadratic. A stack etch reached 156 pieces
    for four regions.
    """
    from deviceflow.process import isotropic_etch

    sizes = []
    original = isotropic_etch._step

    def watched(state, depths, resolution, front, xy, **kwargs):
        sizes.append(len(front))
        return original(state, depths, resolution, front, xy, **kwargs)

    monkeypatch.setattr(isotropic_etch, "_step", watched)

    device = Device("stack", (-0.8, -0.8, 0.8, 0.8), conformal_resolution=0.01, verbose=False)
    for name in ("Si", "SiO2", "SiN", "poly-Si"):
        device.material(name)
    device.deposit("Si", 0.3, mode="planar")
    for _ in range(4):
        device.deposit("SiO2", 0.12, mode="planar")
        device.deposit("SiN", 0.12, mode="planar")
    device.etch(device.masks.circle((0, 0), 0.44), target=["SiO2", "SiN"], depth=0.96)
    # The liner is what fragments the stack: its rounded corner is sampled
    # into dozens of slabs, and each of them used to become a front piece.
    device.deposit("poly-Si", 0.05, mode="conformal")
    device.etch(
        device.masks.rectangle((0.14, 1.8), center=(0.55, 0)),
        target=["SiO2", "SiN", "poly-Si"], depth=0.96,
    )
    device.wet_etch(target="SiN", depth=0.25)

    assert len(sizes) > 3  # it really did advance in steps
    # The void of this structure is a handful of regions however finely
    # the steps have sliced it.
    assert max(sizes) <= 12


def _sealed_shell(*, cavity: bool = False) -> tuple[Device, object]:
    """A target behind a 5 nm sidewall and solid top/bottom caps."""
    device = Device(
        "sealed shell",
        (-1.0, -1.0, 1.0, 1.0),
        conformal_resolution=0.005,
        verbose=False,
    )
    target = device.material("Target", role="dielectric")
    barrier = device.material("Barrier", role="metal")
    outer = Point(0.0, 0.0).buffer(0.5, quad_segs=32)
    inner = Point(0.0, 0.0).buffer(0.495, quad_segs=32)
    wall = outer.difference(inner)
    middle = inner
    if cavity:
        middle = inner.difference(Point(0.0, 0.0).buffer(0.2, quad_segs=32))
    device._state.add_slab(0.0, 0.1, {barrier: outer})
    device._state.add_slab(0.1, 0.9, {barrier: wall, target: middle})
    device._state.add_slab(0.9, 1.0, {barrier: outer})
    return device, target


def test_wet_etch_does_not_tunnel_through_a_closed_lateral_barrier():
    """Barrier thickness is not only a Z distance.

    A 5 nm cylindrical wall is much thinner than the per-step reach of this
    etch.  Euclidean dilation used to jump across it and remove most of the
    target, even though the wall and both caps form a closed shell.
    """
    device, target = _sealed_shell()
    before = device.volume(target)

    device.wet_etch(target=target, depth=0.2)

    assert device.volume(target) == pytest.approx(before, rel=1e-9, abs=1e-12)


def test_a_sealed_void_is_not_an_etchant_source():
    """Only void connected to an opening may start a wet etch."""
    device, target = _sealed_shell(cavity=True)
    before = device.volume(target)

    device.wet_etch(target=target, depth=0.1)

    assert device.volume(target) == pytest.approx(before, rel=1e-9, abs=1e-12)


def test_opening_the_shell_exposes_the_target_to_wet_etch():
    """The accessibility guard must not turn a barrier into a blanket stop."""
    device, target = _sealed_shell()
    barrier = next(material for material in device.materials if material.name == "Barrier")
    opening = Point(0.0, 0.0).buffer(0.08, quad_segs=16)
    top = device._state.slabs[-1]
    top.regions[barrier] = device._state.clean(top.regions[barrier].difference(opening))
    before = device.volume(target)

    device.wet_etch(target=target, depth=0.2)

    assert device.volume(target) < before


def test_barrier_free_reach_keeps_the_exact_fast_path():
    """Accessibility must cost no overlays when there is no blocker."""
    device = Device("open", (-1.0, -1.0, 1.0, 1.0), verbose=False)
    target = device.material("Target", role="dielectric")
    region = MultiPolygon([box(-0.5, -0.5, 0.5, 0.5)])
    device._state.add_slab(0.0, 0.2, {target: region})
    pieces = [(0.0, 0.2, region)]

    assert _accessible_reach(device._state, pieces, pieces, {}) is pieces


def _ring(radius: float, quad: int, centre=(0.0, 0.0)):
    return Point(*centre).buffer(radius, quad_segs=quad)


def test_the_ribbon_test_never_says_two_outlines_match_when_they_do_not():
    """The fast path in _nearly_same must only ever be right.

    It asks whether a ribbon of width tol about one outline swallows the
    other, which is the Hausdorff distance between the curves; the walk it
    stands in for measures that vertex by vertex and can only come out
    smaller. So a yes from the ribbon is a yes. A no is not an answer and
    has to fall through, or a pair the walk would have merged stops being
    merged.
    """
    from deviceflow.process.conformal import RIBBON_WORTH_IT, _nearly_same

    tol = 0.01
    big = 80  # quad segments: enough outline to take the ribbon path
    cases = [
        (_ring(1.0, big), _ring(1.0005, big)),                      # a hair apart
        (_ring(1.0, big), _ring(1.05, big)),                        # clearly apart
        (_ring(1.0, big), _ring(1.0, big, centre=(0.002, 0.0))),    # shifted a hair
        (_ring(1.0, big), _ring(1.0, big, centre=(0.05, 0.0))),     # shifted clearly
        # The trap: as sets these swallow each other, because a ribbon of
        # width tol closes the little hole. Their outlines do not.
        (_ring(1.0, big), _ring(1.0, big).difference(_ring(0.002, 8))),
    ]
    for a, b in cases:
        a, b = P.as_multipolygon(a), P.as_multipolygon(b)
        assert shapely.get_num_coordinates(a) + shapely.get_num_coordinates(b) >= RIBBON_WORTH_IT
        walked = shapely.hausdorff_distance(a.boundary, b.boundary) <= tol
        assert _nearly_same(a, b, tol) == walked, a.wkt[:40]


def test_small_outlines_skip_the_ribbon_and_still_agree():
    from deviceflow.process.conformal import RIBBON_WORTH_IT, _nearly_same

    tol = 0.01
    a, b = P.as_multipolygon(_ring(1.0, 4)), P.as_multipolygon(_ring(1.0005, 4))
    assert shapely.get_num_coordinates(a) + shapely.get_num_coordinates(b) < RIBBON_WORTH_IT
    assert _nearly_same(a, b, tol) == (shapely.hausdorff_distance(a.boundary, b.boundary) <= tol)


def test_what_blocks_a_slab_is_worked_out_once_for_the_whole_etch():
    """Only targets are cut back, so the impermeable regions never move.

    Splitting a slab hands both halves the regions the whole had, so the
    same union serves every step and every slab that still holds them.
    """
    from deviceflow.process.isotropic_etch import _blocker_union

    first = P.as_multipolygon(box(0, 0, 1, 1))
    second = P.as_multipolygon(box(2, 0, 3, 1))
    cache: dict = {}

    once = _blocker_union([first, second], cache)
    assert _blocker_union([first, second], cache) is once, "the same regions, the same union"
    assert once.area == pytest.approx(2.0)

    # Different regions are worked out on their own.
    third = P.as_multipolygon(box(4, 0, 5, 1))
    other = _blocker_union([first, third], cache)
    assert other is not once and other.area == pytest.approx(2.0)
    assert _blocker_union([first, second], cache) is once


def test_a_dilation_is_made_once_per_piece_and_radius(monkeypatch):
    """A step asked for the same dilation over and over.

    A dilation depends on the front piece and the radius and on nothing
    else. The radii repeat by construction: the box front only ever grows
    a piece by the depth or not at all, and the round one gives every
    sample at the same distance from a piece the same radius. On a real
    253-slab stack a step asked for 3,295 of them where a few dozen exist,
    and they were 127 s of the 318 s ten steps cost.
    """
    asked: list[tuple[object, float]] = []
    dilate = MultiPolygon.buffer

    def counted(self, distance, *args, **kwargs):
        # The piece is kept, not just its id: a freed geometry's address is
        # handed to the next one, which would read as a repeat.
        asked.append((self, float(distance)))
        return dilate(self, distance, *args, **kwargs)

    monkeypatch.setattr(MultiPolygon, "buffer", counted)

    device = Device("stack", (-0.8, -0.8, 0.8, 0.8), conformal_resolution=0.01, verbose=False)
    for name in ("Si", "SiO2", "SiN"):
        device.material(name)
    device.deposit("Si", 0.3, mode="planar")
    for _ in range(4):
        device.deposit("SiO2", 0.12, mode="planar")
        device.deposit("SiN", 0.12, mode="planar")
    device.etch(device.masks.circle((0, 0), 0.44), target=["SiO2", "SiN"], depth=0.96)
    device.wet_etch(target="SiN", depth=0.25)

    assert asked, "the etch did dilate something"
    once = {(id(piece), radius) for piece, radius in asked}
    assert len(once) == len(asked), (
        f"{len(asked)} dilations for {len(once)} distinct (piece, radius) pairs"
    )


def test_cleaning_a_valid_region_does_not_go_round_the_houses():
    """What an overlay hands back is already valid, unioned and inside the
    window, and repairing, re-unioning and re-clipping it is three GEOS
    calls for nothing -- an isotropic etch step made 12,000 of each. The
    region that comes out has to be the same one either way.
    """
    from deviceflow._internal.geometry import polygons as P

    device = Device("clean", (-1.0, -1.0, 1.0, 1.0), conformal_resolution=0.01, verbose=False)
    state = device._state
    ring = P.as_multipolygon(
        Point(0, 0).buffer(0.8, quad_segs=16).difference(Point(0, 0).buffer(0.4, quad_segs=16))
    )
    cut = P.as_multipolygon(box(-0.2, -1.0, 0.2, 1.0))
    overlay = ring.difference(cut)
    assert shapely.is_valid(overlay) and isinstance(overlay, MultiPolygon)

    quick = state.clean(overlay)
    # The long way round, as it was: repair, union, then clip to the window.
    slow = P.clean(
        shapely.intersection(
            shapely.unary_union(P.as_multipolygon(shapely.make_valid(overlay))), state._box
        ),
        state.grid,
    )
    assert quick.area == pytest.approx(slow.area, rel=1e-12)
    assert quick.symmetric_difference(slow).area < state.grid * state.grid
    assert len(quick.geoms) == len(slow.geoms)


def test_a_region_reaching_the_window_edge_is_still_clipped():
    """The clip is skipped by looking at the bounds, so anything that
    really does leave the window must still be cut back."""
    device = Device("clip", (-1.0, -1.0, 1.0, 1.0), conformal_resolution=0.01, verbose=False)
    state = device._state
    wide = box(-2.0, -0.5, 2.0, 0.5)
    clipped = state.clean(wide)
    assert clipped.bounds == pytest.approx((-1.0, -0.5, 1.0, 0.5))
