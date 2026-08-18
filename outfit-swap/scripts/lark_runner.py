"""Typed, path-safe argv transport for the installed ``lark-cli`` Base commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence


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
            self, *, app_token: str, table_id: str, field_ids: Sequence[str],
            filter_payload: Path, output: Path, limit: int = 2000, offset: int = 0,
            view_id: str | None = None, retry_failed: bool = False,
    ) -> Path:
        """Write one NDJSON record listing to a new task-local artifact."""
        filter_path, filter_arg = self._input_path(filter_payload)
        self._validate_status_filter(filter_path, retry_failed=retry_failed)
        output_path, output_arg = self._output_path(output)
        command = [
            "base", "+record-list", "--base-token", self._required(app_token, "app token"),
            "--table-id", self._required(table_id, "table ID"),
        ]
        if view_id is not None:
            command.extend(["--view-id", self._required(view_id, "view ID")])
        for field_id in self._required_values(field_ids, "field IDs"):
            command.extend(["--field-id", field_id])
        command.extend([
            "--filter-json", f"@{filter_arg}", "--format", "ndjson", "--limit",
            str(self._page_limit(limit)), "--offset", str(self._page_offset(offset)),
            "--output", output_arg, "--minimal-stdout", "--as", "user",
        ])
        self._run(command, cwd=output_path.parent)
        return output_path

    def download_attachment(
            self, *, app_token: str, table_id: str, record_id: str, token: str,
            output: Path,
    ) -> Path:
        """Download one attachment token to a new task-local artifact."""
        output_path, output_arg = self._output_path(output)
        self._run([
            "base", "+record-download-attachment", "--base-token",
            self._required(app_token, "app token"), "--table-id",
            self._required(table_id, "table ID"), "--record-id",
            self._required(record_id, "record ID"), "--file-token",
            self._required(token, "attachment token"), "--output", output_arg, "--as", "user",
        ], cwd=output_path.parent)
        return output_path

    def upload_attachment(
            self, *, file: Path, app_token: str, table_id: str, record_id: str,
            field_id: str,
    ) -> dict:
        """Upload one existing task-local attachment file."""
        file_path, file_arg = self._input_path(file)
        return self._json([
            "base", "+record-upload-attachment", "--base-token",
            self._required(app_token, "app token"), "--table-id",
            self._required(table_id, "table ID"), "--record-id",
            self._required(record_id, "record ID"), "--field-id",
            self._required(field_id, "field ID"), "--file", file_arg, "--as", "user",
        ], cwd=file_path.parent)

    def update_record(
            self, *, app_token: str, table_id: str, record_id: str, payload: Path,
    ) -> dict:
        """Apply a record-keyed batch update stored in a task-local JSON file."""
        payload_path, payload_arg = self._input_path(payload)
        record_id = self._required(record_id, "record ID")
        self._validate_update_payload(payload_path, record_id)
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
        escapes_task_directory = False
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self._task_dir)
        except (OSError, ValueError):
            escapes_task_directory = True
        if escapes_task_directory:
            raise LarkRunnerError("file argument escapes the task directory")
        return candidate

    def _json(self, command: Sequence[str], *, cwd: Path | None = None) -> dict:
        result = self._run(command, cwd=cwd)
        invalid_json = False
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError:
            invalid_json = True
            decoded = None
        if invalid_json:
            raise LarkRunnerError("lark-cli returned invalid JSON")
        if not isinstance(decoded, dict):
            raise LarkRunnerError("lark-cli returned an unexpected JSON response")
        return decoded

    def _run(self, command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        error_message: str | None = None
        try:
            result = subprocess.run(
                [self._executable, *command], cwd=cwd or self._task_dir, shell=False,
                capture_output=True, text=True, check=False, timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            error_message = "lark-cli command timed out"
        except OSError:
            error_message = "cannot start lark-cli"
        if error_message is not None:
            raise LarkRunnerError(error_message)
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

    @classmethod
    def _required_values(cls, values: Sequence[str], label: str) -> list[str]:
        if isinstance(values, (str, bytes)) or not values:
            raise LarkRunnerError(f"{label} are required")
        return [cls._required(value, label) for value in values]

    @staticmethod
    def _page_limit(limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 2000:
            raise LarkRunnerError("record list limit is invalid")
        return limit

    @staticmethod
    def _page_offset(offset: int) -> int:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise LarkRunnerError("record list offset is invalid")
        return offset

    @staticmethod
    def _validate_update_payload(payload: Path, record_id: str) -> None:
        invalid_payload = False
        try:
            decoded = json.loads(payload.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            invalid_payload = True
            decoded = None
        if not isinstance(decoded, dict):
            invalid_payload = True
        update_records = decoded.get("update_records") if isinstance(decoded, dict) else None
        if (not isinstance(update_records, dict) or set(update_records) != {record_id}
                or not isinstance(update_records.get(record_id), dict)):
            invalid_payload = True
        if invalid_payload:
            raise LarkRunnerError("record update payload is invalid")

    @staticmethod
    def _validate_status_filter(filter_payload: Path, *, retry_failed: bool) -> None:
        if not isinstance(retry_failed, bool):
            raise LarkRunnerError("record list retry intent is invalid")
        invalid_filter = False
        try:
            decoded = json.loads(
                filter_payload.read_text(encoding="utf-8"),
                object_pairs_hook=LarkBaseClient._unique_object,
            )
        except (OSError, UnicodeError, ValueError):
            invalid_filter = True
            decoded = None
        expected_status = "失败" if retry_failed else "未开始"
        expected_filter = {
            "logic": "and",
            "conditions": [["任务状态", "intersects", [expected_status]]],
        }
        if decoded != expected_filter:
            invalid_filter = True
        if invalid_filter:
            raise LarkRunnerError("record list filter is invalid")

    @staticmethod
    def _unique_object(pairs: list[tuple[object, object]]) -> dict[object, object]:
        decoded: dict[object, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError("duplicate JSON key")
            decoded[key] = value
        return decoded
