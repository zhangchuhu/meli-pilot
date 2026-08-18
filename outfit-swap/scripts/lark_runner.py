"""Typed, path-safe argv transport for the installed ``lark-cli`` Base commands."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class LarkRunnerError(RuntimeError):
    """Raised when a Base command cannot be safely run or parsed."""


@dataclass(frozen=True)
class _PrivateUpdate:
    directory: Path
    directory_fd: int
    directory_identity: tuple[int, int]
    payload_identity: tuple[int, int]


@dataclass(frozen=True)
class RecordPage:
    path: Path
    records_count: int
    has_more: bool


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

    def list_fields(
            self, *, app_token: str, table_id: str, limit: int = 200,
            offset: int = 0,
    ) -> dict:
        """Return one schema page through the authenticated CLI."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise LarkRunnerError("field list limit is invalid")
        return self._json([
            "base", "+field-list", "--base-token",
            self._required(app_token, "app token"), "--table-id",
            self._required(table_id, "table ID"), "--limit", str(limit),
            "--offset", str(self._page_offset(offset)), "--as", "user",
        ])

    def create_field(
            self, *, app_token: str, table_id: str, definition: dict,
    ) -> dict:
        """Create only a caller-validated field definition."""
        expected = {
            "name": "处理明细", "type": "text", "style": {"type": "plain"},
        }
        if definition != expected:
            raise LarkRunnerError("field definition is not approved")
        payload = json.dumps(
            expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return self._json([
            "base", "+field-create", "--base-token",
            self._required(app_token, "app token"), "--table-id",
            self._required(table_id, "table ID"), "--json", payload,
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
        command = self._record_list_command(
            app_token=app_token, table_id=table_id, field_ids=field_ids,
            filter_arg=filter_arg, output_arg=output_arg, limit=limit,
            offset=offset, view_id=view_id,
        )
        self._run(command, cwd=output_path.parent)
        return output_path

    def list_records_page(
            self, *, app_token: str, table_id: str, field_ids: Sequence[str],
            filter_payload: Path, output: Path, limit: int = 2000,
            offset: int = 0, view_id: str | None = None,
            retry_failed: bool = False,
    ) -> RecordPage:
        """Write one NDJSON page and return its validated minimal summary."""
        filter_path, filter_arg = self._input_path(filter_payload)
        self._validate_status_filter(filter_path, retry_failed=retry_failed)
        output_path, output_arg = self._output_path(output)
        command = self._record_list_command(
            app_token=app_token, table_id=table_id, field_ids=field_ids,
            filter_arg=filter_arg, output_arg=output_arg, limit=limit,
            offset=offset, view_id=view_id,
        )
        result = self._run(command, cwd=output_path.parent)
        try:
            summary = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise LarkRunnerError("lark-cli returned invalid record summary") from None
        if (not isinstance(summary, dict)
                or not isinstance(summary.get("records_count"), int)
                or isinstance(summary.get("records_count"), bool)
                or summary["records_count"] < 0
                or not isinstance(summary.get("has_more"), bool)):
            raise LarkRunnerError("lark-cli returned invalid record summary")
        return RecordPage(
            path=output_path, records_count=summary["records_count"],
            has_more=summary["has_more"],
        )

    def _record_list_command(
            self, *, app_token: str, table_id: str,
            field_ids: Sequence[str], filter_arg: str, output_arg: str,
            limit: int, offset: int, view_id: str | None,
    ) -> list[str]:
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
        return command

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
        payload_path, _payload_arg = self._input_path(payload)
        record_id = self._required(record_id, "record ID")
        canonical_payload = self._canonical_update_file(payload_path, record_id)
        return self.update_record_canonical(
            app_token=app_token, table_id=table_id, record_id=record_id,
            canonical_payload=canonical_payload,
        )

    def update_record_canonical(
            self, *, app_token: str, table_id: str, record_id: str,
            canonical_payload: bytes,
    ) -> dict:
        """Consume validated canonical bytes from a locked transport-owned file."""
        record_id = self._required(record_id, "record ID")
        canonical_payload = self._validated_canonical_update(
            canonical_payload, record_id,
        )
        private = self._stage_private_update(canonical_payload)
        try:
            return self._json([
                "base", "+record-batch-update", "--base-token",
                self._required(app_token, "app token"), "--table-id",
                self._required(table_id, "table ID"), "--json",
                "@./record-update.json", "--as", "user",
            ], cwd=private.directory)
        finally:
            self._cleanup_private_update(private)

    def get_record(
            self, *, app_token: str, table_id: str, record_id: str,
            field_ids: Sequence[str] = (),
    ) -> dict:
        """Read one record by ID with an optional exact field projection."""
        command = [
            "base", "+record-get", "--base-token", self._required(app_token, "app token"),
            "--table-id", self._required(table_id, "table ID"), "--record-id",
            self._required(record_id, "record ID"),
        ]
        if isinstance(field_ids, (str, bytes)):
            raise LarkRunnerError("field IDs must be a sequence")
        for field_id in field_ids:
            command.extend(["--field-id", self._required(field_id, "field ID")])
        command.extend(["--format", "json", "--as", "user"])
        return self._json(command)

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

    @classmethod
    def _canonical_update_file(cls, payload: Path, record_id: str) -> bytes:
        invalid_payload = False
        try:
            decoded = json.loads(
                payload.read_text(encoding="utf-8"),
                object_pairs_hook=cls._unique_object,
            )
        except (OSError, UnicodeError, ValueError):
            invalid_payload = True
            decoded = None
        if not cls._is_exact_record_update(decoded, record_id):
            invalid_payload = True
        if invalid_payload:
            raise LarkRunnerError("record update payload is invalid")
        return cls._encode_canonical_update(decoded)

    @classmethod
    def _validated_canonical_update(
            cls, supplied: bytes, record_id: str,
    ) -> bytes:
        invalid_payload = type(supplied) is not bytes
        try:
            decoded = json.loads(
                supplied.decode("utf-8") if type(supplied) is bytes else "",
                object_pairs_hook=cls._unique_object,
            )
        except (UnicodeError, ValueError):
            invalid_payload = True
            decoded = None
        if (not cls._is_exact_record_update(decoded, record_id)
                or (isinstance(decoded, dict)
                    and supplied != cls._encode_canonical_update(decoded))):
            invalid_payload = True
        if invalid_payload:
            raise LarkRunnerError("canonical record update payload is invalid")
        return supplied

    @staticmethod
    def _is_exact_record_update(decoded: object, record_id: str) -> bool:
        update_records = decoded.get("update_records") if isinstance(decoded, dict) else None
        return (
            isinstance(decoded, dict)
            and set(decoded) == {"update_records"}
            and isinstance(update_records, dict)
            and set(update_records) == {record_id}
            and isinstance(update_records.get(record_id), dict)
        )

    @staticmethod
    def _encode_canonical_update(decoded: dict) -> bytes:
        return (
            json.dumps(
                decoded, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        ).encode("utf-8")

    @staticmethod
    def _stage_private_update(canonical_payload: bytes) -> _PrivateUpdate:
        directory: Path | None = None
        directory_fd = payload_fd = -1
        try:
            directory = Path(tempfile.mkdtemp(prefix=".lark-update-"))
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0) | no_follow,
            )
            directory_stat = os.fstat(directory_fd)
            if (not stat.S_ISDIR(directory_stat.st_mode)
                    or directory_stat.st_mode & 0o777 != 0o700):
                raise OSError("private update directory is invalid")
            payload_fd = os.open(
                "record-update.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | no_follow,
                stat.S_IRUSR | stat.S_IWUSR,
                dir_fd=directory_fd,
            )
            payload_stat = os.fstat(payload_fd)
            if not stat.S_ISREG(payload_stat.st_mode):
                raise OSError("private update payload is invalid")
            offset = 0
            while offset < len(canonical_payload):
                offset += os.write(payload_fd, canonical_payload[offset:])
            os.fsync(payload_fd)
            os.fchmod(payload_fd, stat.S_IRUSR)
            os.close(payload_fd)
            payload_fd = -1
            os.fsync(directory_fd)
            os.fchmod(directory_fd, stat.S_IRUSR | stat.S_IXUSR)
            return _PrivateUpdate(
                directory=directory,
                directory_fd=directory_fd,
                directory_identity=(directory_stat.st_dev, directory_stat.st_ino),
                payload_identity=(payload_stat.st_dev, payload_stat.st_ino),
            )
        except OSError:
            if payload_fd >= 0:
                os.close(payload_fd)
            if directory_fd >= 0:
                try:
                    os.fchmod(directory_fd, stat.S_IRWXU)
                    os.unlink("record-update.json", dir_fd=directory_fd)
                except OSError:
                    pass
                os.close(directory_fd)
            if directory is not None:
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise LarkRunnerError("cannot stage private record update") from None

    @staticmethod
    def _cleanup_private_update(private: _PrivateUpdate) -> None:
        cleanup_failed = False
        try:
            directory_stat = os.fstat(private.directory_fd)
            if (not stat.S_ISDIR(directory_stat.st_mode)
                    or (directory_stat.st_dev, directory_stat.st_ino)
                    != private.directory_identity):
                raise OSError("private update directory identity changed")
            os.fchmod(private.directory_fd, stat.S_IRWXU)
            payload_stat = os.stat(
                "record-update.json", dir_fd=private.directory_fd,
                follow_symlinks=False,
            )
            if (not stat.S_ISREG(payload_stat.st_mode)
                    or (payload_stat.st_dev, payload_stat.st_ino)
                    != private.payload_identity):
                raise OSError("private update payload identity changed")
            os.unlink("record-update.json", dir_fd=private.directory_fd)
            os.fsync(private.directory_fd)
        except OSError:
            cleanup_failed = True
        finally:
            os.close(private.directory_fd)
        try:
            directory_stat = private.directory.stat(follow_symlinks=False)
            if (not stat.S_ISDIR(directory_stat.st_mode)
                    or (directory_stat.st_dev, directory_stat.st_ino)
                    != private.directory_identity):
                raise OSError("private update directory entry changed")
            private.directory.rmdir()
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            raise LarkRunnerError("cannot clean private record update") from None

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
