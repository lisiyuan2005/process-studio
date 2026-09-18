"""Marks the level-set pictures carry beyond the material colours."""

import numpy as np

from process_studio.worker.render import STEP_LINE_SHADE, _mark_steps


def test_a_cliff_is_marked_on_its_high_side_and_a_ramp_is_not():
    """A slope moves about a node per column; a step is what jumps."""
    dz = 0.01
    labels = np.zeros((1, 9), dtype=np.int16)
    # Flat, then a ramp of one node per column, then flat, then a cliff.
    heights = np.array([[0.20, 0.21, 0.22, 0.23, 0.23, 0.23, 0.23, 0.10, 0.10]])
    rgb = np.zeros((1, 9, 3), dtype=np.uint8)
    rgb[...] = (200, 100, 60)

    marked = _mark_steps(rgb, labels, heights, dz)

    darkened = [index for index in range(9) if not np.array_equal(marked[0, index], rgb[0, index])]
    assert darkened == [6], "only the brink of the cliff, and on the high side"
    assert tuple(marked[0, 6]) == tuple(int(c * STEP_LINE_SHADE) for c in (200, 100, 60))


def test_a_step_between_two_materials_is_not_marked():
    """The colour already changes there; a line would only thicken it."""
    labels = np.array([[0, 0, 1, 1]], dtype=np.int16)
    heights = np.array([[0.3, 0.3, 0.1, 0.1]])
    rgb = np.zeros((1, 4, 3), dtype=np.uint8)
    rgb[...] = (200, 100, 60)

    assert np.array_equal(_mark_steps(rgb, labels, heights, 0.01), rgb)


def test_nothing_is_marked_where_nothing_is():
    """A column with no material has no height, and NaN is not a step."""
    labels = np.array([[-1, -1, 0, 0]], dtype=np.int16)
    heights = np.array([[np.nan, np.nan, 0.3, 0.3]])
    rgb = np.zeros((1, 4, 3), dtype=np.uint8)
    rgb[...] = (200, 100, 60)

    assert np.array_equal(_mark_steps(rgb, labels, heights, 0.01), rgb)
