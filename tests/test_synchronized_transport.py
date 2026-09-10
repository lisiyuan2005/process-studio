import numpy as np
import pytest

from process_studio.kernel.transport import evolve_hamilton_jacobi, _stage, _tiles
from process_studio.kernel.processes import mixed_trench_etch


@pytest.mark.parametrize("order", [1, 2])
@pytest.mark.parametrize("shape,tiles", [
    ((23, 29), (7, 11)),
    ((15, 17, 19), (6, 7, 8)),
    ((7, 8, 9), (1, 2, 3)),
])
def test_each_stage_exchanges_ghosts_and_matches_dense(order, shape, tiles):
    coords = np.meshgrid(*(np.linspace(-.3, .3, n) for n in shape), indexing="ij")
    phi = np.sqrt(sum((x-.013*(i+1))**2 for i, x in enumerate(coords)))-.14
    velocity = tuple(.023*(-1)**i for i in range(len(shape)))
    dense, steps = evolve_hamilton_jacobi(phi, .025, .07, velocity, .2, order=order)
    diagnostics = {}
    blocked, steps2 = evolve_hamilton_jacobi(
        phi, .025, .07, velocity, .2, order=order, tile_shape=tiles, diagnostics=diagnostics,
    )
    assert steps == steps2
    np.testing.assert_array_equal(blocked, dense)
    assert diagnostics["tiles"] > 1
    assert diagnostics["storage"] == "dense-uniform"


def test_tile_traversal_order_cannot_change_a_stage():
    phi = np.random.default_rng(27).normal(size=(17, 19))
    tiles = tuple(_tiles(phi.shape, (5, 7)))
    forward = _stage(phi, .02, .04, (.01, -.02), 2, .01, tiles, original=phi)
    reverse = _stage(phi, .02, .04, (.01, -.02), 2, .01, tiles[::-1], original=phi)
    np.testing.assert_array_equal(forward, reverse)


@pytest.mark.parametrize("speed", [-.1, .1])
def test_offset_circle_second_order_reduces_error_and_converges(speed):
    errors = {1: [], 2: []}
    for n in [41, 81, 161]:
        xy = np.linspace(-.5, .5, n)
        x, y = np.meshgrid(xy, xy)
        phi = np.hypot(x-.013, y+.027)-.22
        exact = phi-speed*.3
        band = abs(exact) < .03
        for order in [1, 2]:
            computed, _ = evolve_hamilton_jacobi(phi, 1/(n-1), speed, (0., 0.), .3, order=order)
            errors[order].append(float(np.sqrt(np.mean((computed[band]-exact[band])**2))))
    assert all(b < .5*a for a, b in zip(errors[1], errors[2], strict=True))
    assert errors[2][2] < errors[2][1] < errors[2][0]


def test_oblique_translation_of_asymmetric_geometry_is_more_accurate():
    xy = np.linspace(-.5, .5, 81)
    y, x = np.meshgrid(xy, xy, indexing="ij")
    def field(x, y):
        return np.sqrt(((x-.017)/.8)**2+((y+.031)/1.2)**2)-.19
    phi = field(x, y)
    exact = field(x+.013, y-.021)
    band = abs(exact) < .03
    errors = []
    for order in (1, 2):
        computed, _ = evolve_hamilton_jacobi(phi, .0125, 0, (.07, -.013/.3), .3, order=order)
        errors.append(np.sqrt(np.mean((computed[band]-exact[band])**2)))
    assert errors[1] < .5*errors[0]


def test_offset_sphere_normal_growth_3d():
    xyz = np.linspace(-.4, .4, 41)
    z, y, x = np.meshgrid(xyz, xyz, xyz, indexing="ij")
    phi = np.sqrt((x-.013)**2+(y+.017)**2+(z-.009)**2)-.18
    exact = phi-.02
    band = abs(exact) < .03
    errors = []
    for order in (1, 2):
        computed, _ = evolve_hamilton_jacobi(phi, .02, .1, (0, 0, 0), .2, order=order)
        errors.append(np.sqrt(np.mean((computed[band]-exact[band])**2)))
    assert errors[1] < .5*errors[0]


def test_process_tiled_second_order_keeps_mask_and_matches_full_solver():
    xy, z = np.linspace(-.3, .3, 31), np.linspace(-.3, .1, 21)
    xx, yy = np.meshgrid(xy, xy)
    mask_phi = np.hypot(xx-.013, yy+.017)-.083
    phi = np.broadcast_to(z[:, None, None], (21, 31, 31)).copy()
    kw = dict(directional_rate=.08, isotropic_rate=.02, exposure_sdf=mask_phi, solver_order=2)
    reference, _ = mixed_trench_etch(phi, mask_phi <= 0, z, .02, .12, **kw)
    computed, _ = mixed_trench_etch(phi, mask_phi <= 0, z, .02, .12, tile_shape=(8, 13, 11), **kw)
    np.testing.assert_array_equal(computed, reference)
    iz = np.argmin(abs(z))
    np.testing.assert_allclose(computed[iz][mask_phi > .01], phi[iz][mask_phi > .01], atol=1e-15)
    assert np.isfinite(computed).all()


@pytest.mark.parametrize("kwargs", [
    {"order": 3}, {"order": True}, {"order": 2.0}, {"tile_shape": True},
    {"tile_shape": 0}, {"tile_shape": (2, 0)},
    {"tile_shape": (2, 3, 4)}, {"normal_speed": float("nan")}, {"total_time": -.1},
])
def test_bad_solver_options_are_rejected(kwargs):
    options = dict(spacing=.1, normal_speed=.1, velocity=(0, 0), total_time=.1)
    options.update(kwargs)
    with pytest.raises(ValueError):
        evolve_hamilton_jacobi(np.ones((9, 9)), **options)


def test_zero_speed_is_exact_identity():
    phi = np.random.default_rng(9).normal(size=(11, 13))
    actual, steps = evolve_hamilton_jacobi(phi, .1, 0, (0, 0), 1, tile_shape=4)
    assert steps == 0
    np.testing.assert_array_equal(actual, phi)
