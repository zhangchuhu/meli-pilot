#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image


def parse_sample(value: str) -> tuple[float, float, float, float]:
    try:
        box = tuple(float(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sample must contain four decimal numbers") from exc
    if len(box) != 4 or not all(0 <= coordinate <= 1 for coordinate in box):
        raise argparse.ArgumentTypeError("sample must be normalized left,top,right,bottom values")
    left, top, right, bottom = box
    if left >= right or top >= bottom:
        raise argparse.ArgumentTypeError("sample right/bottom must exceed left/top")
    return left, top, right, bottom


def extract_color(image: Image.Image, samples: list[tuple[float, float, float, float]]) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    regions = []
    for left, top, right, bottom in samples:
        box = (round(left * width), round(top * height), round(right * width), round(bottom * height))
        regions.append(np.asarray(rgb.crop(box), dtype=np.uint8).reshape(-1, 3))
    pixels = np.concatenate(regions)
    luminance = pixels @ np.array([0.2126, 0.7152, 0.0722])
    low, high = np.percentile(luminance, [10, 90])
    trimmed = pixels[(luminance >= low) & (luminance <= high)]
    if not len(trimmed):
        raise ValueError("sample rectangles did not contain usable pixels")
    return tuple(int(channel) for channel in np.median(trimmed, axis=0))


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a representative garment color.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--sample", action="append", required=True, type=parse_sample)
    parser.add_argument("--swatch", type=Path)
    args = parser.parse_args()

    with Image.open(args.image) as image:
        color = extract_color(image, args.sample)
    hex_color = "#" + "".join(f"{channel:02X}" for channel in color)

    if args.swatch:
        args.swatch.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (256, 256), color).save(args.swatch)

    print(json.dumps({"rgb": list(color), "hex": hex_color}))


if __name__ == "__main__":
    main()
