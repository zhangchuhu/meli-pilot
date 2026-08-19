import sys
import tempfile
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

        self.assertEqual(
            forward.tokens,
            ("front-model", "back-named-front", "upper", "flat", "hem"),
        )
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
                self.assertEqual(set(selection.tokens), {
                    "upper", "flat", "hem", "front", "front-3q",
                    "side", "back-3q", "back",
                })

    def test_side_and_back_rank_complementary_evidence_by_angle_before_score(self) -> None:
        for classification in ("side", "back"):
            with self.subTest(classification=classification):
                selection = reference_selector.select_references((
                    evidence(f"{classification}-model", angle=classification,
                             roles=("model",), score=10),
                    evidence("front-model", angle="front", roles=("model",), score=100),
                    evidence(f"{classification}-upper", angle=classification,
                             roles=("upper_construction",), score=10),
                    evidence("front-upper", angle="front",
                             roles=("upper_construction",), score=100),
                    evidence(f"{classification}-flat", angle=classification,
                             roles=("full_outfit_flat_lay",), score=10),
                    evidence("front-flat", angle="front",
                             roles=("full_outfit_flat_lay",), score=100),
                    evidence(f"{classification}-hem", angle=classification,
                             roles=("skirt_hem",), score=10),
                    evidence("front-hem", angle="front", roles=("skirt_hem",), score=100),
                ), classification=classification)

                self.assertEqual(selection.tokens, (
                    f"{classification}-model", f"{classification}-upper",
                    f"{classification}-flat", f"{classification}-hem", "front-model",
                ))
                self.assertEqual(selection.roles[:4], (
                    "model", "upper_construction", "full_outfit_flat_lay", "skirt_hem",
                ))

    def test_zero_information_sources_are_excluded_without_fixed_role_requirements(self) -> None:
        selection = reference_selector.select_references((
            evidence("zero-side", angle="side", roles=("model",), score=0),
            evidence("front-model", angle="front", roles=("model",), score=10),
        ), classification="side")
        self.assertEqual(selection.tokens, ("front-model",))

    def test_all_novel_evidence_is_sent_without_a_fifth_reference_exception(self) -> None:
        sources = (
            evidence("model", angle="front", roles=("model",)),
            evidence("upper", roles=("upper_construction",)),
            evidence("flat", roles=("full_outfit_flat_lay",)),
            evidence("hem", roles=("skirt_hem",)),
            evidence("waistband", roles=("hidden_waistband",), score=20),
            evidence("back", angle="back", roles=("model",), score=10),
            evidence("duplicate-front", angle="front", roles=("model",), score=100),
        )

        selection = reference_selector.select_references(
            sources, classification="front",
        )

        self.assertEqual(selection.tokens, (
            "duplicate-front", "back", "upper", "flat", "hem", "waistband",
        ))
        self.assertIsNone(selection.fifth_reference_reason)

    def test_exact_duplicate_bytes_are_sent_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.png"
            duplicate = root / "duplicate.png"
            detail = root / "detail.png"
            primary.write_bytes(b"same-image")
            duplicate.write_bytes(b"same-image")
            detail.write_bytes(b"different-image")

            selection = reference_selector.select_references((
                evidence("primary", angle="front", roles=("model",), filename=str(primary)),
                evidence("duplicate", angle="back", roles=("model",), filename=str(duplicate)),
                evidence("detail", roles=("upper_construction",), filename=str(detail)),
            ), classification="front")

            self.assertEqual(selection.tokens, ("primary", "detail"))

    def test_reference_input_is_capped_at_nine_after_filtering(self) -> None:
        sources = [evidence("model", angle="front", roles=("model",), score=100)]
        sources.extend(
            evidence(f"detail-{index}", roles=(f"detail:{index}",), score=100 - index)
            for index in range(1, 12)
        )

        selection = reference_selector.select_references(
            tuple(sources), classification="front",
        )

        self.assertEqual(len(selection.tokens), 9)
        self.assertEqual(selection.tokens[0], "model")


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
        self.assertEqual(set(selection.covered_roles), {
            "instance:blouse", "instance:skirt", "instance:lace detail",
        })

    def test_infographic_rejects_an_uncovered_instance(self) -> None:
        with self.assertRaises(reference_selector.ReferenceSelectionError):
            reference_selector.select_references(
                (evidence("blouse", roles=("instance:blouse",)),),
                classification="infographic",
                garment_instances=("blouse", "skirt"),
            )


if __name__ == "__main__":
    unittest.main()
