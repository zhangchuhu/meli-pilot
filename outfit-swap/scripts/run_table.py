#!/usr/bin/env python3
"""Preflight one exact Base table and schedule serial record workers safely."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

try:
    from scripts import finalize_target, prompt_builder, safe_edit, task_state
    from scripts.run_record import (
        GenerationRequest,
        QCRequest,
        RecordContext,
        RecordResult,
        RecordServices,
        run_record,
    )
except ImportError:  # pragma: no cover - direct script-directory execution
    import finalize_target  # type: ignore[no-redef]
    import prompt_builder  # type: ignore[no-redef]
    import safe_edit  # type: ignore[no-redef]
    import task_state  # type: ignore[no-redef]
    from run_record import (  # type: ignore[no-redef]
        GenerationRequest,
        QCRequest,
        RecordContext,
        RecordResult,
        RecordServices,
        run_record,
    )


class TableSchedulerError(RuntimeError):
    """Raised when table orchestration cannot continue safely."""


class PreflightError(TableSchedulerError):
    """Raised before workers start when global table scope is invalid."""


class PaidCallStopped(TableSchedulerError):
    """Raised inside a worker when global stop closes a paid-call gate."""


@dataclass(frozen=True)
class ServiceLimits:
    record_workers: int = 2
    doubao_requests: int = 2
    qc_requests: int = 2
    lark_writes: int = 1
    lark_reads: int = 2

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class TableConfig:
    base_url: str
    record_concurrency: int = 2
    retry_failed: bool = False
    qc_mode: str = "automatic"

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ValueError("base_url is required")
        if (not isinstance(self.record_concurrency, int)
                or isinstance(self.record_concurrency, bool)
                or self.record_concurrency <= 0):
            raise ValueError("record_concurrency must be a positive integer")
        if not isinstance(self.retry_failed, bool):
            raise ValueError("retry_failed must be boolean")
        if self.qc_mode not in {"automatic", "shadow"}:
            raise ValueError("qc_mode must be automatic or shadow")


@dataclass(frozen=True)
class TableResult:
    selected: int
    succeeded: int
    failed: int
    stopped: int


@dataclass(frozen=True)
class TableScope:
    app_token: str
    table_id: str
    view_id: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (
                self.app_token, self.table_id, self.view_id,
        )):
            raise PreflightError("exact Base table and view scope is required")


@dataclass(frozen=True)
class RecordBaseScope:
    table: TableScope
    record_id: str
    attachment_tokens: frozenset[str]
    output_field_id: str
    status_field_id: str
    detail_field_id: str
    payload_root: Path

    def __post_init__(self) -> None:
        if (not isinstance(self.table, TableScope)
                or not isinstance(self.record_id, str) or not self.record_id
                or not isinstance(self.attachment_tokens, frozenset)
                or not all(
                    isinstance(token, str) and token
                    for token in self.attachment_tokens
                )
                or not isinstance(self.output_field_id, str)
                or not self.output_field_id
                or not isinstance(self.status_field_id, str)
                or not self.status_field_id
                or not isinstance(self.detail_field_id, str)
                or not self.detail_field_id
                or len({
                    self.output_field_id,
                    self.status_field_id,
                    self.detail_field_id,
                }) != 3):
            raise PreflightError("record Base capability scope is invalid")
        try:
            payload_root = Path(self.payload_root).resolve(strict=False)
        except (OSError, TypeError, ValueError):
            raise PreflightError("record Base capability scope is invalid") from None
        object.__setattr__(self, "payload_root", payload_root)


@dataclass(frozen=True)
class BaseField:
    name: str
    field_id: str
    kind: str
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.name, str) or not self.name
                or not isinstance(self.field_id, str) or not self.field_id
                or not isinstance(self.kind, str) or not self.kind
                or not isinstance(self.options, tuple)
                or not all(isinstance(value, str) and value for value in self.options)):
            raise PreflightError("Base field definition is invalid")


@dataclass(frozen=True)
class TableSchema:
    fields: tuple[BaseField, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.fields, tuple)
                or not all(isinstance(field, BaseField) for field in self.fields)):
            raise PreflightError("Base schema is invalid")
        names = tuple(field.name for field in self.fields)
        identifiers = tuple(field.field_id for field in self.fields)
        if len(set(names)) != len(names) or len(set(identifiers)) != len(identifiers):
            raise PreflightError("Base schema contains duplicate fields")

    def field(self, name: str) -> BaseField:
        for field in self.fields:
            if field.name == name:
                return field
        raise PreflightError(f"required Base field is missing: {name}")


@dataclass(frozen=True)
class TableRuntime:
    adapter: object
    base: object
    generator: object
    qc: object
    worker: Callable[[RecordContext, RecordServices], RecordResult] = run_record

    def __post_init__(self) -> None:
        if not callable(self.worker):
            raise TypeError("record worker must be callable")


class GlobalStop:
    """An Event-compatible stop signal with an atomic paid-call start gate."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._active_paid = 0

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        with self._lock:
            self._event.set()

    def try_start_paid(self) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._active_paid += 1
            return True

    def finish_paid(self) -> None:
        with self._lock:
            if self._active_paid <= 0:
                raise RuntimeError("paid-call gate is unbalanced")
            self._active_paid -= 1

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def _start_paid(stop_signal: object) -> bool:
    starter = getattr(stop_signal, "try_start_paid", None)
    if callable(starter):
        return bool(starter())
    checker = getattr(stop_signal, "is_set", None)
    if not callable(checker):
        raise TableSchedulerError("stop signal has no is_set boundary")
    return not bool(checker())


def _finish_paid(stop_signal: object) -> None:
    finisher = getattr(stop_signal, "finish_paid", None)
    if callable(finisher):
        finisher()


def _request_stop(stop_signal: object) -> None:
    setter = getattr(stop_signal, "set", None)
    if not callable(setter):
        raise TableSchedulerError("stop signal has no set boundary")
    setter()


class BoundedGenerator:
    """Limit Seedream transport and recheck stop/state after waiting."""

    def __init__(
            self, service: object, semaphore: threading.BoundedSemaphore,
            stop_signal: object, *,
            checkpoint: Callable[[object], None] | None = None,
    ) -> None:
        self._service = service
        self._semaphore = semaphore
        self._stop_signal = stop_signal
        self._checkpoint = checkpoint or _generation_paid_checkpoint

    @property
    def model(self) -> object:
        return getattr(self._service, "model", None)

    def plan_target(self, *args: object, **kwargs: object) -> object:
        planner = getattr(self._service, "plan_target", None)
        if not callable(planner):
            raise TableSchedulerError("generator has no target-plan boundary")
        return planner(*args, **kwargs)

    def artifact_path(self, *args: object, **kwargs: object) -> object:
        resolver = getattr(self._service, "artifact_path", None)
        if not callable(resolver):
            raise TableSchedulerError("generator has no artifact boundary")
        return resolver(*args, **kwargs)

    def generate(self, request: object) -> object:
        generator = getattr(self._service, "generate", None)
        if not callable(generator):
            raise TableSchedulerError("generator has no paid-call boundary")
        with self._semaphore:
            try:
                self._checkpoint(request)
            except Exception:
                _request_stop(self._stop_signal)
                raise PaidCallStopped(
                    "durable state changed before paid generation",
                ) from None
            if not _start_paid(self._stop_signal):
                raise PaidCallStopped("global stop blocked paid generation")
            try:
                return generator(request)
            finally:
                _finish_paid(self._stop_signal)


class BoundedQC:
    """Limit Ark QC transport and recheck stop/state after waiting."""

    def __init__(
            self, service: object, semaphore: threading.BoundedSemaphore,
            stop_signal: object, *,
            checkpoint: Callable[[object], None] | None = None,
    ) -> None:
        self._service = service
        self._semaphore = semaphore
        self._stop_signal = stop_signal
        self._checkpoint = checkpoint or _qc_paid_checkpoint

    def review(self, request: object) -> object:
        reviewer = getattr(self._service, "review", None)
        if not callable(reviewer):
            raise TableSchedulerError("QC service has no review boundary")
        with self._semaphore:
            try:
                self._checkpoint(request)
            except Exception:
                _request_stop(self._stop_signal)
                raise PaidCallStopped(
                    "durable state changed before paid QC",
                ) from None
            if not _start_paid(self._stop_signal):
                raise PaidCallStopped("global stop blocked paid QC")
            try:
                return reviewer(request)
            finally:
                _finish_paid(self._stop_signal)


class BoundedBase:
    """Apply independent Lark read/write limits without changing its transport."""

    def __init__(
            self, service: object, *,
            read_semaphore: threading.BoundedSemaphore,
            write_semaphore: threading.BoundedSemaphore,
    ) -> None:
        self._service = service
        self._read_semaphore = read_semaphore
        self._write_semaphore = write_semaphore

    def scoped(self, scope: RecordBaseScope) -> "ScopedBase":
        return ScopedBase(self, scope)

    def _read(self, name: str, *args: object, **kwargs: object) -> object:
        method = getattr(self._service, name, None)
        if not callable(method):
            raise TableSchedulerError(f"Base service has no {name} boundary")
        with self._read_semaphore:
            return method(*args, **kwargs)

    def _write(self, name: str, *args: object, **kwargs: object) -> object:
        method = getattr(self._service, name, None)
        if not callable(method):
            raise TableSchedulerError(f"Base service has no {name} boundary")
        with self._write_semaphore:
            return method(*args, **kwargs)

    def resolve_base(self, base_url: str) -> object:
        return self._read("resolve_base", base_url)

    def list_fields(self, **kwargs: object) -> object:
        return self._read("list_fields", **kwargs)

    def list_records(self, **kwargs: object) -> object:
        return self._read("list_records", **kwargs)

    def download_attachment(self, **kwargs: object) -> object:
        return self._read("download_attachment", **kwargs)

    def get_record(self, **kwargs: object) -> object:
        return self._read("get_record", **kwargs)

    def create_field(self, **kwargs: object) -> object:
        return self._write("create_field", **kwargs)

    def upload_attachment(self, **kwargs: object) -> object:
        return self._write("upload_attachment", **kwargs)

    def update_record(self, **kwargs: object) -> object:
        return self._write("update_record", **kwargs)


class ScopedBase:
    """Reject any record-worker Lark call outside the preflighted scope."""

    def __init__(self, base: BoundedBase, scope: RecordBaseScope) -> None:
        if not isinstance(scope, RecordBaseScope):
            raise PreflightError("record scope is invalid")
        self._base = base
        self._scope_value = scope

    def _scope(self, kwargs: dict[str, object], expected_keys: frozenset[str]) -> None:
        if set(kwargs) != expected_keys:
            raise PreflightError("record Base call arguments are outside capability scope")
        token = kwargs.get("app_token", kwargs.get("base_token"))
        if token != self._scope_value.table.app_token:
            raise PreflightError("Base call escaped the preflighted app scope")
        if kwargs.get("table_id") != self._scope_value.table.table_id:
            raise PreflightError("Base call escaped the preflighted table scope")
        if kwargs.get("record_id") != self._scope_value.record_id:
            raise PreflightError("Base call escaped the active record scope")

    def download_attachment(self, **kwargs: object) -> object:
        self._scope(kwargs, frozenset({
            "app_token", "table_id", "record_id", "token", "output",
        }))
        if kwargs["token"] not in self._scope_value.attachment_tokens:
            raise PreflightError("attachment token is outside the active record scope")
        return self._base.download_attachment(**kwargs)

    def get_record(self, **kwargs: object) -> object:
        self._scope(kwargs, frozenset({
            "app_token", "table_id", "record_id", "field_ids",
        }))
        if kwargs["field_ids"] != [
                self._scope_value.output_field_id,
                self._scope_value.detail_field_id,
        ]:
            raise PreflightError("Base read fields escaped the record capability scope")
        return self._base.get_record(**kwargs)

    def upload_attachment(self, **kwargs: object) -> object:
        self._scope(kwargs, frozenset({
            "app_token", "table_id", "record_id", "field_id", "file",
        }))
        if kwargs["field_id"] != self._scope_value.output_field_id:
            raise PreflightError("Base upload field escaped the record capability scope")
        return self._base.upload_attachment(**kwargs)

    def update_record(self, **kwargs: object) -> object:
        self._scope(kwargs, frozenset({
            "app_token", "table_id", "record_id", "payload",
        }))
        self._validate_update_payload(kwargs["payload"])
        return self._base.update_record(**kwargs)

    def _validate_update_payload(self, supplied: object) -> None:
        invalid = False
        path: Path | None = None
        if isinstance(supplied, Path):
            if (supplied.is_absolute() or len(supplied.parts) != 1
                    or supplied.name in {"", ".", ".."}):
                invalid = True
            else:
                path = self._scope_value.payload_root / supplied.name
        else:
            invalid = True
        try:
            if (path is None or path.is_symlink() or not path.is_file()
                    or path.resolve().parent != self._scope_value.payload_root):
                invalid = True
                decoded = None
            else:
                decoded = json.loads(
                    path.read_text(encoding="utf-8"),
                    object_pairs_hook=self._unique_json_object,
                )
        except (OSError, UnicodeError, ValueError):
            invalid = True
            decoded = None

        if not isinstance(decoded, dict) or set(decoded) != {"update_records"}:
            invalid = True
            updates = None
        else:
            updates = decoded["update_records"]
        if (not isinstance(updates, dict)
                or set(updates) != {self._scope_value.record_id}):
            invalid = True
            fields = None
        else:
            fields = updates[self._scope_value.record_id]
        allowed = {"任务状态", "处理明细"}
        if (not isinstance(fields, dict) or not fields
                or not set(fields).issubset(allowed)):
            invalid = True
        else:
            detail = fields.get("处理明细")
            status = fields.get("任务状态")
            if "处理明细" in fields and not isinstance(detail, str):
                invalid = True
            if ("任务状态" in fields
                    and (not isinstance(status, list) or len(status) != 1
                         or not isinstance(status[0], str)
                         or status[0] not in {"未开始", "成功", "失败"})):
                invalid = True
        if invalid:
            raise PreflightError(
                "record update payload escaped field capability scope",
            )

    @staticmethod
    def _unique_json_object(
            pairs: list[tuple[object, object]],
    ) -> dict[object, object]:
        decoded: dict[object, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError("duplicate JSON key")
            decoded[key] = value
        return decoded


class SeedreamGeneratorAdapter:
    """Production generator boundary pinned to ``safe_edit.run_edit``."""

    model = safe_edit.MODEL

    def __init__(
            self, *, doubao_script: str | Path, planner: object,
            image_resolver: Callable[[GenerationRequest], Iterable[Path]],
            approved_run_roots: Iterable[Path],
            artifact_resolver: Callable[
                [RecordContext, dict[str, object]], Path
            ] | None = None,
    ) -> None:
        script = Path(doubao_script).resolve()
        if not script.is_file():
            raise TableSchedulerError("Doubao transport script is missing")
        if not callable(planner) and not callable(getattr(planner, "plan_target", None)):
            raise TypeError("target planner must be callable")
        if not callable(image_resolver):
            raise TypeError("generation image resolver must be callable")
        if artifact_resolver is not None and not callable(artifact_resolver):
            raise TypeError("artifact resolver must be callable")
        if isinstance(approved_run_roots, (str, bytes, Path)):
            raise TypeError("approved run roots must be an iterable of directories")
        try:
            run_roots = tuple(Path(root).resolve() for root in approved_run_roots)
        except (OSError, TypeError, ValueError):
            raise TableSchedulerError("approved run roots are invalid") from None
        if (not run_roots or len(set(run_roots)) != len(run_roots)
                or not all(root.is_dir() for root in run_roots)):
            raise TableSchedulerError("approved run roots are invalid")
        self._doubao_script = script
        self._planner = planner
        self._image_resolver = image_resolver
        self._artifact_resolver = artifact_resolver
        self._approved_run_roots = run_roots

    def plan_target(
            self, context: RecordContext, target_index: int, target_token: str,
    ) -> prompt_builder.TargetPlan:
        planner = (
            self._planner
            if callable(self._planner)
            else getattr(self._planner, "plan_target")
        )
        plan = planner(context, target_index, target_token)
        if not isinstance(plan, prompt_builder.TargetPlan):
            raise TableSchedulerError("target planner returned an invalid plan")
        return plan

    def artifact_path(
            self, context: RecordContext, history: dict[str, object],
    ) -> Path:
        name = history.get("artifact_name")
        run_id = history.get("run_id")
        if (not isinstance(name, str) or not name or Path(name).name != name
                or not isinstance(run_id, str) or not run_id
                or Path(run_id).name != run_id):
            raise TableSchedulerError("attempt artifact identity is invalid")
        value = (
            self._artifact_resolver(context, history)
            if self._artifact_resolver is not None
            else Path(context.task_dir).resolve() / "generated_images" / name
        )
        supplied = Path(value)
        path = supplied.resolve()
        if (path.name != name or supplied.is_symlink()
                or not self._is_approved(path)):
            raise TableSchedulerError("artifact resolver changed the immutable identity")
        current = (
            Path(context.task_dir).resolve() / "generated_images" / name
        )
        if path.exists():
            if not path.is_file():
                raise TableSchedulerError("resolved attempt artifact is not a regular file")
            if path != current and path not in self._prior_artifact_identities(
                    run_id, context.record_id, name,
            ):
                raise TableSchedulerError("prior-run attempt identity is invalid")
        elif path != current:
            raise TableSchedulerError("prior-run attempt artifact is missing")
        return path

    def generate(self, request: GenerationRequest) -> Path:
        if not isinstance(request, GenerationRequest):
            raise TableSchedulerError("generation request is invalid")
        output = Path(request.output_path).resolve()
        expected = (
            Path(request.context.task_dir).resolve()
            / "generated_images" / request.artifact_name
        )
        if (output != expected or not self._is_approved(output)
                or output.exists() or output.is_symlink()):
            raise TableSchedulerError("generation output escaped its immutable path")
        try:
            images = [Path(path).resolve() for path in self._image_resolver(request)]
        except Exception:
            raise TableSchedulerError("generation images could not be resolved") from None
        if (not 2 <= len(images) <= 10
                or not all(path.is_file() for path in images)):
            raise TableSchedulerError("generation requires two through ten local images")
        descriptor, filename = tempfile.mkstemp(
            prefix=".seedream-prompt-", suffix=".txt",
            dir=Path(request.context.task_dir).resolve(),
        )
        prompt_file = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(request.prompt)
                stream.flush()
                os.fsync(stream.fileno())
            returned = safe_edit.run_edit(
                doubao_script=self._doubao_script,
                prompt_file=prompt_file,
                images=images,
                output=output,
            )
        finally:
            try:
                prompt_file.unlink()
            except FileNotFoundError:
                pass
        resolved = Path(returned).resolve()
        if resolved != output:
            raise TableSchedulerError("Seedream transport returned the wrong artifact")
        return resolved

    def _is_approved(self, path: Path) -> bool:
        for root in self._approved_run_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _prior_artifact_identities(
            self, run_id: str, record_id: str, name: str,
    ) -> frozenset[Path]:
        identities: set[Path] = set()
        for root in self._approved_run_roots:
            identities.add(
                root / run_id / record_id / "generated_images" / name,
            )
            if root.name == run_id:
                identities.add(root / record_id / "generated_images" / name)
        return frozenset(identities)


class RecordFinalizerAdapter:
    """Supply Task 8's reconciliation boundary and existing finalizer transaction."""

    def __init__(
            self, *, base: object, app_token: str, table_id: str,
            output_field_id: str, detail_field_id: str,
            clock: Callable[[], str] | None = None, qc_mode: str = "automatic",
    ) -> None:
        if qc_mode not in {"automatic", "shadow"}:
            raise ValueError("qc_mode must be automatic or shadow")
        self.qc_mode = qc_mode
        self._base = base
        self._app_token = app_token
        self._table_id = table_id
        self._output_field_id = output_field_id
        self._detail_field_id = detail_field_id
        self._clock = clock or _utc_now
        self._target = finalize_target.TargetFinalizer(
            base=base,
            app_token=app_token,
            table_id=table_id,
            output_field_id=output_field_id,
            detail_field_id=detail_field_id,
            clock=self._clock,
        )

    def reconcile_record(
            self, context: RecordContext, state_file: Path,
            target_indices: tuple[int, ...],
    ) -> None:
        state_path = Path(state_file)
        if (not isinstance(context, RecordContext)
                or state_path.name != "manifest.json"
                or state_path.parent.resolve() != Path(context.task_dir).resolve()
                or not isinstance(target_indices, tuple)):
            raise finalize_target.FinalizeError("record reconciliation scope is invalid")
        state = task_state.load_state(state_file)
        if state["record_id"] != context.record_id:
            raise finalize_target.FinalizeError("record reconciliation scope is invalid")
        try:
            response = self._base.get_record(
                app_token=self._app_token,
                table_id=self._table_id,
                record_id=context.record_id,
                field_ids=[self._output_field_id, self._detail_field_id],
            )
            fields = finalize_target._record_fields(response, context.record_id)
            outputs = finalize_target.TargetFinalizer._outputs(fields)
        except Exception:
            raise finalize_target.FinalizeError("Base reconciliation failed") from None
        changed = False
        for index in target_indices:
            if (not isinstance(index, int) or isinstance(index, bool)
                    or not 0 <= index < len(state["target_tokens"])):
                raise finalize_target.FinalizeError(
                    "record reconciliation target is invalid",
                )
            token = state["target_tokens"][index]
            status = state["targets"][token]["status"]
            if status not in {"accepted-local", "success"}:
                continue
            try:
                mapping = task_state.reconcile_target_output(
                    state, target_index=index, outputs=outputs,
                    updated_at=self._timestamp(),
                )
            except Exception:
                raise finalize_target.FinalizeError(
                    "Base reconciliation failed",
                ) from None
            if status == "success" and mapping is None:
                raise finalize_target.FinalizeError(
                    "successful attachment is absent from Base readback",
                )
            changed = changed or status == "accepted-local" and mapping is not None
        if changed:
            task_state.save_state(state_file, state)

    def finalize(
            self, request: finalize_target.FinalizeRequest,
    ) -> finalize_target.FinalizeResult:
        return self._target.finalize(request)

    def __call__(
            self, request: finalize_target.FinalizeRequest,
    ) -> finalize_target.FinalizeResult:
        return self.finalize(request)

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, str) or not value:
            raise finalize_target.FinalizeError("record reconciliation clock is invalid")
        return value


class ProductionRecordServicesFactory:
    """Build production record services from the preflighted shared adapters."""

    def __init__(
            self, *, scope: TableScope, schema: TableSchema,
            events_factory: Callable[[RecordContext], object],
            clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(scope, TableScope):
            raise TypeError("table scope is required")
        _validate_schema(schema)
        if not callable(events_factory):
            raise TypeError("record event factory must be callable")
        self._scope = scope
        self._schema = schema
        self._events_factory = events_factory
        self._clock = clock or _utc_now

    def __call__(
            self, context: RecordContext, generator: object, qc: object,
            base: object, stop_signal: object, qc_mode: str,
    ) -> RecordServices:
        events = self._events_factory(context)
        if not callable(getattr(events, "append", None)):
            raise TableSchedulerError("record event service has no append boundary")
        finalizer = RecordFinalizerAdapter(
            base=base,
            app_token=self._scope.app_token,
            table_id=self._scope.table_id,
            output_field_id=self._schema.field("输出图").field_id,
            detail_field_id=self._schema.field("处理明细").field_id,
            clock=self._clock,
            qc_mode=qc_mode,
        )
        return RecordServices(
            generator=generator,
            qc=qc,
            finalizer=finalizer,
            events=events,
            stop_signal=stop_signal,
            clock=self._clock,
            qc_mode=qc_mode,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_REQUIRED_SCHEMA = {
    "原图": "attachment",
    "爆款图": "attachment",
    "输出图": "attachment",
    "任务状态": "single_select",
    "处理明细": "text",
}


def _validate_schema(schema: TableSchema) -> None:
    if not isinstance(schema, TableSchema):
        raise PreflightError("Base schema is invalid")
    for name, kind in _REQUIRED_SCHEMA.items():
        field = schema.field(name)
        if field.kind != kind:
            raise PreflightError(f"required Base field has wrong type: {name}")
    if schema.field("任务状态").options != ("未开始", "成功", "失败"):
        raise PreflightError("task status options do not match the required schema")


def _generation_paid_checkpoint(request: object) -> None:
    if not isinstance(request, GenerationRequest):
        return
    state_file = Path(request.context.task_dir).resolve() / "manifest.json"
    try:
        state = task_state.load_state(state_file)
        token = state["target_tokens"][request.target_index]
        target = state["targets"][token]
        active = target["attempt_history"][-1]
        expected_plan = json.loads(prompt_builder.serialize_plan(request.plan))
        valid = (
            state["record_id"] == request.context.record_id
            and token == request.target_token
            and state["record_error"] is None
            and state["current_target"] == token
            and target["status"] == "running"
            and target["target_plan"] == expected_plan
            and active["attempt"] == request.attempt
            and active["artifact_name"] == request.artifact_name
            and Path(request.output_path).resolve().name == request.artifact_name
        )
    except Exception:
        valid = False
    if not valid:
        raise PaidCallStopped("durable state changed before paid generation")


def _qc_paid_checkpoint(request: object) -> None:
    if not isinstance(request, QCRequest):
        return
    state_file = Path(request.context.task_dir).resolve() / "manifest.json"
    try:
        state = task_state.load_state(state_file)
        token = state["target_tokens"][request.target_index]
        target = state["targets"][token]
        expected_plan = json.loads(prompt_builder.serialize_plan(request.plan))
        cycle = task_state.current_attempt_cycle(state, request.target_index)
        durable = next(
            item for item in cycle
            if (item["artifact_name"] == request.candidate.name
                and item["attempt"] == request.attempt)
        )
        valid = (
            state["record_id"] == request.context.record_id
            and token == request.target_token
            and state["record_error"] is None
            and state["current_target"] == token
            and target["status"] == "running"
            and target["target_plan"] == expected_plan
            and request.candidate.name == durable["artifact_name"]
            and request.candidate.resolve().name == durable["artifact_name"]
            and request.candidate.is_file()
            and not request.candidate.is_symlink()
            and _sha256(request.candidate) == request.candidate_sha256
        )
    except Exception:
        valid = False
    if not valid:
        raise PaidCallStopped("durable state changed before paid QC")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class TableScheduler:
    """Own one process-local stable queue; independent invocations are unsupported."""

    def __init__(
            self, runtime: TableRuntime, *, limits: ServiceLimits | None = None,
    ) -> None:
        if not isinstance(runtime, TableRuntime):
            raise TypeError("table runtime is required")
        self._runtime = runtime
        self._injected_limits = limits
        self._active: set[str] = set()
        self._active_lock = threading.Lock()

    @property
    def active_record_ids(self) -> frozenset[str]:
        with self._active_lock:
            return frozenset(self._active)

    def run(self, config: TableConfig) -> TableResult:
        if not isinstance(config, TableConfig):
            raise TypeError("table configuration is required")
        limits = (
            replace(ServiceLimits(), record_workers=config.record_concurrency)
            if self._injected_limits is None else replace(
                self._injected_limits,
                record_workers=min(
                    config.record_concurrency,
                    self._injected_limits.record_workers,
                ),
            )
        )
        stop_signal = GlobalStop()
        bounded_base = BoundedBase(
            self._runtime.base,
            read_semaphore=threading.BoundedSemaphore(limits.lark_reads),
            write_semaphore=threading.BoundedSemaphore(limits.lark_writes),
        )
        bounded_generator = BoundedGenerator(
            self._runtime.generator,
            threading.BoundedSemaphore(limits.doubao_requests),
            stop_signal,
        )
        bounded_qc = BoundedQC(
            self._runtime.qc,
            threading.BoundedSemaphore(limits.qc_requests),
            stop_signal,
        )

        scope, schema, contexts = self._preflight(config, bounded_base)
        selected = len(contexts)
        if selected == 0:
            return TableResult(0, 0, 0, 0)

        succeeded = failed = stopped = 0
        next_index = 0
        futures: dict[Future[RecordResult], str] = {}
        worker_count = limits.record_workers
        with ThreadPoolExecutor(
                max_workers=worker_count, thread_name_prefix="outfit-record",
        ) as executor:
            while futures or next_index < selected:
                while (not stop_signal.is_set()
                       and len(futures) < worker_count
                       and next_index < selected):
                    context = contexts[next_index]
                    next_index += 1
                    self._claim(context.record_id)
                    future = executor.submit(
                        self._run_one, context, scope, schema, config.qc_mode,
                        bounded_generator, bounded_qc, bounded_base, stop_signal,
                    )
                    futures[future] = context.record_id
                if not futures:
                    break
                done, _pending = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    record_id = futures.pop(future)
                    try:
                        result = future.result()
                    except Exception:
                        result = RecordResult(record_id, "failed", 0)
                    if result.status == "success":
                        succeeded += 1
                    elif result.status == "stopped":
                        stop_signal.set()
                        stopped += 1
                    else:
                        failed += 1
            stopped += selected - next_index
        return TableResult(selected, succeeded, failed, stopped)

    def _preflight(
            self, config: TableConfig, base: BoundedBase,
    ) -> tuple[TableScope, TableSchema, tuple[RecordContext, ...]]:
        adapter = self._runtime.adapter
        try:
            scope = adapter.resolve_base(config.base_url, base)
            if not isinstance(scope, TableScope):
                raise PreflightError("Base resolver did not return exact table scope")
            adapter.authenticate(scope, base)
            schema = adapter.load_schema(scope, base)
            _validate_schema(schema)
            records = tuple(adapter.list_records(
                scope, schema,
                retry_failed=config.retry_failed,
                qc_mode=config.qc_mode,
                base=base,
            ))
        except PreflightError:
            raise
        except Exception:
            raise PreflightError("global table preflight failed") from None
        if not all(isinstance(context, RecordContext) for context in records):
            raise PreflightError("selected record materialization is invalid")
        record_ids = tuple(context.record_id for context in records)
        if len(set(record_ids)) != len(record_ids):
            raise PreflightError("selected record materialization contains duplicates")
        return scope, schema, records

    def _claim(self, record_id: str) -> None:
        with self._active_lock:
            if record_id in self._active:
                raise TableSchedulerError("record is already active in this process")
            self._active.add(record_id)

    def _run_one(
            self, context: RecordContext, scope: TableScope,
            schema: TableSchema, qc_mode: str,
            generator: BoundedGenerator, qc: BoundedQC, base: BoundedBase,
            stop_signal: GlobalStop,
    ) -> RecordResult:
        try:
            if stop_signal.is_set():
                return RecordResult(context.record_id, "stopped", 0)
            record_scope = self._runtime.adapter.record_base_scope(
                context, scope, schema,
            )
            if (not isinstance(record_scope, RecordBaseScope)
                    or record_scope.table != scope
                    or record_scope.record_id != context.record_id
                    or record_scope.output_field_id
                    != schema.field("输出图").field_id
                    or record_scope.status_field_id
                    != schema.field("任务状态").field_id
                    or record_scope.detail_field_id
                    != schema.field("处理明细").field_id
                    or record_scope.payload_root
                    != (Path(context.task_dir).resolve()
                        / "generated_images")):
                raise TableSchedulerError("record Base scope escaped global preflight")
            scoped_base = base.scoped(record_scope)
            services = self._runtime.adapter.record_services(
                context, generator, qc, scoped_base, stop_signal, qc_mode,
            )
            if (not isinstance(services, RecordServices)
                    or services.qc_mode != qc_mode):
                raise TableSchedulerError("record service factory returned invalid services")
            if stop_signal.is_set():
                return RecordResult(context.record_id, "stopped", 0)
            result = self._runtime.worker(context, services)
            if (not isinstance(result, RecordResult)
                    or result.record_id != context.record_id
                    or result.status not in {"success", "failed", "stopped"}):
                raise TableSchedulerError("record worker returned an invalid result")
            return result
        finally:
            with self._active_lock:
                self._active.discard(context.record_id)


def run_table(
        config: TableConfig, runtime: TableRuntime, *,
        limits: ServiceLimits | None = None,
) -> TableResult:
    """Run one process-local table invocation with a stable preflighted queue."""
    return TableScheduler(runtime, limits=limits).run(config)


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_table",
        description="Process one exact Base table with bounded record concurrency.",
    )
    parser.add_argument("base_url")
    parser.add_argument(
        "--record-concurrency", type=_positive_integer, default=2,
    )
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--qc-mode", choices=("automatic", "shadow"), default="automatic",
    )
    return parser


def _missing_runtime(_config: TableConfig) -> TableResult:
    raise TableSchedulerError("production runtime must be supplied by the skill entry point")


def main(
        argv: list[str] | None = None, *,
        execute: Callable[[TableConfig], TableResult] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    config = TableConfig(
        args.base_url,
        record_concurrency=args.record_concurrency,
        retry_failed=args.retry_failed,
        qc_mode=args.qc_mode,
    )
    try:
        result = (execute or _missing_runtime)(config)
        if not isinstance(result, TableResult):
            raise TableSchedulerError("table execution returned an invalid result")
    except (OSError, TableSchedulerError, TypeError, ValueError) as error:
        print(f"run-table error: {error}", file=__import__("sys").stderr)
        return 1
    print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
