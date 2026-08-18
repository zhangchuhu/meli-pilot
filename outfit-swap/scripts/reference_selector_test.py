import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
import reference_selector


def evidence(
        token: str, *, angle: str = "detail", roles: tuple[str, ...],
        score: int = 50, filename: str | None = None,
) -> reference_selector.SourceEvidence:
    return reference_selector.SourceEvidence(
        token=token,
        path=Path(filename or f"{token}.png"),
        angle=angle,
        roles=frozenset(roles),
        information_score=score,
    )


class OrdinaryReferenceSelectionTest(unittest.TestCase):
    def test_front_uses_explicit_evidence_in_stable_role_order(self) -> None:
        sources = (
            evidence("size", angle="front", roles=("size_chart",), score=100,
                     filename="best-front-model.png"),
            evidence("hem", roles=("skirt_hem",), score=70),
            evidence("back-named-front", angle="back", roles=("model",), score=99,
                     filename="front-model.png"),
            evidence("flat", roles=("full_outfit_flat_lay",), score=75),
            evidence("upper", roles=("upper_construction",), score=80),
            evidence("front-model", angle="front", roles=("model",), score=60,
                     filename="zzz.png"),
        )

        forward = reference_selector.select_references(
            sources, classification="front",
        )
        reverse = reference_selector.select_references(
            tuple(reversed(sources)), classification="front",
        )

        self.assertEqual(forward.tokens, ("front-model", "upper", "flat", "hem"))
        self.assertEqual(reverse, forward)
        self.assertIsNone(forward.fifth_reference_reason)

    def test_omits_redundant_hem_and_normally_returns_three(self) -> None:
        selection = reference_selector.select_references((
            evidence("model", angle="front", roles=("model", "skirt_hem")),
            evidence("upper", roles=("upper_construction",)),
            evidence("flat", roles=("full_outfit_flat_lay",)),
            evidence("duplicate-hem", roles=("skirt_hem",), score=100),
        ), classification="front")

        self.assertEqual(selection.tokens, ("model", "upper", "flat"))

    def test_three_quarter_side_and_back_prefer_the_closest_angle(self) -> None:
        common = (
            evidence("upper", roles=("upper_construction",)),
            evidence("flat", roles=("full_outfit_flat_lay",)),
            evidence("hem", roles=("skirt_hem",)),
            evidence("front", angle="front", roles=("model",), score=95),
            evidence("front-3q", angle="front three-quarter", roles=("model",), score=40),
            evidence("side", angle="side", roles=("model",), score=40),
            evidence("back-3q", angle="back three-quarter", roles=("model",), score=40),
            evidence("back", angle="back", roles=("model",), score=40),
        )

        cases = (
            ("front three-quarter", "front-3q"),
            ("side", "side"),
            ("back three-quarter", "back-3q"),
            ("back", "back"),
        )
        for classification, expected_primary in cases:
            with self.subTest(classification=classification):
                selection = reference_selector.select_references(
                    common, classification=classification,
                )
                self.assertEqual(selection.tokens[0], expected_primary)
                self.assertEqual(selection.tokens[1:], ("upper", "flat", "hem"))

    def test_fifth_requires_and_records_nonredundant_unique_evidence(self) -> None:
        sources = (
            evidence("model", angle="front", roles=("model",)),
            evidence("upper", roles=("upper_construction",)),
            evidence("flat", roles=("full_outfit_flat_lay",)),
            evidence("hem", roles=("skirt_hem",)),
            evidence("waistband", roles=("hidden_waistband",), score=20),
        )
        requirement = reference_selector.UniqueEvidenceRequirement(
            role="hidden_waistband",
            reason="Only this reference proves the hidden waistband construction.",
        )

        selection = reference_selector.select_references(
            sources, classification="front", unique_requirement=requirement,
        )

        self.assertEqual(selection.tokens, ("model", "upper", "flat", "hem", "waistband"))
        self.assertEqual(selection.fifth_reference_reason, requirement.reason)
        with self.assertRaises(reference_selector.ReferenceSelectionError):
            reference_selector.select_references(
                sources,
                classification="front",
                unique_requirement=reference_selector.UniqueEvidenceRequirement(
                    role="hidden_waistband", reason="",
                ),
            )


class InfographicReferenceSelectionTest(unittest.TestCase):
    def test_infographic_covers_instances_in_declared_order_within_budget(self) -> None:
        sources = (
            evidence("skirt", roles=("instance:skirt",), score=70),
            evidence("combined", roles=("instance:blouse", "instance:skirt"), score=50),
            evidence("lace", roles=("instance:lace detail",), score=80),
            evidence("blouse", roles=("instance:blouse",), score=70),
            evidence("chart", roles=("size_chart", "instance:blouse"), score=100),
        )

        selection = reference_selector.select_references(
            tuple(reversed(sources)),
            classification="infographic",
            garment_instances=("blouse", "skirt", "lace detail"),
        )

        self.assertEqual(selection.tokens, ("blouse", "skirt", "lace"))
        self.assertEqual(selection.covered_roles, (
            "instance:blouse", "instance:skirt", "instance:lace detail",
        ))

    def test_infographic_rejects_an_uncovered_instance(self) -> None:
        with self.assertRaises(reference_selector.ReferenceSelectionError):
            reference_selector.select_references(
                (evidence("blouse", roles=("instance:blouse",)),),
                classification="infographic",
                garment_instances=("blouse", "skirt"),
            )


if __name__ == "__main__":
    unittest.main()
