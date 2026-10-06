"""Local image pyramid and tile generator.

Everything is stored on the local filesystem or in memory. No map server,
CDN, cloud storage or any other external service is involved.
"""

from .config import PyramidConfig
from .builder import BuildResult, build_pyramid, compute_levels
from .resample import resize_bilinear, resize_nearest

__all__ = [
    "PyramidConfig",
    "BuildResult",
    "build_pyramid",
    "compute_levels",
    "resize_bilinear",
    "resize_nearest",
]

__version__ = "1.0.0"
