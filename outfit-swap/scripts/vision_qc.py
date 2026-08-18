"""Pure structured visual-QC validation, acceptance, and selection policy."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


class VisionQCError(ValueError):
    """Raised when a visual-QC report does not match the fixed schema."""


class DefectCode(str, Enum):
    WRONG_COLLAR = "wrong_collar"
    OPEN_FRONT = "open_front"
    MISSING_SLEEVE = "missing_sleeve"
    WRONG_SKIRT_SHAPE = "wrong_skirt_shape"
    WRONG_COLOR = "wrong_color"
    ORIGINAL_CLOTHING_REMAINS = "original_clothing_remains"
    IDENTITY_CHANGED = "identity_changed"
    POSE_CHANGED = "pose_changed"
    ACCESSORY_CHANGED = "accessory_changed"
    BACKGROUND_CHANGED = "background_changed"
    BAD_OCCLUSION = "bad_occlusion"
    ANATOMY_DISTORTION = "anatomy_distortion"
    SECONDARY_GARMENT_DETAILS_CHANGED = "secondary_garment_details_changed"
    MISSING_INFOGRAPHIC_INSTANCE = "missing_infographic_instance"
    TEXT_CHANGED = "text_changed"
    LAYOUT_CHANGED = "layout_changed"


@dataclass(frozen=True)
class Scores:
    garment_construction: int
    color_material: int
    garment_details: int
    target_preservation: int
    text_layout: int | None


@dataclass(frozen=True)
class QCReport:
    candidate: str
    scores: Scores
    critical_defects: tuple[DefectCode, ...]
    primary_defect: DefectCode | None
    confidence: float
    decision: str


_REPORT_FIELDS = frozenset({
    "schema_version", "candidate", "scores", "critical_defects",
    "primary_defect", "evidence", "confidence", "decision",
})
_SCORE_FIELDS = frozenset({
    "garment_construction", "color_material", "garment_details",
    "target_preservation", "text_layout",
})
_DECISIONS = frozenset({"accept", "reject", "retry"})
_DEFECT_PRIORITY = (
    DefectCode.WRONG_COLLAR,
    DefectCode.OPEN_FRONT,
    DefectCode.MISSING_SLEEVE,
    DefectCode.WRONG_SKIRT_SHAPE,
    DefectCode.ORIGINAL_CLOTHING_REMAINS,
    DefectCode.MISSING_INFOGRAPHIC_INSTANCE,
    DefectCode.TEXT_CHANGED,
    DefectCode.LAYOUT_CHANGED,
    DefectCode.IDENTITY_CHANGED,
    DefectCode.POSE_CHANGED,
    DefectCode.ACCESSORY_CHANGED,
    DefectCode.BACKGROUND_CHANGED,
    DefectCode.ANATOMY_DISTORTION,
    DefectCode.BAD_OCCLUSION,
    DefectCode.WRONG_COLOR,
    DefectCode.SECONDARY_GARMENT_DETAILS_CHANGED,
)
_DEFECT_RANK = {defect: index for index, defect in enumerate(_DEFECT_PRIORITY)}


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise VisionQCError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_nonstandard_json_number(_value: str) -> None:
    raise VisionQCError("non-standard JSON number")


def _require_exact_fields(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise VisionQCError(f"{name} fields do not match the fixed schema")
    return value


def _score(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise VisionQCError(f"{name} must be an integer from 0 through 100")
    return value


def _defect(value: Any, name: str) -> DefectCode:
    if not isinstance(value, str):
        raise VisionQCError(f"{name} must be a defect code")
    try:
        return DefectCode(value)
    except ValueError as exc:
        raise VisionQCError(f"unknown defect code: {value}") from exc


def parse_report(raw: str, *, infographic: bool) -> QCReport:
    """Parse the exact version-one visual-QC report schema without I/O."""
    if not isinstance(raw, str):
        raise VisionQCError("visual-QC report must be JSON text")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonstandard_json_number,
        )
    except (json.JSONDecodeError, TypeError, VisionQCError) as exc:
        if isinstance(exc, VisionQCError):
            raise
        raise VisionQCError("visual-QC report must contain exactly one JSON value") from exc

    report = _require_exact_fields(payload, _REPORT_FIELDS, "report")
    if report["schema_version"] != 1 or isinstance(report["schema_version"], bool):
        raise VisionQCError("unsupported visual-QC schema version")
    candidate = report["candidate"]
    if not isinstance(candidate, str) or not candidate:
        raise VisionQCError("candidate must be a non-empty string")

    raw_scores = _require_exact_fields(report["scores"], _SCORE_FIELDS, "scores")
    text_layout = raw_scores["text_layout"]
    if infographic:
        text_layout = _score(text_layout, "text_layout")
    elif text_layout is not None:
        raise VisionQCError("text_layout must be null for an ordinary candidate")
    scores = Scores(
        garment_construction=_score(raw_scores["garment_construction"], "garment_construction"),
        color_material=_score(raw_scores["color_material"], "color_material"),
        garment_details=_score(raw_scores["garment_details"], "garment_details"),
        target_preservation=_score(raw_scores["target_preservation"], "target_preservation"),
        text_layout=text_layout,
    )

    raw_defects = report["critical_defects"]
    if not isinstance(raw_defects, list):
        raise VisionQCError("critical_defects must be a JSON array")
    defects = tuple(_defect(value, "critical_defects") for value in raw_defects)
    if len(set(defects)) != len(defects):
        raise VisionQCError("critical_defects must not contain duplicates")
    raw_primary = report["primary_defect"]
    if raw_primary is not None and not isinstance(raw_primary, str):
        raise VisionQCError("primary_defect must be a defect code or null")
    primary = None if raw_primary is None else _defect(raw_primary, "primary_defect")

    if not isinstance(report["evidence"], list):
        raise VisionQCError("evidence must be a JSON array")
    confidence = report["confidence"]
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise VisionQCError("confidence must be a finite number from 0 through 1")
    decision = report["decision"]
    if not isinstance(decision, str) or decision not in _DECISIONS:
        raise VisionQCError("decision must be accept, reject, or retry")
    return QCReport(
        candidate=candidate,
        scores=scores,
        critical_defects=defects,
        primary_defect=primary,
        confidence=float(confidence),
        decision=decision,
    )


def early_accept(
        report: QCReport, *, infographic: bool, text_exact: bool = True,
        panels_exact: bool = True,
) -> bool:
    """Return whether a first- or second-attempt report clears all gates."""
    scores = report.scores
    if not (
        scores.garment_construction >= 90
        and scores.color_material >= 88
        and scores.garment_details >= 88
        and scores.target_preservation >= 90
        and report.confidence >= 0.85
        and not report.critical_defects
    ):
        return False
    if not infographic:
        return scores.text_layout is None
    return (
        scores.text_layout is not None
        and scores.text_layout >= 95
        and text_exact
        and panels_exact
    )


def correction_for(report: QCReport) -> DefectCode | None:
    """Choose one correction target using the fixed policy priority order."""
    defects = set(report.critical_defects)
    if report.primary_defect is not None:
        defects.add(report.primary_defect)
    if not defects:
        return None
    return min(
        defects,
        key=lambda defect: (
            _DEFECT_RANK[defect],
            defect != report.primary_defect,
        ),
    )


def select_best(
        reports: Sequence[QCReport], attempt_by_candidate: Mapping[str, int],
) -> QCReport:
    """Select the third-attempt candidate lexicographically, garment first."""
    if not reports:
        raise VisionQCError("cannot select a candidate from no reports")
    candidates = [report.candidate for report in reports]
    if len(set(candidates)) != len(candidates):
        raise VisionQCError("candidate reports must be unique")

    def rank(report: QCReport) -> tuple[int, int, int, int, int, int]:
        attempt = attempt_by_candidate.get(report.candidate)
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0:
            raise VisionQCError(f"candidate has no valid attempt number: {report.candidate}")
        scores = report.scores
        return (
            scores.garment_construction,
            scores.color_material,
            scores.garment_details,
            scores.target_preservation,
            -1 if scores.text_layout is None else scores.text_layout,
            -attempt,
        )

    return max(reports, key=rank)
