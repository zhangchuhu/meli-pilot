"""Immutable target plans and deterministic edit-prompt construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

try:
    from . import infographic_text, vision_qc
except ImportError:  # pragma: no cover - direct script-directory import
    import infographic_text
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


@dataclass(frozen=True)
class GarmentFacts:
    required: tuple[str, ...]
    forbidden: tuple[str, ...]

    def __post_init__(self) -> None:
        _string_tuple(self.required, "required garment facts")
        _string_tuple(self.forbidden, "forbidden garment facts")


@dataclass(frozen=True)
class SelectedReference:
    token: str
    role: str

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise PromptPlanError("selected reference token must not be empty")
        if not isinstance(self.role, str) or not self.role:
            raise PromptPlanError("selected reference role must not be empty")


@dataclass(frozen=True)
class TargetPlan:
    classification: str
    selected_references: tuple[SelectedReference, ...]
    garment_facts: GarmentFacts
    infographic_inventory: infographic_text.InfographicInventory | None
    fifth_reference_reason: str | None = None

    def __post_init__(self) -> None:
        classifications = frozenset({
            "front", "front three-quarter", "side", "back three-quarter", "back",
            "detail or flat lay", "infographic",
        })
        if self.classification not in classifications:
            raise PromptPlanError("classification is not supported")
        if (not isinstance(self.selected_references, tuple)
                or not self.selected_references
                or len(self.selected_references) > 5
                or not all(
                    isinstance(reference, SelectedReference)
                    for reference in self.selected_references
                )):
            raise PromptPlanError(
                "selected references must be one through five typed entries",
            )
        tokens = tuple(reference.token for reference in self.selected_references)
        if len(set(tokens)) != len(tokens):
            raise PromptPlanError("selected reference tokens must be unique")
        if not isinstance(self.garment_facts, GarmentFacts):
            raise PromptPlanError("garment facts are invalid")
        if self.classification == "infographic":
            if not isinstance(
                    self.infographic_inventory, infographic_text.InfographicInventory,
            ):
                raise PromptPlanError(
                    "infographic targets require a typed settled inventory",
                )
        elif self.infographic_inventory is not None:
            raise PromptPlanError(
                "ordinary targets cannot carry an infographic inventory",
            )
        if len(self.selected_references) == 5:
            if (not isinstance(self.fifth_reference_reason, str)
                    or not self.fifth_reference_reason.strip()):
                raise PromptPlanError(
                    "a fifth reference requires a recorded reason",
                )
            first_roles = {
                reference.role for reference in self.selected_references[:4]
            }
            if self.selected_references[4].role in first_roles:
                raise PromptPlanError(
                    "the fifth reference must provide a unique evidence role",
                )
        elif self.fifth_reference_reason is not None:
            raise PromptPlanError(
                "a fifth reference reason must be bound to an actual fifth entry",
            )

    @property
    def reference_tokens(self) -> tuple[str, ...]:
        return tuple(reference.token for reference in self.selected_references)


@dataclass(frozen=True)
class PromptArtifact:
    base_prompt: str
    text: str
    selected_references: tuple[str, ...]
    correction: str | None
    correction_code: vision_qc.DefectCode | None


_CORRECTIONS: Mapping[vision_qc.DefectCode, str] = MappingProxyType({
    vision_qc.DefectCode.WRONG_COLLAR:
        "Retry correction: Restore the evidenced collar construction exactly.",
    vision_qc.DefectCode.OPEN_FRONT:
        "Retry correction: Restore the front opening exactly from the evidenced garment facts and references.",
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
        "schema_version": 2,
        "classification": plan.classification,
        "selected_references": [
            {"token": reference.token, "role": reference.role}
            for reference in plan.selected_references
        ],
        "fifth_reference_reason": plan.fifth_reference_reason,
        "garment_facts": {
            "required": list(plan.garment_facts.required),
            "forbidden": list(plan.garment_facts.forbidden),
        },
        "infographic_inventory": (
            None if plan.infographic_inventory is None
            else plan.infographic_inventory.plan_dict()
        ),
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
        + ", ".join(plan.reference_tokens) + ".",
    ]
    if plan.garment_facts.required:
        lines.append("Required garment facts:")
        lines.extend(f"- {fact}" for fact in plan.garment_facts.required)
    if plan.garment_facts.forbidden:
        lines.append("Forbidden garment structures:")
        lines.extend(f"- {fact}" for fact in plan.garment_facts.forbidden)
    if plan.infographic_inventory is not None:
        inventory = json.dumps(
            plan.infographic_inventory.to_dict(),
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
    """Build the immutable initial prompt; retries require a QC report."""
    if not isinstance(plan, TargetPlan):
        raise PromptPlanError("target plan is invalid")
    if attempt != 1 or isinstance(attempt, bool):
        raise PromptPlanError("build_prompt constructs attempt one only")
    if correction is not None:
        raise PromptPlanError("retry corrections require a QC report")

    base = _base_prompt(plan)
    return PromptArtifact(
        base_prompt=base,
        text=base,
        selected_references=plan.reference_tokens,
        correction=None,
        correction_code=None,
    )


def build_retry_prompt(
        plan: TargetPlan, *, attempt: int, report: vision_qc.QCReport,
) -> PromptArtifact:
    """Append only the highest-priority correction from a structured QC report."""
    if not isinstance(plan, TargetPlan):
        raise PromptPlanError("target plan is invalid")
    if (not isinstance(attempt, int) or isinstance(attempt, bool)
            or attempt not in (2, 3)):
        raise PromptPlanError("retry attempt must be two or three")
    if not isinstance(report, vision_qc.QCReport):
        raise PromptPlanError("retry requires a structured QC report")
    correction_code = vision_qc.correction_for(report)
    if correction_code is None:
        raise PromptPlanError("retry report contains no correctable defect")
    base = _base_prompt(plan)
    correction_text = _CORRECTIONS[correction_code]
    return PromptArtifact(
        base_prompt=base,
        text=base + "\n" + correction_text,
        selected_references=plan.reference_tokens,
        correction=correction_text,
        correction_code=correction_code,
    )
