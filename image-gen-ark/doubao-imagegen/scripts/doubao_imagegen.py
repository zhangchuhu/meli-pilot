#!/usr/bin/env python3
"""Generate, edit, or decompose images with Volcengine Ark Doubao Seedream."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seedream-5-0-pro-260628"
SUPPORTED_INPUTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif"}
MAX_INPUTS = 10
MAX_FILE_BYTES = 30 * 1024 * 1024


class CliError(Exception):
    pass


def is_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "data:image/"))


def image_value(value: str, *, allowed_suffixes: set[str] | None = None) -> str:
    if is_url(value):
        return value
    path = Path(value).expanduser()
    if not path.is_file():
        raise CliError(f"Input image does not exist: {path}")
    supported = allowed_suffixes or SUPPORTED_INPUTS
    if path.suffix.lower() not in supported:
        raise CliError(f"Unsupported input image format: {path.suffix or '(none)'}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise CliError(f"Input image exceeds 30 MB: {path}")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if mime == "image/jpg":
        mime = "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime.lower()};base64,{encoded}"


def validate_size(size: str, *, decompose: bool) -> None:
    tiers = {"1K", "1.5K", "2K", "auto"} if decompose else {"1K", "1.5K", "2K"}
    if size in tiers:
        return
    if decompose:
        raise CliError("Layer decomposition size must be auto, 1K, 1.5K, or 2K")
    match = re.fullmatch(r"(\d+)x(\d+)", size)
    if not match:
        raise CliError("Size must be 1K, 1.5K, 2K, or WIDTHxHEIGHT")
    width, height = map(int, match.groups())
    pixels = width * height
    ratio = width / height
    if not 921_600 <= pixels <= 4_624_220 or not 1 / 16 <= ratio <= 16:
        raise CliError("Explicit size violates Seedream 5.0 pro pixel-count or aspect-ratio limits")


def validate_payload(payload: dict[str, Any], *, decompose: bool = False) -> None:
    model = payload.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model:
        raise CliError("model must be a non-empty string")
    prompt = payload.get("prompt")
    if not decompose and (not isinstance(prompt, str) or not prompt.strip()):
        raise CliError("prompt is required")
    images = payload.get("image", [])
    if isinstance(images, str):
        images = [images]
    if images and not isinstance(images, list):
        raise CliError("image must be a string or array")
    if len(images) > MAX_INPUTS:
        raise CliError(f"At most {MAX_INPUTS} input images are supported")
    if decompose:
        if model != DEFAULT_MODEL:
            raise CliError("Layer decomposition requires doubao-seedream-5-0-pro-260628")
        if len(images) != 1:
            raise CliError("Layer decomposition requires exactly one input image")
    output_format = payload.get("output_format", "png")
    if output_format not in {"png", "jpeg"}:
        raise CliError("output_format must be png or jpeg")
    response_format = payload.get("response_format", "url")
    if response_format not in {"url", "b64_json"}:
        raise CliError("response_format must be url or b64_json")
    background = payload.get("background", "opaque")
    if background not in {"opaque", "transparent"}:
        raise CliError("background must be opaque or transparent")
    if background == "transparent":
        if len(images) != 1 or output_format != "png":
            raise CliError("Transparent mode requires exactly one input image and PNG output")
    optimize = payload.get("optimize_prompt_options", {}).get("mode", "standard")
    if optimize not in {"standard", "fast"}:
        raise CliError("optimize prompt mode must be standard or fast")
    validate_size(str(payload.get("size", "auto" if decompose else "2K")), decompose=decompose)


def api_request(payload: dict[str, Any], *, base_url: str, api_key: str, timeout: int) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/images/generations"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CliError(f"Ark API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CliError(f"Ark API request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CliError("Ark API returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise CliError("Ark API returned an unexpected response")
    return parsed


def suffix_for(item: dict[str, Any], fallback: str) -> str:
    value = str(item.get("output_format", fallback)).lower()
    return ".jpg" if value in {"jpg", "jpeg"} else ".png"


def unique_path(path: Path, *, overwrite: bool) -> Path:
    if overwrite or not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}-v{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def save_item(item: dict[str, Any], path: Path, *, overwrite: bool, timeout: int) -> Path:
    path = unique_path(path, overwrite=overwrite)
    path.parent.mkdir(parents=True, exist_ok=True)
    if item.get("b64_json"):
        try:
            content = base64.b64decode(item["b64_json"], validate=True)
        except (ValueError, TypeError) as exc:
            raise CliError("Response contains invalid base64 image data") from exc
    elif item.get("url"):
        try:
            with urllib.request.urlopen(item["url"], timeout=timeout) as response:
                content = response.read()
        except urllib.error.URLError as exc:
            raise CliError(f"Could not download generated image: {exc.reason}") from exc
    else:
        raise CliError("Response image contains neither url nor b64_json")
    path.write_bytes(content)
    if not content:
        raise CliError(f"Saved image is empty: {path}")
    return path.resolve()


def execute(payload: dict[str, Any], args: argparse.Namespace, *, out: Path | None, out_dir: Path | None, decompose: bool) -> list[Path]:
    validate_payload(payload, decompose=decompose)
    if args.dry_run:
        printable = json.loads(json.dumps(payload))
        images = printable.get("image")
        values = images if isinstance(images, list) else [images] if images else []
        redacted = [
            f"<data-url:{len(value)} chars>" if isinstance(value, str) and value.startswith("data:image/") else value
            for value in values
        ]
        if images is not None:
            printable["image"] = redacted if isinstance(images, list) else redacted[0]
        print(json.dumps(printable, ensure_ascii=False, indent=2))
        return []
    api_key = os.getenv("ARK_API_KEY")
    if not api_key:
        raise CliError("ARK_API_KEY is not set. Set it locally before making a live API call.")
    response = api_request(payload, base_url=args.base_url, api_key=api_key, timeout=args.timeout)
    data = response.get("data")
    if not isinstance(data, list) or not data:
        raise CliError(f"Ark API returned no image data: {json.dumps(response, ensure_ascii=False)}")
    saved: list[Path] = []
    if decompose:
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        ordered = sorted(data, key=lambda item: item.get("z_index", 0))
        manifest_items = []
        for index, item in enumerate(ordered):
            z_index = int(item.get("z_index", index))
            label = "base" if z_index == 0 else f"layer-{z_index:02d}"
            path = out_dir / f"{label}{suffix_for(item, payload.get('output_format', 'png'))}"
            local = save_item(item, path, overwrite=args.overwrite, timeout=args.timeout)
            manifest_item = dict(item)
            manifest_item.pop("b64_json", None)
            manifest_item["local_file"] = local.name
            manifest_items.append(manifest_item)
            saved.append(local)
        manifest = {key: value for key, value in response.items() if key != "data"}
        manifest["data"] = manifest_items
        manifest_path = unique_path(out_dir / "manifest.json", overwrite=args.overwrite)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        saved.append(manifest_path.resolve())
    else:
        assert out is not None
        for index, item in enumerate(data):
            path = out if len(data) == 1 else out.with_name(f"{out.stem}-{index + 1}{suffix_for(item, out.suffix.lstrip('.'))}")
            saved.append(save_item(item, path, overwrite=args.overwrite, timeout=args.timeout))
    for path in saved:
        print(path)
    return saved


def base_payload(args: argparse.Namespace, images: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": args.model,
        "prompt": args.prompt,
        "size": args.size,
        "output_format": args.output_format,
        "response_format": args.response_format,
        "background": args.background,
        "watermark": args.watermark,
        "optimize_prompt_options": {"mode": args.optimize_prompt},
    }
    if images:
        payload["image"] = images[0] if len(images) == 1 else images
    return payload


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--size", default="2K")
    parser.add_argument("--output-format", choices=("png", "jpeg"), default="png")
    parser.add_argument("--response-format", choices=("url", "b64_json"), default="url")
    parser.add_argument("--background", choices=("opaque", "transparent"), default="opaque")
    parser.add_argument("--optimize-prompt", choices=("standard", "fast"), default="standard")
    parser.add_argument("--watermark", action=argparse.BooleanOptionalAction, default=False)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--base-url", default=os.getenv("ARK_BASE_URL", DEFAULT_BASE_URL))
    root.add_argument("--timeout", type=int, default=300)
    root.add_argument("--dry-run", action="store_true")
    root.add_argument("--overwrite", action="store_true")
    sub = root.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="Generate an image from text")
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--out", type=Path, required=True)
    add_common(generate)

    edit = sub.add_parser("edit", help="Edit or fuse input images")
    edit.add_argument("--prompt", required=True)
    edit.add_argument("--image", action="append", required=True)
    edit.add_argument("--out", type=Path, required=True)
    add_common(edit)

    decompose = sub.add_parser("decompose", help="Decompose one image into layers")
    decompose.add_argument("--image", required=True)
    decompose.add_argument("--prompt")
    decompose.add_argument("--out-dir", type=Path, required=True)
    decompose.add_argument("--model", default=DEFAULT_MODEL)
    decompose.add_argument("--size", choices=("auto", "1K", "1.5K", "2K"), default="auto")
    decompose.add_argument("--output-format", choices=("png", "jpeg"), default="png")
    decompose.add_argument("--response-format", choices=("url", "b64_json"), default="url")
    decompose.add_argument("--watermark", action=argparse.BooleanOptionalAction, default=False)

    batch = sub.add_parser("generate-batch", help="Run independent jobs from JSONL")
    batch.add_argument("--input", type=Path, required=True)
    batch.add_argument("--out-dir", type=Path, required=True)
    return root


def run_batch(args: argparse.Namespace) -> None:
    if not args.input.is_file():
        raise CliError(f"Batch input does not exist: {args.input}")
    for line_number, line in enumerate(args.input.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CliError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
        if not isinstance(job, dict):
            raise CliError(f"Batch line {line_number} must be a JSON object")
        filename = job.pop("filename", f"image-{line_number:03d}.{job.get('output_format', 'png').replace('jpeg', 'jpg')}")
        images = job.get("image")
        if isinstance(images, str):
            job["image"] = image_value(images)
        elif isinstance(images, list):
            job["image"] = [image_value(str(value)) for value in images]
        job.setdefault("model", DEFAULT_MODEL)
        job.setdefault("size", "2K")
        job.setdefault("output_format", "png")
        job.setdefault("response_format", "url")
        job.setdefault("background", "opaque")
        job.setdefault("watermark", False)
        job.setdefault("optimize_prompt_options", {"mode": "standard"})
        execute(job, args, out=args.out_dir / filename, out_dir=None, decompose=False)


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "generate-batch":
            run_batch(args)
            return 0
        if args.command == "generate":
            payload = base_payload(args)
            execute(payload, args, out=args.out, out_dir=None, decompose=False)
        elif args.command == "edit":
            images = [image_value(value) for value in args.image]
            payload = base_payload(args, images)
            execute(payload, args, out=args.out, out_dir=None, decompose=False)
        else:
            payload = {
                "model": args.model,
                "image": image_value(args.image, allowed_suffixes={".png", ".jpg", ".jpeg"}),
                "size": args.size,
                "output_format": args.output_format,
                "response_format": args.response_format,
                "watermark": args.watermark,
                "layer_decomposition": True,
            }
            if args.prompt:
                payload["prompt"] = args.prompt
            execute(payload, args, out=None, out_dir=args.out_dir, decompose=True)
        return 0
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
