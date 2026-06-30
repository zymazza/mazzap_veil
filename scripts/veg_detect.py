"""Generic stem detectors — the lower rungs of the vegetation capability
ladder, used by analyze_vegetation.py when no LiDAR segmentation stems exist.

Both detectors are dependency-free (numpy only, no scipy): the canopy is
divided into ~`spacing`-meter blocks and at most one stem is placed per block,
at the block's peak. Deterministic given the same inputs.

Coordinates are scene-local meters; rasters are assumed aligned to the grid's
outer cell-edge footprint (outerMinX..outerMaxX / outerMinY..outerMaxY), the
same invariant analyze_vegetation.py relies on (docs/grid-contract.md).
"""

import numpy as np


def _outer(grid):
    return grid["outerMinX"], grid["outerMaxX"], grid["outerMinY"], grid["outerMaxY"]


def _block_peaks(values, mask, grid, spacing, picker):
    """Yield (x, y, peak_value) — one per block of ~spacing meters where the
    block holds any masked (canopy) cell. picker(window)->(row,col) selects
    the in-block peak. values/mask are HxW arrays over the outer footprint."""
    h, w = values.shape
    ox0, ox1, oy0, oy1 = _outer(grid)
    px_per_m_x = w / (ox1 - ox0)
    px_per_m_y = h / (oy1 - oy0)
    bx = max(1, int(round(spacing * px_per_m_x)))
    by = max(1, int(round(spacing * px_per_m_y)))
    for r0 in range(0, h, by):
        for c0 in range(0, w, bx):
            wmask = mask[r0:r0 + by, c0:c0 + bx]
            if not wmask.any():
                continue
            wval = values[r0:r0 + by, c0:c0 + bx]
            rr, cc = picker(np.where(wmask, wval, -np.inf))
            row, col = r0 + rr, c0 + cc
            x = ox0 + (col + 0.5) / w * (ox1 - ox0)
            y = oy1 - (row + 0.5) / h * (oy1 - oy0)
            yield x, y, float(values[row, col])


def detect_from_chm(dsm, dtm, grid, spacing=6.0, min_height=2.5,
                    terrain_valid=None):
    """Canopy Height Model (DSM - DTM) local maxima. Heights come straight
    from the CHM. dsm/dtm are HxW arrays over the outer footprint."""
    chm = dsm - dtm
    chm[~np.isfinite(chm)] = 0.0
    mask = chm >= min_height
    stems = []
    argmax = lambda win: np.unravel_index(np.argmax(win), win.shape)  # noqa: E731
    for x, y, height in _block_peaks(chm, mask, grid, spacing, argmax):
        if terrain_valid and not terrain_valid(x, y):
            continue
        z = float(dtm_sample(dtm, grid, x, y))
        stems.append({"x": round(x, 3), "y": round(y, 3), "z": round(z, 2),
                      "height": round(float(height), 2), "confidence": 0.5,
                      "source": "chm"})
    return stems


def detect_from_ndvi(ndvi, grid, spacing=6.0, ndvi_min=0.2,
                     nominal_height=10.0, terrain_valid=None,
                     elevation=None):
    """NDVI canopy local maxima — the weakest rung: positions only, no real
    heights (a nominal canopy height with low confidence). ndvi is HxW over
    the outer footprint."""
    mask = ndvi >= ndvi_min
    stems = []
    argmax = lambda win: np.unravel_index(np.argmax(win), win.shape)  # noqa: E731
    for x, y, _peak in _block_peaks(ndvi, mask, grid, spacing, argmax):
        if terrain_valid and not terrain_valid(x, y):
            continue
        z = float(elevation(x, y)) if elevation else grid["minElevation"]
        stems.append({"x": round(x, 3), "y": round(y, 3), "z": round(z, 2),
                      "height": round(float(nominal_height), 2), "confidence": 0.3,
                      "source": "ndvi"})
    return stems


def dtm_sample(dtm, grid, x, y):
    """Nearest-cell DTM elevation at a scene-local point."""
    h, w = dtm.shape
    ox0, ox1, oy0, oy1 = _outer(grid)
    col = min(w - 1, max(0, int((x - ox0) / (ox1 - ox0) * w)))
    row = min(h - 1, max(0, int((oy1 - y) / (oy1 - oy0) * h)))
    v = dtm[row, col]
    return v if np.isfinite(v) else grid["minElevation"]
