#!/usr/bin/env python3
"""Invoke the installed Doubao edit CLI with file-backed, argv-safe inputs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


MODEL = "doubao-seedream-5-0-pro-260628"
ATTEMPT_NAME = re.compile(
    r"^attempt-\d{2,}-[0-9a-f]{12}-(?!0+\.png)\d{2,}\.png$",
)
MAX_PROMPT_CHARACTERS = 100_000
TIMEOUT_SECONDS = 600


class SafeEditError(ValueError):
    """Raised when the safe edit transport or child process fails."""


def _parser() -> argparse.ArgumentParser:
    class SafeEditArgumentParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            raise SafeEditError(message)

    parser = SafeEditArgumentParser(prog="safe_edit")
    parser.add_argument("--doubao-script", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--image", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _nonempty_file(path: str, label: str) -> Path:
    value = Path(path)
    if not value.is_file():
        raise SafeEditError(f"{label} is not a file: {value}")
    return value


def run_edit(
        *, doubao_script: str | Path, prompt_file: str | Path,
        images: list[str | Path], output: str | Path, dry_run: bool = False,
) -> Path:
    """Run exactly one Doubao edit without interpreting any data as shell syntax."""
    script_path = _nonempty_file(str(doubao_script), "Doubao script")
    prompt_path = _nonempty_file(str(prompt_file), "prompt file")
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise SafeEditError("prompt file must be valid UTF-8") from error
    if not prompt:
        raise SafeEditError("prompt file must not be empty")
    if len(prompt) > MAX_PROMPT_CHARACTERS:
        raise SafeEditError("prompt file is too large")
    if not 2 <= len(images) <= 10:
        raise SafeEditError("edit requires two through ten ordered images")
    image_paths = [_nonempty_file(str(path), "image") for path in images]
    output_path = Path(output)
    if not ATTEMPT_NAME.fullmatch(output_path.name):
        raise SafeEditError("output must use the immutable attempt filename contract")
    if not output_path.parent.is_dir():
        raise SafeEditError(f"output directory does not exist: {output_path.parent}")
    if output_path.exists():
        raise SafeEditError(f"output already exists: {output_path}")

    command = [sys.executable, str(script_path)]
    if dry_run:
        command.append("--dry-run")
    command.extend(["edit", "--prompt", prompt])
    for image_path in image_paths:
        command.extend(["--image", str(image_path)])
    command.extend([
        "--out", str(output_path),
        "--model", MODEL,
        "--size", "2K",
        "--output-format", "png",
        "--response-format", "url",
        "--background", "opaque",
        "--optimize-prompt", "standard",
        "--no-watermark",
    ])
    try:
        result = subprocess.run(
            command, shell=False, capture_output=True, text=True,
            check=False, timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise SafeEditError(
            f"Doubao edit timed out after {TIMEOUT_SECONDS} seconds",
        ) from error
    except OSError as error:
        raise SafeEditError(f"cannot start Doubao edit: {error}") from error
    if result.returncode != 0:
        raise SafeEditError(f"Doubao edit exited with status {result.returncode}")
    if not dry_run and not output_path.is_file():
        raise SafeEditError("Doubao edit reported success without the requested output")
    return output_path


def _json_stdout(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        output = run_edit(
            doubao_script=args.doubao_script,
            prompt_file=args.prompt_file,
            images=args.image,
            output=args.out,
            dry_run=args.dry_run,
        )
        _json_stdout({"dry_run": args.dry_run, "output": str(output)})
        return 0
    except (SafeEditError, OSError, TypeError, ValueError) as error:
        print(f"safe-edit error: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
