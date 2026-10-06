"""Deterministic image resampling (nearest / bilinear) implemented with numpy.

Both filters use the "pixel centre" convention: destination pixel (x, y)
samples the source at ((x + 0.5) * sw / dw - 0.5, (y + 0.5) * sh / dh - 0.5).
All arithmetic is float64 numpy, so the same input always yields the same
output bytes on a given platform.
"""

from __future__ import annotations

import numpy as np


def _to_array(image) -> np.ndarray:
    return np.asarray(image, dtype=np.uint8)


def _to_image(array: np.ndarray):
    from PIL import Image

    return Image.fromarray(array)


def _sample_coords(src_len: int, dst_len: int) -> np.ndarray:
    """Source coordinates (float64) of destination pixel centres."""
    scale = src_len / dst_len
    return (np.arange(dst_len, dtype=np.float64) + 0.5) * scale - 0.5


def resize_nearest(image, out_w: int, out_h: int):
    """Nearest-neighbour resize; ties resolve toward the lower index."""
    src = _to_array(image)
    sh, sw = src.shape[:2]
    xs = np.clip(np.floor(_sample_coords(sw, out_w) + 0.5), 0, sw - 1).astype(np.int64)
    ys = np.clip(np.floor(_sample_coords(sh, out_h) + 0.5), 0, sh - 1).astype(np.int64)
    return _to_image(src[np.ix_(ys, xs)])


def resize_bilinear(image, out_w: int, out_h: int):
    """Bilinear resize with edge clamping, computed in float64."""
    src = _to_array(image).astype(np.float64)
    sh, sw = src.shape[:2]

    xs = np.clip(_sample_coords(sw, out_w), 0.0, sw - 1.0)
    ys = np.clip(_sample_coords(sh, out_h), 0.0, sh - 1.0)

    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    x1 = np.minimum(x0 + 1, sw - 1)
    y1 = np.minimum(y0 + 1, sh - 1)
    wx = (xs - x0)[np.newaxis, :, np.newaxis]
    wy = (ys - y0)[:, np.newaxis, np.newaxis]

    top = src[np.ix_(y0, x0)] * (1.0 - wx) + src[np.ix_(y0, x1)] * wx
    bottom = src[np.ix_(y1, x0)] * (1.0 - wx) + src[np.ix_(y1, x1)] * wx
    out = top * (1.0 - wy) + bottom * wy

    out = np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8)
    return _to_image(out)


def resize(image, out_w: int, out_h: int, mode: str):
    if mode == "nearest":
        return resize_nearest(image, out_w, out_h)
    if mode == "bilinear":
        return resize_bilinear(image, out_w, out_h)
    raise ValueError(f"unknown resample mode: {mode}")
