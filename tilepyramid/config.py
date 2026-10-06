"""Configuration for pyramid generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

RESAMPLE_MODES = ("nearest", "bilinear")
EDGE_MODES = ("crop", "pad")


@dataclass(frozen=True)
class PyramidConfig:
    """Tunable parameters of the pyramid builder.

    tile_size: edge length of one tile in pixels (square tiles).
    min_size:  pyramid stops once both dimensions of a level are <= min_size.
    resample:  "nearest" or "bilinear" downscaling.
    edge:      "crop" keeps edge tiles at their real (smaller) size;
               "pad" extends them to a full tile with pad_color.
    pad_color: RGB fill used when edge == "pad".
    """

    tile_size: int = 256
    min_size: int = 256
    resample: str = "bilinear"
    edge: str = "crop"
    pad_color: tuple = (0, 0, 0)

    def __post_init__(self) -> None:
        if self.tile_size < 1:
            raise ValueError("tile_size must be >= 1")
        if self.min_size < 1:
            raise ValueError("min_size must be >= 1")
        if self.resample not in RESAMPLE_MODES:
            raise ValueError(f"resample must be one of {RESAMPLE_MODES}")
        if self.edge not in EDGE_MODES:
            raise ValueError(f"edge must be one of {EDGE_MODES}")
        if len(self.pad_color) != 3:
            raise ValueError("pad_color must be an RGB triple")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["pad_color"] = list(self.pad_color)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PyramidConfig":
        data = dict(data)
        data["pad_color"] = tuple(data["pad_color"])
        return cls(**data)
