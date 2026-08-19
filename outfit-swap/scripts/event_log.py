"""Durable, sanitized pipeline-event logging and pure run metrics."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EVENT_NAMES = frozenset({
    "table_started", "table_finished",
    "record_started", "record_finished",
    "target_started", "target_finished",
    "generation_started", "generation_finished",
    "qc_started", "qc_finished",
    "comparative_qc_started", "comparative_qc_finished",
    "qc_request_accounted",
    "finalize_started", "finalize_finished",
    "download_started", "download_finished",
    "classification_started", "classification_finished",
    "reference_selection_started", "reference_selection_finished",
    "upload_started", "upload_finished",
    "detail_update_started", "detail_update_finished",
    "readback_started", "readback_finished",
    "retry_decided", "third_attempt_selected", "stop_observed",
})
FIELD_NAMES = frozenset({
    "table_id", "record_id", "target_id", "run_id", "attempt", "duration_ms",
    "status", "defect", "score", "scores", "candidate_digest", "concurrency",
    "error_category", "reference_count", "input_bytes", "phase", "ark_request_count",
})
STATUSES = frozenset({
    "pending", "running", "success", "failed", "interrupted", "accepted",
    "accepted-local", "early_accept", "reject", "retry", "selected", "stopped",
})
ACCEPTED_TARGET_STATUSES = frozenset({
    "success", "accepted", "accepted-local", "early_accept",
})
TERMINAL_TARGET_STATUSES = ACCEPTED_TARGET_STATUSES | frozenset({"failed", "stopped"})
ERROR_CATEGORIES = frozenset({
    "missing-source", "missing-target", "corrupt-source", "corrupt-target",
    "invalid-source", "invalid-target", "record-data", "external-call",
    "generation", "qc", "lark", "invalid-artifact", "preflight", "stopped",
})
PHASES = frozenset({
    "download", "classification", "reference_selection", "generation", "qc",
    "finalize", "upload", "detail_update", "readback", "lark_read", "lark_write",
})
SCORE_NAMES = frozenset({
    "garment_construction", "color_material", "garment_details",
    "target_preservation", "text_layout",
})
DEFECT_CODES = frozenset({
    "wrong_collar", "open_front", "missing_sleeve", "wrong_skirt_shape",
    "wrong_color", "original_clothing_remains", "identity_changed", "pose_changed",
    "accessory_changed", "background_changed", "bad_occlusion", "anatomy_distortion",
    "secondary_garment_details_changed", "missing_infographic_instance", "text_changed",
    "layout_changed",
})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{12,64}\Z")
_MAX_LINE_BYTES = 4_096


class EventLogError(ValueError):
    """Raised when telemetry is malformed or would cross the sanitization boundary."""


class EventLog:
    """Append one validated event per durable NDJSON line.

    Every append uses an exclusive advisory lock and O_APPEND writes so
    independently scheduled record workers cannot interleave event payloads.
    """

    def __init__(self, path: str | Path, *, clock_ms: Callable[[], int | float] | None = None) -> None:
        self._path = Path(path)
        self._clock_ms = clock_ms or _wall_clock_ms

    @property
    def path(self) -> Path:
        """Return the append-only event file path."""
        return self._path

    def append(self, event: str, /, **fields: Any) -> dict[str, Any]:
        """Validate, append, fsync, and return one event without diagnostic text."""
        timestamp_ms = _timestamp(self._clock_ms())
        payload = _validated_event({
            "schema_version": SCHEMA_VERSION,
            "event": event,
            "timestamp_ms": timestamp_ms,
            **fields,
        })
        encoded = (json.dumps(
            payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
        ) + "\n").encode("utf-8")
        if len(encoded) > _MAX_LINE_BYTES:  # defensive: preserve one-write atomicity.
            raise EventLogError("event is too large")
        self._append_durable(encoded)
        return payload

    def _append_durable(self, encoded: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        original_end = 0
        appended_bytes = 0
        try:
            descriptor = os.open(self._path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise EventLogError("event log must be a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            original_end = os.lseek(descriptor, 0, os.SEEK_END)
            while appended_bytes < len(encoded):
                written = os.write(descriptor, encoded[appended_bytes:])
                remaining = len(encoded) - appended_bytes
                if not 0 < written <= remaining:
                    raise OSError("event log write was incomplete")
                appended_bytes += written
            os.fsync(descriptor)
        except (OSError, EventLogError) as exc:
            if descriptor >= 0 and appended_bytes:
                try:
                    os.ftruncate(descriptor, original_end)
                    os.fsync(descriptor)
                except OSError:
                    pass
            raise EventLogError("event log append failed") from exc
        finally:
            if descriptor >= 0:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


def summarize_events(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return deterministic metrics from already-sanitized events without I/O."""
    if isinstance(events, (str, bytes, Mapping)):
        raise EventLogError("events must be an iterable of event objects")
    try:
        normalized = [_validated_event(event) for event in events]
    except TypeError as exc:
        raise EventLogError("events must be an iterable of event objects") from exc

    target_started = {_target_key(event) for event in normalized if event["event"] == "target_started"}
    terminal_targets = {
        _target_key(event) for event in normalized
        if event["event"] == "target_finished" and event.get("status") in TERMINAL_TARGET_STATUSES
    }
    targets = terminal_targets or target_started
    accepted_targets = {
        _target_key(event) for event in normalized
        if event["event"] == "target_finished" and event.get("status") in ACCEPTED_TARGET_STATUSES
    }
    early_accepted = {
        _target_key(event) for event in normalized
        if event["event"] == "qc_finished" and event.get("status") == "early_accept"
    } & accepted_targets
    retry_targets = {
        _target_key(event) for event in normalized if event["event"] == "retry_decided"
    }
    failed_targets = {
        _target_key(event) for event in normalized
        if event["event"] == "target_finished" and event.get("status") == "failed"
    }
    reference_events = [event for event in normalized if event["event"] == "target_started"]
    total_references = sum(event.get("reference_count", 0) for event in reference_events)
    total_input_bytes = sum(event.get("input_bytes", 0) for event in reference_events)

    phase_samples: dict[str, list[float]] = {}
    totals = {"doubao": 0, "qc": 0, "lark": 0}
    for event in normalized:
        if not event["event"].endswith("_finished") or "duration_ms" not in event:
            continue
        phase = event.get("phase", event["event"].removesuffix("_finished"))
        duration = float(event["duration_ms"])
        phase_samples.setdefault(phase, []).append(duration)
        if phase == "generation":
            totals["doubao"] += event["duration_ms"]
        elif phase == "qc":
            totals["qc"] += event["duration_ms"]
        elif phase in {"upload", "detail_update", "readback", "lark_read", "lark_write"}:
            totals["lark"] += event["duration_ms"]

    starts = [event["timestamp_ms"] for event in normalized if event["event"] == "table_started"]
    finishes = [event["timestamp_ms"] for event in normalized if event["event"] == "table_finished"]
    phase_latency = {
        phase: {
            "count": len(samples),
            "p50": _percentile(samples, 0.50),
            "p95": _percentile(samples, 0.95),
        }
        for phase, samples in sorted(phase_samples.items())
    }
    accepted_count = len(accepted_targets)
    target_count = len(targets)
    actual_qc_requests = sum(event.get("ark_request_count", 0) for event in normalized)
    if not any("ark_request_count" in event for event in normalized):
        actual_qc_requests = sum(event["event"] == "qc_started" for event in normalized)
    return {
        "total_wall_time_ms": max(finishes) - min(starts) if starts and finishes else None,
        "records": len({event.get("record_id") for event in normalized if event["event"] == "record_finished" and "record_id" in event}),
        "targets": target_count,
        "accepted_targets": accepted_count,
        "paid_generation_calls": sum(event["event"] == "generation_started" for event in normalized),
        "paid_generations_per_accepted_target": (
            sum(event["event"] == "generation_started" for event in normalized) / accepted_count
            if accepted_count else None
        ),
        "qc_calls": actual_qc_requests,
        "comparative_qc_calls": sum(
            event.get("ark_request_count", 0) for event in normalized
            if event["event"] == "comparative_qc_finished"
        ),
        "early_pass_rate": len(early_accepted) / accepted_count if accepted_count else None,
        "retry_rate": len(retry_targets & targets) / target_count if target_count else None,
        "failure_rate": len(failed_targets) / target_count if target_count else None,
        "reference_count": {
            "total": total_references,
            "average_per_target": total_references / len(reference_events) if reference_events else None,
        },
        "input_bytes": {"total": total_input_bytes},
        "service_totals_ms": totals,
        "phase_latency_ms": phase_latency,
    }


def summarize(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compatibility-friendly short name for :func:`summarize_events`."""
    return summarize_events(events)


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _timestamp(value: int | float) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise EventLogError("clock must return a finite timestamp")
    if value < 0 or int(value) != value:
        raise EventLogError("clock must return a non-negative integer timestamp")
    return int(value)


def _validated_event(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EventLogError("event must be an object")
    event = dict(value)
    required = {"schema_version", "event", "timestamp_ms"}
    if not required <= set(event) or set(event) - required - FIELD_NAMES:
        raise EventLogError("event fields do not match the sanitized schema")
    if event.get("schema_version") != SCHEMA_VERSION or isinstance(event.get("schema_version"), bool):
        raise EventLogError("unsupported event schema")
    if event.get("event") not in EVENT_NAMES:
        raise EventLogError("event name is not allowed")
    _timestamp(event["timestamp_ms"])
    for field in ("table_id", "record_id", "target_id", "run_id"):
        if field in event and (not isinstance(event[field], str) or _ID.fullmatch(event[field]) is None):
            raise EventLogError(f"{field} is invalid")
    if "attempt" in event:
        _bounded_integer(event["attempt"], "attempt", minimum=1, maximum=3)
    if "duration_ms" in event:
        _nonnegative_number(event["duration_ms"], "duration_ms")
    if "status" in event and event["status"] not in STATUSES:
        raise EventLogError("status is invalid")
    if "defect" in event and event["defect"] not in DEFECT_CODES:
        raise EventLogError("defect is invalid")
    if "score" in event:
        _bounded_integer(event["score"], "score", minimum=0, maximum=100)
    if "scores" in event:
        scores = event["scores"]
        if not isinstance(scores, Mapping) or not scores or set(scores) - SCORE_NAMES:
            raise EventLogError("scores are invalid")
        for score in scores.values():
            _bounded_integer(score, "score", minimum=0, maximum=100)
    if "candidate_digest" in event and (
            not isinstance(event["candidate_digest"], str)
            or _DIGEST.fullmatch(event["candidate_digest"]) is None):
        raise EventLogError("candidate digest is invalid")
    if "concurrency" in event:
        _bounded_integer(event["concurrency"], "concurrency", minimum=1, maximum=1_000)
    if "error_category" in event and event["error_category"] not in ERROR_CATEGORIES:
        raise EventLogError("error category is invalid")
    if "reference_count" in event:
        _bounded_integer(event["reference_count"], "reference_count", minimum=0, maximum=9)
    if "input_bytes" in event:
        _bounded_integer(event["input_bytes"], "input_bytes", minimum=0, maximum=512_000_000)
    if "ark_request_count" in event:
        _bounded_integer(event["ark_request_count"], "ark_request_count", minimum=0, maximum=10)
    if "phase" in event and event["phase"] not in PHASES:
        raise EventLogError("phase is invalid")
    return event


def _bounded_integer(value: Any, label: str, *, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise EventLogError(f"{label} is invalid")


def _nonnegative_number(value: Any, label: str) -> None:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0):
        raise EventLogError(f"{label} is invalid")


def _target_key(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    return event.get("record_id"), event.get("target_id")


def _percentile(samples: list[float], percentile: float) -> float:
    values = sorted(samples)
    index = (len(values) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (index - lower)
