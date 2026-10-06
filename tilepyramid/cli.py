"""Command line interface: python -m tilepyramid build <image> -o <out_dir>"""

from __future__ import annotations

import argparse
import sys

from .builder import build_pyramid
from .config import EDGE_MODES, RESAMPLE_MODES, PyramidConfig


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tilepyramid",
        description="Local image pyramid and tile generator (no external services).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build or incrementally rebuild a pyramid")
    build.add_argument("source", help="input image (PNG or JPEG)")
    build.add_argument("-o", "--out-dir", required=True, help="output directory")
    build.add_argument("--tile-size", type=int, default=256)
    build.add_argument("--min-size", type=int, default=256)
    build.add_argument("--resample", choices=RESAMPLE_MODES, default="bilinear")
    build.add_argument("--edge", choices=EDGE_MODES, default="crop")
    args = parser.parse_args(argv)

    config = PyramidConfig(
        tile_size=args.tile_size,
        min_size=args.min_size,
        resample=args.resample,
        edge=args.edge,
    )
    result = build_pyramid(args.source, args.out_dir, config)
    print(f"levels:        {result.levels}")
    print(f"tiles total:   {result.tiles_total}")
    print(f"tiles written: {result.tiles_written}")
    print(f"tiles reused:  {result.tiles_reused}")
    print(f"tiles removed: {result.tiles_removed}")
    print(f"manifest:      {result.manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
