"""Deterministic, evidence-based garment reference selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class ReferenceSelectionError(ValueError):
    """Raised when the supplied evidence cannot support a target plan."""


@dataclass(frozen=True)
class SourceEvidence:
    token: str
    path: Path
    angle: str
    roles: frozenset[str]
    information_score: int

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise ReferenceSelectionError("source token must be a non-empty string")
        if not isinstance(self.path, Path):
            raise ReferenceSelectionError("source path must be a Path")
        if not isinstance(self.angle, str) or not self.angle:
            raise ReferenceSelectionError("source angle must be a non-empty string")
        if (not isinstance(self.roles, frozenset) or not self.roles
                or not all(isinstance(role, str) and role for role in self.roles)):
            raise ReferenceSelectionError("source roles must be non-empty strings")
        if (not isinstance(self.information_score, int)
                or isinstance(self.information_score, bool)
                or self.information_score < 0):
            raise ReferenceSelectionError(
                "source information score must be a non-negative integer",
            )


@dataclass(frozen=True)
class UniqueEvidenceRequirement:
    role: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ReferenceSelectionError("unique evidence role must not be empty")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ReferenceSelectionError("unique evidence requires a recorded reason")


@dataclass(frozen=True)
class ReferenceSelection:
    selected: tuple[SourceEvidence, ...]
    roles: tuple[str, ...]
    covered_roles: tuple[str, ...]
    fifth_reference_reason: str | None = None

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(source.token for source in self.selected)


_ORDINARY_CLASSIFICATIONS = frozenset({
    "front", "front three-quarter", "side", "back three-quarter", "back",
})
_ANGLE_ORDER = (
    "front", "front three-quarter", "side", "back three-quarter", "back",
)
_COMPLEMENTARY_ROLES = (
    "upper_construction", "full_outfit_flat_lay", "skirt_hem",
)


def _source_key(source: SourceEvidence) -> tuple[int, str, str]:
    return (-source.information_score, source.token, source.path.as_posix())


def _eligible(sources: Sequence[SourceEvidence]) -> tuple[SourceEvidence, ...]:
    tokens = [source.token for source in sources]
    if len(set(tokens)) != len(tokens):
        raise ReferenceSelectionError("source evidence tokens must be unique")
    return tuple(
        source for source in sources
        if "size_chart" not in source.roles and source.information_score > 0
    )


def _angle_distance(target: str, source: str) -> int:
    try:
        target_index = _ANGLE_ORDER.index(target)
        source_index = _ANGLE_ORDER.index(source)
    except ValueError:
        return len(_ANGLE_ORDER) + 1
    return abs(target_index - source_index)


def _best_for_role(
        sources: Iterable[SourceEvidence], role: str,
        selected: Sequence[SourceEvidence], *, target_angle: str | None = None,
) -> SourceEvidence | None:
    selected_tokens = {source.token for source in selected}
    candidates = [
        source for source in sources
        if role in source.roles and source.token not in selected_tokens
    ]
    if not candidates:
        return None
    if target_angle is None:
        return min(candidates, key=_source_key)
    return min(
        candidates,
        key=lambda source: (
            _angle_distance(target_angle, source.angle), *_source_key(source),
        ),
    )


def _select_ordinary(
        sources: tuple[SourceEvidence, ...], classification: str,
) -> tuple[list[SourceEvidence], list[str], list[str]]:
    models = [source for source in sources if "model" in source.roles]
    if not models:
        raise ReferenceSelectionError("ordinary targets require model evidence")
    primary = min(
        models,
        key=lambda source: (
            _angle_distance(classification, source.angle), *_source_key(source),
        ),
    )
    selected = [primary]
    selected_roles = ["model"]
    covered = ["model"]
    roles_present = set(primary.roles)
    for role in _COMPLEMENTARY_ROLES:
        if role in roles_present:
            covered.append(role)
            continue
        candidate = _best_for_role(
            sources, role, selected, target_angle=classification,
        )
        if candidate is None:
            if role == "skirt_hem":
                continue
            raise ReferenceSelectionError(
                f"ordinary targets require evidence for role: {role}",
            )
        selected.append(candidate)
        selected_roles.append(role)
        covered.append(role)
        roles_present.update(candidate.roles)
    return selected, selected_roles, covered


def _select_infographic(
        sources: tuple[SourceEvidence, ...], garment_instances: Sequence[str],
) -> tuple[list[SourceEvidence], list[str], list[str]]:
    if (not garment_instances
            or not all(isinstance(instance, str) and instance for instance in garment_instances)
            or len(set(garment_instances)) != len(garment_instances)):
        raise ReferenceSelectionError(
            "infographic garment instances must be unique non-empty strings",
        )
    selected: list[SourceEvidence] = []
    selected_roles: list[str] = []
    covered: list[str] = []
    roles_present: set[str] = set()
    for instance in garment_instances:
        role = f"instance:{instance}"
        if role in roles_present:
            covered.append(role)
            continue
        candidate = _best_for_role(sources, role, selected)
        if candidate is None:
            raise ReferenceSelectionError(
                f"infographic instance has no reference evidence: {instance}",
            )
        selected.append(candidate)
        selected_roles.append(role)
        covered.append(role)
        roles_present.update(candidate.roles)
        if len(selected) > 4:
            raise ReferenceSelectionError(
                "infographic evidence exceeds the normal four-reference budget",
            )
    return selected, selected_roles, covered


def select_references(
        sources: Sequence[SourceEvidence], *, classification: str,
        garment_instances: Sequence[str] = (),
        unique_requirement: UniqueEvidenceRequirement | None = None,
) -> ReferenceSelection:
    """Select stable complementary evidence without consulting filenames."""
    if isinstance(sources, (str, bytes)) or not isinstance(classification, str):
        raise ReferenceSelectionError("reference selection input is invalid")
    try:
        source_tuple = tuple(sources)
    except TypeError as exc:
        raise ReferenceSelectionError("sources must be a sequence") from exc
    if not source_tuple or not all(isinstance(item, SourceEvidence) for item in source_tuple):
        raise ReferenceSelectionError("sources must contain SourceEvidence")
    eligible = _eligible(source_tuple)

    if classification in _ORDINARY_CLASSIFICATIONS:
        if garment_instances:
            raise ReferenceSelectionError(
                "garment instances apply only to infographic targets",
            )
        selected, selected_roles, covered = _select_ordinary(
            eligible, classification,
        )
    elif classification == "infographic":
        selected, selected_roles, covered = _select_infographic(
            eligible, garment_instances,
        )
    else:
        raise ReferenceSelectionError(f"unsupported target classification: {classification}")

    fifth_reason = None
    if unique_requirement is not None:
        if not isinstance(unique_requirement, UniqueEvidenceRequirement):
            raise ReferenceSelectionError("unique requirement is invalid")
        roles_present = {role for source in selected for role in source.roles}
        if unique_requirement.role not in roles_present:
            unique_source = _best_for_role(
                eligible, unique_requirement.role, selected,
            )
            if unique_source is None:
                raise ReferenceSelectionError(
                    "the recorded unique evidence role has no supporting source",
                )
            if len(selected) >= 5:
                raise ReferenceSelectionError("reference selection cannot exceed five")
            selected.append(unique_source)
            selected_roles.append(unique_requirement.role)
            covered.append(unique_requirement.role)
            if len(selected) == 5:
                fifth_reason = unique_requirement.reason

    if len(selected) > 4 and fifth_reason is None:
        raise ReferenceSelectionError(
            "a fifth reference requires a recorded unique-evidence reason",
        )
    return ReferenceSelection(
        selected=tuple(selected),
        roles=tuple(selected_roles),
        covered_roles=tuple(covered),
        fifth_reference_reason=fifth_reason,
    )
