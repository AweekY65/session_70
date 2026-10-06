"""Pyramid + tile builder with incremental rebuild and atomic writes.

Layout of the output directory:

    <out_dir>/manifest.json
    <out_dir>/tiles/<level>/<x>_<y>.png

Level numbering: level 0 is the coarsest (smallest) level; the highest
level is the original image at full resolution. Each step up doubles the
dimensions (ceil-halving when going down).

Atomicity: every file (tiles and manifest) is written to a temporary
file in the same directory and then moved into place with os.replace().
The manifest is written last, so an interrupted build never leaves a
manifest that references half-written tiles.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .config import PyramidConfig
from .resample import resize

MANIFEST_NAME = "manifest.json"
TILES_DIR = "tiles"
MANIFEST_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(data: bytes, path: Path) -> None:
    """Write data to path atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _save_png_atomic(image, path: Path) -> None:
    """Encode and atomically write one PNG tile."""
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    _atomic_write_bytes(buffer.getvalue(), path)


def _cleanup_stale_tmp(out_dir: Path) -> None:
    """Remove leftover temp files from an interrupted previous run."""
    if not out_dir.exists():
        return
    for tmp in out_dir.rglob("*.tmp-*"):
        try:
            tmp.unlink()
        except OSError:
            pass


def compute_levels(width: int, height: int, min_size: int) -> list:
    """Return [(level, w, h), ...] from coarsest (0) to finest (original).

    Going down one level halves both dimensions with ceil rounding, so odd
    sizes shrink as (n + 1) // 2. The pyramid stops at the first level
    where both dimensions are <= min_size.
    """
    if width < 1 or height < 1:
        raise ValueError("image dimensions must be positive")
    sizes = [(width, height)]
    while sizes[-1][0] > min_size or sizes[-1][1] > min_size:
        w, h = sizes[-1]
        sizes.append((max(1, (w + 1) // 2), max(1, (h + 1) // 2)))
    sizes.reverse()
    return [(level, w, h) for level, (w, h) in enumerate(sizes)]


def iter_tiles(width: int, height: int, tile_size: int):
    """Yield (tx, ty, x0, y0, tw, th) covering a width x height level."""
    for ty in range((height + tile_size - 1) // tile_size):
        for tx in range((width + tile_size - 1) // tile_size):
            x0, y0 = tx * tile_size, ty * tile_size
            yield tx, ty, x0, y0, min(tile_size, width - x0), min(tile_size, height - y0)


def tile_rel_path(level: int, tx: int, ty: int) -> str:
    return f"{TILES_DIR}/{level}/{tx}_{ty}.png"


@dataclass
class BuildResult:
    out_dir: Path
    levels: int = 0
    tiles_total: int = 0
    tiles_written: int = 0
    tiles_reused: int = 0
    tiles_removed: int = 0
    manifest_path: Path = None
    manifest: dict = field(default_factory=dict)


def _load_manifest(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("version") != MANIFEST_VERSION:
            return None
        return data
    except (OSError, ValueError):
        return None


def _tile_is_valid(out_dir: Path, entry: dict) -> bool:
    path = out_dir / entry["file"]
    if not path.is_file():
        return False
    return sha256_file(path) == entry["sha256"]


def build_pyramid(source_path, out_dir, config: PyramidConfig | None = None) -> BuildResult:
    """Build (or incrementally rebuild) the pyramid for source_path."""
    config = config or PyramidConfig()
    source_path = Path(source_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_tmp(out_dir)

    source_hash = sha256_file(source_path)
    with Image.open(source_path) as opened:
        source_image = opened.convert("RGB")
    src_w, src_h = source_image.size

    levels = compute_levels(src_w, src_h, config.min_size)
    old_manifest = _load_manifest(out_dir / MANIFEST_NAME)
    old_tiles = {}
    reusable = False
    if old_manifest is not None:
        old_source = old_manifest.get("source", {})
        try:
            old_config = PyramidConfig.from_dict(old_manifest.get("config", {}))
        except (TypeError, ValueError, KeyError):
            old_config = None
        reusable = (
            old_source.get("sha256") == source_hash
            and old_config == config
        )
        if reusable:
            for old_level in old_manifest.get("levels", []):
                for entry in old_level.get("tiles", []):
                    old_tiles[entry["file"]] = entry

    result = BuildResult(out_dir=out_dir, levels=len(levels))
    manifest_levels = []
    referenced = set()

    for level, level_w, level_h in levels:
        if (level_w, level_h) == (src_w, src_h):
            level_image = source_image
        else:
            level_image = resize(source_image, level_w, level_h, config.resample)

        tile_entries = []
        for tx, ty, x0, y0, tw, th in iter_tiles(level_w, level_h, config.tile_size):
            rel = tile_rel_path(level, tx, ty)
            referenced.add(rel)
            old_entry = old_tiles.get(rel)
            if (
                old_entry is not None
                and old_entry["width"] == (config.tile_size if config.edge == "pad" else tw)
                and old_entry["height"] == (config.tile_size if config.edge == "pad" else th)
                and _tile_is_valid(out_dir, old_entry)
            ):
                tile_entries.append(old_entry)
                result.tiles_reused += 1
                continue

            tile = level_image.crop((x0, y0, x0 + tw, y0 + th))
            if config.edge == "pad" and (tw != config.tile_size or th != config.tile_size):
                canvas = Image.new("RGB", (config.tile_size, config.tile_size), tuple(config.pad_color))
                canvas.paste(tile, (0, 0))
                tile = canvas

            target = out_dir / rel
            _save_png_atomic(tile, target)
            entry = {
                "x": tx,
                "y": ty,
                "file": rel,
                "width": tile.size[0],
                "height": tile.size[1],
                "sha256": sha256_file(target),
            }
            tile_entries.append(entry)
            result.tiles_written += 1

        result.tiles_total += len(tile_entries)
        manifest_levels.append(
            {
                "level": level,
                "width": level_w,
                "height": level_h,
                "scale": 2 ** (levels[-1][0] - level),
                "tiles": tile_entries,
            }
        )

    # Remove tile files from previous builds that are no longer referenced.
    tiles_root = out_dir / TILES_DIR
    if tiles_root.is_dir():
        for stale in sorted(tiles_root.rglob("*.png")):
            rel = stale.relative_to(out_dir).as_posix()
            if rel not in referenced:
                stale.unlink()
                result.tiles_removed += 1
        for empty_dir in sorted((p for p in tiles_root.rglob("*") if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
            try:
                empty_dir.rmdir()
            except OSError:
                pass

    manifest = {
        "version": MANIFEST_VERSION,
        "source": {
            "path": source_path.name,
            "sha256": source_hash,
            "width": src_w,
            "height": src_h,
        },
        "config": config.to_dict(),
        "levels": manifest_levels,
    }
    manifest_path = out_dir / MANIFEST_NAME
    _atomic_write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        manifest_path,
    )
    result.manifest_path = manifest_path
    result.manifest = manifest
    return result
