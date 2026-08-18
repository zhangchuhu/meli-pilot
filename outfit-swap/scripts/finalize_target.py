"""Idempotent commit transaction for one already-selected target bitmap."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

try:
    from scripts import image_qc, task_state
except ImportError:  # Support direct execution from the scripts directory.
    import image_qc  # type: ignore[no-redef]
    import task_state  # type: ignore[no-redef]


OUTPUT_FIELD = "输出图"
DETAIL_FIELD = "处理明细"
SHA256 = re.compile(r"[0-9a-f]{64}")


class FinalizeError(RuntimeError):
    """Raised when a finalization checkpoint cannot advance safely."""


class BaseClient(Protocol):
    def upload_attachment(self, **kwargs: object) -> dict: ...
    def update_record(self, **kwargs: object) -> dict: ...
    def get_record(self, **kwargs: object) -> dict: ...


@dataclass(frozen=True)
class FinalizeRequest:
    task_dir: Path
    state_file: Path
    record_id: str
    target_index: int
    candidate: Path
    candidate_sha256: str


@dataclass(frozen=True)
class FinalizeResult:
    output_path: Path
    attachment_token: str
    resumed_from: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TargetFinalizer:
    """Finalize targets through the typed ``LarkBaseClient`` boundary."""

    def __init__(
            self, *, base: BaseClient, app_token: str, table_id: str,
            output_field_id: str, clock: Callable[[], str] = _utc_now,
    ) -> None:
        if not all(isinstance(value, str) and value
                   for value in (app_token, table_id, output_field_id)):
            raise FinalizeError("Base finalization scope is incomplete")
        self._base = base
        self._app_token = app_token
        self._table_id = table_id
        self._output_field_id = output_field_id
        self._clock = clock

    def __call__(self, request: FinalizeRequest) -> FinalizeResult:
        return self.finalize(request)

    def finalize(self, request: FinalizeRequest) -> FinalizeResult:
        root, candidate = self._validate_request(request)
        lock_name = hashlib.sha256(
            f"{request.record_id}\0{request.target_index}".encode(),
        ).hexdigest()[:20]
        lock_path = root / f".finalize-{lock_name}.lock"
        descriptor = -1
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
            )
            lock = os.fdopen(descriptor, "a+b")
            descriptor = -1
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise FinalizeError("cannot acquire target finalization lock") from error
        with lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return self._finalize_locked(request, root, candidate)

    def _finalize_locked(
            self, request: FinalizeRequest, root: Path, candidate: Path,
    ) -> FinalizeResult:
        self._revalidate_candidate(candidate, request.candidate_sha256)
        try:
            state = task_state.load_state(request.state_file)
        except task_state.TaskStateError as error:
            raise FinalizeError("target state is invalid") from error
        target_token, target = self._target(state, request)
        output_name = task_state.promoted_output_name(candidate.name, target_token)
        output_path = root / output_name
        self._require_selected_candidate(target, candidate.name)
        self._promote_append_safe(candidate, output_path, request.candidate_sha256)

        resumed_from = target["status"]
        if resumed_from == "running":
            resumed_from = "candidate"
            try:
                task_state.record_local_acceptance(
                    state, target_token=target_token, artifact_name=candidate.name,
                    name=output_name, updated_at=self._timestamp(),
                )
                task_state.save_state(request.state_file, state)
            except task_state.TaskStateError as error:
                raise FinalizeError("cannot persist local acceptance") from error

        fields = self._read_fields(request.record_id, "Base reconciliation failed")
        outputs = self._outputs(fields)
        target = state["targets"][target_token]
        if target["status"] == "success":
            mapping = target["output"]
            if mapping not in outputs:
                raise FinalizeError("successful attachment is absent from Base readback")
            detail = task_state.compact_detail(state)
            if fields.get(DETAIL_FIELD) == detail:
                return FinalizeResult(output_path, mapping["file_token"], "verified")
            resumed_from = "success"
        else:
            try:
                mapping = task_state.reconcile_target_output(
                    state, target_index=request.target_index, outputs=outputs,
                    updated_at=self._timestamp(),
                )
            except task_state.TaskStateError as error:
                raise FinalizeError("cannot reconcile uploaded attachment") from error
            if mapping is not None:
                resumed_from = "uploaded"
                try:
                    task_state.save_state(request.state_file, state)
                except task_state.TaskStateError as error:
                    raise FinalizeError("cannot persist reconciled attachment") from error
            else:
                mapping = self._upload(request.record_id, output_path)
                try:
                    task_state.record_success(
                        state, target_token=target_token,
                        file_token=mapping["file_token"], name=output_name,
                        updated_at=self._timestamp(),
                    )
                    task_state.save_state(request.state_file, state)
                except task_state.TaskStateError as error:
                    raise FinalizeError("cannot persist successful attachment mapping") from error

        detail = task_state.compact_detail(state)
        self._write_detail(root, request.record_id, detail)
        readback = self._read_fields(request.record_id, "Base readback failed")
        if (mapping not in self._outputs(readback)
                or readback.get(DETAIL_FIELD) != detail):
            raise FinalizeError("Base readback mismatch")
        return FinalizeResult(output_path, mapping["file_token"], resumed_from)

    @staticmethod
    def _validate_request(request: FinalizeRequest) -> tuple[Path, Path]:
        if not isinstance(request, FinalizeRequest):
            raise FinalizeError("finalize request is invalid")
        root = Path(request.task_dir).resolve()
        candidate = Path(request.candidate).resolve()
        if (not root.is_dir() or candidate.parent != root
                or not isinstance(request.record_id, str) or not request.record_id
                or not isinstance(request.target_index, int)
                or isinstance(request.target_index, bool)
                or not SHA256.fullmatch(request.candidate_sha256)):
            raise FinalizeError("finalize request is invalid")
        return root, candidate

    @staticmethod
    def _revalidate_candidate(candidate: Path, expected_sha256: str) -> None:
        try:
            image_qc.validate_image(candidate)
        except (image_qc.ImageQCError, OSError) as error:
            raise FinalizeError("candidate image is invalid") from error
        if _sha256(candidate) != expected_sha256:
            raise FinalizeError("candidate digest does not match QC identity")

    @staticmethod
    def _target(
            state: dict[str, Any], request: FinalizeRequest,
    ) -> tuple[str, dict[str, Any]]:
        if state["record_id"] != request.record_id:
            raise FinalizeError("request record does not match target state")
        if not 0 <= request.target_index < len(state["target_tokens"]):
            raise FinalizeError("target index is outside current state")
        token = state["target_tokens"][request.target_index]
        return token, state["targets"][token]

    @staticmethod
    def _require_selected_candidate(
            target: dict[str, Any], candidate_name: str,
    ) -> None:
        status = target["status"]
        if status == "running":
            return
        if status == "accepted-local":
            accepted = target["local_acceptance"]
            if accepted["artifact_name"] == candidate_name:
                return
        elif status == "success":
            if any(
                    entry.get("artifact_name") == candidate_name
                    and entry.get("outcome") == "success"
                    for entry in target["attempt_history"]
            ):
                return
        raise FinalizeError("candidate does not match the durable target checkpoint")

    @staticmethod
    def _promote_append_safe(
            candidate: Path, output: Path, candidate_sha256: str,
    ) -> None:
        if output.exists() or output.is_symlink():
            if (output.is_symlink() or not output.is_file()
                    or _sha256(output) != candidate_sha256):
                raise FinalizeError("deterministic output conflicts with accepted candidate")
            return
        try:
            image_qc.promote_output(candidate, output)
        except (image_qc.ImageQCError, OSError) as error:
            raise FinalizeError("candidate promotion failed") from error
        if _sha256(output) != candidate_sha256:
            raise FinalizeError("promoted output identity mismatch")

    def _upload(self, record_id: str, output: Path) -> dict[str, str]:
        try:
            response = self._base.upload_attachment(
                file=Path(output.name), app_token=self._app_token,
                table_id=self._table_id, record_id=record_id,
                field_id=self._output_field_id,
            )
            return _upload_mapping(
                response, output.name, record_id, self._output_field_id,
            )
        except Exception:
            raise FinalizeError("attachment upload failed") from None

    def _write_detail(self, root: Path, record_id: str, detail: str) -> None:
        descriptor, filename = tempfile.mkstemp(
            prefix=".finalize-detail-", suffix=".json", dir=root,
        )
        payload = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"update_records": {record_id: {DETAIL_FIELD: detail}}},
                    handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                self._base.update_record(
                    app_token=self._app_token, table_id=self._table_id,
                    record_id=record_id, payload=Path(payload.name),
                )
            except Exception:
                raise FinalizeError("detail update failed") from None
        finally:
            try:
                payload.unlink()
            except FileNotFoundError:
                pass

    def _read_fields(self, record_id: str, message: str) -> dict[str, Any]:
        try:
            response = self._base.get_record(
                app_token=self._app_token, table_id=self._table_id,
                record_id=record_id,
                field_ids=[self._output_field_id, DETAIL_FIELD],
            )
            return _record_fields(response, record_id)
        except Exception:
            raise FinalizeError(message) from None

    @staticmethod
    def _outputs(fields: dict[str, Any]) -> list[dict[str, str]]:
        raw = fields.get(OUTPUT_FIELD, [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise FinalizeError("Base output field is invalid")
        outputs: list[dict[str, str]] = []
        for item in raw:
            if (not isinstance(item, dict)
                    or not isinstance(item.get("file_token"), str)
                    or not item["file_token"]
                    or not isinstance(item.get("name"), str)
                    or not item["name"]):
                raise FinalizeError("Base output field is invalid")
            outputs.append({"file_token": item["file_token"], "name": item["name"]})
        return outputs

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, str) or not value:
            raise FinalizeError("finalization clock returned an invalid timestamp")
        return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FinalizeError("cannot read finalization artifact") from error
    return digest.hexdigest()


def _upload_mapping(
        response: Any, expected_name: str, record_id: str, field_id: str,
) -> dict[str, str]:
    candidates = [response]
    if isinstance(response, dict):
        candidates.extend(response.get(key) for key in ("data", "file", "attachment"))
        data = response.get("data")
        if isinstance(data, dict):
            candidates.extend(data.get(key) for key in ("file", "attachment"))
    for value in candidates:
        if not isinstance(value, dict):
            continue
        token = value.get("file_token")
        name = value.get("name", expected_name)
        if isinstance(token, str) and token and name == expected_name:
            return {"file_token": token, "name": expected_name}
    envelopes = [response]
    if isinstance(response, dict):
        envelopes.append(response.get("data"))
    for envelope in envelopes:
        attachments = envelope.get("attachments") if isinstance(envelope, dict) else None
        record = attachments.get(record_id) if isinstance(attachments, dict) else None
        values = record.get(field_id) if isinstance(record, dict) else None
        if not isinstance(values, list) or len(values) != 1:
            continue
        value = values[0]
        if (isinstance(value, dict)
                and isinstance(value.get("file_token"), str)
                and value["file_token"]
                and value.get("name") == expected_name):
            return {"file_token": value["file_token"], "name": expected_name}
    raise FinalizeError("attachment upload returned an invalid mapping")


def _record_fields(response: Any, record_id: str) -> dict[str, Any]:
    candidates = [response]
    if isinstance(response, dict):
        candidates.append(response.get("record"))
        data = response.get("data")
        candidates.append(data)
        if isinstance(data, dict):
            candidates.append(data.get("record"))
    for value in candidates:
        if not isinstance(value, dict) or not isinstance(value.get("fields"), dict):
            continue
        returned_id = value.get("record_id", value.get("id"))
        if returned_id is not None and returned_id != record_id:
            continue
        return value["fields"]
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        names = data.get("fields")
        rows = data.get("data")
        record_ids = data.get("record_id_list")
        if (isinstance(names, list) and all(isinstance(name, str) for name in names)
                and isinstance(rows, list) and len(rows) == 1
                and isinstance(rows[0], list) and len(rows[0]) == len(names)
                and record_ids == [record_id]
                and len(names) == len(set(names))):
            return dict(zip(names, rows[0], strict=True))
    raise FinalizeError("Base returned an invalid record")
