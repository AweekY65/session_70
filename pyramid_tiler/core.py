"""Core pyramid / tile generation logic.

Storage layout under the output directory::

    <out>/
        manifest.json
        tiles/
            level_00/tile_0_0.png ...
            level_01/...

Tiling rules
------------
* Tile (x, y) covers source pixels ``[x*ts, x*ts+ts) x [y*ts, y*ts+ts)``
  with the origin at the top-left corner of the level image.
* Every tile file is exactly ``tile_size x tile_size`` pixels.  Edge tiles
  whose image region is smaller than a full tile are **zero padded**
  (transparent black for RGBA, black otherwise); the real content size is
  recorded in the manifest as ``valid_width`` / ``valid_height``.

Atomicity
---------
Every tile is written to a temporary file in the same directory and then
``os.replace``-d into place.  ``manifest.json`` is written last, also
atomically.  If the process is interrupted, the on-disk manifest is always
the previous fully-consistent one; half-written files can never be
referenced by it, and stale temporary files are removed on the next run.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

from .resample import resize

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
GENERATOR = "pyramid-tiler"


class ManifestError(Exception):
    """Raised when a manifest is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class PyramidConfig:
    tile_size: int = 256
    resample: str = "bilinear"  # "nearest" | "bilinear"
    min_size: int = 256  # stop when max(level_w, level_h) <= min_size
    tile_format: str = "png"  # lossless, keeps hashes stable

    def __post_init__(self):
        if self.tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if self.min_size <= 0:
            raise ValueError("min_size must be positive")
        if self.resample not in ("nearest", "bilinear"):
            raise ValueError(f"unsupported resample method: {self.resample!r}")
        if self.tile_format != "png":
            raise ValueError("only png tile_format is supported")

    def to_dict(self) -> dict:
        return {
            "tile_size": self.tile_size,
            "resample": self.resample,
            "min_size": self.min_size,
            "tile_format": self.tile_format,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PyramidConfig":
        return cls(
            tile_size=d["tile_size"],
            resample=d["resample"],
            min_size=d["min_size"],
            tile_format=d.get("tile_format", "png"),
        )


@dataclass
class BuildResult:
    output_dir: Path
    levels: int
    tiles_total: int
    tiles_written: int
    tiles_reused: int
    manifest_path: Path
    interrupted: bool = False
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically (temp file + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clean_stale_temps(directory: Path) -> int:
    """Remove leftover ``*.tmp`` files from interrupted runs."""
    removed = 0
    if not directory.is_dir():
        return 0
    for tmp in directory.rglob("*.tmp"):
        try:
            tmp.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _load_source(path: Path) -> tuple[np.ndarray, str, str]:
    """Return (uint8 HxWxC array, mode, sha256 of the original file)."""
    digest = sha256_file(path)
    with Image.open(path) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
        elif im.mode != "RGB":
            im = im.convert("RGB")
        arr = np.asarray(im, dtype=np.uint8).copy()
    return arr, im.mode, digest


def level_dimensions(width: int, height: int, min_size: int) -> list[tuple[int, int]]:
    """Dimensions of every pyramid level, level 0 being the source.

    Each level halves the previous one (ceil division, clamped to >= 1).
    Generation stops once ``max(w, h) <= min_size``; that final small level
    is included.
    """
    dims = [(width, height)]
    while max(width, height) > min_size:
        width = max(1, -(-width // 2))  # ceil halving
        height = max(1, -(-height // 2))
        dims.append((width, height))
    return dims


def _tile_rel_path(level: int, tx: int, ty: int) -> str:
    return f"tiles/level_{level:02d}/tile_{tx}_{ty}.png"


def _encode_tile(tile: np.ndarray, mode: str) -> bytes:
    img = Image.fromarray(tile, mode)
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _make_tiles(level_img: np.ndarray, tile_size: int) -> list[dict]:
    """Cut a level image into zero-padded fixed-size tiles (in memory)."""
    h, w = level_img.shape[:2]
    channels = level_img.shape[2] if level_img.ndim == 3 else 1
    tiles_x = -(-w // tile_size)
    tiles_y = -(-h // tile_size)
    tiles = []
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            x0, y0 = tx * tile_size, ty * tile_size
            valid_w = min(tile_size, w - x0)
            valid_h = min(tile_size, h - y0)
            if valid_w == tile_size and valid_h == tile_size:
                tile = level_img[y0:y0 + tile_size, x0:x0 + tile_size]
            else:
                shape = (tile_size, tile_size, channels) if channels > 1 else (tile_size, tile_size)
                tile = np.zeros(shape, dtype=np.uint8)
                tile[:valid_h, :valid_w] = level_img[y0:y0 + valid_h, x0:x0 + valid_w]
            tiles.append({"x": tx, "y": ty, "valid_width": valid_w,
                          "valid_height": valid_h, "data": tile})
    return tiles


# ---------------------------------------------------------------------------
# manifest I/O
# ---------------------------------------------------------------------------

def load_manifest(output_dir: Path) -> dict:
    path = Path(output_dir) / MANIFEST_NAME
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if manifest.get("version") != MANIFEST_VERSION:
        raise ManifestError(f"unsupported manifest version: {manifest.get('version')!r}")
    return manifest


def verify_manifest(output_dir: Path) -> list[str]:
    """Check every tile referenced by the manifest. Returns a list of problems."""
    problems: list[str] = []
    try:
        manifest = load_manifest(output_dir)
    except ManifestError as exc:
        return [str(exc)]
    base = Path(output_dir)
    for level in manifest.get("levels", []):
        for tile in level.get("tiles", []):
            path = base / tile["file"]
            if not path.is_file():
                problems.append(f"missing tile: {tile['file']}")
                continue
            if sha256_file(path) != tile["sha256"]:
                problems.append(f"corrupt tile (hash mismatch): {tile['file']}")
    return problems


# ---------------------------------------------------------------------------
# main build
# ---------------------------------------------------------------------------

def build_pyramid(
    source: os.PathLike | str,
    output_dir: os.PathLike | str,
    config: Optional[PyramidConfig] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> BuildResult:
    """Build (or incrementally rebuild) an image pyramid under *output_dir*.

    Incremental behaviour: if an existing manifest matches the current
    source file hash and configuration, tiles whose file exists and whose
    sha256 matches the manifest are left untouched; missing or corrupt
    tiles are regenerated.  Any configuration or source change triggers a
    full rebuild of affected levels.
    """
    config = config or PyramidConfig()
    source = Path(source)
    output_dir = Path(output_dir)
    log = progress or (lambda msg: None)

    if not source.is_file():
        raise FileNotFoundError(f"source image not found: {source}")

    removed = _clean_stale_temps(output_dir)
    if removed:
        log(f"removed {removed} stale temporary file(s)")

    src_arr, mode, src_hash = _load_source(source)
    src_h, src_w = src_arr.shape[:2]
    log(f"source {source.name}: {src_w}x{src_h} {mode} sha256={src_hash[:12]}...")

    # --- figure out what can be reused from a previous run ---------------
    old_manifest: Optional[dict] = None
    try:
        candidate = load_manifest(output_dir)
        same_source = candidate["source"]["sha256"] == src_hash
        same_config = candidate["config"] == config.to_dict()
        if same_source and same_config:
            old_manifest = candidate
            log("existing manifest matches source and config: incremental rebuild")
        else:
            log("source or config changed: full rebuild")
    except ManifestError:
        log("no usable manifest found: full rebuild")

    old_tiles: dict[str, dict] = {}
    if old_manifest:
        for lvl in old_manifest["levels"]:
            for t in lvl["tiles"]:
                old_tiles[t["file"]] = t

    dims = level_dimensions(src_w, src_h, config.min_size)
    manifest_levels = []
    tiles_total = tiles_written = tiles_reused = 0

    level_img = src_arr
    for level_idx, (lw, lh) in enumerate(dims):
        if level_idx > 0:
            level_img = resize(level_img, lw, lh, config.resample)
        assert level_img.shape[1] == lw and level_img.shape[0] == lh

        tiles = _make_tiles(level_img, config.tile_size)
        tiles_x = -(-lw // config.tile_size)
        tiles_y = -(-lh // config.tile_size)
        tile_entries = []

        for t in tiles:
            rel = _tile_rel_path(level_idx, t["x"], t["y"])
            payload = _encode_tile(t["data"], mode)
            digest = sha256_bytes(payload)
            entry = {
                "x": t["x"],
                "y": t["y"],
                "file": rel,
                "width": config.tile_size,
                "height": config.tile_size,
                "valid_width": t["valid_width"],
                "valid_height": t["valid_height"],
                "sha256": digest,
                "bytes": len(payload),
            }
            tiles_total += 1

            old = old_tiles.get(rel)
            dst = output_dir / rel
            if (
                old is not None
                and old["sha256"] == digest
                and dst.is_file()
                and sha256_file(dst) == digest
            ):
                tiles_reused += 1
            else:
                _atomic_write(dst, payload)
                tiles_written += 1
                reason = "missing/corrupt" if old is not None else "new"
                log(f"wrote {rel} ({reason})")
            tile_entries.append(entry)

        manifest_levels.append({
            "level": level_idx,
            "width": lw,
            "height": lh,
            "tiles_x": tiles_x,
            "tiles_y": tiles_y,
            "tiles": tile_entries,
        })
        log(f"level {level_idx}: {lw}x{lh} -> {tiles_x}x{tiles_y} tiles")

    manifest = {
        "version": MANIFEST_VERSION,
        "generator": GENERATOR,
        "source": {
            "file": source.name,
            "sha256": src_hash,
            "width": src_w,
            "height": src_h,
            "mode": mode,
        },
        "config": config.to_dict(),
        "levels": manifest_levels,
    }
    # The manifest is written LAST and atomically: an interrupted run can
    # never leave a manifest that vouches for half-written tiles.
    manifest_path = output_dir / MANIFEST_NAME
    _atomic_write(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    log(f"manifest written: {manifest_path}")

    return BuildResult(
        output_dir=output_dir,
        levels=len(dims),
        tiles_total=tiles_total,
        tiles_written=tiles_written,
        tiles_reused=tiles_reused,
        manifest_path=manifest_path,
    )
