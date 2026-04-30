"""
Canonical pytest suite for the fractal toolkit.

Grader-supplied: copied verbatim into each team's workspace as
tests/test_canonical_fractals.py immediately before pytest runs.
Tests only the documented public surface of:
  - src.fractals.mandelbrot  (iterate, grid)
  - src.fractals.julia       (iterate, grid)
  - src.fractals.stats       (convergence_ratio, mean_escape_time, histogram)

10 test cases total — identical across both teams.
"""

from __future__ import annotations

import pytest

from src.fractals.mandelbrot import iterate as mb_iterate, grid as mb_grid
from src.fractals.julia import iterate as ju_iterate, grid as ju_grid
from src.fractals.stats import convergence_ratio, mean_escape_time, histogram

JULIA_C = -0.7 + 0.27j  # classic filled-Julia parameter


# ── Mandelbrot ────────────────────────────────────────────────────────────────


def test_mandelbrot_origin_no_escape():
    """Origin c=0+0j never escapes; must return max_iter."""
    assert mb_iterate(0 + 0j, 100) == 100


def test_mandelbrot_far_point_escapes_fast():
    """Point far outside the set diverges within a few iterations."""
    assert mb_iterate(2 + 2j, 100) < 5


def test_mandelbrot_grid_shape():
    g = mb_grid(8, 6, -2.0, 1.0, -1.0, 1.0, 50)
    assert len(g) == 6, "grid must have height rows"
    assert all(len(row) == 8 for row in g), "each row must have width cols"


def test_mandelbrot_grid_values_in_range():
    g = mb_grid(10, 8, -2.5, 1.0, -1.2, 1.2, 30)
    for row in g:
        for v in row:
            assert 1 <= v <= 30, f"escape time {v} out of range [1, max_iter]"


# ── Julia ─────────────────────────────────────────────────────────────────────


def test_julia_iterate_returns_int():
    result = ju_iterate(0 + 0j, JULIA_C, 100)
    assert isinstance(result, int)
    assert 1 <= result <= 100


def test_julia_far_point_escapes_fast():
    assert ju_iterate(2 + 2j, JULIA_C, 100) < 5


def test_julia_grid_shape():
    g = ju_grid(8, 6, -1.5, 1.5, -1.5, 1.5, JULIA_C, 50)
    assert len(g) == 6
    assert all(len(row) == 8 for row in g)


# ── Stats ─────────────────────────────────────────────────────────────────────


def test_stats_convergence_ratio():
    # 2 out of 4 values equal max_iter (100) → ratio = 0.5
    grid = [[100, 50], [25, 100]]
    assert convergence_ratio(grid, 100) == pytest.approx(0.5)


def test_stats_mean_escape_time():
    grid = [[10, 20], [30, 40]]
    assert mean_escape_time(grid) == pytest.approx(25.0)


def test_stats_histogram_length_and_sum():
    grid = [[1, 2, 3], [4, 5, 6]]
    h = histogram(grid, 3)
    assert len(h) == 3
    assert sum(h) == 6
