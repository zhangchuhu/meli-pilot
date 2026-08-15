#!/usr/bin/env python3
"""Validate image inputs and build labeled contact sheets with FFmpeg."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


MAX_FILE_BYTES = 30 * 1024 * 1024
MIN_PIXELS = 196
MAX_PIXELS = 36_000_000
MIN_SIDE = 14
MAX_RATIO = 16
PROBE_TIMEOUT_SECONDS = 30
PROCESS_TIMEOUT_SECONDS = 120
CELL_SIZE = 320
IMAGE_HEIGHT = 280
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,4}$")
CLASSIFICATIONS = frozenset({
    "front",
    "front three-quarter",
    "side",
    "back three-quarter",
    "back",
    "detail or flat lay",
    "infographic",
})
SUPPORTED_INPUT_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif",
    ".heic", ".heif",
})
RASTER_CODECS_BY_SUFFIX = {
    ".jpg": frozenset({"mjpeg"}),
    ".jpeg": frozenset({"mjpeg"}),
    ".png": frozenset({"png"}),
    ".webp": frozenset({"webp"}),
    ".bmp": frozenset({"bmp"}),
    ".tif": frozenset({"tiff"}),
    ".tiff": frozenset({"tiff"}),
    ".gif": frozenset({"gif"}),
    ".heic": frozenset({"hevc", "av1"}),
    ".heif": frozenset({"hevc", "av1"}),
}
VIDEO_CONTAINER_NAMES = frozenset({
    "avi", "flv", "matroska", "mov", "mp4", "mpeg", "mpegts", "ogg", "webm",
})
HEIF_SUFFIXES = frozenset({".heic", ".heif"})
HEIF_MAJOR_BRANDS = frozenset({
    "avif", "avis", "heic", "heix", "hevc", "hevx", "heim", "heis", "hevm",
    "hevs", "mif1", "msf1",
})
ATTEMPT_OUTPUT_NAME = re.compile(
    r"^attempt-(?P<index>\d{2,})-(?P<digest>[0-9a-f]{12})-"
    r"(?P<ordinal>(?!0+\.png)\d{2,})\.png$",
)
ACCEPTED_OUTPUT_NAME = re.compile(
    r"^look-(?P<index>\d{2,})-(?P<digest>[0-9a-f]{12})\.png$",
)
_FONT_3X5 = {
    " ": ("000", "000", "000", "000", "000"),
    "-": ("000", "000", "111", "000", "000"),
    "_": ("000", "000", "000", "000", "111"),
    "·": ("000", "000", "010", "000", "000"),
    "?": ("110", "001", "010", "000", "010"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "111", "100", "111"),
    "3": ("110", "001", "111", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "110"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "111", "011"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "a": ("000", "011", "101", "111", "101"),
    "b": ("100", "100", "110", "101", "110"),
    "c": ("000", "011", "100", "100", "011"),
    "d": ("001", "001", "011", "101", "011"),
    "e": ("000", "010", "101", "110", "011"),
    "f": ("011", "010", "111", "010", "010"),
    "g": ("011", "101", "011", "001", "110"),
    "h": ("100", "100", "110", "101", "101"),
    "i": ("010", "000", "110", "010", "111"),
    "j": ("001", "000", "001", "101", "010"),
    "k": ("100", "101", "110", "101", "101"),
    "l": ("110", "010", "010", "010", "111"),
    "m": ("000", "101", "111", "111", "101"),
    "n": ("000", "110", "101", "101", "101"),
    "o": ("000", "010", "101", "101", "010"),
    "p": ("000", "110", "101", "110", "100"),
    "q": ("000", "011", "101", "011", "001"),
    "r": ("000", "101", "110", "100", "100"),
    "s": ("000", "011", "110", "001", "110"),
    "t": ("010", "111", "010", "010", "011"),
    "u": ("000", "101", "101", "101", "011"),
    "v": ("000", "101", "101", "101", "010"),
    "w": ("000", "101", "111", "111", "101"),
    "x": ("000", "101", "010", "010", "101"),
    "y": ("000", "101", "011", "001", "110"),
    "z": ("000", "111", "001", "010", "111"),
}


class ImageQCError(ValueError):
    """Raised when an image does not meet the image-QC contract."""


@dataclass(frozen=True)
class ImageInfo:
    path: Path
    width: int
    height: int
    codec_name: str
    size_bytes: int
    format_name: str = ""
    major_brand: str = ""
    frame_count: int | None = None

    @property
    def pixels(self) -> int:
        return self.width * self.height


def _run(
        command: list[str], *, timeout: int | float = PROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImageQCError(
            f"{command[0]} timed out after {timeout:g} seconds",
        ) from exc
    except OSError as exc:
        raise ImageQCError(f"cannot run {command[0]}: {exc}") from exc


def probe_image(path: str | Path) -> ImageInfo:
    image_path = Path(path)
    result = _run([
        "ffprobe", "-v", "error", "-count_frames",
        "-show_entries",
        "stream=codec_type,width,height,codec_name,nb_read_frames:format=format_name:format_tags=major_brand",
        "-of", "json",
        str(image_path),
    ], timeout=PROBE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe failed"
        raise ImageQCError(f"cannot probe image {image_path}: {detail}")
    try:
        payload = json.loads(result.stdout)
        streams = payload["streams"]
        if (len(streams) != 1 or streams[0].get("codec_type") != "video"):
            raise ValueError("expected exactly one visual stream")
        stream = streams[0]
        width = int(stream["width"])
        height = int(stream["height"])
        codec_name = str(stream["codec_name"])
        format_name = str(payload["format"]["format_name"])
        major_brand = str(payload["format"].get("tags", {}).get("major_brand", "")).lower()
        raw_frame_count = stream.get("nb_read_frames")
        frame_count = (
            None if raw_frame_count in {None, "N/A"} else int(raw_frame_count)
        )
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ImageQCError(f"cannot probe image {image_path}: unsupported raster content") from exc
    return ImageInfo(
        image_path, width, height, codec_name, image_path.stat().st_size,
        format_name, major_brand, frame_count,
    )


def validate_image(path: str | Path) -> ImageInfo:
    image_path = Path(path)
    if not image_path.is_file():
        raise ImageQCError(f"image is not a file: {image_path}")
    suffix = image_path.suffix.lower()
    if suffix not in SUPPORTED_INPUT_SUFFIXES:
        raise ImageQCError(f"unsupported image suffix: {image_path.suffix or '(none)'}")
    size_bytes = image_path.stat().st_size
    if size_bytes > MAX_FILE_BYTES:
        raise ImageQCError(f"image exceeds 30 MiB: {image_path}")

    info = probe_image(image_path)
    allowed_codecs = RASTER_CODECS_BY_SUFFIX[suffix]
    format_names = set(info.format_name.split(","))
    video_container = bool(format_names & VIDEO_CONTAINER_NAMES)
    valid_heif_container = (
        suffix in HEIF_SUFFIXES
        and info.major_brand in HEIF_MAJOR_BRANDS
        and info.frame_count == 1
    )
    if (info.codec_name not in allowed_codecs
            or suffix in HEIF_SUFFIXES and not valid_heif_container
            or video_container and suffix not in HEIF_SUFFIXES):
        raise ImageQCError(f"unsupported raster content: {image_path}")
    if info.width <= MIN_SIDE or info.height <= MIN_SIDE:
        raise ImageQCError(f"image sides must be greater than 14 pixels: {image_path}")
    if not MIN_PIXELS <= info.pixels <= MAX_PIXELS:
        raise ImageQCError(f"image pixel count is outside 196..36000000: {image_path}")
    ratio = info.width / info.height
    if not 1 / MAX_RATIO <= ratio <= MAX_RATIO:
        raise ImageQCError(f"image aspect ratio is outside 1:16..16:1: {image_path}")

    result = _run([
        "ffmpeg", "-v", "error", "-i", str(image_path), "-f", "null", "-",
    ])
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffmpeg failed"
        raise ImageQCError(f"image failed full decode {image_path}: {detail}")
    return info


def validate_manifest(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ImageQCError("image manifest must be a JSON array")
    if not items:
        raise ImageQCError("image manifest must not be empty")

    normalized: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ImageQCError(f"manifest item {index} must be an object")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not SAFE_ID.fullmatch(identifier):
            raise ImageQCError(f"unsafe image id at item {index}: {identifier!r}")
        if identifier in identifiers:
            raise ImageQCError(f"duplicate image id: {identifier}")
        identifiers.add(identifier)

        classification = item.get("classification")
        if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
            raise ImageQCError(f"unknown classification for {identifier}: {classification!r}")
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ImageQCError(f"invalid image path for {identifier}")
        normalized.append({
            "id": identifier,
            "path": Path(raw_path),
            "classification": classification,
        })

    for item in normalized:
        item["info"] = validate_image(item["path"])
    return normalized


def escape_drawtext(value: str) -> str:
    """Escape text embedded in a single-quoted FFmpeg drawtext value."""
    return (value.replace("\\", "\\\\")
            .replace("'", "'" + "\\" * 3 + "''")
            .replace(":", "\\:")
            .replace("%", "\\%")
            .replace(",", "\\,"))


def _contact_sheet_command(items: Sequence[dict[str, Any]], output: Path) -> list[str]:
    columns = math.ceil(math.sqrt(len(items)))
    chains: list[str] = []
    for index, item in enumerate(items):
        label = escape_drawtext(f"{item['id']} · {item['classification']}")
        chains.append(
            f"[{index}:v]scale={CELL_SIZE}:{IMAGE_HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={CELL_SIZE}:{CELL_SIZE}:(ow-iw)/2:0:white,"
            f"drawtext=text='{label}':x=10:y=290:fontsize=18:fontcolor=black[v{index}]"
        )
    if len(items) == 1:
        chains.append("[v0]null[out]")
    else:
        layout = "|".join(
            f"{(index % columns) * CELL_SIZE}_{(index // columns) * CELL_SIZE}"
            for index in range(len(items))
        )
        inputs = "".join(f"[v{index}]" for index in range(len(items)))
        chains.append(
            f"{inputs}xstack=inputs={len(items)}:layout={layout}:fill=white[out]"
        )
    command = ["ffmpeg", "-v", "error", "-y"]
    for item in items:
        command.extend(["-i", str(item["path"])])
    command.extend([
        "-filter_complex", ";".join(chains),
        "-map", "[out]", "-frames:v", "1", "-q:v", "2", str(output),
    ])
    return command


def _write_bitmap_label(path: Path, value: str) -> None:
    """Write a tiny PPM label for FFmpeg builds compiled without drawtext."""
    width, height = CELL_SIZE, CELL_SIZE - IMAGE_HEIGHT
    origin_x, origin_y = 10, 7
    glyph_units = max(1, len(value) * 4 - 1)
    scale = max(1, min(3, (width - origin_x) // glyph_units))
    pixels = bytearray(b"\xff" * width * height * 3)
    for character_index, character in enumerate(value):
        glyph = _FONT_3X5.get(character, _FONT_3X5["?"])
        glyph_x = origin_x + character_index * 4 * scale
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit != "1":
                    continue
                for offset_y in range(scale):
                    y = origin_y + row * scale + offset_y
                    for offset_x in range(scale):
                        x = glyph_x + column * scale + offset_x
                        if x >= width:
                            continue
                        start = (y * width + x) * 3
                        pixels[start:start + 3] = b"\x00\x00\x00"
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)


def _fallback_contact_sheet_command(
        items: Sequence[dict[str, Any]], labels: Sequence[Path], output: Path,
) -> list[str]:
    columns = math.ceil(math.sqrt(len(items)))
    command = ["ffmpeg", "-v", "error", "-y"]
    for item in items:
        command.extend(["-i", str(item["path"])])
    for label in labels:
        command.extend(["-i", str(label)])

    chains: list[str] = []
    count = len(items)
    for index in range(count):
        chains.append(
            f"[{index}:v]scale={CELL_SIZE}:{IMAGE_HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={CELL_SIZE}:{CELL_SIZE}:(ow-iw)/2:0:white[base{index}]"
        )
        chains.append(
            f"[base{index}][{count + index}:v]overlay=0:{IMAGE_HEIGHT}[v{index}]"
        )
    if count == 1:
        chains.append("[v0]null[out]")
    else:
        layout = "|".join(
            f"{(index % columns) * CELL_SIZE}_{(index // columns) * CELL_SIZE}"
            for index in range(count)
        )
        inputs = "".join(f"[v{index}]" for index in range(count))
        chains.append(f"{inputs}xstack=inputs={count}:layout={layout}:fill=white[out]")
    command.extend([
        "-filter_complex", ";".join(chains),
        "-map", "[out]", "-frames:v", "1", "-q:v", "2", str(output),
    ])
    return command


def _build_without_drawtext(
        items: Sequence[dict[str, Any]], output: Path,
) -> subprocess.CompletedProcess[str]:
    labels: list[Path] = []
    try:
        for item in items:
            with tempfile.NamedTemporaryFile(
                prefix=f".{output.name}.label-", suffix=".ppm",
                dir=output.parent, delete=False,
            ) as stream:
                label_path = Path(stream.name)
            labels.append(label_path)
            _write_bitmap_label(
                label_path, f"{item['id']} · {item['classification']}",
            )
        return _run(_fallback_contact_sheet_command(items, labels, output))
    finally:
        for label in labels:
            try:
                label.unlink()
            except FileNotFoundError:
                pass


def build_contact_sheet(items: Any, output: str | Path) -> ImageInfo:
    validated = validate_manifest(items)
    output_path = Path(output)
    resolved_output = output_path.resolve()
    if any(Path(item["path"]).resolve() == resolved_output for item in validated):
        raise ImageQCError(f"contact-sheet output equals an input: {output_path}")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.", suffix=".jpg",
            dir=output_path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
        result = _run(_contact_sheet_command(validated, temporary))
        if result.returncode != 0 and "No such filter: 'drawtext'" in result.stderr:
            result = _build_without_drawtext(validated, temporary)
        if result.returncode != 0:
            detail = result.stderr.strip() or "ffmpeg failed"
            raise ImageQCError(f"cannot build contact sheet: {detail}")
        info = validate_image(temporary)
        os.replace(temporary, output_path)
        temporary = None
        return ImageInfo(
            output_path, info.width, info.height, info.codec_name, info.size_bytes,
        )
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def build_empty_contact_sheet(
        output: str | Path, label: str = "NO ACCEPTED OUTPUTS",
) -> ImageInfo:
    """Create an atomic, labelled contact sheet when no output images exist."""
    compact_label = " ".join(label.split())
    if not compact_label:
        raise ImageQCError("empty contact-sheet label must not be blank")
    output_path = Path(output)
    label_path: Path | None = None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.label-", suffix=".ppm",
            dir=output_path.parent, delete=False,
        ) as stream:
            label_path = Path(stream.name)
        _write_bitmap_label(label_path, compact_label)
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.", suffix=".jpg",
            dir=output_path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
        result = _run([
            "ffmpeg", "-v", "error", "-y", "-i", str(label_path),
            "-vf", f"pad={CELL_SIZE}:{CELL_SIZE}:0:{IMAGE_HEIGHT}:white",
            "-frames:v", "1", "-q:v", "2", str(temporary),
        ])
        if result.returncode != 0:
            detail = result.stderr.strip() or "ffmpeg failed"
            raise ImageQCError(f"cannot build empty contact sheet: {detail}")
        info = validate_image(temporary)
        os.replace(temporary, output_path)
        temporary = None
        return ImageInfo(
            output_path, info.width, info.height, info.codec_name, info.size_bytes,
        )
    finally:
        for path in (label_path, temporary):
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def promote_output(attempt: str | Path, output: str | Path) -> Path:
    """Atomically copy an accepted immutable attempt to its deterministic name."""
    attempt_path = Path(attempt)
    output_path = Path(output)
    attempt_match = ATTEMPT_OUTPUT_NAME.fullmatch(attempt_path.name)
    output_match = ACCEPTED_OUTPUT_NAME.fullmatch(output_path.name)
    if not attempt_path.is_file():
        raise ImageQCError(f"accepted attempt is not a file: {attempt_path}")
    if attempt_path.parent.resolve() != output_path.parent.resolve():
        raise ImageQCError("attempt and accepted output must share generated_images")
    if (attempt_match is None or output_match is None
            or attempt_match.group("index") != output_match.group("index")
            or attempt_match.group("digest") != output_match.group("digest")):
        raise ImageQCError("attempt and accepted output identities do not match")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with attempt_path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output_path)
        temporary = None
        directory = os.open(output_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return output_path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _read_manifest(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImageQCError(f"cannot read manifest {path}: {exc}") from exc


def _report(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for item in items:
        info: ImageInfo = item["info"]
        report.append({
            "id": item["id"],
            "path": str(item["path"]),
            "classification": item["classification"],
            "width": info.width,
            "height": info.height,
            "pixels": info.pixels,
            "codec_name": info.codec_name,
            "format_name": info.format_name,
            "major_brand": info.major_brand,
            "frame_count": info.frame_count,
            "size_bytes": info.size_bytes,
        })
    return report


def _write_json(path: Path, value: Any) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "contact-sheet"):
        command = subparsers.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    empty = subparsers.add_parser("empty-contact-sheet")
    empty.add_argument("--output", type=Path, required=True)
    empty.add_argument("--label", default="NO ACCEPTED OUTPUTS")
    promote = subparsers.add_parser("promote-output")
    promote.add_argument("--input", type=Path, required=True)
    promote.add_argument("--output", type=Path, required=True)
    return parser


def _print_summary(count: int, output: Path) -> None:
    print(json.dumps(
        {"count": count, "output": str(output)},
        ensure_ascii=False, separators=(",", ":"),
    ))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "promote-output":
            promote_output(args.input, args.output)
            count = 1
        elif args.command == "empty-contact-sheet":
            build_empty_contact_sheet(args.output, args.label)
            count = 0
        else:
            items = _read_manifest(args.input)
        if args.command == "validate":
            validated = validate_manifest(items)
            _write_json(args.output, _report(validated))
            count = len(validated)
        elif args.command == "contact-sheet":
            build_contact_sheet(items, args.output)
            count = len(items)
        _print_summary(count, args.output)
        return 0
    except (ImageQCError, OSError) as exc:
        print(f"image-qc error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
