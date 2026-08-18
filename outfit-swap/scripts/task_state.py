"""Persistent, resumable state for outfit-generation tasks."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 3
MAX_ATTEMPTS = 3
MAX_RECORDED_ATTEMPT = 5
CLASSIFICATIONS = frozenset({
    "front",
    "front three-quarter",
    "side",
    "back three-quarter",
    "back",
    "detail or flat lay",
    "infographic",
})
RECORD_ERROR_CODES = frozenset({
    "missing-source",
    "missing-target",
    "corrupt-source",
    "corrupt-target",
    "invalid-source",
    "invalid-target",
    "record-data",
    "external-call",
})


class TaskStateError(ValueError):
    """Raised when persisted task state is invalid or a transition is invalid."""


def canonical_state_root() -> Path:
    """Return the stable per-user root for cross-run record manifests."""
    return Path.home() / ".codex" / "state" / "outfit-swap" / "tables"


def canonical_state_path(
        state_root: str | Path, base_token: str, table_id: str, record_id: str,
) -> Path:
    """Return a non-secret stable manifest path for one table record."""
    base_token = _nonempty_string(base_token, "base_token")
    table_id = _nonempty_string(table_id, "table_id")
    record_id = _nonempty_string(record_id, "record_id")
    table_digest = hashlib.sha256(f"{base_token}\0{table_id}".encode()).hexdigest()[:20]
    record_digest = hashlib.sha256(record_id.encode()).hexdigest()[:20]
    return Path(state_root).resolve() / table_digest / "records" / f"{record_digest}.json"


def bind_manifest(
        *, state_root: str | Path, base_token: str, table_id: str,
        record_id: str, run_manifest: str | Path,
) -> Path:
    """Bind a per-run manifest link to the stable cross-run state file."""
    state_path = canonical_state_path(
        state_root, base_token, table_id, record_id,
    )
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_path = Path(run_manifest)
    run_path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(run_path):
        if not run_path.is_symlink() or run_path.resolve(strict=False) != state_path:
            raise TaskStateError(f"run manifest is not bound to canonical state: {run_path}")
    else:
        os.symlink(state_path, run_path)
    return state_path


def output_name(index: int, target_token: str) -> str:
    """Return the deterministic output filename for a target image."""
    digest = hashlib.sha256(target_token.encode()).hexdigest()[:12]
    return f"look-{index:02d}-{digest}.png"


def attempt_output_name(index: int, target_token: str, artifact_ordinal: int) -> str:
    """Return a unique non-final filename for one immutable attempt artifact."""
    if (not isinstance(index, int) or isinstance(index, bool) or index <= 0
            or not isinstance(artifact_ordinal, int) or isinstance(artifact_ordinal, bool)
            or artifact_ordinal <= 0
            or not isinstance(target_token, str) or not target_token):
        raise TaskStateError("attempt output identity is invalid")
    digest = hashlib.sha256(target_token.encode()).hexdigest()[:12]
    return f"attempt-{index:02d}-{digest}-{artifact_ordinal:02d}.png"


def promoted_output_name(artifact_name: str, target_token: str) -> str:
    """Derive the deterministic look name from an immutable attempt identity."""
    if not isinstance(target_token, str) or not target_token:
        raise TaskStateError("target_token must not be empty")
    digest = hashlib.sha256(target_token.encode()).hexdigest()[:12]
    match = re.fullmatch(
        rf"attempt-(?P<index>\d{{2,}})-{digest}-(?!0+\.png)\d{{2,}}\.png",
        artifact_name,
    ) if isinstance(artifact_name, str) else None
    if match is None:
        raise TaskStateError("attempt artifact does not match target")
    return f"look-{match.group('index')}-{digest}.png"


def _output_name_matches_target(name: str, target_token: str) -> bool:
    """Match output identity by token digest, not its display-order prefix."""
    digest = hashlib.sha256(target_token.encode()).hexdigest()[:12]
    return re.fullmatch(rf"look-\d{{2,}}-{digest}\.png", name) is not None


def _attempt_name_matches_target(
        name: Any, target_token: str, artifact_ordinal: int,
) -> bool:
    if not isinstance(name, str):
        return False
    digest = hashlib.sha256(target_token.encode()).hexdigest()[:12]
    match = re.fullmatch(rf"attempt-\d{{2,}}-{digest}-(\d{{2,}})\.png", name)
    return match is not None and int(match.group(1)) == artifact_ordinal


def _source_identity(tokens: Iterable[str]) -> tuple[str, ...]:
    """Compare garment source identity independently of attachment order."""
    return tuple(sorted(tokens))


def new_state(
    *,
    record_id: str,
    run_id: str,
    source_tokens: list[str],
    target_tokens: list[str],
    started_at: str,
) -> dict:
    """Create the initial state without changing the supplied token order."""
    record_id = _nonempty_string(record_id, "record_id")
    run_id = _nonempty_string(run_id, "run_id")
    started_at = _nonempty_string(started_at, "started_at")
    source_tokens = _token_list(source_tokens, "source_tokens")
    target_tokens = _token_list(target_tokens, "target_tokens")
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "run_id": run_id,
        "started_at": started_at,
        "current_target": None,
        "record_error": None,
        "source_tokens": list(source_tokens),
        "target_tokens": list(target_tokens),
        "targets": {
            token: {
                "status": "pending",
                "classification": None,
                "reference_tokens": [],
                "attempts": 0,
                "output": None,
                "local_acceptance": None,
                "prompt_sha256": None,
                "model": None,
                "error": None,
                "stale_output_tokens": [],
                "updated_at": started_at,
                "attempt_history": [],
                "target_plan": None,
                "qc_reports": [],
                "selection_reason": None,
                "selection_reason_history": [],
            }
            for token in target_tokens
        },
    }


def _target_template(updated_at: str) -> dict[str, Any]:
    return {"status": "pending", "classification": None, "reference_tokens": [],
            "attempts": 0, "output": None, "local_acceptance": None,
            "prompt_sha256": None, "model": None,
            "error": None, "stale_output_tokens": [],
            "updated_at": updated_at, "attempt_history": [],
            "target_plan": None, "qc_reports": [], "selection_reason": None,
            "selection_reason_history": []}


def _token_list(value: Any, name: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(token, str) and token for token in value):
        raise TaskStateError(f"{name} must be a JSON array of non-empty strings")
    if not allow_empty and not value:
        raise TaskStateError(f"{name} must not be empty")
    return list(value)


def _token_iterable(
        value: Iterable[str], name: str, *, allow_empty: bool = False,
) -> list[str]:
    if isinstance(value, (str, bytes, dict)):
        raise TaskStateError(f"{name} must be an iterable of non-empty strings")
    try:
        tokens = list(value)
    except TypeError as error:
        raise TaskStateError(f"{name} must be an iterable of non-empty strings") from error
    if not all(isinstance(token, str) and token for token in tokens):
        raise TaskStateError(f"{name} must be an iterable of non-empty strings")
    if not allow_empty and not tokens:
        raise TaskStateError(f"{name} must not be empty")
    return tokens


def _outputs_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TaskStateError("outputs must be a JSON array")
    return _outputs_iterable(value)


def _outputs_iterable(value: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, dict)):
        raise TaskStateError("outputs must be an iterable of JSON objects")
    try:
        outputs = list(value)
    except TypeError as error:
        raise TaskStateError("outputs must be an iterable of JSON objects") from error
    for output in outputs:
        if not isinstance(output, dict):
            raise TaskStateError("each output must be a JSON object")
        if not isinstance(output.get("file_token"), str) or not output["file_token"]:
            raise TaskStateError("each output requires a non-empty file_token")
        if not isinstance(output.get("name"), str) or not output["name"]:
            raise TaskStateError("each output requires a non-empty name")
    return outputs


def _artifact_identities_iterable(
        value: Iterable[dict[str, str]],
) -> set[tuple[str, str]]:
    """Validate identities of locally present, already image-validated artifacts."""
    if isinstance(value, (str, bytes, dict)):
        raise TaskStateError("resumable_artifacts must be an iterable of JSON objects")
    try:
        artifacts = list(value)
    except TypeError as error:
        raise TaskStateError(
            "resumable_artifacts must be an iterable of JSON objects",
        ) from error
    identities: set[tuple[str, str]] = set()
    for artifact in artifacts:
        if (not isinstance(artifact, dict)
                or set(artifact) != {"run_id", "artifact_name"}
                or not isinstance(artifact.get("run_id"), str)
                or not artifact["run_id"]
                or not isinstance(artifact.get("artifact_name"), str)
                or not artifact["artifact_name"]):
            raise TaskStateError(
                "each resumable artifact requires only run_id and artifact_name",
            )
        identities.add((artifact["run_id"], artifact["artifact_name"]))
    return identities


def _require_target(state: dict[str, Any], target_token: str) -> dict[str, Any]:
    if target_token not in state["target_tokens"]:
        raise TaskStateError(f"target is not current: {target_token}")
    try:
        return state["targets"][target_token]
    except KeyError as error:
        raise TaskStateError(f"target is missing from state: {target_token}") from error


def _sanitize_error(error: str) -> str:
    """Return a compact error summary safe for local state and Base detail."""
    if not isinstance(error, str):
        raise TaskStateError("error must be a string")
    value = " ".join(error.split())
    value = re.sub(
        r"(?i)data:image/[^\s,;]+(?:;[^\s,]+)*,[^\s]+",
        "[redacted-data-url]",
        value,
    )
    value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", value)
    value = re.sub(
        r"(?i)\b(ARK_API_KEY|api[_-]?key|access[_-]?token|secret|signature|sig)=\S+",
        lambda match: f"{match.group(1)}=[redacted]",
        value,
    )
    value = re.sub(r"(https?://[^\s?#]+)\?[^\s]+", r"\1?[redacted]", value)
    value = value[:500]
    if not value:
        raise TaskStateError("error must not be empty")
    return value


def _record_error_value(code: str, error: str, updated_at: str) -> dict[str, str]:
    if code not in RECORD_ERROR_CODES:
        raise TaskStateError(f"unknown record error code: {code!r}")
    if not isinstance(updated_at, str) or not updated_at:
        raise TaskStateError("updated_at must not be empty")
    return {"code": code, "message": _sanitize_error(error), "updated_at": updated_at}


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TaskStateError(f"{name} must not be empty")
    return value


def _json_object(value: Any, name: str) -> dict[str, Any]:
    """Validate and normalize a JSON object kept in durable target metadata."""
    if not isinstance(value, dict):
        raise TaskStateError(f"{name} must be a JSON object")
    pending = [(value, True)]
    active_containers: set[int] = set()
    while pending:
        current, entering = pending.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8", "strict")
            except UnicodeError as error:
                raise TaskStateError(f"{name} must contain valid Unicode") from error
        if not isinstance(current, (dict, list, tuple)):
            continue
        identity = id(current)
        if not entering:
            active_containers.remove(identity)
            continue
        if identity in active_containers:
            raise TaskStateError(f"{name} must not contain a cycle")
        active_containers.add(identity)
        pending.append((current, False))
        if isinstance(current, dict):
            if not all(isinstance(key, str) for key in current):
                raise TaskStateError(f"{name} must contain only string keys")
            pending.extend((child, True) for child in current.values())
        else:
            pending.extend((child, True) for child in current)
    try:
        normalized = json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError, UnicodeError) as error:
        raise TaskStateError(f"{name} must contain JSON values") from error
    return normalized


def _qc_report(value: Any) -> dict[str, Any]:
    report = _json_object(value, "qc report")
    attempt = report.get("attempt")
    if (not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0):
        raise TaskStateError("qc report must include a positive attempt number")
    if not any(
            isinstance(report.get(key), str) and report[key]
            for key in ("artifact_sha256", "artifact_digest")):
        raise TaskStateError("qc report must include an artifact digest")
    return report


def _target_at_index(state: dict[str, Any], target_index: int) -> dict[str, Any]:
    """Return a target by its zero-based current attachment-order index."""
    if (not isinstance(target_index, int) or isinstance(target_index, bool)
            or not 0 <= target_index < len(state["target_tokens"])):
        raise TaskStateError("target_index is outside the current target order")
    return state["targets"][state["target_tokens"][target_index]]


def new_record_error_state(
    *,
    record_id: str,
    run_id: str,
    source_tokens: list[str],
    target_tokens: list[str],
    started_at: str,
    code: str,
    error: str,
    updated_at: str,
) -> dict[str, Any]:
    """Create durable failure state even when required attachment sets are empty."""
    sources = _token_list(source_tokens, "source_tokens", allow_empty=True)
    targets = _token_list(target_tokens, "target_tokens", allow_empty=True)
    state = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "run_id": run_id,
        "started_at": started_at,
        "current_target": None,
        "record_error": _record_error_value(code, error, updated_at),
        "source_tokens": sources,
        "target_tokens": targets,
        "targets": {token: _target_template(updated_at) for token in targets},
    }
    return _validate_state(state)


def _current_cycle_history(target: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the final contiguous 1..N attempt suffix for the current cycle."""
    history = target.get("attempt_history")
    if not isinstance(history, list):
        raise TaskStateError("target has no current attempt cycle")
    start = next(
        (index for index in range(len(history) - 1, -1, -1)
         if history[index].get("attempt") == 1),
        None,
    )
    if start is None:
        raise TaskStateError("target has no current attempt cycle")
    cycle = history[start:]
    if (not cycle
            or any(entry.get("attempt") != number
                   for number, entry in enumerate(cycle, start=1))
            or len(cycle) != target.get("attempts")):
        raise TaskStateError("target has an invalid current attempt cycle")
    return cycle


def _validate_historical_selection(
        target: dict[str, Any], selected: dict[str, Any],
) -> None:
    """Require a compatible non-latest selection after an exhausted cycle."""
    cycle = _current_cycle_history(target)
    try:
        selected_index = next(
            (index for index, entry in enumerate(cycle) if entry is selected),
        )
    except StopIteration as error:
        raise TaskStateError("selected entry is not in the current attempt cycle") from error
    if selected_index == len(cycle) - 1:
        return
    if selected.get("reference_tokens") != cycle[-1].get("reference_tokens"):
        raise TaskStateError(
            "historical selection reference_tokens do not match current attempt cycle",
        )
    if (len(cycle) < MAX_ATTEMPTS
            or any(entry.get("outcome") not in {"failed", "interrupted"}
                   for entry in cycle[selected_index + 1:])):
        raise TaskStateError("historical selection has invalid later attempt history")


def _accepted_history_entry(target: dict[str, Any]) -> dict[str, Any]:
    """Return the sole history entry matching local_acceptance artifact identity."""
    local_acceptance = target.get("local_acceptance")
    if not isinstance(local_acceptance, dict):
        raise TaskStateError("target has no local acceptance")
    accepted_entries = [
        entry for entry in _current_cycle_history(target)
        if entry.get("outcome") == "accepted-local"
    ]
    if len(accepted_entries) != 1:
        raise TaskStateError("local acceptance does not match current attempt cycle")
    accepted = accepted_entries[0]
    if (accepted.get("run_id") != local_acceptance.get("run_id")
            or accepted.get("artifact_name") != local_acceptance.get("artifact_name")):
        raise TaskStateError("local acceptance does not match current attempt cycle")
    _validate_historical_selection(target, accepted)
    return accepted


def _validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise TaskStateError("state must be a JSON object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise TaskStateError(f"unsupported schema version: {state.get('schema_version')!r}")
    for key in ("record_id", "run_id", "started_at", "current_target", "record_error",
                "source_tokens", "target_tokens", "targets"):
        if key not in state:
            raise TaskStateError(f"state is missing {key}")
    for key in ("record_id", "run_id", "started_at"):
        if not isinstance(state[key], str) or not state[key]:
            raise TaskStateError(f"state has invalid {key}")
    record_error = state["record_error"]
    if record_error is not None:
        if (not isinstance(record_error, dict)
                or record_error.get("code") not in RECORD_ERROR_CODES
                or not isinstance(record_error.get("message"), str)
                or not record_error["message"]
                or not isinstance(record_error.get("updated_at"), str)
                or not record_error["updated_at"]):
            raise TaskStateError("state has invalid record_error")
    allow_empty = record_error is not None
    _token_list(state["source_tokens"], "source_tokens", allow_empty=allow_empty)
    _token_list(state["target_tokens"], "target_tokens", allow_empty=allow_empty)
    current_target = state["current_target"]
    if current_target is not None and (
            not isinstance(current_target, str)
            or current_target not in state["target_tokens"]):
        raise TaskStateError("state has invalid current_target")
    if not isinstance(state["targets"], dict):
        raise TaskStateError("state targets are invalid")
    for token, target in state["targets"].items():
        if not isinstance(token, str) or not token:
            raise TaskStateError("state has an invalid target token")
        if not isinstance(target, dict):
            raise TaskStateError(f"target entry is invalid: {token}")
        target.setdefault("selection_reason_history", [])
        required = ("status", "classification", "reference_tokens", "attempts", "output",
                    "local_acceptance", "prompt_sha256", "model", "error", "stale_output_tokens",
                    "updated_at", "attempt_history", "target_plan", "qc_reports",
                    "selection_reason", "selection_reason_history")
        if any(key not in target for key in required):
            raise TaskStateError(f"target entry is incomplete: {token}")
        if (not isinstance(target["status"], str)
                or not isinstance(target["reference_tokens"], list)
                or not isinstance(target["attempts"], int)
                or isinstance(target["attempts"], bool)
                or not 0 <= target["attempts"] <= MAX_RECORDED_ATTEMPT
                or not isinstance(target["updated_at"], str) or not target["updated_at"]
                or not isinstance(target["stale_output_tokens"], list)
                or not all(isinstance(item, str) and item
                           for item in target["stale_output_tokens"])
                or len(target["stale_output_tokens"])
                != len(set(target["stale_output_tokens"]))
                or not isinstance(target["attempt_history"], list)
                or not all(isinstance(entry, dict) for entry in target["attempt_history"])
                or not isinstance(target["qc_reports"], list)
                or not isinstance(target["selection_reason_history"], list)):
            raise TaskStateError(f"target entry has invalid fields: {token}")
        if target["target_plan"] is not None:
            _json_object(target["target_plan"], "target_plan")
        if target["selection_reason"] is not None:
            _json_object(target["selection_reason"], "selection_reason")
        for reason in target["selection_reason_history"]:
            _json_object(reason, "selection_reason_history")
        for report in target["qc_reports"]:
            _qc_report(report)
        if (target["status"] not in {
                    "pending", "running", "accepted-local", "success", "failed",
                }
                or target["classification"] is not None
                and target["classification"] not in CLASSIFICATIONS
                or not all(isinstance(reference, str) and reference
                           for reference in target["reference_tokens"])
                or target["prompt_sha256"] is not None and not isinstance(target["prompt_sha256"], str)
                or target["model"] is not None and not isinstance(target["model"], str)
                or target["error"] is not None and not isinstance(target["error"], str)):
            raise TaskStateError(f"target entry has invalid fields: {token}")
        if target["output"] is not None:
            _outputs_list([target["output"]])
            if target["output"]["file_token"] in target["stale_output_tokens"]:
                raise TaskStateError(f"target output is stale: {token}")
        if target["status"] != "success" and target["output"] is not None:
            raise TaskStateError(f"non-successful target has output: {token}")
        local_acceptance = target["local_acceptance"]
        if local_acceptance is not None:
            if (not isinstance(local_acceptance, dict)
                    or set(local_acceptance) != {
                        "run_id", "artifact_name", "name", "accepted_at",
                    }
                    or not all(
                        isinstance(local_acceptance.get(key), str)
                        and local_acceptance[key]
                        for key in (
                            "run_id", "artifact_name", "name", "accepted_at",
                        )
                    )
                    or not _output_name_matches_target(
                        local_acceptance["name"], token,
                    )):
                raise TaskStateError(f"target has invalid local acceptance: {token}")
        if target["status"] == "accepted-local" and local_acceptance is None:
            raise TaskStateError(f"accepted target has no local acceptance: {token}")
        if target["status"] != "accepted-local" and local_acceptance is not None:
            raise TaskStateError(f"non-accepted target has local acceptance: {token}")
        if target["status"] == "running" and current_target != token:
            raise TaskStateError(f"running target is not current: {token}")
        active_history = [
            entry for entry in target["attempt_history"]
            if entry.get("outcome") == "running"
        ]
        artifact_ordinals: list[int] = []
        for entry in target["attempt_history"]:
            ordinal = entry.get("artifact_ordinal")
            if (not isinstance(entry.get("attempt"), int)
                    or isinstance(entry.get("attempt"), bool)
                    or not 1 <= entry["attempt"] <= MAX_RECORDED_ATTEMPT
                    or not isinstance(ordinal, int) or isinstance(ordinal, bool)
                    or ordinal <= 0
                    or not _attempt_name_matches_target(
                        entry.get("artifact_name"), token, ordinal,
                    )
                    or entry.get("classification") not in CLASSIFICATIONS
                    or not isinstance(entry.get("reference_tokens"), list)
                    or not entry["reference_tokens"]
                    or not all(isinstance(item, str) and item
                               for item in entry["reference_tokens"])
                    or not isinstance(entry.get("prompt"), str) or not entry["prompt"]
                    or entry.get("prompt_sha256")
                    != hashlib.sha256(entry["prompt"].encode()).hexdigest()
                    or not isinstance(entry.get("model"), str) or not entry["model"]
                    or not isinstance(entry.get("started_at"), str)
                    or not entry["started_at"]
                    or not isinstance(entry.get("run_id"), str) or not entry["run_id"]
                    or entry.get("outcome") not in {
                        "running", "interrupted", "accepted-local", "success", "failed",
                    }):
                raise TaskStateError(f"target has invalid attempt history: {token}")
            if entry["outcome"] == "running":
                if entry.get("finished_at") is not None:
                    raise TaskStateError(f"running attempt is already finished: {token}")
            elif (not isinstance(entry.get("finished_at"), str)
                  or not entry["finished_at"]):
                raise TaskStateError(f"finished attempt has no timestamp: {token}")
            artifact_ordinals.append(ordinal)
        if artifact_ordinals != list(range(1, len(artifact_ordinals) + 1)):
            raise TaskStateError(f"target artifact ordinals are not monotonic: {token}")
        if target["status"] == "running" and (
                len(active_history) != 1
                or target["attempt_history"][-1] is not active_history[0]):
            raise TaskStateError(f"running target has invalid attempt history: {token}")
        if target["status"] == "running" and (
                not 1 <= target["attempts"] <= MAX_RECORDED_ATTEMPT
                or active_history[0]["attempt"] != target["attempts"]):
            raise TaskStateError(f"running attempt does not match budget: {token}")
        if target["status"] != "running" and active_history:
            raise TaskStateError(f"non-running target has active attempt history: {token}")
        if target["status"] == "accepted-local":
            try:
                _accepted_history_entry(target)
            except TaskStateError as error:
                raise TaskStateError(
                    f"local acceptance does not match attempt history: {token}",
                ) from error
            if local_acceptance["name"] != promoted_output_name(
                    local_acceptance["artifact_name"], token,
            ):
                raise TaskStateError(
                    f"local acceptance does not match attempt history: {token}",
                )
    for token in state["target_tokens"]:
        if token not in state["targets"]:
            raise TaskStateError(f"state is missing target {token}")
    for token in state["target_tokens"]:
        target = state["targets"][token]
        if target["status"] == "success":
            output = target["output"]
            if output is None or not _output_name_matches_target(output["name"], token):
                raise TaskStateError(f"successful target has invalid output: {token}")
            if target["attempt_history"]:
                try:
                    success_entries = [
                        entry for entry in _current_cycle_history(target)
                        if entry.get("outcome") == "success"
                    ]
                except TaskStateError as error:
                    raise TaskStateError(
                        f"successful target has invalid attempt history: {token}",
                    ) from error
                if len(success_entries) != 1 or success_entries[0].get("output") != output:
                    raise TaskStateError(
                        f"successful target has invalid attempt history: {token}",
                    )
                try:
                    _validate_historical_selection(target, success_entries[0])
                except TaskStateError as error:
                    raise TaskStateError(
                        f"successful target has invalid attempt history: {token}",
                    ) from error
    if current_target is not None and state["targets"][current_target]["status"] != "running":
        raise TaskStateError("current_target is not running")
    return state


def _migrate_v1_state(value: Any) -> Any:
    """Upgrade baseline/enhanced v1 manifests without discarding valid decisions."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return value
    state = copy.deepcopy(value)
    state["schema_version"] = 2
    state.setdefault("current_target", None)
    state.setdefault("record_error", None)
    targets = state.get("targets")
    target_tokens = state.get("target_tokens")
    if not isinstance(targets, dict) or not isinstance(target_tokens, list):
        return state
    ordered_tokens = list(target_tokens) + [
        token for token in targets if token not in target_tokens
    ]
    running_tokens: list[str] = []
    for position, token in enumerate(ordered_tokens, start=1):
        target = targets.get(token)
        if not isinstance(target, dict):
            continue
        target.setdefault("stale_output_tokens", [])
        target.setdefault("local_acceptance", None)
        if isinstance(target.get("error"), str):
            try:
                target["error"] = _sanitize_error(target["error"])
            except TaskStateError:
                target["error"] = None
        history = target.setdefault("attempt_history", [])
        if isinstance(history, list):
            for ordinal, entry in enumerate(history, start=1):
                if not isinstance(entry, dict):
                    continue
                entry.setdefault("artifact_ordinal", ordinal)
                entry.setdefault("run_id", state.get("run_id"))
                entry.setdefault(
                    "artifact_name",
                    attempt_output_name(position, token, ordinal),
                )
        classification = target.get("classification")
        if classification is not None and classification not in CLASSIFICATIONS:
            target["classification"] = None
            target["error"] = "legacy classification requires reclassification"
        if target.get("status") != "success":
            target["output"] = None
        if target.get("status") == "running":
            if (isinstance(history, list) and history
                    and isinstance(history[-1], dict)
                    and history[-1].get("outcome") == "running"):
                running_tokens.append(token)
            else:
                target["status"] = "pending"
                attempts = target.get("attempts")
                if isinstance(attempts, int) and not isinstance(attempts, bool):
                    target["attempts"] = max(0, attempts - 1)
                target["error"] = "migrated interrupted legacy attempt; retrying"
    current = state.get("current_target")
    if current not in running_tokens:
        state["current_target"] = running_tokens[0] if len(running_tokens) == 1 else None
    if len(running_tokens) > 1:
        for token in running_tokens:
            target = targets[token]
            target["status"] = "pending"
            target["attempts"] = max(0, target["attempts"] - 1)
            if target["attempt_history"]:
                entry = target["attempt_history"][-1]
                entry["outcome"] = "interrupted"
                entry["finished_at"] = target["updated_at"]
                entry["error"] = "ambiguous legacy active attempts; retrying"
                entry["output"] = None
        state["current_target"] = None
    sources = state.get("source_tokens")
    current_targets = state.get("target_tokens")
    if (state.get("record_error") is None
            and isinstance(sources, list) and isinstance(current_targets, list)
            and (not sources or not current_targets)):
        code = "missing-source" if not sources else "missing-target"
        state["record_error"] = {
            "code": code,
            "message": "migrated record has missing required attachments",
            "updated_at": state.get("started_at"),
        }
    record_error = state.get("record_error")
    if isinstance(record_error, dict) and isinstance(record_error.get("message"), str):
        try:
            record_error["message"] = _sanitize_error(record_error["message"])
        except TaskStateError:
            record_error["message"] = "migrated record error"
    return state


def _migrate_v2_state(value: Any) -> Any:
    """Add automatic-QC checkpoints without modifying historical attempts."""
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        return value
    state = copy.deepcopy(value)
    state["schema_version"] = SCHEMA_VERSION
    targets = state.get("targets")
    if not isinstance(targets, dict):
        return state
    for target in targets.values():
        if not isinstance(target, dict):
            continue
        target.setdefault("target_plan", None)
        target.setdefault("qc_reports", [])
        target.setdefault("selection_reason", None)
        target.setdefault("selection_reason_history", [])
    return state


def _migrate_v3_state(value: Any) -> Any:
    """Add append-only selection history to schema-three manifests."""
    if not isinstance(value, dict) or value.get("schema_version") != 3:
        return value
    state = copy.deepcopy(value)
    targets = state.get("targets")
    if isinstance(targets, dict):
        for target in targets.values():
            if isinstance(target, dict):
                target.setdefault("selection_reason_history", [])
    return state


def _normalize_legacy_budget(state: dict[str, Any]) -> dict[str, Any]:
    """Terminalize pending legacy work that has exceeded the active budget."""
    for target in state["targets"].values():
        if (target["status"] == "pending"
                and target["attempts"] >= MAX_ATTEMPTS):
            target["status"] = "failed"
            target["error"] = "legacy attempt budget exceeds current three-call limit"
    return state


def load_state(path: str | Path) -> dict[str, Any]:
    """Load and validate a persisted state file."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            state = _validate_state(
                _migrate_v3_state(_migrate_v2_state(_migrate_v1_state(json.load(handle)))),
            )
        state = _normalize_legacy_budget(state)
        return _validate_state(state)
    except (OSError, json.JSONDecodeError) as error:
        raise TaskStateError(f"cannot load state: {error}") from error


def _fsync_directory(path: Path) -> None:
    """Persist a completed rename in its containing directory."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: str | Path, state: dict[str, Any]) -> None:
    target = Path(path)
    if target.is_symlink():
        target = target.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        _fsync_directory(target.parent)
    except (OSError, UnicodeError) as error:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise TaskStateError(f"cannot write state: {error}") from error


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    """Validate and atomically persist a state transition."""
    _validate_state(state)
    _atomic_write(path, state)


def _finish_running_history(
        target: dict[str, Any], *, outcome: str, error: str | None, updated_at: str,
        output: dict[str, str] | None = None,
) -> None:
    if not target["attempt_history"]:
        raise TaskStateError("running target is missing attempt history")
    entry = target["attempt_history"][-1]
    if entry.get("outcome") != "running":
        raise TaskStateError("running target has no active history entry")
    entry["outcome"] = outcome
    entry["finished_at"] = updated_at
    entry["error"] = error
    entry["output"] = output


def reconcile(state: dict[str, Any], *, source_tokens: Iterable[str],
              target_tokens: Iterable[str], outputs: Iterable[dict[str, Any]],
              run_id: str, started_at: str, updated_at: str,
              resumable_artifacts: Iterable[dict[str, str]] = ()) -> dict[str, Any]:
    """Align current inputs, Base outputs, and locally validated paid artifacts."""
    _validate_state(state)
    current_sources = _token_iterable(source_tokens, "source_tokens")
    current_tokens = _token_iterable(target_tokens, "target_tokens")
    current_outputs = _outputs_iterable(outputs)
    available_artifacts = _artifact_identities_iterable(resumable_artifacts)
    source_changed = _source_identity(current_sources) != _source_identity(
        state["source_tokens"],
    )
    run_id = _nonempty_string(run_id, "run_id")
    started_at = _nonempty_string(started_at, "started_at")
    updated_at = _nonempty_string(updated_at, "updated_at")

    candidate = copy.deepcopy(state)
    candidate["run_id"] = run_id
    candidate["started_at"] = started_at
    candidate["source_tokens"] = current_sources
    candidate["target_tokens"] = current_tokens
    candidate["current_target"] = None
    candidate["record_error"] = None
    for token in current_tokens:
        candidate["targets"].setdefault(token, _target_template(updated_at))
    if source_changed:
        for token, target in list(candidate["targets"].items()):
            if target["status"] == "running":
                _finish_running_history(
                    target, outcome="interrupted",
                    error="source attachments changed; prior attempt is obsolete",
                    updated_at=updated_at,
                )
            stale_tokens = list(target["stale_output_tokens"])
            existing = target.get("output")
            if existing is not None and existing["file_token"] not in stale_tokens:
                stale_tokens.append(existing["file_token"])
            for output in current_outputs:
                if (_output_name_matches_target(output["name"], token)
                        and output["file_token"] not in stale_tokens):
                    stale_tokens.append(output["file_token"])
            history = list(target["attempt_history"])
            replacement = _target_template(updated_at)
            replacement["attempt_history"] = history
            replacement["stale_output_tokens"] = stale_tokens
            replacement["target_plan"] = copy.deepcopy(target["target_plan"])
            replacement["qc_reports"] = copy.deepcopy(target["qc_reports"])
            replacement["selection_reason_history"] = copy.deepcopy(
                target["selection_reason_history"],
            )
            if target["selection_reason"] is not None:
                replacement["selection_reason_history"].append(
                    copy.deepcopy(target["selection_reason"]),
                )
            candidate["targets"][token] = replacement
    else:
        for token, target in candidate["targets"].items():
            if target["status"] == "running" and token not in current_tokens:
                interrupted = "target attachment removed; retrying if restored"
                _finish_running_history(
                    target, outcome="interrupted", error=interrupted,
                    updated_at=updated_at,
                )
                target["status"] = (
                    "failed" if target["attempts"] >= MAX_ATTEMPTS else "pending"
                )
                target["error"] = interrupted
                target["updated_at"] = updated_at

    preserved_current: str | None = None
    for token in current_tokens:
        target = candidate["targets"][token]
        existing = target.get("output")
        valid_outputs = [
            output for output in current_outputs
            if output["file_token"] not in target["stale_output_tokens"]
        ]
        recovered = (
            existing
            if existing in valid_outputs
            and _output_name_matches_target(existing["name"], token)
            else next(
                (output for output in valid_outputs
                 if _output_name_matches_target(output["name"], token)),
                None,
            )
        )
        if recovered is not None:
            uploaded = {
                "file_token": recovered["file_token"], "name": recovered["name"],
            }
            if target["status"] == "running":
                _finish_running_history(
                    target, outcome="success", error=None, updated_at=updated_at,
                    output=uploaded,
                )
            elif target["status"] == "accepted-local":
                history = _accepted_history_entry(target)
                history["outcome"] = "success"
                history["finished_at"] = updated_at
                history["error"] = None
                history["output"] = uploaded
            target["status"] = "success"
            target["output"] = uploaded
            target["local_acceptance"] = None
            target["error"] = None
            target["updated_at"] = updated_at
        elif target["status"] == "running":
            active = target["attempt_history"][-1]
            identity = (active["run_id"], active["artifact_name"])
            if (identity in available_artifacts
                    or target["attempts"] >= MAX_ATTEMPTS):
                preserved_current = token
            else:
                interrupted = (
                    "interrupted attempt has no complete local artifact; "
                    "initiated edit counted conservatively"
                )
                _finish_running_history(
                    target, outcome="interrupted", error=interrupted,
                    updated_at=updated_at,
                )
                target["status"] = (
                    "failed" if target["attempts"] >= MAX_ATTEMPTS else "pending"
                )
                target["error"] = interrupted
                target["updated_at"] = updated_at
        elif target["status"] == "accepted-local":
            accepted = target["local_acceptance"]
            identity = (accepted["run_id"], accepted["artifact_name"])
            if identity not in available_artifacts:
                missing = "accepted local artifact is missing; regeneration required"
                history = _accepted_history_entry(target)
                history["outcome"] = "failed"
                history["finished_at"] = updated_at
                history["error"] = missing
                target["status"] = (
                    "failed" if target["attempts"] >= MAX_ATTEMPTS else "pending"
                )
                target["local_acceptance"] = None
                target["error"] = missing
                target["updated_at"] = updated_at
        elif target.get("status") == "success":
            target["status"] = "pending"
            target["output"] = None
            target["updated_at"] = updated_at
    candidate["current_target"] = preserved_current
    _validate_state(candidate)
    state.clear()
    state.update(candidate)
    return state


def reconcile_error(
        state: dict[str, Any], *, source_tokens: Iterable[str],
        target_tokens: Iterable[str], outputs: Iterable[dict[str, Any]],
        run_id: str, started_at: str,
        code: str, error: str, updated_at: str,
) -> dict[str, Any]:
    """Refresh current inputs and stop a record without discarding historical audit state."""
    _validate_state(state)
    current_sources = _token_iterable(
        source_tokens, "source_tokens", allow_empty=True,
    )
    current_targets = _token_iterable(
        target_tokens, "target_tokens", allow_empty=True,
    )
    current_outputs = _outputs_iterable(outputs)
    run_id = _nonempty_string(run_id, "run_id")
    started_at = _nonempty_string(started_at, "started_at")
    record_error_value = _record_error_value(code, error, updated_at)

    candidate = copy.deepcopy(state)
    source_changed = _source_identity(current_sources) != _source_identity(
        candidate["source_tokens"],
    )
    if candidate["current_target"] is not None:
        target = candidate["targets"][candidate["current_target"]]
        interrupted = record_error_value["message"]
        _finish_running_history(
            target, outcome="failed", error=interrupted, updated_at=updated_at,
        )
        target["status"] = "failed"
        target["error"] = interrupted
        target["updated_at"] = updated_at
    candidate["run_id"] = run_id
    candidate["started_at"] = started_at
    candidate["source_tokens"] = current_sources
    candidate["target_tokens"] = current_targets
    candidate["current_target"] = None
    candidate["record_error"] = record_error_value
    for token in current_targets:
        candidate["targets"].setdefault(token, _target_template(updated_at))
    if source_changed:
        for token, target in list(candidate["targets"].items()):
            existing = target.get("output")
            stale_output_tokens = list(target["stale_output_tokens"])
            if (existing is not None
                    and existing["file_token"] not in stale_output_tokens):
                stale_output_tokens.append(existing["file_token"])
            for output in current_outputs:
                if (_output_name_matches_target(output["name"], token)
                        and output["file_token"] not in stale_output_tokens):
                    stale_output_tokens.append(output["file_token"])
            history = list(target["attempt_history"])
            replacement = _target_template(updated_at)
            replacement["attempt_history"] = history
            replacement["stale_output_tokens"] = stale_output_tokens
            replacement["target_plan"] = copy.deepcopy(target["target_plan"])
            replacement["qc_reports"] = copy.deepcopy(target["qc_reports"])
            replacement["selection_reason_history"] = copy.deepcopy(
                target["selection_reason_history"],
            )
            if target["selection_reason"] is not None:
                replacement["selection_reason_history"].append(
                    copy.deepcopy(target["selection_reason"]),
                )
            candidate["targets"][token] = replacement
    _validate_state(candidate)
    state.clear()
    state.update(candidate)
    return state


def prepare_retry(state: dict[str, Any], *, updated_at: str) -> dict[str, Any]:
    """Reset failed work without discarding successes or pending uploads."""
    _validate_state(state)
    if state["current_target"] is not None:
        raise TaskStateError("cannot retry while an active attempt is running; reconcile first")
    _nonempty_string(updated_at, "updated_at")
    if (state["record_error"] is not None
            or not state["source_tokens"] or not state["target_tokens"]):
        raise TaskStateError("reconcile valid current attachments before retry")
    candidate = copy.deepcopy(state)
    for token in candidate["target_tokens"]:
        target = candidate["targets"][token]
        if (target["status"] == "success" and target["output"] is not None
                or target["status"] == "accepted-local"):
            continue
        history = list(target["attempt_history"])
        stale_output_tokens = list(target["stale_output_tokens"])
        replacement = _target_template(updated_at)
        replacement["attempt_history"] = history
        replacement["stale_output_tokens"] = stale_output_tokens
        replacement["target_plan"] = copy.deepcopy(target["target_plan"])
        replacement["qc_reports"] = copy.deepcopy(target["qc_reports"])
        replacement["selection_reason_history"] = copy.deepcopy(
            target["selection_reason_history"],
        )
        if target["selection_reason"] is not None:
            replacement["selection_reason_history"].append(
                copy.deepcopy(target["selection_reason"]),
            )
        candidate["targets"][token] = replacement
    candidate["current_target"] = None
    candidate["record_error"] = None
    _validate_state(candidate)
    state.clear()
    state.update(candidate)
    return state


def begin_attempt(state: dict[str, Any], *, target_token: str, classification: str,
                  reference_tokens: Iterable[str], prompt: str, model: str,
                  updated_at: str) -> dict[str, Any]:
    """Start an attempt, recording only the prompt digest in persistent state."""
    _validate_state(state)
    target = _require_target(state, target_token)
    if target["attempts"] == MAX_ATTEMPTS:
        raise TaskStateError(f"target has exhausted attempts: {target_token}")
    if target["status"] != "pending":
        raise TaskStateError(f"target is not pending: {target_token}")
    if state["current_target"] is not None:
        raise TaskStateError(f"target is already running: {state['current_target']}")
    if target["attempts"] >= MAX_ATTEMPTS:
        raise TaskStateError(f"target has exhausted attempts: {target_token}")
    if classification not in CLASSIFICATIONS:
        raise TaskStateError(f"unknown classification: {classification!r}")
    references = _token_iterable(reference_tokens, "reference_tokens")
    if (target["attempts"] > 0
            and references
            != _current_cycle_history(target)[0]["reference_tokens"]):
        raise TaskStateError(
            "reference_tokens must preserve current attempt cycle order",
        )
    prompt = _nonempty_string(prompt, "prompt")
    model = _nonempty_string(model, "model")
    updated_at = _nonempty_string(updated_at, "updated_at")
    prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
    artifact_ordinal = len(target["attempt_history"]) + 1
    artifact_name = attempt_output_name(
        state["target_tokens"].index(target_token) + 1,
        target_token,
        artifact_ordinal,
    )
    target["attempts"] += 1
    target["status"] = "running"
    target["classification"] = classification
    target["reference_tokens"] = references
    target["prompt_sha256"] = prompt_sha256
    target["model"] = model
    target["error"] = None
    target["updated_at"] = updated_at
    target["attempt_history"].append({
        "attempt": target["attempts"],
        "artifact_ordinal": artifact_ordinal,
        "artifact_name": artifact_name,
        "run_id": state["run_id"],
        "classification": classification,
        "reference_tokens": list(target["reference_tokens"]),
        "prompt": prompt,
        "prompt_sha256": target["prompt_sha256"],
        "model": model,
        "started_at": updated_at,
        "finished_at": None,
        "outcome": "running",
        "error": None,
        "output": None,
    })
    state["current_target"] = target_token
    return state


def record_local_acceptance(
        state: dict[str, Any], *, target_token: str, artifact_name: str,
        name: str, updated_at: str,
) -> dict[str, Any]:
    """Durably record accepted local QC before any upload side effect."""
    _validate_state(state)
    target = _require_target(state, target_token)
    if target["status"] != "running" or state["current_target"] != target_token:
        raise TaskStateError(f"target attempt is not running: {target_token}")
    active = target["attempt_history"][-1]
    artifact_name = _nonempty_string(artifact_name, "artifact_name")
    selected = active
    if artifact_name != active["artifact_name"]:
        current_cycle = _current_cycle_history(target)
        selected = next(
            (entry for entry in current_cycle
             if (entry["artifact_name"] == artifact_name
                 and entry["outcome"] in {"failed", "interrupted"})),
            None,
        )
        if selected is None:
            raise TaskStateError("artifact is not in the current attempt cycle")
        if target["attempts"] < MAX_ATTEMPTS:
            raise TaskStateError("local acceptance does not match active artifact")
    expected_name = promoted_output_name(artifact_name, target_token)
    if name != expected_name:
        raise TaskStateError(
            f"output name does not match target: expected {expected_name}",
        )
    updated_at = _nonempty_string(updated_at, "updated_at")

    candidate = copy.deepcopy(state)
    accepted_target = candidate["targets"][target_token]
    accepted_active = accepted_target["attempt_history"][-1]
    accepted_selected = next(
        entry for entry in accepted_target["attempt_history"]
        if (entry["run_id"], entry["artifact_name"])
        == (selected["run_id"], selected["artifact_name"])
    )
    if accepted_selected is not accepted_active:
        _finish_running_history(
            accepted_target, outcome="failed",
            error=_sanitize_error("not selected; lower garment-reference similarity"),
            updated_at=updated_at,
        )
        accepted_selected["outcome"] = "accepted-local"
        accepted_selected["finished_at"] = updated_at
        accepted_selected["error"] = None
        accepted_selected["output"] = None
        for field in ("classification", "reference_tokens", "prompt_sha256", "model"):
            accepted_target[field] = copy.deepcopy(accepted_selected[field])
    else:
        _finish_running_history(
            accepted_target, outcome="accepted-local", error=None,
            updated_at=updated_at,
        )
    accepted_target["status"] = "accepted-local"
    accepted_target["local_acceptance"] = {
        "run_id": accepted_selected["run_id"],
        "artifact_name": artifact_name,
        "name": name,
        "accepted_at": updated_at,
    }
    accepted_target["error"] = None
    accepted_target["updated_at"] = updated_at
    candidate["current_target"] = None
    _validate_state(candidate)
    state.clear()
    state.update(candidate)
    return state


def record_success(state: dict[str, Any], *, target_token: str, file_token: str,
                   name: str, updated_at: str) -> dict[str, Any]:
    """Record upload success only after durable local QC acceptance."""
    _validate_state(state)
    target = _require_target(state, target_token)
    if target["status"] != "accepted-local" or target["local_acceptance"] is None:
        raise TaskStateError(f"target has no durable local acceptance: {target_token}")
    if not 1 <= target["attempts"] <= MAX_RECORDED_ATTEMPT:
        raise TaskStateError(f"target has no accepted attempt: {target_token}")
    if not isinstance(file_token, str) or not file_token:
        raise TaskStateError("file_token must not be empty")
    if file_token in target["stale_output_tokens"]:
        raise TaskStateError("file_token belongs to a stale source output")
    if not _output_name_matches_target(name, target_token):
        raise TaskStateError("output name does not match target token")
    if name != target["local_acceptance"]["name"]:
        raise TaskStateError("uploaded name does not match local acceptance")
    updated_at = _nonempty_string(updated_at, "updated_at")
    candidate = copy.deepcopy(state)
    successful = candidate["targets"][target_token]
    uploaded = {"file_token": file_token, "name": name}
    successful["status"] = "success"
    successful["output"] = uploaded
    history = _accepted_history_entry(successful)
    history["outcome"] = "success"
    history["finished_at"] = updated_at
    history["error"] = None
    history["output"] = uploaded
    successful["local_acceptance"] = None
    successful["error"] = None
    successful["updated_at"] = updated_at
    _validate_state(candidate)
    state.clear()
    state.update(candidate)
    return state


def reconcile_target_output(
        state: dict[str, Any], *, target_index: int,
        outputs: Iterable[dict[str, Any]], updated_at: str,
) -> dict[str, str] | None:
    """Reconcile one accepted target with its append-only Base attachment.

    A successful mapping is replay-safe. An accepted-local target advances only
    when exactly one non-stale attachment has its deterministic accepted name.
    No match leaves the caller's state untouched so it can decide whether an
    upload is still required.
    """
    _validate_state(state)
    current_outputs = _outputs_iterable(outputs)
    updated_at = _nonempty_string(updated_at, "updated_at")
    if (not isinstance(target_index, int) or isinstance(target_index, bool)
            or not 0 <= target_index < len(state["target_tokens"])):
        raise TaskStateError("target_index is outside the current target order")
    target_token = state["target_tokens"][target_index]
    target = state["targets"][target_token]
    if target["status"] == "success":
        mapped = target["output"]
        present = any(
            output["file_token"] == mapped["file_token"]
            and output["name"] == mapped["name"]
            for output in current_outputs
        )
        return copy.deepcopy(mapped) if present else None
    if target["status"] != "accepted-local":
        raise TaskStateError("target is not ready for output reconciliation")

    accepted_name = target["local_acceptance"]["name"]
    matches = [
        {"file_token": output["file_token"], "name": output["name"]}
        for output in current_outputs
        if (output["name"] == accepted_name
            and output["file_token"] not in target["stale_output_tokens"])
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise TaskStateError("accepted target has ambiguous current attachments")
    uploaded = matches[0]
    record_success(
        state, target_token=target_token, file_token=uploaded["file_token"],
        name=uploaded["name"], updated_at=updated_at,
    )
    return copy.deepcopy(uploaded)


def record_failure(state: dict[str, Any], *, target_token: str, error: str,
                   updated_at: str) -> dict[str, Any]:
    """Record a failed attempt; only the final budgeted failure is terminal."""
    _validate_state(state)
    target = _require_target(state, target_token)
    if target["status"] != "running":
        raise TaskStateError(f"target attempt is not running: {target_token}")
    if not 1 <= target["attempts"] <= MAX_RECORDED_ATTEMPT:
        raise TaskStateError(f"target has no active attempt: {target_token}")
    concise_error = _sanitize_error(error)
    updated_at = _nonempty_string(updated_at, "updated_at")
    target["status"] = (
        "failed" if target["attempts"] >= MAX_ATTEMPTS else "pending"
    )
    target["error"] = concise_error
    target["updated_at"] = updated_at
    _finish_running_history(
        target, outcome="failed", error=concise_error, updated_at=updated_at,
    )
    state["current_target"] = None
    return state


def record_error(state: dict[str, Any], *, code: str, error: str,
                 updated_at: str) -> dict[str, Any]:
    """Record a sanitized record-level failure and stop target generation."""
    _validate_state(state)
    record_error_value = _record_error_value(code, error, updated_at)
    if state["current_target"] is not None:
        target = state["targets"][state["current_target"]]
        interrupted = record_error_value["message"]
        _finish_running_history(
            target, outcome="failed", error=interrupted, updated_at=updated_at,
        )
        target["status"] = "failed"
        target["error"] = interrupted
        target["updated_at"] = updated_at
    state["current_target"] = None
    state["record_error"] = record_error_value
    return state


def record_target_plan(state: dict, target_index: int, plan: dict) -> None:
    """Persist one immutable target plan by zero-based attachment-order index."""
    _validate_state(state)
    plan_value = _json_object(plan, "target plan")
    candidate = copy.deepcopy(state)
    target = _target_at_index(candidate, target_index)
    existing = target["target_plan"]
    if existing is not None and existing != plan_value:
        raise TaskStateError("target plan is already recorded and cannot be replaced")
    if existing is None:
        target["target_plan"] = plan_value
    _validate_state(candidate)
    state.clear()
    state.update(candidate)


def record_qc_report(state: dict, target_index: int, report: dict) -> None:
    """Append an immutable automatic-QC report by zero-based target index."""
    _validate_state(state)
    report_value = _qc_report(report)
    candidate = copy.deepcopy(state)
    _target_at_index(candidate, target_index)["qc_reports"].append(report_value)
    _validate_state(candidate)
    state.clear()
    state.update(candidate)


def current_attempt_cycle(state: dict, target_index: int) -> list[dict[str, Any]]:
    """Return a detached snapshot of the target's current paid-attempt cycle."""
    _validate_state(state)
    target = _target_at_index(state, target_index)
    if not target["attempt_history"]:
        return []
    return copy.deepcopy(_current_cycle_history(target))


def record_qc_failure(
        state: dict[str, Any], *, target_token: str, error: str,
        updated_at: str,
) -> dict[str, Any]:
    """Checkpoint a recoverable QC outage without closing the paid attempt."""
    _validate_state(state)
    target = _require_target(state, target_token)
    if target["status"] != "running" or state["current_target"] != target_token:
        raise TaskStateError(f"target attempt is not running: {target_token}")
    concise_error = _sanitize_error(error)
    updated_at = _nonempty_string(updated_at, "updated_at")
    candidate = copy.deepcopy(state)
    pending = candidate["targets"][target_token]
    pending["error"] = concise_error
    pending["updated_at"] = updated_at
    _validate_state(candidate)
    state.clear()
    state.update(candidate)
    return state


def record_selection_reason(state: dict, target_index: int, reason: dict) -> None:
    """Persist one immutable final-selection reason by zero-based target index."""
    _validate_state(state)
    reason_value = _json_object(reason, "selection reason")
    candidate = copy.deepcopy(state)
    target = _target_at_index(candidate, target_index)
    existing = target["selection_reason"]
    if existing is not None and existing != reason_value:
        raise TaskStateError(
            "selection reason is already recorded and cannot be replaced",
        )
    if existing is None:
        target["selection_reason"] = reason_value
    _validate_state(candidate)
    state.clear()
    state.update(candidate)


def pending_targets(state: dict[str, Any]) -> list[str]:
    """Return current non-success targets in their input order."""
    _validate_state(state)
    if state["record_error"] is not None:
        return []
    return [token for token in state["target_tokens"]
            if state["targets"][token].get("status") == "pending"]


def pending_uploads(state: dict[str, Any]) -> list[dict[str, str]]:
    """Return current accepted bitmaps that must be uploaded before generation."""
    _validate_state(state)
    if state["record_error"] is not None:
        return []
    uploads: list[dict[str, str]] = []
    for token in state["target_tokens"]:
        target = state["targets"][token]
        if target["status"] != "accepted-local":
            continue
        accepted = target["local_acceptance"]
        uploads.append({
            "target_token": token,
            "run_id": accepted["run_id"],
            "artifact_name": accepted["artifact_name"],
            "name": accepted["name"],
        })
    return uploads


def aggregate_status(state: dict[str, Any]) -> str:
    """Return aggregate success only for present outputs on all current targets."""
    _validate_state(state)
    if state["record_error"] is not None:
        return "失败"
    success = all(
        state["targets"][token]["status"] == "success"
        and state["targets"][token]["output"] is not None
        for token in state["target_tokens"]
    )
    return "成功" if success else "失败"


def compact_detail(state: dict[str, Any]) -> str:
    """Produce minimal durable detail JSON without prompt or diagnostic blobs."""
    _validate_state(state)
    targets = {token: {key: target.get(key) for key in ("status", "classification",
               "reference_tokens", "attempts", "output", "local_acceptance",
               "prompt_sha256", "model", "error", "updated_at")}
               for token, target in state["targets"].items()}
    detail = {"schema_version": state["schema_version"], "record_id": state["record_id"],
              "run_id": state["run_id"], "started_at": state["started_at"],
              "current_target": state["current_target"],
              "record_error": state["record_error"],
              "source_tokens": state["source_tokens"], "target_tokens": state["target_tokens"],
              "targets": targets}
    return json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: str) -> Any:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise TaskStateError(f"cannot read JSON input: {error}") from error


def _json_stdout(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _state_stdout(state: dict[str, Any]) -> None:
    """Print a transition summary without local-only prompts or attempt history."""
    summary = json.loads(compact_detail(state))
    current = state["current_target"]
    summary["active_artifact"] = (
        {
            "run_id": state["targets"][current]["attempt_history"][-1]["run_id"],
            "artifact_name": state["targets"][current]["attempt_history"][-1]["artifact_name"],
        }
        if current is not None
        else None
    )
    summary["pending_uploads"] = pending_uploads(state)
    _json_stdout(summary)


def _parser() -> argparse.ArgumentParser:
    class TaskStateArgumentParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            raise TaskStateError(message)

    parser = TaskStateArgumentParser(prog="task_state")
    commands = parser.add_subparsers(dest="command", required=True)
    bind = commands.add_parser("bind")
    bind.add_argument("--base-token", required=True)
    bind.add_argument("--table-id", required=True)
    bind.add_argument("--record-id", required=True)
    bind.add_argument("--run-manifest", required=True)
    init = commands.add_parser("init")
    init.add_argument("--state", required=True); init.add_argument("--record-id", required=True)
    init.add_argument("--run-id", required=True); init.add_argument("--started-at", required=True)
    init.add_argument("--source-tokens-json", required=True); init.add_argument("--target-tokens-json", required=True)
    init_error = commands.add_parser("init-error")
    init_error.add_argument("--state", required=True); init_error.add_argument("--record-id", required=True)
    init_error.add_argument("--run-id", required=True); init_error.add_argument("--started-at", required=True)
    init_error.add_argument("--source-tokens-json", required=True); init_error.add_argument("--target-tokens-json", required=True)
    init_error.add_argument("--code", required=True); init_error.add_argument("--error-file", required=True)
    init_error.add_argument("--updated-at", required=True)
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("--source-tokens-json", required=True)
    reconcile_parser.add_argument("--state", required=True); reconcile_parser.add_argument("--target-tokens-json", required=True)
    reconcile_parser.add_argument("--outputs-json", required=True); reconcile_parser.add_argument("--updated-at", required=True)
    reconcile_parser.add_argument("--run-id", required=True); reconcile_parser.add_argument("--started-at", required=True)
    reconcile_parser.add_argument("--resumable-artifacts-json", required=True)
    reconcile_error_parser = commands.add_parser("reconcile-error")
    reconcile_error_parser.add_argument("--state", required=True)
    reconcile_error_parser.add_argument("--source-tokens-json", required=True)
    reconcile_error_parser.add_argument("--target-tokens-json", required=True)
    reconcile_error_parser.add_argument("--outputs-json", required=True)
    reconcile_error_parser.add_argument("--run-id", required=True)
    reconcile_error_parser.add_argument("--started-at", required=True)
    reconcile_error_parser.add_argument("--code", required=True)
    reconcile_error_parser.add_argument("--error-file", required=True)
    reconcile_error_parser.add_argument("--updated-at", required=True)
    retry = commands.add_parser("retry")
    retry.add_argument("--state", required=True); retry.add_argument("--updated-at", required=True)
    record_error_parser = commands.add_parser("record-error")
    record_error_parser.add_argument("--state", required=True)
    record_error_parser.add_argument("--code", required=True)
    record_error_parser.add_argument("--error-file", required=True)
    record_error_parser.add_argument("--updated-at", required=True)
    attempt = commands.add_parser("attempt")
    attempt.add_argument("--state", required=True); attempt.add_argument("--target-token", required=True)
    attempt.add_argument("--classification", required=True); attempt.add_argument("--references-json", required=True)
    attempt.add_argument("--prompt-file", required=True); attempt.add_argument("--model", required=True); attempt.add_argument("--updated-at", required=True)
    accept_local = commands.add_parser("accept-local")
    accept_local.add_argument("--state", required=True)
    accept_local.add_argument("--target-token", required=True)
    accept_local.add_argument("--artifact-name", required=True)
    accept_local.add_argument("--name", required=True)
    accept_local.add_argument("--updated-at", required=True)
    success = commands.add_parser("success")
    success.add_argument("--state", required=True); success.add_argument("--target-token", required=True)
    success.add_argument("--file-token", required=True); success.add_argument("--name", required=True); success.add_argument("--updated-at", required=True)
    failure = commands.add_parser("failure")
    failure.add_argument("--state", required=True); failure.add_argument("--target-token", required=True)
    failure.add_argument("--error-file", required=True); failure.add_argument("--updated-at", required=True)
    for command in ("target-plan", "qc-report", "selection-reason"):
        checkpoint = commands.add_parser(command)
        checkpoint.add_argument("--state", required=True)
        checkpoint.add_argument("--target-index", required=True, type=int)
        checkpoint.add_argument("--payload-json", required=True)
    for command in ("pending", "uploads", "summary", "compact"):
        item = commands.add_parser(command); item.add_argument("--state", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "bind":
            state_path = bind_manifest(
                state_root=canonical_state_root(), base_token=args.base_token,
                table_id=args.table_id, record_id=args.record_id,
                run_manifest=args.run_manifest,
            )
            _json_stdout({
                "run_manifest": str(Path(args.run_manifest)),
                "state": str(state_path),
            })
        elif args.command == "init":
            state = new_state(record_id=args.record_id, run_id=args.run_id, source_tokens=_read_json(args.source_tokens_json), target_tokens=_read_json(args.target_tokens_json), started_at=args.started_at)
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "init-error":
            state = new_record_error_state(record_id=args.record_id, run_id=args.run_id, source_tokens=_read_json(args.source_tokens_json), target_tokens=_read_json(args.target_tokens_json), started_at=args.started_at, code=args.code, error=Path(args.error_file).read_text(encoding="utf-8"), updated_at=args.updated_at)
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "reconcile":
            state = reconcile(load_state(args.state), source_tokens=_read_json(args.source_tokens_json), target_tokens=_read_json(args.target_tokens_json), outputs=_read_json(args.outputs_json), run_id=args.run_id, started_at=args.started_at, updated_at=args.updated_at, resumable_artifacts=_read_json(args.resumable_artifacts_json))
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "reconcile-error":
            state = reconcile_error(load_state(args.state), source_tokens=_read_json(args.source_tokens_json), target_tokens=_read_json(args.target_tokens_json), outputs=_read_json(args.outputs_json), run_id=args.run_id, started_at=args.started_at, code=args.code, error=Path(args.error_file).read_text(encoding="utf-8"), updated_at=args.updated_at)
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "retry":
            state = prepare_retry(load_state(args.state), updated_at=args.updated_at)
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "record-error":
            state = record_error(
                load_state(args.state), code=args.code,
                error=Path(args.error_file).read_text(encoding="utf-8"),
                updated_at=args.updated_at,
            )
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "attempt":
            state = load_state(args.state)
            begin_attempt(state, target_token=args.target_token, classification=args.classification, reference_tokens=_token_list(_read_json(args.references_json), "references"), prompt=Path(args.prompt_file).read_text(encoding="utf-8"), model=args.model, updated_at=args.updated_at)
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "accept-local":
            state = record_local_acceptance(
                load_state(args.state), target_token=args.target_token,
                artifact_name=args.artifact_name, name=args.name,
                updated_at=args.updated_at,
            )
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "success":
            state = record_success(load_state(args.state), target_token=args.target_token, file_token=args.file_token, name=args.name, updated_at=args.updated_at)
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "failure":
            state = record_failure(load_state(args.state), target_token=args.target_token, error=Path(args.error_file).read_text(encoding="utf-8"), updated_at=args.updated_at)
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command in {"target-plan", "qc-report", "selection-reason"}:
            state = load_state(args.state)
            payload = _read_json(args.payload_json)
            if args.command == "target-plan":
                record_target_plan(state, args.target_index, payload)
            elif args.command == "qc-report":
                record_qc_report(state, args.target_index, payload)
            else:
                record_selection_reason(state, args.target_index, payload)
            _atomic_write(args.state, state); _state_stdout(state)
        elif args.command == "pending":
            _json_stdout(pending_targets(load_state(args.state)))
        elif args.command == "uploads":
            _json_stdout(pending_uploads(load_state(args.state)))
        elif args.command == "summary":
            _json_stdout(aggregate_status(load_state(args.state)))
        else:
            print(compact_detail(load_state(args.state)))
        return 0
    except (TaskStateError, OSError, UnicodeError, TypeError, KeyError) as error:
        print(f"task-state error: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
