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
    garment_instances: tuple[str, ...] = ()

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
        if not self.garment_instances:
            inferred = (
                self.infographic_inventory.garment_instances
                if isinstance(self.infographic_inventory, infographic_text.InfographicInventory)
                else ("primary clothing",)
            )
            object.__setattr__(self, "garment_instances", inferred)
        _string_tuple(self.garment_instances, "garment instances", allow_empty=False)
        if len(self.garment_instances) > 12:
            raise PromptPlanError("garment instances must contain at most twelve entries")
        if self.classification == "infographic":
            if not isinstance(
                    self.infographic_inventory, infographic_text.InfographicInventory,
            ):
                raise PromptPlanError(
                    "infographic targets require a typed settled inventory",
                )
            if self.garment_instances != self.infographic_inventory.garment_instances:
                raise PromptPlanError("infographic garment instances must match settled inventory")
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
        "schema_version": 3,
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
        "garment_instances": list(plan.garment_instances),
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def deserialize_plan(value: object) -> TargetPlan:
    """Reconstruct one canonical persisted plan through the typed validators."""
    plan_fields_v2 = frozenset({
        "schema_version", "classification", "selected_references",
        "fifth_reference_reason", "garment_facts", "infographic_inventory",
    })
    plan_fields_v3 = plan_fields_v2 | {"garment_instances"}
    inventory_fields = frozenset({
        "target_token", "visible_text", "panels", "garment_instances",
        "reading_count", "adjudicated", "settled",
    })
    if not isinstance(value, dict):
        raise PromptPlanError("persisted target plan fields are invalid")
    version = value.get("schema_version")
    if (type(version) is not int or version not in (2, 3)
            or set(value) != (plan_fields_v2 if version == 2 else plan_fields_v3)):
        raise PromptPlanError("persisted target plan fields are invalid")
    references_value = value["selected_references"]
    facts_value = value["garment_facts"]
    inventory_value = value["infographic_inventory"]
    if (not isinstance(references_value, list)
            or not all(
                isinstance(reference, dict)
                and set(reference) == {"token", "role"}
                for reference in references_value
            )
            or not isinstance(facts_value, dict)
            or set(facts_value) != {"required", "forbidden"}):
        raise PromptPlanError("persisted target plan structure is invalid")

    def string_list(
            candidate: object, name: str, *, allow_empty: bool,
    ) -> tuple[str, ...]:
        if (not isinstance(candidate, list)
                or (not allow_empty and not candidate)
                or not all(isinstance(item, str) and item for item in candidate)):
            raise PromptPlanError(f"persisted {name} is invalid")
        return tuple(candidate)

    try:
        references = tuple(
            SelectedReference(
                token=reference["token"], role=reference["role"],
            )
            for reference in references_value
        )
        facts = GarmentFacts(
            required=string_list(
                facts_value["required"], "required garment facts",
                allow_empty=True,
            ),
            forbidden=string_list(
                facts_value["forbidden"], "forbidden garment facts",
                allow_empty=True,
            ),
        )
        inventory = None
        if inventory_value is not None:
            if (not isinstance(inventory_value, dict)
                    or set(inventory_value) != inventory_fields):
                raise PromptPlanError(
                    "persisted infographic inventory fields are invalid",
                )
            inventory = infographic_text.InfographicInventory(
                target_token=inventory_value["target_token"],
                visible_text=string_list(
                    inventory_value["visible_text"], "visible text",
                    allow_empty=False,
                ),
                panels=string_list(
                    inventory_value["panels"], "panels", allow_empty=False,
                ),
                garment_instances=string_list(
                    inventory_value["garment_instances"], "garment instances",
                    allow_empty=False,
                ),
                reading_count=inventory_value["reading_count"],
                adjudicated=inventory_value["adjudicated"],
                settled=inventory_value["settled"],
            )
        instances = (
            inventory.garment_instances
            if version == 2 and inventory is not None else
            ("primary clothing",) if version == 2 else
            string_list(value["garment_instances"], "garment instances", allow_empty=False)
        )
        plan = TargetPlan(
            classification=value["classification"],
            selected_references=references,
            garment_facts=facts,
            infographic_inventory=inventory,
            fifth_reference_reason=value["fifth_reference_reason"],
            garment_instances=instances,
        )
    except PromptPlanError:
        raise
    except (KeyError, TypeError, infographic_text.InventoryError) as error:
        raise PromptPlanError("persisted target plan values are invalid") from error
    if version == 3 and json.loads(serialize_plan(plan)) != value:
        raise PromptPlanError("persisted target plan is not canonical")
    return plan


def plan_digest(plan: TargetPlan) -> str:
    """Return the SHA-256 identity of the canonical plan."""
    return hashlib.sha256(serialize_plan(plan).encode("utf-8")).hexdigest()


def _base_prompt(plan: TargetPlan) -> str:
    lines = [
        "Replace the target's clothing with these ordered garment instances:",
        *(f"{index}. {instance}" for index, instance in enumerate(plan.garment_instances, 1)),
        "Remove all original clothing, including every visible remnant of the replaced garments.",
        f"Target classification: {plan.classification}.",
        "Preserve face, identity, body, skin, hair, hands, feet, shoes, and carried objects exactly.",
        "Preserve pose, composition, framing, background, lighting, shadows, and color grade exactly.",
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
