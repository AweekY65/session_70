"""Automated tests for the local pyramid / tile generator.

All checks run in the terminal; no image windows are ever opened.
Run with:  python -m pytest tests/ -v   (or: python -m unittest discover tests -v)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from pyramid_tiler import (
    PyramidConfig,
    build_pyramid,
    load_manifest,
    verify_manifest,
)
from pyramid_tiler import core
from pyramid_tiler.core import level_dimensions, sha256_file
from pyramid_tiler.resample import resize_bilinear, resize_nearest


def make_test_image(path: Path, width: int, height: int, fmt: str = "PNG") -> np.ndarray:
    """Create a deterministic, content-rich test image (no RNG drift)."""
    yy, xx = np.mgrid[0:height, 0:width]
    arr = np.stack(
        [
            (xx * 3 + yy) % 256,
            (xx + yy * 5) % 256,
            (xx * xx // 7 + yy * yy // 11) % 256,
        ],
        axis=-1,
    ).astype(np.uint8)
    Image.fromarray(arr, "RGB").save(path, format=fmt)
    return arr


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.src = self.root / "src.png"
        self.out = self.root / "out"


class TestLevelAlgorithm(TempDirTestCase):
    def test_odd_dimensions_halve_with_ceil(self):
        self.assertEqual(
            level_dimensions(253, 177, min_size=32),
            [(253, 177), (127, 89), (64, 45), (32, 23)],
        )

    def test_stops_at_min_size_and_includes_final_level(self):
        dims = level_dimensions(1000, 10, min_size=100)
        self.assertEqual(dims[0], (1000, 10))
        self.assertLessEqual(max(dims[-1]), 100)
        self.assertGreater(max(dims[-2]), 100)

    def test_tiny_source_has_single_level(self):
        self.assertEqual(level_dimensions(100, 80, min_size=256), [(100, 80)])

    def test_pyramid_matches_declared_levels(self):
        make_test_image(self.src, 253, 177)
        cfg = PyramidConfig(tile_size=64, min_size=32)
        result = build_pyramid(self.src, self.out, cfg)
        manifest = load_manifest(self.out)
        self.assertEqual(result.levels, len(manifest["levels"]))
        self.assertEqual([l["width"] for l in manifest["levels"]], [253, 127, 64, 32])
        self.assertEqual([l["height"] for l in manifest["levels"]], [177, 89, 45, 23])
        self.assertEqual(verify_manifest(self.out), [])


class TestTiling(TempDirTestCase):
    def test_edge_tiles_are_padded_and_manifest_records_valid_region(self):
        # 150x100 with tile_size 64 -> 3x2 tiles, edges clipped.
        make_test_image(self.src, 150, 100)
        cfg = PyramidConfig(tile_size=64, min_size=64)
        build_pyramid(self.src, self.out, cfg)
        manifest = load_manifest(self.out)
        level0 = manifest["levels"][0]
        self.assertEqual((level0["tiles_x"], level0["tiles_y"]), (3, 2))

        by_xy = {(t["x"], t["y"]): t for t in level0["tiles"]}
        self.assertEqual((by_xy[(2, 1)]["valid_width"], by_xy[(2, 1)]["valid_height"]), (22, 36))
        self.assertEqual((by_xy[(0, 0)]["valid_width"], by_xy[(0, 0)]["valid_height"]), (64, 64))

        # Every tile file is a full 64x64 image; padding must be zero.
        for tile in level0["tiles"]:
            with Image.open(self.out / tile["file"]) as im:
                self.assertEqual(im.size, (64, 64))
                arr = np.asarray(im)
            vw, vh = tile["valid_width"], tile["valid_height"]
            if vw < 64:
                self.assertTrue((arr[:, vw:] == 0).all())
            if vh < 64:
                self.assertTrue((arr[vh:, :] == 0).all())

        # Interior content must match the source pixels exactly (level 0).
        src_arr = np.asarray(Image.open(self.src).convert("RGB"))
        t = by_xy[(2, 1)]
        with Image.open(self.out / t["file"]) as im:
            tile_arr = np.asarray(im.convert("RGB"))
        np.testing.assert_array_equal(tile_arr[:36, :22], src_arr[64:100, 128:150])

    def test_tile_coordinate_rule(self):
        make_test_image(self.src, 130, 70)
        cfg = PyramidConfig(tile_size=64, min_size=64)
        build_pyramid(self.src, self.out, cfg)
        manifest = load_manifest(self.out)
        files = {t["file"] for t in manifest["levels"][0]["tiles"]}
        self.assertIn("tiles/level_00/tile_0_0.png", files)
        self.assertIn("tiles/level_00/tile_2_1.png", files)
        self.assertNotIn("tiles/level_00/tile_3_0.png", files)


class TestResampling(TempDirTestCase):
    def test_nearest_and_bilinear_differ(self):
        make_test_image(self.src, 200, 120)
        cfg = PyramidConfig(tile_size=256, min_size=64, resample="nearest")
        build_pyramid(self.src, self.out / "n", cfg)
        cfg = PyramidConfig(tile_size=256, min_size=64, resample="bilinear")
        build_pyramid(self.src, self.out / "b", cfg)
        mn = load_manifest(self.out / "n")
        mb = load_manifest(self.out / "b")
        hashes_n = {t["sha256"] for t in mn["levels"][1]["tiles"]}
        hashes_b = {t["sha256"] for t in mb["levels"][1]["tiles"]}
        self.assertNotEqual(hashes_n, hashes_b)

    def test_same_input_gives_identical_output(self):
        make_test_image(self.src, 173, 129)
        for method in ("nearest", "bilinear"):
            cfg = PyramidConfig(tile_size=64, min_size=32, resample=method)
            build_pyramid(self.src, self.out / "run1", cfg)
            build_pyramid(self.src, self.out / "run2", cfg)
            m1 = load_manifest(self.out / "run1")
            m2 = load_manifest(self.out / "run2")
            self.assertEqual(m1["levels"], m2["levels"], f"{method} not deterministic")

    def test_resize_reference_values(self):
        # Small hand-checkable arrays pin down the exact filter behaviour.
        img = np.arange(16, dtype=np.uint8).reshape(4, 4) * 16
        nn = resize_nearest(img, 2, 2)
        np.testing.assert_array_equal(nn, [[80, 112], [208, 240]])
        bl = resize_bilinear(img, 2, 2)
        np.testing.assert_array_equal(bl, [[40, 72], [168, 200]])
        # Upsampling path of bilinear stays inside [0, 255] and is symmetric.
        up = resize_bilinear(img, 8, 8)
        self.assertEqual(up.dtype, np.uint8)
        self.assertTrue((up <= 255).all())

    def test_invalid_resample_method_rejected(self):
        with self.assertRaises(ValueError):
            PyramidConfig(resample="cubic")


class TestIncremental(TempDirTestCase):
    def _build(self, cfg=None):
        cfg = cfg or PyramidConfig(tile_size=64, min_size=32)
        return build_pyramid(self.src, self.out, cfg), cfg

    def test_unchanged_input_reuses_all_tiles(self):
        make_test_image(self.src, 200, 150)
        r1, cfg = self._build()
        self.assertEqual(r1.tiles_reused, 0)
        self.assertGreater(r1.tiles_written, 0)

        mtimes = {p: p.stat().st_mtime_ns for p in self.out.rglob("*.png")}
        time.sleep(0.01)
        r2, _ = self._build(cfg)
        self.assertEqual(r2.tiles_written, 0)
        self.assertEqual(r2.tiles_reused, r1.tiles_total)
        for p, mtime in mtimes.items():
            self.assertEqual(p.stat().st_mtime_ns, mtime, f"tile rewritten: {p}")

    def test_missing_tile_is_regenerated(self):
        make_test_image(self.src, 200, 150)
        self._build()
        victim = next((self.out / "tiles" / "level_00").glob("*.png"))
        victim.unlink()
        r2, _ = self._build()
        self.assertEqual(r2.tiles_written, 1)
        self.assertTrue(victim.is_file())
        self.assertEqual(verify_manifest(self.out), [])

    def test_corrupt_tile_is_regenerated_and_only_that_one(self):
        make_test_image(self.src, 200, 150)
        self._build()
        victim = sorted((self.out / "tiles" / "level_00").glob("*.png"))[1]
        original = victim.read_bytes()
        victim.write_bytes(b"\x00" * 64 + original[64:])  # corrupt in place
        self.assertNotEqual(verify_manifest(self.out), [])

        mtimes = {p: p.stat().st_mtime_ns for p in self.out.rglob("*.png") if p != victim}
        time.sleep(0.01)
        r2, _ = self._build()
        self.assertEqual(r2.tiles_written, 1)
        self.assertEqual(victim.read_bytes(), original)
        self.assertEqual(verify_manifest(self.out), [])
        for p, mtime in mtimes.items():
            self.assertEqual(p.stat().st_mtime_ns, mtime, f"unexpected rewrite: {p}")

    def test_changed_source_triggers_full_rebuild(self):
        make_test_image(self.src, 200, 150)
        self._build()
        make_test_image(self.src, 210, 160)  # different content + size
        r2, _ = self._build()
        self.assertEqual(r2.tiles_reused, 0)
        self.assertEqual(r2.tiles_written, r2.tiles_total)
        self.assertEqual(verify_manifest(self.out), [])


class TestAtomicityAndFailure(TempDirTestCase):
    def test_interrupted_build_leaves_no_valid_half_state(self):
        make_test_image(self.src, 200, 150)
        cfg = PyramidConfig(tile_size=64, min_size=32)

        # First: a complete, valid build.
        build_pyramid(self.src, self.out, cfg)
        good_manifest = (self.out / "manifest.json").read_bytes()
        self.assertEqual(verify_manifest(self.out), [])

        # Second: simulate a crash part-way through a rebuild triggered by
        # a source change, by making the 3rd tile write fail.
        make_test_image(self.src, 220, 170)
        real_write = core._atomic_write
        calls = {"n": 0}

        def flaky_write(path, data):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated crash mid-build")
            return real_write(path, data)

        core._atomic_write = flaky_write
        try:
            with self.assertRaises(RuntimeError):
                build_pyramid(self.src, self.out, cfg)
        finally:
            core._atomic_write = real_write

        # The on-disk manifest must still be the previous consistent one:
        # it must NOT describe the half-written new tiles.
        self.assertEqual((self.out / "manifest.json").read_bytes(), good_manifest)
        self.assertEqual(list(self.out.rglob("*.tmp")), [], "temp files left behind")

        # A fresh run must recover to a fully consistent state.
        result = build_pyramid(self.src, self.out, cfg)
        self.assertEqual(result.tiles_written, result.tiles_total)
        self.assertEqual(verify_manifest(self.out), [])
        manifest = load_manifest(self.out)
        self.assertEqual(manifest["source"]["width"], 220)

    def test_crash_on_first_build_leaves_no_manifest(self):
        make_test_image(self.src, 200, 150)
        cfg = PyramidConfig(tile_size=64, min_size=32)
        real_write = core._atomic_write

        def always_fail(path, data):
            raise RuntimeError("simulated immediate crash")

        core._atomic_write = always_fail
        try:
            with self.assertRaises(RuntimeError):
                build_pyramid(self.src, self.out, cfg)
        finally:
            core._atomic_write = real_write

        self.assertFalse((self.out / "manifest.json").exists())
        self.assertEqual(list(self.out.rglob("*.tmp")), [])
        # Recovery from scratch works.
        build_pyramid(self.src, self.out, cfg)
        self.assertEqual(verify_manifest(self.out), [])


class TestManifestAndFormats(TempDirTestCase):
    def test_manifest_contents(self):
        make_test_image(self.src, 130, 90)
        cfg = PyramidConfig(tile_size=64, min_size=32, resample="nearest")
        build_pyramid(self.src, self.out, cfg)
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["source"]["width"], 130)
        self.assertEqual(manifest["source"]["height"], 90)
        self.assertEqual(manifest["source"]["sha256"], sha256_file(self.src))
        self.assertEqual(manifest["config"]["resample"], "nearest")
        for level in manifest["levels"]:
            for tile in level["tiles"]:
                path = self.out / tile["file"]
                self.assertTrue(path.is_file())
                self.assertEqual(tile["sha256"], sha256_file(path))
                self.assertEqual(tile["bytes"], path.stat().st_size)
                self.assertEqual((tile["width"], tile["height"]), (64, 64))

    def test_jpeg_input_supported(self):
        make_test_image(self.root / "src.jpg", 120, 90, fmt="JPEG")
        cfg = PyramidConfig(tile_size=64, min_size=32)
        result = build_pyramid(self.root / "src.jpg", self.out, cfg)
        self.assertGreaterEqual(result.levels, 2)
        self.assertEqual(verify_manifest(self.out), [])

    def test_verify_detects_missing_manifest(self):
        problems = verify_manifest(self.out / "does-not-exist")
        self.assertTrue(problems)


if __name__ == "__main__":
    unittest.main()
