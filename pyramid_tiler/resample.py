"""Deterministic image resampling implemented with numpy.

Both filters use the standard "pixel centers" mapping (align_corners=False):

    src_coord = (dst_coord + 0.5) * (src_size / dst_size) - 0.5

The implementation is pure numpy and contains no randomness, so the same
input array always produces bit-identical output.
"""

from __future__ import annotations

import numpy as np


def _sample_coords(src_size: int, dst_size: int) -> np.ndarray:
    scale = src_size / dst_size
    return (np.arange(dst_size, dtype=np.float64) + 0.5) * scale - 0.5


def resize_nearest(img: np.ndarray, dst_w: int, dst_h: int) -> np.ndarray:
    """Nearest-neighbour resize of a HxWxC (or HxW) uint8 array."""
    src_h, src_w = img.shape[:2]
    xs = np.clip(np.floor(_sample_coords(src_w, dst_w) + 0.5).astype(np.int64), 0, src_w - 1)
    ys = np.clip(np.floor(_sample_coords(src_h, dst_h) + 0.5).astype(np.int64), 0, src_h - 1)
    return img[ys][:, xs]


def resize_bilinear(img: np.ndarray, dst_w: int, dst_h: int) -> np.ndarray:
    """Bilinear resize of a HxWxC (or HxW) uint8 array.

    Interpolation is computed in float64 and rounded half away from zero,
    which keeps the result stable across platforms.
    """
    src_h, src_w = img.shape[:2]
    src = img.astype(np.float64)

    def interp_axis(a: np.ndarray, src_n: int, dst_n: int, axis: int) -> np.ndarray:
        pos = np.clip(_sample_coords(src_n, dst_n), 0.0, float(src_n - 1))
        lo = np.floor(pos).astype(np.int64)
        hi = np.minimum(lo + 1, src_n - 1)
        frac = (pos - lo).astype(np.float64)
        lo_idx = [slice(None)] * a.ndim
        hi_idx = [slice(None)] * a.ndim
        lo_idx[axis] = lo
        hi_idx[axis] = hi
        a_lo = a[tuple(lo_idx)]
        a_hi = a[tuple(hi_idx)]
        shape = [1] * a.ndim
        shape[axis] = dst_n
        w = frac.reshape(shape)
        return a_lo * (1.0 - w) + a_hi * w

    out = interp_axis(src, src_w, dst_w, axis=1)
    out = interp_axis(out, src_h, dst_h, axis=0)
    # Round half away from zero for cross-platform determinism.
    rounded = np.floor(out + 0.5)
    return np.clip(rounded, 0, 255).astype(np.uint8)


def resize(img: np.ndarray, dst_w: int, dst_h: int, method: str) -> np.ndarray:
    if method == "nearest":
        return resize_nearest(img, dst_w, dst_h)
    if method == "bilinear":
        return resize_bilinear(img, dst_w, dst_h)
    raise ValueError(f"unsupported resample method: {method!r}")
