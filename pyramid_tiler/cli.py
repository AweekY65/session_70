"""Command line interface for the local pyramid / tile generator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import PyramidConfig, build_pyramid, verify_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyramid-tiler",
        description="Generate a local image pyramid and tiles (no external services).",
    )
    parser.add_argument("source", type=Path, help="input PNG/JPEG image")
    parser.add_argument("-o", "--output", type=Path, required=True, help="output directory")
    parser.add_argument("--tile-size", type=int, default=256, help="tile size in pixels (default 256)")
    parser.add_argument("--resample", choices=["nearest", "bilinear"], default="bilinear",
                        help="downscale filter (default bilinear)")
    parser.add_argument("--min-size", type=int, default=256,
                        help="stop when max(level width, height) <= this (default 256)")
    parser.add_argument("--verify", action="store_true",
                        help="only verify an existing output directory against its manifest")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    args = parser.parse_args(argv)

    if args.verify:
        problems = verify_manifest(args.output)
        if problems:
            for p in problems:
                print(f"FAIL: {p}")
            return 1
        print(f"OK: {args.output} is consistent with its manifest")
        return 0

    config = PyramidConfig(tile_size=args.tile_size, resample=args.resample, min_size=args.min_size)
    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    try:
        result = build_pyramid(args.source, args.output, config, progress=log)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"levels={result.levels} tiles={result.tiles_total} "
          f"written={result.tiles_written} reused={result.tiles_reused} "
          f"manifest={result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
