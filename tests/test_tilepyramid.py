"""Automated tests for tilepyramid. All checks run in the terminal; no
image windows are ever opened."""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

import tilepyramid.builder as builder
from tilepyramid import PyramidConfig, build_pyramid, compute_levels


def make_gradient(path: Path, width: int, height: int, fmt: str = "PNG") -> Path:
    """Deterministic gradient test image with distinct RGB channels."""
    xs = np.arange(width, dtype=np.uint32)[np.newaxis, :]
    ys = np.arange(height, dtype=np.uint32)[:, np.newaxis]
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[..., 0] = (xs * 3 + ys) % 256
    arr[..., 1] = (xs + ys * 5) % 256
    arr[..., 2] = (xs * xs // 7 + ys * ys // 11) % 256
    Image.fromarray(arr).save(path, format=fmt)
    return path


def manifest_hashes_ok(out_dir: Path) -> bool:
    with open(out_dir / "manifest.json", encoding="utf-8") as handle:
        manifest = json.load(handle)
    for level in manifest["levels"]:
        for tile in level["tiles"]:
            tile_path = out_dir / tile["file"]
            if not tile_path.is_file():
                return False
            if builder.sha256_file(tile_path) != tile["sha256"]:
                return False
            with Image.open(tile_path) as img:
                if img.size != (tile["width"], tile["height"]):
                    return False
    return True


# ---------------------------------------------------------------- levels

def test_compute_levels_odd_sizes():
    levels = compute_levels(37, 53, min_size=8)
    assert levels[-1] == (len(levels) - 1, 37, 53)
    # coarsest level fits within min_size, the one above does not
    assert max(levels[0][1], levels[0][2]) <= 8
    assert max(levels[1][1], levels[1][2]) > 8
    # each step up doubles with ceil rounding: 37 -> 19 -> 10 -> 5
    widths = [w for _, w, _ in levels]
    assert widths == sorted(widths)
    assert widths[-2] == (37 + 1) // 2


def test_compute_levels_small_image_single_level():
    assert compute_levels(10, 10, min_size=256) == [(0, 10, 10)]


# ---------------------------------------------------------------- tiling

def test_odd_size_crop_tiles_cover_image(tmp_path):
    src = make_gradient(tmp_path / "odd.png", 37, 53)
    cfg = PyramidConfig(tile_size=16, min_size=8, resample="nearest", edge="crop")
    result = build_pyramid(src, tmp_path / "out", cfg)
    assert manifest_hashes_ok(tmp_path / "out")
    for level in result.manifest["levels"]:
        # tiles exactly cover the level: no overlap, no gap
        xs = sorted({t["x"] for t in level["tiles"]})
        ys = sorted({t["y"] for t in level["tiles"]})
        assert xs == list(range((level["width"] + 15) // 16))
        assert ys == list(range((level["height"] + 15) // 16))
        for t in level["tiles"]:
            expected_w = min(16, level["width"] - t["x"] * 16)
            expected_h = min(16, level["height"] - t["y"] * 16)
            assert (t["width"], t["height"]) == (expected_w, expected_h)


def test_edge_pad_fills_to_full_tile(tmp_path):
    src = make_gradient(tmp_path / "img.png", 20, 12)
    cfg = PyramidConfig(tile_size=16, min_size=64, edge="pad", pad_color=(7, 8, 9))
    result = build_pyramid(src, tmp_path / "out", cfg)
    assert result.levels == 1  # image already <= min_size
    tiles = result.manifest["levels"][0]["tiles"]
    assert len(tiles) == 2  # 2 x 1 grid
    for t in tiles:
        assert (t["width"], t["height"]) == (16, 16)
    # the padded tile carries pad_color in the padding area
    padded = tmp_path / "out" / tiles[1]["file"]
    pixel = Image.open(padded).convert("RGB").getpixel((15, 15))
    assert pixel == (7, 8, 9)


def test_edge_crop_keeps_partial_size(tmp_path):
    src = make_gradient(tmp_path / "img.png", 20, 12)
    cfg = PyramidConfig(tile_size=16, min_size=64, edge="crop")
    result = build_pyramid(src, tmp_path / "out", cfg)
    tiles = result.manifest["levels"][0]["tiles"]
    sizes = sorted((t["width"], t["height"]) for t in tiles)
    assert sizes == [(4, 12), (16, 12)]


# -------------------------------------------------------------- resample

def test_nearest_and_bilinear_differ_and_are_stable(tmp_path):
    src = make_gradient(tmp_path / "img.png", 64, 64)
    hashes = {}
    for mode in ("nearest", "bilinear"):
        for run in (0, 1):
            out = tmp_path / f"{mode}-{run}"
            cfg = PyramidConfig(tile_size=64, min_size=8, resample=mode)
            result = build_pyramid(src, out, cfg)
            level_hashes = tuple(
                t["sha256"] for lvl in result.manifest["levels"] for t in lvl["tiles"]
            )
            hashes.setdefault(mode, []).append(level_hashes)
    # deterministic: identical input -> identical tile hashes on rebuild
    assert hashes["nearest"][0] == hashes["nearest"][1]
    assert hashes["bilinear"][0] == hashes["bilinear"][1]
    # the two resampling methods actually produce different pyramids
    assert hashes["nearest"][0] != hashes["bilinear"][0]


# ----------------------------------------------------------- incremental

def test_incremental_rebuild_skips_unchanged_tiles(tmp_path):
    src = make_gradient(tmp_path / "img.png", 40, 30)
    cfg = PyramidConfig(tile_size=16, min_size=8)
    first = build_pyramid(src, tmp_path / "out", cfg)
    assert first.tiles_written == first.tiles_total > 0

    second = build_pyramid(src, tmp_path / "out", cfg)
    assert second.tiles_written == 0
    assert second.tiles_reused == second.tiles_total
    assert manifest_hashes_ok(tmp_path / "out")


def test_corrupted_tile_is_regenerated(tmp_path):
    src = make_gradient(tmp_path / "img.png", 40, 30)
    cfg = PyramidConfig(tile_size=16, min_size=8)
    first = build_pyramid(src, tmp_path / "out", cfg)
    victim = first.manifest["levels"][-1]["tiles"][0]
    victim_path = tmp_path / "out" / victim["file"]
    victim_path.write_bytes(b"corrupted!" + victim_path.read_bytes())

    second = build_pyramid(src, tmp_path / "out", cfg)
    assert second.tiles_written == 1
    assert builder.sha256_file(victim_path) == victim["sha256"]
    assert manifest_hashes_ok(tmp_path / "out")


def test_missing_tile_is_regenerated(tmp_path):
    src = make_gradient(tmp_path / "img.png", 40, 30)
    cfg = PyramidConfig(tile_size=16, min_size=8)
    first = build_pyramid(src, tmp_path / "out", cfg)
    victim = first.manifest["levels"][0]["tiles"][0]
    (tmp_path / "out" / victim["file"]).unlink()

    second = build_pyramid(src, tmp_path / "out", cfg)
    assert second.tiles_written == 1
    assert manifest_hashes_ok(tmp_path / "out")


def test_changed_source_rebuilds_everything(tmp_path):
    src = make_gradient(tmp_path / "img.png", 32, 32)
    cfg = PyramidConfig(tile_size=16, min_size=8)
    build_pyramid(src, tmp_path / "out", cfg)
    make_gradient(tmp_path / "img.png", 48, 24)  # same path, new content
    result = build_pyramid(src, tmp_path / "out", cfg)
    assert result.tiles_written == result.tiles_total
    assert result.tiles_reused == 0
    assert manifest_hashes_ok(tmp_path / "out")


# ------------------------------------------------------------- atomicity

def test_interrupted_build_leaves_no_valid_manifest(tmp_path, monkeypatch):
    src = make_gradient(tmp_path / "img.png", 40, 30)
    cfg = PyramidConfig(tile_size=16, min_size=8)
    out = tmp_path / "out"

    calls = {"n": 0}
    real_save = builder._save_png_atomic

    def flaky_save(image, path):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash mid-build")
        return real_save(image, path)

    monkeypatch.setattr(builder, "_save_png_atomic", flaky_save)
    with pytest.raises(RuntimeError):
        build_pyramid(src, out, cfg)

    # no manifest was written, so nothing can mistake the partial output
    # for a valid pyramid; leftover temp files are not valid tiles either
    assert not (out / "manifest.json").exists()
    assert not list(out.rglob("*.tmp-*"))

    monkeypatch.setattr(builder, "_save_png_atomic", real_save)
    result = build_pyramid(src, out, cfg)
    assert result.tiles_written == result.tiles_total  # full rebuild
    assert manifest_hashes_ok(out)


def test_stale_tmp_files_are_cleaned(tmp_path):
    src = make_gradient(tmp_path / "img.png", 20, 20)
    out = tmp_path / "out"
    (out / "tiles" / "0").mkdir(parents=True)
    (out / "tiles" / "0" / "0_0.png.tmp-999").write_bytes(b"junk")
    build_pyramid(src, out, PyramidConfig(tile_size=16, min_size=64))
    assert not list(out.rglob("*.tmp-*"))
    assert manifest_hashes_ok(out)


# ----------------------------------------------------------------- misc

def test_jpeg_input_supported(tmp_path):
    src = make_gradient(tmp_path / "img.jpg", 33, 21, fmt="JPEG")
    cfg = PyramidConfig(tile_size=16, min_size=8, resample="bilinear")
    result = build_pyramid(src, tmp_path / "out", cfg)
    assert result.manifest["source"]["width"] == 33
    assert result.manifest["source"]["height"] == 21
    assert manifest_hashes_ok(tmp_path / "out")


def test_manifest_records_required_fields(tmp_path):
    src = make_gradient(tmp_path / "img.png", 30, 18)
    cfg = PyramidConfig(tile_size=16, min_size=8, resample="nearest")
    result = build_pyramid(src, tmp_path / "out", cfg)
    manifest = result.manifest
    assert manifest["source"]["width"] == 30
    assert manifest["source"]["height"] == 18
    assert len(manifest["source"]["sha256"]) == 64
    assert manifest["config"]["resample"] == "nearest"
    for level in manifest["levels"]:
        assert {"level", "width", "height", "scale", "tiles"} <= set(level)
        for tile in level["tiles"]:
            assert {"x", "y", "file", "width", "height", "sha256"} <= set(tile)
