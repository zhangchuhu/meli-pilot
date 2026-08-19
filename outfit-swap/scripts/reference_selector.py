"""Deterministic closest-angle ordering and all-qualified reference filtering."""

from __future__ import annotations

import hashlib
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
_MAX_REFERENCES = 9


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


def _primary_ordinary(
        sources: tuple[SourceEvidence, ...], classification: str,
) -> SourceEvidence:
    models = [source for source in sources if "model" in source.roles]
    if not models:
        raise ReferenceSelectionError("ordinary targets require model evidence")
    return min(
        models,
        key=lambda source: (
            _angle_distance(classification, source.angle), *_source_key(source),
        ),
    )


def _required_infographic(
        sources: tuple[SourceEvidence, ...], garment_instances: Sequence[str],
) -> tuple[list[SourceEvidence], list[str]]:
    if (not garment_instances
            or not all(isinstance(instance, str) and instance for instance in garment_instances)
            or len(set(garment_instances)) != len(garment_instances)):
        raise ReferenceSelectionError(
            "infographic garment instances must be unique non-empty strings",
        )
    selected: list[SourceEvidence] = []
    selected_roles: list[str] = []
    roles_present: set[str] = set()
    for instance in garment_instances:
        role = f"instance:{instance}"
        if role in roles_present:
            continue
        candidate = _best_for_role(sources, role, selected)
        if candidate is None:
            raise ReferenceSelectionError(
                f"infographic instance has no reference evidence: {instance}",
            )
        selected.append(candidate)
        selected_roles.append(role)
        roles_present.update(candidate.roles)
    return selected, selected_roles


def _content_digest(source: SourceEvidence) -> str | None:
    if not source.path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with source.path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ReferenceSelectionError("source evidence could not be read") from error
    return digest.hexdigest()


def _features(source: SourceEvidence) -> set[str]:
    features = {f"role:{role}" for role in source.roles if role != "size_chart"}
    if source.angle in _ANGLE_ORDER:
        features.add(f"angle:{source.angle}")
    return features


def _selection_role(source: SourceEvidence, new_features: set[str]) -> str:
    roles = sorted(feature.removeprefix("role:") for feature in new_features
                   if feature.startswith("role:") and feature != "role:model")
    if roles:
        return roles[0]
    angles = sorted(feature.removeprefix("angle:") for feature in new_features
                    if feature.startswith("angle:"))
    return f"angle:{angles[0]}" if angles else "garment_evidence"


def _role_priority(source: SourceEvidence) -> int:
    for index, role in enumerate((
            "model", "upper_construction", "full_outfit_flat_lay", "skirt_hem",
    )):
        if role in source.roles:
            return index
    return 4


def _all_qualified(
        sources: tuple[SourceEvidence, ...], selected: list[SourceEvidence],
        selected_roles: list[str], classification: str,
) -> tuple[list[SourceEvidence], list[str], list[str]]:
    covered_features = {
        feature for source in selected for feature in _features(source)
    }
    covered_roles = {
        role for source in selected for role in source.roles if role != "size_chart"
    }
    selected_tokens = {source.token for source in selected}
    digests = {
        digest for source in selected if (digest := _content_digest(source)) is not None
    }
    for source in sorted(
            sources,
            key=lambda item: (
                _angle_distance(classification, item.angle),
                -item.information_score,
                _role_priority(item),
                item.token,
            ),
    ):
        if source.token in selected_tokens or len(selected) >= _MAX_REFERENCES:
            continue
        digest = _content_digest(source)
        if digest is not None and digest in digests:
            continue
        new_features = _features(source) - covered_features
        if not new_features:
            continue
        selected.append(source)
        selected_tokens.add(source.token)
        selected_roles.append(_selection_role(source, new_features))
        covered_features.update(new_features)
        covered_roles.update(source.roles - {"size_chart"})
        if digest is not None:
            digests.add(digest)
    return selected, selected_roles, sorted(covered_roles)


def select_references(
        sources: Sequence[SourceEvidence], *, classification: str,
        garment_instances: Sequence[str] = (),
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
        primary = _primary_ordinary(eligible, classification)
        selected = [primary]
        selected_roles = ["model"]
    elif classification == "infographic":
        selected, selected_roles = _required_infographic(
            eligible, garment_instances,
        )
    elif classification == "detail or flat lay":
        if garment_instances:
            raise ReferenceSelectionError(
                "garment instances apply only to infographic targets",
            )
        if not eligible:
            raise ReferenceSelectionError("detail targets require garment evidence")
        primary = min(eligible, key=_source_key)
        selected = [primary]
        selected_roles = ["garment_evidence"]
    else:
        raise ReferenceSelectionError(f"unsupported target classification: {classification}")
    if len(selected) > _MAX_REFERENCES:
        raise ReferenceSelectionError("required evidence exceeds the nine-reference cap")
    selected, selected_roles, covered = _all_qualified(
        eligible, selected, selected_roles, classification,
    )
    if classification == "infographic":
        missing = [
            instance for instance in garment_instances
            if f"instance:{instance}" not in covered
        ]
        if missing:
            raise ReferenceSelectionError(
                f"infographic instance has no reference evidence: {missing[0]}",
            )
    return ReferenceSelection(
        selected=tuple(selected),
        roles=tuple(selected_roles),
        covered_roles=tuple(covered),
        fifth_reference_reason=None,
    )
