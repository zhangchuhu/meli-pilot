#!/usr/bin/env python3
"""Resumable orchestration for one record with strictly serial targets."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from scripts import image_qc, prompt_builder, task_state, vision_qc
    from scripts.ark_vision_qc import QCReviewResult
    from scripts.finalize_target import FinalizeRequest
except ImportError:  # pragma: no cover - direct script-directory execution
    import image_qc  # type: ignore[no-redef]
    import prompt_builder  # type: ignore[no-redef]
    import task_state  # type: ignore[no-redef]
    import vision_qc  # type: ignore[no-redef]
    from ark_vision_qc import QCReviewResult  # type: ignore[no-redef]
    from finalize_target import FinalizeRequest  # type: ignore[no-redef]


class RecordWorkerError(RuntimeError):
    """Raised when the injected record boundary is incomplete or inconsistent."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RecordContext:
    task_dir: Path
    record_id: str
    target_indices: tuple[int, ...]


@dataclass(frozen=True)
class RecordServices:
    generator: object
    qc: object
    finalizer: object
    events: object
    stop_signal: object
    clock: Callable[[], str] = _utc_now


@dataclass(frozen=True)
class RecordResult:
    record_id: str
    status: str
    accepted_targets: int


@dataclass(frozen=True)
class GenerationRequest:
    context: RecordContext
    target_index: int
    target_token: str
    attempt: int
    artifact_name: str
    output_path: Path
    prompt: str
    reference_tokens: tuple[str, ...]
    plan: prompt_builder.TargetPlan


@dataclass(frozen=True)
class QCRequest:
    context: RecordContext
    target_index: int
    target_token: str
    attempt: int
    candidate: Path
    candidate_sha256: str
    plan: prompt_builder.TargetPlan


def run_record(context: RecordContext, services: RecordServices) -> RecordResult:
    """Run or resume exactly one record, never overlapping its target attempts."""
    root, state_file = _validate_context(context, services)
    _event(services, "record_started", record_id=context.record_id, status="running")
    try:
        _base_reconcile(context, state_file, services)
        state = _load_bound_state(state_file, context)
    except Exception:
        try:
            failed_state = task_state.load_state(state_file)
            if failed_state["record_id"] == context.record_id:
                _record_error(
                    state_file, failed_state, services, code="external-call",
                    message="Base reconciliation failed",
                )
        except Exception:
            pass
        _set_stop(services.stop_signal)
        return _result_from_file(
            context, state_file, services, status="failed",
        )

    if state["record_error"] is not None:
        return _finish_result(context, state, services, "failed")

    drain_status = _drain_accepted_local(
        context, state_file, services,
    )
    if drain_status is not None:
        state = task_state.load_state(state_file)
        return _finish_result(context, state, services, drain_status)

    state = task_state.load_state(state_file)
    if _all_success(state, context.target_indices):
        return _finish_result(context, state, services, "success")
    if state["record_error"] is not None:
        return _finish_result(context, state, services, "failed")

    plans: dict[int, prompt_builder.TargetPlan] = {}
    state = task_state.load_state(state_file)
    active = state["current_target"]
    if active is not None:
        active_index = state["target_tokens"].index(active)
        if active_index not in context.target_indices:
            raise RecordWorkerError("active target is outside the requested record scope")
        try:
            plans[active_index] = _resolve_target_plan(
                context, state_file, active_index, services,
            )
        except Exception:
            return _planning_failure(context, state_file, services)
        status = _process_active_candidate(
            context, state_file, active_index, plans[active_index], services,
        )
        if status in {"failed", "stopped"}:
            return _finish_result(
                context, task_state.load_state(state_file), services, status,
            )

    for target_index in context.target_indices:
        while True:
            state = task_state.load_state(state_file)
            if state["record_error"] is not None:
                return _finish_result(context, state, services, "failed")
            target_token = state["target_tokens"][target_index]
            target = state["targets"][target_token]
            if target["status"] == "success":
                break
            if target["status"] == "accepted-local":
                status = _finalize_accepted_local(
                    context, state_file, target_index, services,
                )
                if status is not None:
                    return _finish_result(
                        context, task_state.load_state(state_file), services, status,
                    )
                continue
            if target["status"] == "failed":
                return _finish_result(context, state, services, "failed")
            if target_index not in plans:
                try:
                    plans[target_index] = _resolve_target_plan(
                        context, state_file, target_index, services,
                    )
                except Exception:
                    return _planning_failure(context, state_file, services)
            if target["status"] == "running":
                status = _process_active_candidate(
                    context, state_file, target_index, plans[target_index], services,
                )
            elif target["status"] == "pending":
                status = _start_generation(
                    context, state_file, target_index, plans[target_index], services,
                )
            else:  # validated state makes this defensive only.
                raise RecordWorkerError("target has an unsupported durable status")
            if status in {"failed", "stopped"}:
                return _finish_result(
                    context, task_state.load_state(state_file), services, status,
                )

    return _finish_result(
        context, task_state.load_state(state_file), services, "success",
    )


def _validate_context(
        context: RecordContext, services: RecordServices,
) -> tuple[Path, Path]:
    if not isinstance(context, RecordContext) or not isinstance(services, RecordServices):
        raise RecordWorkerError("record context and services are required")
    root = Path(context.task_dir).resolve()
    if (not root.is_dir()
            or not isinstance(context.record_id, str) or not context.record_id
            or not isinstance(context.target_indices, tuple)
            or not all(isinstance(value, int) and not isinstance(value, bool)
                       and value >= 0 for value in context.target_indices)
            or tuple(sorted(set(context.target_indices))) != context.target_indices):
        raise RecordWorkerError("record context is invalid")
    if not callable(services.clock):
        raise RecordWorkerError("record clock is invalid")
    state_file = root / "manifest.json"
    if not state_file.is_file():
        raise RecordWorkerError("record manifest is missing")
    return root, state_file


def _base_reconcile(
        context: RecordContext, state_file: Path, services: RecordServices,
) -> None:
    boundary = getattr(services.finalizer, "reconcile_record", None)
    if boundary is None:
        boundary = getattr(services.finalizer, "reconcile", None)
    if not callable(boundary):
        raise RecordWorkerError("finalizer has no Base reconciliation boundary")
    result = boundary(context, state_file, context.target_indices)
    if isinstance(result, dict):
        task_state.save_state(state_file, result)
    elif result is not None:
        raise RecordWorkerError("Base reconciliation returned an invalid checkpoint")


def _load_bound_state(state_file: Path, context: RecordContext) -> dict[str, Any]:
    state = task_state.load_state(state_file)
    if state["record_id"] != context.record_id:
        raise RecordWorkerError("record context does not match its manifest")
    if any(index >= len(state["target_tokens"]) for index in context.target_indices):
        raise RecordWorkerError("target index is outside the current attachment order")
    return state


def _resolve_target_plan(
        context: RecordContext, state_file: Path, target_index: int,
        services: RecordServices,
) -> prompt_builder.TargetPlan:
    state = task_state.load_state(state_file)
    target_token = state["target_tokens"][target_index]
    target = state["targets"][target_token]
    persisted = target["target_plan"]
    if persisted is not None:
        plan = prompt_builder.deserialize_plan(persisted)
    else:
        planner = getattr(services.generator, "plan_target", None)
        if not callable(planner):
            raise RecordWorkerError("generator has no target-plan boundary")
        plan = planner(context, target_index, target_token)
        if not isinstance(plan, prompt_builder.TargetPlan):
            raise RecordWorkerError("target planner returned an invalid plan")
        serialized = json.loads(prompt_builder.serialize_plan(plan))
        task_state.record_target_plan(state, target_index, serialized)
        task_state.save_state(state_file, state)
    _event(
        services, "target_started", record_id=context.record_id,
        target_id=target_token, run_id=state["run_id"], status="running",
        reference_count=len(plan.reference_tokens),
    )
    return plan


def _planning_failure(
        context: RecordContext, state_file: Path, services: RecordServices,
) -> RecordResult:
    state = task_state.load_state(state_file)
    _record_error(
        state_file, state, services, code="record-data",
        message="target planning failed",
    )
    return _finish_result(
        context, task_state.load_state(state_file), services, "failed",
    )


def _drain_accepted_local(
        context: RecordContext, state_file: Path, services: RecordServices,
) -> str | None:
    for target_index in context.target_indices:
        state = task_state.load_state(state_file)
        if state["record_error"] is not None:
            return "failed"
        token = state["target_tokens"][target_index]
        if state["targets"][token]["status"] != "accepted-local":
            continue
        status = _finalize_accepted_local(
            context, state_file, target_index, services,
        )
        if status is not None:
            return status
    return None


def _finalize_accepted_local(
        context: RecordContext, state_file: Path, target_index: int,
        services: RecordServices,
) -> str | None:
    state = task_state.load_state(state_file)
    token = state["target_tokens"][target_index]
    target = state["targets"][token]
    accepted = target["local_acceptance"]
    history = next(
        (entry for entry in target["attempt_history"]
         if entry["run_id"] == accepted["run_id"]
         and entry["artifact_name"] == accepted["artifact_name"]),
        None,
    )
    if history is None:
        return _terminal_external_call(
            state_file, state, services, "accepted candidate checkpoint is missing",
        )
    candidate = _artifact_path(context, history, services)
    try:
        digest = _validate_candidate(candidate, history, target)
    except (OSError, image_qc.ImageQCError, RecordWorkerError):
        return _terminal_external_call(
            state_file, state, services, "accepted candidate artifact is invalid",
        )
    return _finalize_candidate(
        context, state_file, target_index, candidate, digest, services,
    )


def _start_generation(
        context: RecordContext, state_file: Path, target_index: int,
        plan: prompt_builder.TargetPlan, services: RecordServices,
) -> str:
    if _stop_requested(services.stop_signal):
        state = task_state.load_state(state_file)
        _event(
            services, "stop_observed", record_id=context.record_id,
            target_id=state["target_tokens"][target_index], status="stopped",
        )
        return "stopped"

    state = task_state.load_state(state_file)
    token = state["target_tokens"][target_index]
    target = state["targets"][token]
    if (state["record_error"] is not None or state["current_target"] is not None
            or target["status"] != "pending"):
        raise RecordWorkerError("durable target changed before paid generation")
    next_attempt = target["attempts"] + 1
    if next_attempt > task_state.MAX_ATTEMPTS:
        return _terminal_external_call(
            state_file, state, services, "target exhausted its paid attempt budget",
        )
    prompt = _prompt_for_attempt(
        context, state, target_index, plan, services,
    )
    model = getattr(services.generator, "model", None)
    if not isinstance(model, str) or not model:
        raise RecordWorkerError("generator model identity is missing")
    task_state.begin_attempt(
        state, target_token=token, classification=plan.classification,
        reference_tokens=plan.reference_tokens, prompt=prompt, model=model,
        updated_at=_timestamp(services),
    )
    task_state.save_state(state_file, state)
    active = state["targets"][token]["attempt_history"][-1]
    candidate = _artifact_path(context, active, services)
    if candidate.exists() or candidate.is_symlink():
        raise RecordWorkerError("new immutable attempt path already exists")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    request = GenerationRequest(
        context=context, target_index=target_index, target_token=token,
        attempt=active["attempt"], artifact_name=active["artifact_name"],
        output_path=candidate, prompt=prompt,
        reference_tokens=tuple(plan.reference_tokens), plan=plan,
    )

    confirmed = task_state.load_state(state_file)
    confirmed_target = confirmed["targets"][token]
    confirmed_active = confirmed_target["attempt_history"][-1]
    if (confirmed["current_target"] != token
            or confirmed_target["status"] != "running"
            or confirmed_active["artifact_name"] != active["artifact_name"]):
        raise RecordWorkerError("durable attempt changed before paid generation")
    if _stop_requested(services.stop_signal):
        _event(
            services, "stop_observed", record_id=context.record_id,
            target_id=token, attempt=active["attempt"], status="stopped",
        )
        return "stopped"

    _event(
        services, "generation_started", record_id=context.record_id,
        target_id=token, run_id=active["run_id"], attempt=active["attempt"],
        status="running", phase="generation",
    )
    generator = getattr(services.generator, "generate", None)
    if not callable(generator):
        raise RecordWorkerError("generator has no paid-call boundary")
    try:
        returned = Path(generator(request)).resolve()
        if returned != candidate.resolve():
            raise RecordWorkerError("generator returned the wrong attempt artifact")
        digest = _validate_candidate(candidate, active, confirmed_target)
    except Exception:
        _event(
            services, "generation_finished", record_id=context.record_id,
            target_id=token, run_id=active["run_id"], attempt=active["attempt"],
            status="failed", phase="generation", error_category="invalid-artifact",
        )
        return _handle_unusable_active(
            context, state_file, target_index, plan, services,
            "generation did not produce a complete decodable artifact",
        )
    _event(
        services, "generation_finished", record_id=context.record_id,
        target_id=token, run_id=active["run_id"], attempt=active["attempt"],
        status="success", phase="generation", candidate_digest=digest,
    )
    return _apply_candidate_qc(
        context, state_file, target_index, plan, candidate, digest, services,
    )


def _process_active_candidate(
        context: RecordContext, state_file: Path, target_index: int,
        plan: prompt_builder.TargetPlan, services: RecordServices,
) -> str:
    state = task_state.load_state(state_file)
    token = state["target_tokens"][target_index]
    target = state["targets"][token]
    if target["status"] != "running" or state["current_target"] != token:
        raise RecordWorkerError("target has no active artifact checkpoint")
    active = target["attempt_history"][-1]
    candidate = _artifact_path(context, active, services)
    try:
        digest = _validate_candidate(candidate, active, target)
    except (OSError, image_qc.ImageQCError, RecordWorkerError):
        return _handle_unusable_active(
            context, state_file, target_index, plan, services,
            "initiated attempt artifact is missing or invalid",
        )
    return _apply_candidate_qc(
        context, state_file, target_index, plan, candidate, digest, services,
    )


def _handle_unusable_active(
        context: RecordContext, state_file: Path, target_index: int,
        plan: prompt_builder.TargetPlan, services: RecordServices, message: str,
) -> str:
    state = task_state.load_state(state_file)
    token = state["target_tokens"][target_index]
    target = state["targets"][token]
    if target["attempts"] >= task_state.MAX_ATTEMPTS:
        return _select_after_third_attempt(
            context, state_file, target_index, plan, services,
        )
    task_state.record_failure(
        state, target_token=token, error=message,
        updated_at=_timestamp(services),
    )
    task_state.save_state(state_file, state)
    return "retry"


def _apply_candidate_qc(
        context: RecordContext, state_file: Path, target_index: int,
        plan: prompt_builder.TargetPlan, candidate: Path, digest: str,
        services: RecordServices,
) -> str:
    state = task_state.load_state(state_file)
    token = state["target_tokens"][target_index]
    target = state["targets"][token]
    active = target["attempt_history"][-1]
    persisted = _persisted_report(target, active, digest)
    if persisted is None:
        report = _request_qc(
            context, state_file, target_index, plan, active, candidate, digest,
            services,
        )
        if report is None:
            return "stopped"
    else:
        report = persisted

    if active["attempt"] >= task_state.MAX_ATTEMPTS:
        return _select_after_third_attempt(
            context, state_file, target_index, plan, services,
        )
    if vision_qc.early_accept(
            report, infographic=plan.classification == "infographic",
    ):
        state = task_state.load_state(state_file)
        task_state.record_selection_reason(state, target_index, {
            "artifact_name": active["artifact_name"],
            "artifact_sha256": digest,
            "attempt": active["attempt"],
            "reason": "early full-QC acceptance",
        })
        task_state.save_state(state_file, state)
        _event(
            services, "qc_finished", record_id=context.record_id,
            target_id=token, run_id=active["run_id"], attempt=active["attempt"],
            status="early_accept", phase="qc", candidate_digest=digest,
            scores=_event_scores(report),
        )
        return _finalize_candidate(
            context, state_file, target_index, candidate, digest, services,
        ) or "completed"

    defect = vision_qc.correction_for(report)
    _event(
        services, "qc_finished", record_id=context.record_id,
        target_id=token, run_id=active["run_id"], attempt=active["attempt"],
        status="reject", phase="qc", candidate_digest=digest,
        scores=_event_scores(report),
        **({"defect": defect.value} if defect is not None else {}),
    )
    state = task_state.load_state(state_file)
    task_state.record_failure(
        state, target_token=token,
        error=(
            "automatic QC rejected candidate"
            if defect is None else f"automatic QC rejected candidate: {defect.value}"
        ),
        updated_at=_timestamp(services),
    )
    task_state.save_state(state_file, state)
    _event(
        services, "retry_decided", record_id=context.record_id,
        target_id=token, run_id=active["run_id"], attempt=active["attempt"],
        status="retry", **({"defect": defect.value} if defect is not None else {}),
    )
    return "retry"


def _request_qc(
        context: RecordContext, state_file: Path, target_index: int,
        plan: prompt_builder.TargetPlan, history: dict[str, Any],
        candidate: Path, digest: str, services: RecordServices,
) -> vision_qc.QCReport | None:
    checkpoint = _qc_checkpoint(
        context, state_file, target_index, plan, history, candidate, digest,
        services,
    )
    if checkpoint is None:
        return None
    token = checkpoint["target_tokens"][target_index]
    _event(
        services, "qc_started", record_id=context.record_id, target_id=token,
        run_id=history["run_id"], attempt=history["attempt"], status="running",
        phase="qc", candidate_digest=digest,
    )
    reviewer = getattr(services.qc, "review", None)
    if not callable(reviewer):
        raise RecordWorkerError("QC service has no review boundary")
    request = QCRequest(
        context=context, target_index=target_index, target_token=token,
        attempt=history["attempt"], candidate=candidate,
        candidate_sha256=digest, plan=plan,
    )
    immediate = _qc_checkpoint(
        context, state_file, target_index, plan, history, candidate, digest,
        services,
    )
    if immediate is None:
        _event(
            services, "qc_finished", record_id=context.record_id,
            target_id=token, run_id=history["run_id"], attempt=history["attempt"],
            status="stopped", phase="qc", candidate_digest=digest,
            error_category="stopped",
        )
        return None
    if immediate != checkpoint:
        _set_stop(services.stop_signal)
        _event(
            services, "stop_observed", record_id=context.record_id,
            target_id=token, attempt=history["attempt"], status="stopped",
            error_category="stopped",
        )
        return None
    try:
        value = reviewer(request)
        report = value.report if isinstance(value, QCReviewResult) else value
        if (not isinstance(report, vision_qc.QCReport)
                or report.candidate != history["artifact_name"]):
            raise RecordWorkerError("QC report does not identify the candidate")
    except Exception:
        state = task_state.load_state(state_file)
        task_state.record_qc_failure(
            state, target_token=token, error="automatic QC service unavailable",
            updated_at=_timestamp(services),
        )
        task_state.save_state(state_file, state)
        _event(
            services, "qc_finished", record_id=context.record_id,
            target_id=token, run_id=history["run_id"], attempt=history["attempt"],
            status="failed", phase="qc", candidate_digest=digest,
            error_category="qc",
        )
        return None
    state = task_state.load_state(state_file)
    task_state.record_qc_report(
        state, target_index, _report_payload(history, digest, report),
    )
    task_state.save_state(state_file, state)
    return report


def _qc_checkpoint(
        context: RecordContext, state_file: Path, target_index: int,
        plan: prompt_builder.TargetPlan, history: dict[str, Any],
        candidate: Path, digest: str, services: RecordServices,
) -> dict[str, Any] | None:
    """Stop unless durable state still owns this exact current-cycle candidate."""
    if _stop_requested(services.stop_signal):
        _event(
            services, "stop_observed", record_id=context.record_id,
            attempt=history.get("attempt"), status="stopped",
            error_category="stopped",
        )
        return None
    try:
        state = _load_bound_state(state_file, context)
        token = state["target_tokens"][target_index]
        target = state["targets"][token]
        canonical_plan = json.loads(prompt_builder.serialize_plan(plan))
        if (state["record_error"] is not None
                or state["current_target"] != token
                or target["status"] != "running"
                or target["target_plan"] != canonical_plan):
            raise RecordWorkerError("QC candidate is no longer active")
        cycle = task_state.current_attempt_cycle(state, target_index)
        durable = next(
            entry for entry in cycle
            if (
                entry["run_id"], entry["artifact_name"], entry["attempt"],
                entry["artifact_ordinal"], entry["prompt_sha256"],
            ) == (
                history.get("run_id"), history.get("artifact_name"),
                history.get("attempt"), history.get("artifact_ordinal"),
                history.get("prompt_sha256"),
            )
        )
        if (_artifact_path(context, durable, services) != candidate.resolve()
                or _validate_candidate(candidate, durable, target) != digest):
            raise RecordWorkerError("QC candidate identity changed")
    except (
            IndexError, KeyError, OSError, StopIteration, TypeError,
            image_qc.ImageQCError, prompt_builder.PromptPlanError,
            task_state.TaskStateError, RecordWorkerError,
    ):
        _set_stop(services.stop_signal)
        event_fields: dict[str, Any] = {
            "record_id": context.record_id,
            "attempt": history.get("attempt"),
            "status": "stopped",
            "error_category": "stopped",
        }
        if ("state" in locals()
                and target_index < len(state.get("target_tokens", []))):
            event_fields["target_id"] = state["target_tokens"][target_index]
        _event(services, "stop_observed", **event_fields)
        return None
    if _stop_requested(services.stop_signal):
        _event(
            services, "stop_observed", record_id=context.record_id,
            target_id=token, attempt=history["attempt"], status="stopped",
            error_category="stopped",
        )
        return None
    return state


def _select_after_third_attempt(
        context: RecordContext, state_file: Path, target_index: int,
        plan: prompt_builder.TargetPlan, services: RecordServices,
) -> str:
    state = task_state.load_state(state_file)
    token = state["target_tokens"][target_index]
    target = state["targets"][token]
    if target["attempts"] < task_state.MAX_ATTEMPTS:
        raise RecordWorkerError("final selection started before the paid budget was used")
    reports: list[vision_qc.QCReport] = []
    attempts: dict[str, int] = {}
    paths: dict[str, Path] = {}
    digests: dict[str, str] = {}
    for history in task_state.current_attempt_cycle(state, target_index):
        candidate = _artifact_path(context, history, services)
        try:
            digest = _validate_candidate(candidate, history, target)
        except (OSError, image_qc.ImageQCError, RecordWorkerError):
            continue
        report = _persisted_report(target, history, digest)
        if report is None:
            report = _request_qc(
                context, state_file, target_index, plan, history, candidate,
                digest, services,
            )
            if report is None:
                return "stopped"
            state = task_state.load_state(state_file)
            target = state["targets"][token]
        reports.append(report)
        attempts[report.candidate] = history["attempt"]
        paths[report.candidate] = candidate
        digests[report.candidate] = digest
    if not reports:
        return _terminal_external_call(
            state_file, task_state.load_state(state_file), services,
            "third attempt has no complete decodable current-cycle candidate",
        )
    selected = vision_qc.select_best(reports, attempts)
    selected_attempt = attempts[selected.candidate]
    selected_digest = digests[selected.candidate]
    state = task_state.load_state(state_file)
    task_state.record_selection_reason(state, target_index, {
        "artifact_name": selected.candidate,
        "artifact_sha256": selected_digest,
        "attempt": selected_attempt,
        "reason": "third-attempt garment-first lexicographic selection",
        "scores": _event_scores(selected),
    })
    task_state.save_state(state_file, state)
    _event(
        services, "third_attempt_selected", record_id=context.record_id,
        target_id=token, run_id=state["run_id"], attempt=selected_attempt,
        status="selected", candidate_digest=selected_digest,
        scores=_event_scores(selected),
    )
    return _finalize_candidate(
        context, state_file, target_index, paths[selected.candidate],
        selected_digest, services,
    ) or "completed"


def _finalize_candidate(
        context: RecordContext, state_file: Path, target_index: int,
        candidate: Path, digest: str, services: RecordServices,
) -> str | None:
    state = task_state.load_state(state_file)
    token = state["target_tokens"][target_index]
    target = state["targets"][token]
    attempt = next(
        entry["attempt"] for entry in target["attempt_history"]
        if entry["artifact_name"] == candidate.name
    )
    _event(
        services, "finalize_started", record_id=context.record_id,
        target_id=token, run_id=state["run_id"], attempt=attempt,
        status="running", phase="finalize", candidate_digest=digest,
    )
    boundary = getattr(services.finalizer, "finalize", None)
    if not callable(boundary) and callable(services.finalizer):
        boundary = services.finalizer
    if not callable(boundary):
        raise RecordWorkerError("finalizer has no target commit boundary")
    request = FinalizeRequest(
        task_dir=candidate.parent, state_file=state_file,
        record_id=context.record_id, target_index=target_index,
        candidate=candidate, candidate_sha256=digest,
    )
    try:
        boundary(request)
    except Exception:
        _event(
            services, "finalize_finished", record_id=context.record_id,
            target_id=token, run_id=state["run_id"], attempt=attempt,
            status="failed", phase="finalize", candidate_digest=digest,
            error_category="lark",
        )
        state = task_state.load_state(state_file)
        _record_error(
            state_file, state, services, code="external-call",
            message="target finalization failed",
        )
        _set_stop(services.stop_signal)
        return "failed"
    _event(
        services, "finalize_finished", record_id=context.record_id,
        target_id=token, run_id=state["run_id"], attempt=attempt,
        status="success", phase="finalize", candidate_digest=digest,
    )
    return None


def _prompt_for_attempt(
        context: RecordContext, state: dict[str, Any], target_index: int,
        plan: prompt_builder.TargetPlan, services: RecordServices,
) -> str:
    del context, services
    token = state["target_tokens"][target_index]
    target = state["targets"][token]
    attempt = target["attempts"] + 1
    if attempt == 1:
        return prompt_builder.build_prompt(plan, attempt=1).text
    cycle = task_state.current_attempt_cycle(state, target_index)
    if not cycle:
        raise RecordWorkerError("retry has no prior attempt checkpoint")
    previous = cycle[-1]
    report = _persisted_report_without_digest(target, previous)
    visual_rejection = isinstance(previous.get("error"), str) and previous[
        "error"
    ].startswith("automatic QC rejected candidate")
    if (visual_rejection and report is not None
            and vision_qc.correction_for(report) is not None):
        return prompt_builder.build_retry_prompt(
            plan, attempt=attempt, report=report,
        ).text
    return previous["prompt"]


def _artifact_path(
        context: RecordContext, history: dict[str, Any], services: RecordServices,
) -> Path:
    resolver = getattr(services.generator, "artifact_path", None)
    value = (
        resolver(context, history) if callable(resolver)
        else context.task_dir / "generated_images" / history["artifact_name"]
    )
    path = Path(value).resolve()
    if path.name != history["artifact_name"]:
        raise RecordWorkerError("artifact resolver changed the immutable identity")
    return path


def _validate_candidate(
        candidate: Path, history: dict[str, Any], target: dict[str, Any],
) -> str:
    if candidate.name != history["artifact_name"]:
        raise RecordWorkerError("candidate name does not match attempt history")
    image_qc.validate_decodable_raster(candidate)
    digest = _sha256(candidate)
    for payload in target["qc_reports"]:
        artifact_name = payload.get("artifact_name")
        report = payload.get("report")
        if artifact_name is None and isinstance(report, dict):
            artifact_name = report.get("candidate")
        if artifact_name == history["artifact_name"]:
            recorded = payload.get("artifact_sha256", payload.get("artifact_digest"))
            if recorded != digest:
                raise RecordWorkerError("candidate digest changed after QC")
    return digest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _report_payload(
        history: dict[str, Any], digest: str, report: vision_qc.QCReport,
) -> dict[str, Any]:
    return {
        "attempt": history["attempt"],
        "artifact_name": history["artifact_name"],
        "artifact_sha256": digest,
        "report": {
            "schema_version": 1,
            "candidate": report.candidate,
            "scores": _report_scores(report),
            "critical_defects": [item.value for item in report.critical_defects],
            "primary_defect": (
                None if report.primary_defect is None else report.primary_defect.value
            ),
            "evidence": [],
            "confidence": report.confidence,
            "decision": report.decision,
        },
    }


def _persisted_report(
        target: dict[str, Any], history: dict[str, Any], digest: str,
) -> vision_qc.QCReport | None:
    for payload in reversed(target["qc_reports"]):
        artifact_name = payload.get("artifact_name")
        body = payload.get("report")
        if artifact_name is None and isinstance(body, dict):
            artifact_name = body.get("candidate")
        if (payload.get("attempt") == history["attempt"]
                and artifact_name == history["artifact_name"]
                and payload.get("artifact_sha256", payload.get("artifact_digest")) == digest
                and isinstance(body, dict)):
            try:
                return vision_qc.parse_report(
                    json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    infographic=target["classification"] == "infographic",
                )
            except vision_qc.VisionQCError as error:
                raise RecordWorkerError("persisted QC report is invalid") from error
    return None


def _persisted_report_without_digest(
        target: dict[str, Any], history: dict[str, Any],
) -> vision_qc.QCReport | None:
    for payload in reversed(target["qc_reports"]):
        body = payload.get("report")
        artifact_name = payload.get("artifact_name")
        if artifact_name is None and isinstance(body, dict):
            artifact_name = body.get("candidate")
        if (payload.get("attempt") == history["attempt"]
                and artifact_name == history["artifact_name"]
                and isinstance(body, dict)):
            try:
                return vision_qc.parse_report(
                    json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    infographic=target["classification"] == "infographic",
                )
            except vision_qc.VisionQCError as error:
                raise RecordWorkerError("persisted QC report is invalid") from error
    return None


def _event_scores(report: vision_qc.QCReport) -> dict[str, int]:
    scores = {
        "garment_construction": report.scores.garment_construction,
        "color_material": report.scores.color_material,
        "garment_details": report.scores.garment_details,
        "target_preservation": report.scores.target_preservation,
    }
    if report.scores.text_layout is not None:
        scores["text_layout"] = report.scores.text_layout
    return scores


def _report_scores(report: vision_qc.QCReport) -> dict[str, int | None]:
    return {
        "garment_construction": report.scores.garment_construction,
        "color_material": report.scores.color_material,
        "garment_details": report.scores.garment_details,
        "target_preservation": report.scores.target_preservation,
        "text_layout": report.scores.text_layout,
    }


def _record_error(
        state_file: Path, state: dict[str, Any], services: RecordServices, *,
        code: str, message: str,
) -> None:
    task_state.record_error(
        state, code=code, error=message, updated_at=_timestamp(services),
    )
    task_state.save_state(state_file, state)


def _terminal_external_call(
        state_file: Path, state: dict[str, Any], services: RecordServices,
        message: str,
) -> str:
    _record_error(
        state_file, state, services, code="external-call", message=message,
    )
    return "failed"


def _all_success(state: dict[str, Any], target_indices: tuple[int, ...]) -> bool:
    return all(
        state["targets"][state["target_tokens"][index]]["status"] == "success"
        for index in target_indices
    )


def _accepted_count(
        state: dict[str, Any], target_indices: tuple[int, ...],
) -> int:
    return sum(
        state["targets"][state["target_tokens"][index]]["status"]
        in {"success", "accepted-local"}
        for index in target_indices
    )


def _finish_result(
        context: RecordContext, state: dict[str, Any], services: RecordServices,
        status: str,
) -> RecordResult:
    for index in context.target_indices:
        token = state["target_tokens"][index]
        target_status = state["targets"][token]["status"]
        if target_status in {"success", "failed"}:
            _event(
                services, "target_finished", record_id=context.record_id,
                target_id=token, status=target_status,
            )
    accepted = _accepted_count(state, context.target_indices)
    _event(
        services, "record_finished", record_id=context.record_id,
        status=status,
    )
    return RecordResult(context.record_id, status, accepted)


def _result_from_file(
        context: RecordContext, state_file: Path, services: RecordServices, *,
        status: str,
) -> RecordResult:
    try:
        state = task_state.load_state(state_file)
    except Exception:
        _event(
            services, "record_finished", record_id=context.record_id,
            status=status,
        )
        return RecordResult(context.record_id, status, 0)
    return _finish_result(context, state, services, status)


def _event(services: RecordServices, event: str, /, **fields: Any) -> None:
    append = getattr(services.events, "append", None)
    if not callable(append):
        raise RecordWorkerError("event service has no append boundary")
    append(event, **fields)


def _timestamp(services: RecordServices) -> str:
    value = services.clock()
    if not isinstance(value, str) or not value:
        raise RecordWorkerError("record clock returned an invalid timestamp")
    return value


def _stop_requested(stop_signal: object) -> bool:
    checker = getattr(stop_signal, "is_set", None)
    if not callable(checker):
        raise RecordWorkerError("stop signal has no is_set boundary")
    return bool(checker())


def _set_stop(stop_signal: object) -> None:
    setter = getattr(stop_signal, "set", None)
    if callable(setter):
        setter()


def _diagnose(context: RecordContext) -> dict[str, Any]:
    if (not isinstance(context, RecordContext)
            or not isinstance(context.record_id, str) or not context.record_id
            or not isinstance(context.target_indices, tuple)
            or not context.target_indices
            or not all(isinstance(value, int) and not isinstance(value, bool)
                       and value >= 0 for value in context.target_indices)
            or tuple(sorted(set(context.target_indices))) != context.target_indices):
        raise RecordWorkerError("record diagnostic context is invalid")
    root = Path(context.task_dir).resolve()
    if not root.is_dir():
        raise RecordWorkerError("record diagnostic directory is invalid")
    state = _load_bound_state(root / "manifest.json", context)
    if any(index >= len(state["target_tokens"]) for index in context.target_indices):
        raise RecordWorkerError("target index is outside the current attachment order")
    statuses = {
        state["target_tokens"][index]: state["targets"][
            state["target_tokens"][index]
        ]["status"]
        for index in context.target_indices
    }
    return {
        "record_id": context.record_id,
        "status": (
            "failed" if state["record_error"] is not None
            or any(value == "failed" for value in statuses.values())
            else "success" if all(value == "success" for value in statuses.values())
            else "pending"
        ),
        "accepted_targets": sum(
            value in {"success", "accepted-local"} for value in statuses.values()
        ),
        "targets": statuses,
        "current_target": state["current_target"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_record", description="Inspect one prepared record worker checkpoint.",
    )
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--record-id", required=True)
    parser.add_argument("--target-index", action="append", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        context = RecordContext(
            task_dir=Path(args.task_dir), record_id=args.record_id,
            target_indices=tuple(args.target_index),
        )
        payload = _diagnose(context)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, task_state.TaskStateError, RecordWorkerError) as error:
        print(f"run-record error: {error}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
