"""Typed, path-safe argv transport for the installed ``lark-cli`` Base commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Sequence


class LarkRunnerError(RuntimeError):
    """Raised when a Base command cannot be safely run or parsed."""


class LarkBaseClient:
    """Run Base commands without exposing task-local paths to the CLI argv."""

    def __init__(
            self, *, task_dir: Path, executable: str | Path = "lark-cli",
            timeout_seconds: float = 60.0,
    ) -> None:
        resolved_task_dir = Path(task_dir).resolve()
        if not resolved_task_dir.is_dir():
            raise LarkRunnerError("task directory is invalid")
        if timeout_seconds <= 0:
            raise LarkRunnerError("timeout must be positive")
        self._task_dir = resolved_task_dir
        self._executable = str(executable)
        self._timeout_seconds = timeout_seconds

    def resolve_base(self, base_url: str) -> dict:
        """Resolve one Base URL using the authenticated user identity."""
        return self._json([
            "base", "+url-resolve", "--url", self._required(base_url, "base URL"),
            "--as", "user",
        ])

    def list_records(
            self, *, app_token: str, table_id: str, view_id: str, output: Path,
    ) -> Path:
        """Write one NDJSON record listing to a new task-local artifact."""
        output_path, output_arg = self._output_path(output)
        self._run([
            "base", "+record-list", "--base-token", self._required(app_token, "app token"),
            "--table-id", self._required(table_id, "table ID"),
            "--view-id", self._required(view_id, "view ID"), "--output", output_arg,
            "--minimal-stdout", "--as", "user",
        ], cwd=output_path.parent)
        return output_path

    def download_attachment(self, *, token: str, output: Path) -> Path:
        """Download one attachment token to a new task-local artifact."""
        output_path, output_arg = self._output_path(output)
        self._run([
            "base", "+record-download-attachment", "--file-token",
            self._required(token, "attachment token"), "--output", output_arg, "--as", "user",
        ], cwd=output_path.parent)
        return output_path

    def upload_attachment(
            self, *, file: Path, app_token: str, table_id: str,
    ) -> dict:
        """Upload one existing task-local attachment file."""
        file_path, file_arg = self._input_path(file)
        return self._json([
            "base", "+record-upload-attachment", "--base-token",
            self._required(app_token, "app token"), "--table-id",
            self._required(table_id, "table ID"), "--file", file_arg, "--as", "user",
        ], cwd=file_path.parent)

    def update_record(
            self, *, app_token: str, table_id: str, record_id: str, payload: Path,
    ) -> dict:
        """Apply a record-keyed batch update stored in a task-local JSON file."""
        payload_path, payload_arg = self._input_path(payload)
        self._required(record_id, "record ID")
        return self._json([
            "base", "+record-batch-update", "--base-token",
            self._required(app_token, "app token"), "--table-id",
            self._required(table_id, "table ID"), "--json", f"@{payload_arg}", "--as", "user",
        ], cwd=payload_path.parent)

    def get_record(self, *, app_token: str, table_id: str, record_id: str) -> dict:
        """Read one record by ID."""
        return self._json([
            "base", "+record-get", "--base-token", self._required(app_token, "app token"),
            "--table-id", self._required(table_id, "table ID"), "--record-id",
            self._required(record_id, "record ID"), "--as", "user",
        ])

    def _input_path(self, supplied: Path) -> tuple[Path, str]:
        path = self._task_file(supplied)
        if not path.is_file():
            raise LarkRunnerError("file input is missing or invalid")
        return path, f"./{path.name}"

    def _output_path(self, supplied: Path) -> tuple[Path, str]:
        path = self._task_file(supplied)
        if path.exists() or path.is_symlink():
            raise LarkRunnerError("file output already exists or is invalid")
        return path, f"./{path.name}"

    def _task_file(self, supplied: Path) -> Path:
        path = Path(supplied)
        if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
            raise LarkRunnerError("file argument must be one task-local filename")
        candidate = self._task_dir / path.name
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self._task_dir)
        except (OSError, ValueError) as error:
            raise LarkRunnerError("file argument escapes the task directory") from error
        return candidate

    def _json(self, command: Sequence[str], *, cwd: Path | None = None) -> dict:
        result = self._run(command, cwd=cwd)
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise LarkRunnerError("lark-cli returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise LarkRunnerError("lark-cli returned an unexpected JSON response")
        return decoded

    def _run(self, command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self._executable, *command], cwd=cwd or self._task_dir, shell=False,
                capture_output=True, text=True, check=False, timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise LarkRunnerError("lark-cli command timed out") from error
        except OSError as error:
            raise LarkRunnerError("cannot start lark-cli") from error
        if result.returncode != 0:
            raise LarkRunnerError(
                f"lark-cli command failed with status {result.returncode}",
            )
        return result

    @staticmethod
    def _required(value: str, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise LarkRunnerError(f"{label} is required")
        return value
