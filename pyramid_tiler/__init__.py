"""Local image pyramid and tile generator.

Everything is stored in local files or in memory: no map server, CDN,
cloud storage or any external service is involved.
"""

from .core import (
    PyramidConfig,
    BuildResult,
    build_pyramid,
    load_manifest,
    verify_manifest,
    ManifestError,
)
from .resample import resize_nearest, resize_bilinear

__all__ = [
    "PyramidConfig",
    "BuildResult",
    "build_pyramid",
    "load_manifest",
    "verify_manifest",
    "ManifestError",
    "resize_nearest",
    "resize_bilinear",
]

__version__ = "1.0.0"
