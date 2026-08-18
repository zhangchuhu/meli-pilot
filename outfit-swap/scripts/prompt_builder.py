"""Immutable target plans and deterministic edit-prompt construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

try:
    from . import vision_qc
except ImportError:  # pragma: no cover - direct script-directory import
    import vision_qc


class PromptPlanError(ValueError):
    """Raised when a target plan or attempt contract is invalid."""


def _string_tuple(value: object, name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if (not isinstance(value, tuple)
            or not all(isinstance(item, str) and item for item in value)
            or len(set(value)) != len(value)
            or (not allow_empty and not value)):
        raise PromptPlanError(f"{name} must be a tuple of unique non-empty strings")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise PromptPlanError("inventory keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise PromptPlanError("inventory contains a non-JSON value")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class GarmentFacts:
    required: tuple[str, ...]
    forbidden: tuple[str, ...]

    def __post_init__(self) -> None:
        _string_tuple(self.required, "required garment facts")
        _string_tuple(self.forbidden, "forbidden garment facts")


@dataclass(frozen=True)
class TargetPlan:
    classification: str
    selected_references: tuple[str, ...]
    garment_facts: GarmentFacts
    infographic_inventory: dict | None

    def __post_init__(self) -> None:
        if not isinstance(self.classification, str) or not self.classification:
            raise PromptPlanError("classification must be a non-empty string")
        _string_tuple(
            self.selected_references, "selected references", allow_empty=False,
        )
        if not isinstance(self.garment_facts, GarmentFacts):
            raise PromptPlanError("garment facts are invalid")
        if self.infographic_inventory is not None:
            if not isinstance(self.infographic_inventory, Mapping):
                raise PromptPlanError("infographic inventory must be a JSON object")
            object.__setattr__(
                self, "infographic_inventory", _freeze(self.infographic_inventory),
            )


@dataclass(frozen=True)
class PromptArtifact:
    base_prompt: str
    text: str
    selected_references: tuple[str, ...]
    correction: str | None


_CORRECTIONS: Mapping[vision_qc.DefectCode, str] = MappingProxyType({
    vision_qc.DefectCode.WRONG_COLLAR:
        "Retry correction: Restore the evidenced collar construction exactly.",
    vision_qc.DefectCode.OPEN_FRONT:
        "Retry correction: Fully close the front opening exactly as required by the evidenced garment facts.",
    vision_qc.DefectCode.MISSING_SLEEVE:
        "Retry correction: Restore every missing sleeve exactly from the evidenced garment facts and references.",
    vision_qc.DefectCode.WRONG_SKIRT_SHAPE:
        "Retry correction: Restore the evidenced skirt silhouette and hem construction.",
    vision_qc.DefectCode.WRONG_COLOR:
        "Retry correction: Match the evidenced garment color without changing material appearance.",
    vision_qc.DefectCode.ORIGINAL_CLOTHING_REMAINS:
        "Retry correction: Remove every remaining visible part of the original replaced clothing.",
    vision_qc.DefectCode.IDENTITY_CHANGED:
        "Retry correction: Restore the target person's original identity exactly.",
    vision_qc.DefectCode.POSE_CHANGED:
        "Retry correction: Restore the target person's original pose exactly.",
    vision_qc.DefectCode.ACCESSORY_CHANGED:
        "Retry correction: Restore every original accessory exactly.",
    vision_qc.DefectCode.BACKGROUND_CHANGED:
        "Retry correction: Restore the original background and framing exactly.",
    vision_qc.DefectCode.BAD_OCCLUSION:
        "Retry correction: Repair garment-body occlusion while preserving pose and anatomy.",
    vision_qc.DefectCode.ANATOMY_DISTORTION:
        "Retry correction: Restore the target person's original anatomy and proportions.",
    vision_qc.DefectCode.SECONDARY_GARMENT_DETAILS_CHANGED:
        "Retry correction: Restore all evidenced details of garments not being replaced.",
    vision_qc.DefectCode.MISSING_INFOGRAPHIC_INSTANCE:
        "Retry correction: Restore the missing garment instance from the settled infographic inventory.",
    vision_qc.DefectCode.TEXT_CHANGED:
        "Retry correction: Restore every literal text item exactly from the settled infographic inventory.",
    vision_qc.DefectCode.LAYOUT_CHANGED:
        "Retry correction: Restore the settled infographic panel layout exactly.",
})


def serialize_plan(plan: TargetPlan) -> str:
    """Return canonical JSON suitable for durable state and hashing."""
    if not isinstance(plan, TargetPlan):
        raise PromptPlanError("target plan is invalid")
    payload = {
        "schema_version": 1,
        "classification": plan.classification,
        "selected_references": list(plan.selected_references),
        "garment_facts": {
            "required": list(plan.garment_facts.required),
            "forbidden": list(plan.garment_facts.forbidden),
        },
        "infographic_inventory": _thaw(plan.infographic_inventory),
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def plan_digest(plan: TargetPlan) -> str:
    """Return the SHA-256 identity of the canonical plan."""
    return hashlib.sha256(serialize_plan(plan).encode("utf-8")).hexdigest()


def _base_prompt(plan: TargetPlan) -> str:
    lines = [
        "Edit only the clothing in the target image.",
        f"Target classification: {plan.classification}.",
        "Preserve the target person's identity, pose, anatomy, accessories, framing, and background.",
        "Use garment references in this exact order: "
        + ", ".join(plan.selected_references) + ".",
    ]
    if plan.garment_facts.required:
        lines.append("Required garment facts:")
        lines.extend(f"- {fact}" for fact in plan.garment_facts.required)
    if plan.garment_facts.forbidden:
        lines.append("Forbidden garment structures:")
        lines.extend(f"- {fact}" for fact in plan.garment_facts.forbidden)
    if plan.infographic_inventory is not None:
        inventory = json.dumps(
            _thaw(plan.infographic_inventory),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        lines.append("Preserve this settled infographic inventory exactly: " + inventory)
    return "\n".join(lines)


def build_prompt(
        plan: TargetPlan, *, attempt: int,
        correction: vision_qc.DefectCode | None = None,
) -> PromptArtifact:
    """Build attempt one or one deterministic single-correction retry."""
    if not isinstance(plan, TargetPlan):
        raise PromptPlanError("target plan is invalid")
    if (not isinstance(attempt, int) or isinstance(attempt, bool)
            or attempt not in (1, 2, 3)):
        raise PromptPlanError("attempt must be one, two, or three")
    if attempt == 1 and correction is not None:
        raise PromptPlanError("the initial prompt cannot contain a retry correction")
    if attempt > 1 and correction is None:
        raise PromptPlanError("a retry requires exactly one defect correction")
    if correction is not None and not isinstance(correction, vision_qc.DefectCode):
        raise PromptPlanError("retry correction must be a known defect code")

    base = _base_prompt(plan)
    correction_text = None if correction is None else _CORRECTIONS[correction]
    text = base if correction_text is None else base + "\n" + correction_text
    return PromptArtifact(
        base_prompt=base,
        text=text,
        selected_references=plan.selected_references,
        correction=correction_text,
    )
