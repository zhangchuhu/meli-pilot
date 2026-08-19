#!/usr/bin/env python3
"""Read-only historical visual-QC replay with opt-in live Ark evaluation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts import ark_vision_qc, vision_qc
except ImportError:  # pragma: no cover - direct script execution
    import ark_vision_qc  # type: ignore[no-redef]
    import vision_qc  # type: ignore[no-redef]


class ReplayError(ValueError):
    """Raised when a replay manifest or QC path is not safe and deterministic."""


@dataclass(frozen=True)
class ReplaySummary:
    targets: int
    agreement_rate: float
    false_accept_rate: float
    false_retry_rate: float
    mean_qc_calls: float


@dataclass(frozen=True)
class ReplayCandidate:
    attempt: int
    name: str
    images: tuple[Path, ...]
    expected_outcome: str
    expected_defects: tuple[vision_qc.DefectCode, ...]
    changed_text: tuple[str, ...]
    text_exact: bool
    panels_exact: bool
    offline_responses: tuple[str, ...]


@dataclass(frozen=True)
class ReplayTarget:
    target_id: str
    infographic: bool
    expected_accepted_attempt: int
    candidates: tuple[ReplayCandidate, ...]


@dataclass(frozen=True)
class ReplayManifest:
    path: Path
    targets: tuple[ReplayTarget, ...]


@dataclass(frozen=True)
class CandidateReplayResult:
    attempt: int
    candidate_alias: str
    expected_outcome: str
    predicted_outcome: str
    reported_defects: tuple[str, ...]
    qc_calls: int
    review_count: int
    adjudicated: bool


@dataclass(frozen=True)
class TargetReplayResult:
    target_id: str
    infographic: bool
    expected_accepted_attempt: int
    predicted_accepted_attempt: int | None
    predicted_paid_attempts: int
    candidates: tuple[CandidateReplayResult, ...]


@dataclass(frozen=True)
class ShadowGates:
    missed_critical_defects: tuple[str, ...]
    missed_infographic_text_changes: tuple[str, ...]
    false_retry_within_limit: bool
    response_paths_valid: bool

    @property
    def passed(self) -> bool:
        return (
            not self.missed_critical_defects
            and not self.missed_infographic_text_changes
            and self.false_retry_within_limit
            and self.response_paths_valid
        )


@dataclass(frozen=True)
class ReplayResult:
    mode: str
    summary: ReplaySummary
    mean_predicted_paid_attempts: float
    gates: ShadowGates
    target_results: tuple[TargetReplayResult, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic sanitized report without prompts or image paths."""
        return {
            "schema_version": 1,
            "mode": self.mode,
            "summary": asdict(self.summary),
            "mean_predicted_paid_attempts": self.mean_predicted_paid_attempts,
            "gates": {
                **asdict(self.gates),
                "passed": self.gates.passed,
            },
            "targets": [
                {
                    "id": target.target_id,
                    "infographic": target.infographic,
                    "expected_accepted_attempt": target.expected_accepted_attempt,
                    "predicted_accepted_attempt": target.predicted_accepted_attempt,
                    "predicted_paid_attempts": target.predicted_paid_attempts,
                    "candidates": [asdict(candidate) for candidate in target.candidates],
                }
                for target in self.target_results
            ],
        }


_MANIFEST_FIELDS = frozenset({"schema_version", "targets"})
_TARGET_FIELDS = frozenset({
    "id", "infographic", "expected_accepted_attempt", "candidates",
})
_CANDIDATE_FIELDS = frozenset({
    "attempt", "name", "images", "expected_outcome", "expected_defects",
    "changed_text", "text_exact", "panels_exact", "offline_responses",
})
_OUTCOMES = frozenset({"accept", "retry"})
_FORBIDDEN_FEATURE_FIELDS = frozenset({
    "aspect", "aspectratio", "dimension", "dimensions", "height",
    "imagedimensions", "outputsize", "pixeldimensions", "pixelheight",
    "pixelwidth", "ratio", "resolution", "width",
})
_CONSTRUCTION_DEFECTS = frozenset({
    vision_qc.DefectCode.WRONG_COLLAR,
    vision_qc.DefectCode.OPEN_FRONT,
    vision_qc.DefectCode.MISSING_SLEEVE,
    vision_qc.DefectCode.WRONG_SKIRT_SHAPE,
})
_INFOGRAPHIC_CHANGE_DEFECTS = frozenset({
    vision_qc.DefectCode.TEXT_CHANGED,
    vision_qc.DefectCode.LAYOUT_CHANGED,
})
_HISTORICAL_CONSTRUCTION_TARGETS = ("6", "7", "9")
_HISTORICAL_INFOGRAPHIC_TARGET = "8"
_HISTORICAL_CHANGED_LITERAL = "FLOWY HEM"
_MAX_PAID_ATTEMPTS = 3
_SAFE_PATH_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SUPPORTED_IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
_REPLAY_SYSTEM_PROMPT = (
    "You are the visual quality reviewer. Return exactly one JSON object using "
    "visual-QC schema version 1. Base every score and defect only on visible "
    "image content."
)
_FORBIDDEN_REQUEST_TEXT = re.compile(
    r"\d+\s*[x×]\s*\d+|\d+\s*:\s*\d+|\bresolution\b|\bpixel\w*\b|"
    r"\bwidth\b|\bheight\b|\bdimensions?\b|\baspect\b|\bratio\b",
    re.IGNORECASE,
)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ReplayError(f"duplicate manifest field: {name}")
        result[name] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ReplayError("manifest contains a non-standard JSON number")


def _object(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ReplayError(f"{label} fields do not match the fixed replay schema")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(f"{label} must be a non-empty string")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReplayError(f"{label} must be a positive integer")
    return value


def _string_list(value: Any, label: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ReplayError(f"{label} must be a JSON array of strings")
    result = tuple(_nonempty_string(item, label) for item in value)
    if len(set(result)) != len(result):
        raise ReplayError(f"{label} must not contain duplicates")
    return result


def _reject_dimension_features(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = (
                "".join(character for character in key.casefold() if character.isalnum())
                if isinstance(key, str) else ""
            )
            if normalized in _FORBIDDEN_FEATURE_FIELDS:
                raise ReplayError("dimension and aspect-ratio fields are forbidden")
            _reject_dimension_features(item)
    elif isinstance(value, list):
        for item in value:
            _reject_dimension_features(item)


def _response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"), sort_keys=True,
        )
    raise ReplayError("offline_responses entries must be JSON objects or strings")


def _safe_image_path(value: str, root: Path) -> Path:
    relative = Path(value)
    if (
            relative.is_absolute()
            or not relative.parts
            or any(
                part in {".", ".."} or _SAFE_PATH_PART.fullmatch(part) is None
                for part in relative.parts
            )
            or relative.suffix.casefold() not in _SUPPORTED_IMAGE_SUFFIXES
    ):
        raise ReplayError("candidate images must be safe relative image paths")
    approved_root = root.resolve()
    resolved = (approved_root / relative).resolve(strict=False)
    if not resolved.is_relative_to(approved_root):
        raise ReplayError("candidate image resolves outside the approved manifest root")
    return resolved


def _candidate(value: Any, root: Path, *, infographic: bool) -> ReplayCandidate:
    raw = _object(value, _CANDIDATE_FIELDS, "candidate")
    attempt = _positive_integer(raw["attempt"], "candidate attempt")
    name = _nonempty_string(raw["name"], "candidate name")
    if (
            _SAFE_PATH_PART.fullmatch(name) is None
            or Path(name).name != name
            or Path(name).suffix.casefold() not in _SUPPORTED_IMAGE_SUFFIXES
    ):
        raise ReplayError("candidate name must be a sanitized image basename")
    raw_images = _string_list(raw["images"], "candidate images", allow_empty=False)
    images = tuple(_safe_image_path(image, root) for image in raw_images)
    if name not in {image.name for image in images}:
        raise ReplayError("candidate name must identify one configured candidate image")
    outcome = raw["expected_outcome"]
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        raise ReplayError("expected_outcome must be accept or retry")
    raw_defects = _string_list(
        raw["expected_defects"], "expected_defects", allow_empty=True,
    )
    try:
        defects = tuple(vision_qc.DefectCode(defect) for defect in raw_defects)
    except ValueError as exc:
        raise ReplayError("expected_defects contains an unknown defect code") from exc
    changed_text = _string_list(
        raw["changed_text"], "changed_text", allow_empty=True,
    )
    if changed_text and not infographic:
        raise ReplayError("changed_text is valid only for infographic targets")
    text_exact = raw["text_exact"]
    panels_exact = raw["panels_exact"]
    if not isinstance(text_exact, bool) or not isinstance(panels_exact, bool):
        raise ReplayError("text_exact and panels_exact must be booleans")
    semantic_changes: set[vision_qc.DefectCode] = set()
    if infographic:
        if not text_exact or changed_text:
            semantic_changes.add(vision_qc.DefectCode.TEXT_CHANGED)
        if not panels_exact:
            semantic_changes.add(vision_qc.DefectCode.LAYOUT_CHANGED)
    elif not text_exact or not panels_exact:
        raise ReplayError("ordinary candidates cannot declare text or panel changes")
    annotated_changes = set(defects) & _INFOGRAPHIC_CHANGE_DEFECTS
    if semantic_changes != annotated_changes:
        raise ReplayError(
            "infographic expected_defects must match text and panel change semantics",
        )
    responses = raw["offline_responses"]
    if not isinstance(responses, list) or not responses:
        raise ReplayError("offline_responses must be a non-empty JSON array")
    offline_responses = tuple(_response_text(response) for response in responses)
    return ReplayCandidate(
        attempt=attempt,
        name=name,
        images=images,
        expected_outcome=outcome,
        expected_defects=defects,
        changed_text=changed_text,
        text_exact=text_exact,
        panels_exact=panels_exact,
        offline_responses=offline_responses,
    )


def _target(value: Any, root: Path) -> ReplayTarget:
    raw = _object(value, _TARGET_FIELDS, "target")
    target_id = _nonempty_string(raw["id"], "target id")
    infographic = raw["infographic"]
    if not isinstance(infographic, bool):
        raise ReplayError("target infographic must be a boolean")
    expected_attempt = _positive_integer(
        raw["expected_accepted_attempt"], "expected_accepted_attempt",
    )
    raw_candidates = raw["candidates"]
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ReplayError("target candidates must be a non-empty JSON array")
    if len(raw_candidates) > _MAX_PAID_ATTEMPTS:
        raise ReplayError("target candidates exceed the three-attempt paid budget")
    candidates = tuple(sorted(
        (_candidate(item, root, infographic=infographic) for item in raw_candidates),
        key=lambda item: (item.attempt, item.name),
    ))
    attempts = tuple(candidate.attempt for candidate in candidates)
    names = tuple(candidate.name for candidate in candidates)
    if len(set(attempts)) != len(attempts) or len(set(names)) != len(names):
        raise ReplayError("candidate attempts and names must be unique within a target")
    if attempts != tuple(range(1, len(candidates) + 1)):
        raise ReplayError("candidate attempts must be contiguous and start at one")
    accepted = [
        candidate.attempt for candidate in candidates
        if candidate.expected_outcome == "accept"
    ]
    if accepted != [expected_attempt]:
        raise ReplayError(
            "exactly one accepted candidate must match expected_accepted_attempt",
        )
    return ReplayTarget(
        target_id=target_id,
        infographic=infographic,
        expected_accepted_attempt=expected_attempt,
        candidates=candidates,
    )


def load_manifest(path: str | Path) -> ReplayManifest:
    """Load and strictly validate a replay manifest without changing any files."""
    manifest_path = Path(path).resolve()
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
        payload = json.loads(
            raw_text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except ReplayError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError("replay manifest could not be read as strict JSON") from exc
    _reject_dimension_features(payload)
    raw = _object(payload, _MANIFEST_FIELDS, "manifest")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ReplayError("unsupported replay manifest schema version")
    raw_targets = raw["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ReplayError("manifest targets must be a non-empty JSON array")
    targets = tuple(_target(item, manifest_path.parent) for item in raw_targets)
    target_ids = tuple(target.target_id for target in targets)
    if len(set(target_ids)) != len(target_ids):
        raise ReplayError("target ids must be unique")
    return ReplayManifest(
        path=manifest_path,
        targets=targets,
    )


def _candidate_alias(attempt: int) -> str:
    return f"candidate-{attempt:02d}"


def _replay_user_prompt(candidate_alias: str, *, infographic: bool) -> str:
    kind_instruction = (
        "It is an infographic; compare all visible text literally and verify the "
        "panel arrangement."
        if infographic else
        "It is an ordinary fashion image."
    )
    return (
        f"Review opaque candidate '{candidate_alias}' against every preceding approved "
        f"reference image. {kind_instruction} Return the candidate field exactly as "
        f"'{candidate_alias}'."
    )


def _safe_response_candidate(
        raw: str, *, manifest_name: str, candidate_alias: str,
) -> str:
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ReplayError):
        return raw
    if not isinstance(payload, dict) or payload.get("candidate") != manifest_name:
        return raw
    payload["candidate"] = candidate_alias
    if "exact_text" not in payload:
        infographic = isinstance(payload.get("scores"), dict) and payload["scores"].get("text_layout") is not None
        payload.update({
            "exact_text": True if infographic else None,
            "added_text": [] if infographic else None,
            "missing_text": [] if infographic else None,
            "instances_exact": True if infographic else None,
            "panel_count_exact": True if infographic else None,
            "panel_layout_exact": True if infographic else None,
        })
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), sort_keys=True,
    )


def _assert_safe_request_prompts(system_prompt: str, user_prompt: str) -> None:
    if (
            not isinstance(system_prompt, str)
            or not isinstance(user_prompt, str)
            or not system_prompt
            or not user_prompt
            or _FORBIDDEN_REQUEST_TEXT.search(system_prompt) is not None
            or _FORBIDDEN_REQUEST_TEXT.search(user_prompt) is not None
    ):
        raise ReplayError("Ark replay prompt contains a forbidden sizing feature")


class _OfflineClient:
    def __init__(
            self, candidate: ReplayCandidate, *, candidate_alias: str,
            system_prompt: str,
    ) -> None:
        self._responses = [
            _safe_response_candidate(
                raw,
                manifest_name=candidate.name,
                candidate_alias=candidate_alias,
            )
            for raw in candidate.offline_responses
        ]
        self._expected_images = candidate.images
        self._expected_system_prompt = system_prompt
        self.calls = 0

    @property
    def remaining(self) -> int:
        return len(self._responses)

    def complete_json(
            self, *, system_prompt: str, user_prompt: str,
            images: Sequence[Path],
    ) -> str:
        if (
                tuple(Path(path) for path in images) != self._expected_images
                or system_prompt != self._expected_system_prompt
        ):
            raise ReplayError("offline QC left the same-candidate review path")
        if not isinstance(user_prompt, str) or not user_prompt:
            raise ReplayError("offline QC request prompt is invalid")
        self.calls += 1
        if not self._responses:
            raise ReplayError("offline QC response sequence was exhausted")
        return self._responses.pop(0)


class _SafePromptClient:
    def __init__(self, delegate: ark_vision_qc.VisionClient) -> None:
        self._delegate = delegate

    def complete_json(
            self, *, system_prompt: str, user_prompt: str,
            images: Sequence[Path],
    ) -> str:
        _assert_safe_request_prompts(system_prompt, user_prompt)
        return self._delegate.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
        )


def _review(
        candidate: ReplayCandidate, *, infographic: bool,
        live_ark: bool, client: ark_vision_qc.VisionClient | None,
) -> tuple[ark_vision_qc.QCReviewResult, int]:
    candidate_alias = _candidate_alias(candidate.attempt)
    system_prompt = _REPLAY_SYSTEM_PROMPT
    user_prompt = _replay_user_prompt(candidate_alias, infographic=infographic)
    if live_ark:
        if client is None:
            raise ReplayError("live Ark replay has no client")
        review_client = client
        offline_client = None
    else:
        offline_client = _OfflineClient(
            candidate,
            candidate_alias=candidate_alias,
            system_prompt=system_prompt,
        )
        review_client = offline_client
    safe_client = _SafePromptClient(review_client)
    try:
        result = ark_vision_qc.review_candidate(
            safe_client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=candidate.images,
            candidate=candidate_alias,
            infographic=infographic,
        )
    except ark_vision_qc.ArkVisionError as exc:
        raise ReplayError("QC replay failed after same-candidate review") from exc
    calls = result.request_count if offline_client is None else offline_client.calls
    if offline_client is not None and offline_client.remaining:
        raise ReplayError("offline_responses contains unused responses")
    return result, calls


def _validate_live_images(manifest: ReplayManifest) -> None:
    approved_root = manifest.path.parent.resolve()
    for target in manifest.targets:
        for candidate in target.candidates:
            for image in candidate.images:
                try:
                    resolved = image.resolve(strict=True)
                except OSError as exc:
                    raise ReplayError("live Ark image is missing or inaccessible") from exc
                if not resolved.is_relative_to(approved_root) or not resolved.is_file():
                    raise ReplayError("live Ark images must be regular approved fixture files")


def _coverage_misses(
        manifest: ReplayManifest,
        target_results: tuple[TargetReplayResult, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    results = {
        target.target_id: {
            candidate.attempt: candidate for candidate in target.candidates
        }
        for target in target_results
    }
    critical_misses: list[str] = []
    infographic_misses: list[str] = []
    for target in manifest.targets:
        result_candidates = results[target.target_id]
        for candidate in target.candidates:
            reported = set(result_candidates[candidate.attempt].reported_defects)
            for defect in set(candidate.expected_defects) & _CONSTRUCTION_DEFECTS:
                if defect.value not in reported:
                    critical_misses.append(
                        f"target {target.target_id} attempt {candidate.attempt}: {defect.value}",
                    )
            if target.infographic:
                expected_changes: set[vision_qc.DefectCode] = set()
                if not candidate.text_exact or candidate.changed_text:
                    expected_changes.add(vision_qc.DefectCode.TEXT_CHANGED)
                if not candidate.panels_exact:
                    expected_changes.add(vision_qc.DefectCode.LAYOUT_CHANGED)
                for defect in expected_changes:
                    if defect.value not in reported:
                        infographic_misses.append(
                            f"target {target.target_id} attempt {candidate.attempt}: {defect.value}",
                        )

    by_id = {target.target_id: target for target in manifest.targets}
    for target_id in _HISTORICAL_CONSTRUCTION_TARGETS:
        target = by_id.get(target_id)
        if target is None or not any(
                set(candidate.expected_defects) & _CONSTRUCTION_DEFECTS
                for candidate in target.candidates
        ):
            critical_misses.append(
                f"historical target {target_id}: missing construction-defect fixture",
            )
    infographic = by_id.get(_HISTORICAL_INFOGRAPHIC_TARGET)
    target_eight_covered = infographic is not None and infographic.infographic and any(
        _HISTORICAL_CHANGED_LITERAL in candidate.changed_text
        and _INFOGRAPHIC_CHANGE_DEFECTS <= set(candidate.expected_defects)
        for candidate in infographic.candidates
    )
    if not target_eight_covered:
        infographic_misses.append(
            "historical target 8: missing FLOWY HEM text/layout fixture",
        )
    return tuple(critical_misses), tuple(infographic_misses)


def replay_manifest(
        manifest: ReplayManifest, *, live_ark: bool = False,
        client: ark_vision_qc.VisionClient | None = None,
) -> ReplayResult:
    """Replay adjudicated history without task-state, Base, or generation writes."""
    if not isinstance(manifest, ReplayManifest):
        raise ReplayError("a validated replay manifest is required")
    if not isinstance(live_ark, bool):
        raise ReplayError("live_ark must be a boolean")
    if client is not None and not live_ark:
        raise ReplayError("a live client requires explicit live_ark=True")
    if live_ark:
        _validate_live_images(manifest)

    target_results: list[TargetReplayResult] = []
    expected_retries = 0
    false_accepts = 0
    accepted_ordinary = 0
    false_retries = 0
    total_qc_calls = 0
    accepted_attempt_matches = 0
    total_predicted_paid_attempts = 0

    for target in manifest.targets:
        candidate_results: list[CandidateReplayResult] = []
        predicted_accepted_attempt: int | None = None
        for candidate in target.candidates:
            review, calls = _review(
                candidate,
                infographic=target.infographic,
                live_ark=live_ark,
                client=client,
            )
            predicted_accept = vision_qc.early_accept(
                review.report,
                infographic=target.infographic,
                text_exact=candidate.text_exact,
                panels_exact=candidate.panels_exact,
            )
            predicted_outcome = "accept" if predicted_accept else "retry"
            if predicted_accept and predicted_accepted_attempt is None:
                predicted_accepted_attempt = candidate.attempt
            if candidate.expected_outcome == "retry":
                expected_retries += 1
                false_accepts += int(predicted_accept)
            elif not target.infographic:
                accepted_ordinary += 1
                false_retries += int(not predicted_accept)
            total_qc_calls += calls
            candidate_results.append(CandidateReplayResult(
                attempt=candidate.attempt,
                candidate_alias=_candidate_alias(candidate.attempt),
                expected_outcome=candidate.expected_outcome,
                predicted_outcome=predicted_outcome,
                reported_defects=tuple(
                    defect.value for defect in review.report.critical_defects
                ),
                qc_calls=calls,
                review_count=review.review_count,
                adjudicated=review.adjudicated,
            ))
        accepted_attempt_matches += int(
            predicted_accepted_attempt == target.expected_accepted_attempt
        )
        predicted_paid_attempts = (
            predicted_accepted_attempt
            if predicted_accepted_attempt is not None
            else min(_MAX_PAID_ATTEMPTS, target.candidates[-1].attempt + 1)
        )
        total_predicted_paid_attempts += predicted_paid_attempts
        target_results.append(TargetReplayResult(
            target_id=target.target_id,
            infographic=target.infographic,
            expected_accepted_attempt=target.expected_accepted_attempt,
            predicted_accepted_attempt=predicted_accepted_attempt,
            predicted_paid_attempts=predicted_paid_attempts,
            candidates=tuple(candidate_results),
        ))

    targets = len(target_results)
    false_retry_rate = false_retries / accepted_ordinary if accepted_ordinary else 0.0
    summary = ReplaySummary(
        targets=targets,
        agreement_rate=accepted_attempt_matches / targets,
        false_accept_rate=false_accepts / expected_retries if expected_retries else 0.0,
        false_retry_rate=false_retry_rate,
        mean_qc_calls=total_qc_calls / targets,
    )
    frozen_results = tuple(target_results)
    critical_misses, infographic_misses = _coverage_misses(
        manifest, frozen_results,
    )
    return ReplayResult(
        mode="live-ark" if live_ark else "offline",
        summary=summary,
        mean_predicted_paid_attempts=total_predicted_paid_attempts / targets,
        gates=ShadowGates(
            missed_critical_defects=critical_misses,
            missed_infographic_text_changes=infographic_misses,
            false_retry_within_limit=false_retry_rate <= 0.10,
            response_paths_valid=True,
        ),
        target_results=frozen_results,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay historical visual-QC decisions without mutating task state.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--live-ark",
        action="store_true",
        help="explicitly replace stored responses with live Ark vision requests",
    )
    return parser


def _live_response_archive() -> Path:
    raw_root = os.environ.get("OUTFIT_SWAP_RUNS_ROOT")
    runs_root = (
        Path(raw_root).expanduser()
        if isinstance(raw_root, str) and raw_root.strip()
        else Path.home() / ".codex" / "state" / "outfit-swap" / "runs"
    ).resolve(strict=False)
    run = (
        runs_root / "qc-replay"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    return run / "ark-responses"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        client = (
            ark_vision_qc.ArkVisionClient(
                response_archive_dir=_live_response_archive(),
            )
            if args.live_ark else None
        )
        result = replay_manifest(
            manifest, live_ark=args.live_ark, client=client,
        )
    except ReplayError as exc:
        print(f"qc replay error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(
        result.to_dict(), ensure_ascii=True, allow_nan=False,
        indent=2, sort_keys=True,
    ))
    return 0 if result.gates.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
