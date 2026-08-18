import dataclasses
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
import prompt_builder
import infographic_text
import vision_qc


IVORY_FACTS = prompt_builder.GarmentFacts(
    required=(
        "Use a small pointed collar.",
        "Close the blouse completely from the small collar down the continuous pearl-button placket.",
        "Keep two complete full-length lace sleeves with cuffs.",
    ),
    forbidden=(
        "Do not create a V neckline or cardigan opening.",
        "Do not expose camisole straps or an undergarment neckline.",
    ),
)


def refs(*pairs: tuple[str, str]) -> tuple[prompt_builder.SelectedReference, ...]:
    return tuple(
        prompt_builder.SelectedReference(token=token, role=role)
        for token, role in pairs
    )


def report(
        *defects: vision_qc.DefectCode,
        primary: vision_qc.DefectCode | None = None,
) -> vision_qc.QCReport:
    return vision_qc.QCReport(
        candidate="attempt.png",
        scores=vision_qc.Scores(
            garment_construction=70,
            color_material=90,
            garment_details=80,
            target_preservation=95,
            text_layout=None,
        ),
        critical_defects=tuple(defects),
        primary_defect=primary,
        confidence=0.9,
        decision="retry",
    )


class TargetPlanTest(unittest.TestCase):
    def test_multi_garment_prompt_is_ordered_and_preserves_full_scene_contract(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="front", selected_references=refs(("a", "model")),
            garment_facts=prompt_builder.GarmentFacts(required=(), forbidden=()),
            infographic_inventory=None,
            garment_instances=("ivory lace blouse", "pleated skirt"),
        )
        text = prompt_builder.build_prompt(plan, attempt=1).text
        self.assertLess(text.index("1. ivory lace blouse"), text.index("2. pleated skirt"))
        for literal in (
            "Remove all original clothing", "face, identity, body, skin, hair, hands, feet, shoes",
            "carried objects", "pose, composition, framing, background, lighting, shadows, and color grade",
        ):
            self.assertIn(literal, text)
        self.assertEqual(
            prompt_builder.deserialize_plan(__import__("json").loads(
                prompt_builder.serialize_plan(plan),
            )).garment_instances,
            ("ivory lace blouse", "pleated skirt"),
        )

    def test_plan_is_deeply_immutable_and_serializes_canonically(self) -> None:
        inventory = {
            "visible_text": ["FLOWY HEM"],
            "panels": ["main panel"],
            "garment_instances": ["skirt"],
        }
        plan = prompt_builder.TargetPlan(
            classification="infographic",
            selected_references=refs(
                ("model", "model"), ("hem", "skirt_hem"),
            ),
            garment_facts=prompt_builder.GarmentFacts(required=(), forbidden=()),
            infographic_inventory=infographic_text.InfographicInventory(
                target_token="target-info",
                visible_text=("FLOWY HEM",),
                panels=("main panel",),
                garment_instances=("skirt",),
                reading_count=2,
                adjudicated=False,
            ),
        )
        inventory["visible_text"][0] = "changed later"

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.classification = "front"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            plan.infographic_inventory.panels[0] = "changed"  # type: ignore[index,union-attr]
        serialized = __import__("json").loads(prompt_builder.serialize_plan(plan))
        self.assertEqual(serialized["schema_version"], 3)
        self.assertEqual(serialized["garment_instances"], ["skirt"])
        self.assertEqual(len(prompt_builder.plan_digest(plan)), 64)

    def test_serialized_ordinary_and_infographic_plans_round_trip_to_typed_plans(self) -> None:
        ordinary = prompt_builder.TargetPlan(
            classification="front",
            selected_references=refs(
                ("model", "model"), ("flat", "full_outfit_flat_lay"),
            ),
            garment_facts=prompt_builder.GarmentFacts(
                required=("round neckline",), forbidden=("open front",),
            ),
            infographic_inventory=None,
        )
        infographic = prompt_builder.TargetPlan(
            classification="infographic",
            selected_references=refs(("info", "instance:skirt")),
            garment_facts=prompt_builder.GarmentFacts(required=(), forbidden=()),
            infographic_inventory=infographic_text.InfographicInventory(
                target_token="target-info", visible_text=("FLOWY HEM",),
                panels=("main panel",), garment_instances=("skirt",),
                reading_count=2, adjudicated=True,
            ),
        )

        for expected in (ordinary, infographic):
            with self.subTest(classification=expected.classification):
                persisted = __import__("json").loads(
                    prompt_builder.serialize_plan(expected),
                )
                actual = prompt_builder.deserialize_plan(persisted)
                self.assertEqual(actual, expected)
                self.assertEqual(
                    prompt_builder.serialize_plan(actual),
                    prompt_builder.serialize_plan(expected),
                )

    def test_deserialize_plan_rejects_noncanonical_or_incomplete_payloads(self) -> None:
        valid = {
            "schema_version": 2,
            "classification": "front",
            "selected_references": [{"token": "model", "role": "model"}],
            "fifth_reference_reason": None,
            "garment_facts": {"required": [], "forbidden": []},
            "infographic_inventory": None,
        }
        invalid_values = (
            {**valid, "schema_version": True},
            {**valid, "unexpected": "field"},
            {**valid, "selected_references": [{"token": "model"}]},
            {**valid, "garment_facts": {"required": [], "forbidden": [], "extra": []}},
            {**valid, "infographic_inventory": {"visible_text": ["FLOWY HEM"]}},
        )

        for payload in invalid_values:
            with self.subTest(payload=payload), self.assertRaises(
                    prompt_builder.PromptPlanError,
            ):
                prompt_builder.deserialize_plan(payload)

    def test_infographic_plan_requires_a_typed_complete_settled_inventory(self) -> None:
        selected = refs(("model", "model"))
        facts = prompt_builder.GarmentFacts(required=(), forbidden=())

        for inventory in (None, {
            "visible_text": ["FLOWY HEM"], "panels": ["main"],
        }):
            with self.subTest(inventory=inventory):
                with self.assertRaises(prompt_builder.PromptPlanError):
                    prompt_builder.TargetPlan(
                        classification="infographic",
                        selected_references=selected,
                        garment_facts=facts,
                        infographic_inventory=inventory,  # type: ignore[arg-type]
                    )

        settled = infographic_text.InfographicInventory(
            target_token="info", visible_text=("FLOWY HEM",),
            panels=("main",), garment_instances=("skirt",),
            reading_count=2, adjudicated=False,
        )
        with self.assertRaises(prompt_builder.PromptPlanError):
            prompt_builder.TargetPlan(
                classification="front",
                selected_references=selected,
                garment_facts=facts,
                infographic_inventory=settled,
            )

    def test_fifth_reference_reason_is_bound_to_a_unique_fifth_entry(self) -> None:
        entries = refs(
            ("model", "model"), ("upper", "upper_construction"),
            ("flat", "full_outfit_flat_lay"), ("hem", "skirt_hem"),
            ("waist", "hidden_waistband"),
        )
        facts = prompt_builder.GarmentFacts(required=(), forbidden=())
        plan = prompt_builder.TargetPlan(
            classification="front", selected_references=entries,
            garment_facts=facts, infographic_inventory=None,
            fifth_reference_reason="Only the fifth image proves the hidden waistband.",
        )
        self.assertEqual(plan.selected_references[4].role, "hidden_waistband")
        self.assertIn(
            '"fifth_reference_reason":"Only the fifth image proves the hidden waistband."',
            prompt_builder.serialize_plan(plan),
        )

        with self.assertRaises(prompt_builder.PromptPlanError):
            prompt_builder.TargetPlan(
                classification="front", selected_references=entries,
                garment_facts=facts, infographic_inventory=None,
            )
        with self.assertRaises(prompt_builder.PromptPlanError):
            prompt_builder.TargetPlan(
                classification="front", selected_references=entries[:4],
                garment_facts=facts, infographic_inventory=None,
                fifth_reference_reason="Not bound to an actual fifth reference.",
            )

    def test_digest_covers_roles_fifth_reason_and_complete_inventory(self) -> None:
        facts = prompt_builder.GarmentFacts(required=(), forbidden=())
        base_entries = refs(
            ("model", "model"), ("upper", "upper_construction"),
            ("flat", "full_outfit_flat_lay"), ("hem", "skirt_hem"),
        )
        base = prompt_builder.TargetPlan(
            classification="front", selected_references=base_entries,
            garment_facts=facts, infographic_inventory=None,
        )
        changed_role = prompt_builder.TargetPlan(
            classification="front",
            selected_references=(*base_entries[:3], prompt_builder.SelectedReference(
                token="hem", role="lace_detail",
            )),
            garment_facts=facts, infographic_inventory=None,
        )
        fifth_a = prompt_builder.TargetPlan(
            classification="front",
            selected_references=(*base_entries, prompt_builder.SelectedReference(
                token="waist", role="hidden_waistband",
            )),
            garment_facts=facts, infographic_inventory=None,
            fifth_reference_reason="Reason A",
        )
        fifth_b = dataclasses.replace(fifth_a, fifth_reference_reason="Reason B")
        info_a = prompt_builder.TargetPlan(
            classification="infographic",
            selected_references=refs(("info", "instance:skirt")),
            garment_facts=facts,
            infographic_inventory=infographic_text.InfographicInventory(
                target_token="target", visible_text=("FLOWY HEM",),
                panels=("main",), garment_instances=("skirt",),
                reading_count=2, adjudicated=False,
            ),
        )
        info_b = dataclasses.replace(
            info_a,
            infographic_inventory=infographic_text.InfographicInventory(
                target_token="target", visible_text=("FLOWY HEM",),
                panels=("main", "detail"),
                garment_instances=("skirt", "lace detail"),
                reading_count=2, adjudicated=True,
            ),
            garment_instances=("skirt", "lace detail"),
        )

        self.assertNotEqual(
            prompt_builder.plan_digest(base), prompt_builder.plan_digest(changed_role),
        )
        self.assertNotEqual(
            prompt_builder.plan_digest(fifth_a), prompt_builder.plan_digest(fifth_b),
        )
        self.assertNotEqual(
            prompt_builder.plan_digest(info_a), prompt_builder.plan_digest(info_b),
        )


class PromptConstructionTest(unittest.TestCase):
    def test_initial_prompt_includes_only_evidenced_ivory_lace_guards(self) -> None:
        ivory_plan = prompt_builder.TargetPlan(
            classification="front",
            selected_references=refs(
                ("front", "model"), ("collar", "upper_construction"),
                ("flat", "full_outfit_flat_lay"), ("hem", "skirt_hem"),
            ),
            garment_facts=IVORY_FACTS,
            infographic_inventory=None,
        )

        artifact = prompt_builder.build_prompt(ivory_plan, attempt=1)

        for clause in (*IVORY_FACTS.required, *IVORY_FACTS.forbidden):
            self.assertIn(clause, artifact.text)
        self.assertEqual(artifact.text, artifact.base_prompt)
        self.assertEqual(artifact.selected_references, (
            "front", "collar", "flat", "hem",
        ))
        self.assertIsNone(artifact.correction)

        unrelated = prompt_builder.build_prompt(prompt_builder.TargetPlan(
            classification="front",
            selected_references=refs(
                ("other-model", "model"),
                ("other-flat", "full_outfit_flat_lay"),
            ),
            garment_facts=prompt_builder.GarmentFacts(
                required=("Preserve the evidenced round neckline.",), forbidden=(),
            ),
            infographic_inventory=None,
        ), attempt=1)
        for leaked_fragment in (
            "small pointed collar", "pearl-button placket", "V neckline",
            "cardigan opening", "camisole straps", "full-length lace sleeves",
        ):
            self.assertNotIn(leaked_fragment, unrelated.text)

    def test_attempt_two_adds_exactly_one_controlled_correction(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="front",
            selected_references=refs(
                ("front", "model"), ("upper", "upper_construction"),
                ("flat", "full_outfit_flat_lay"),
            ),
            garment_facts=IVORY_FACTS,
            infographic_inventory=None,
        )
        initial = prompt_builder.build_prompt(plan, attempt=1)
        retry = prompt_builder.build_retry_prompt(
            plan, attempt=2, report=report(vision_qc.DefectCode.OPEN_FRONT),
        )

        expected = (
            "Retry correction: Restore the front opening exactly from the evidenced "
            "garment facts and references."
        )
        self.assertEqual(retry.base_prompt, initial.base_prompt)
        self.assertEqual(retry.selected_references, initial.selected_references)
        self.assertEqual(retry.correction, expected)
        self.assertEqual(retry.text, initial.text + "\n" + expected)
        self.assertEqual(retry.text.count("Retry correction:"), 1)

    def test_attempt_three_uses_only_the_requested_defect_mapping(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="side",
            selected_references=refs(
                ("side", "model"), ("upper", "upper_construction"),
                ("flat", "full_outfit_flat_lay"),
            ),
            garment_facts=IVORY_FACTS,
            infographic_inventory=None,
        )

        retry = prompt_builder.build_retry_prompt(
            plan, attempt=3, report=report(vision_qc.DefectCode.MISSING_SLEEVE),
        )

        self.assertEqual(
            retry.correction,
            "Retry correction: Restore every missing sleeve exactly from the evidenced garment facts and references.",
        )
        self.assertNotIn("Fully close the front opening", retry.text)
        self.assertEqual(retry.text.count("Retry correction:"), 1)

    def test_retry_correction_does_not_invent_cuffs_for_unrelated_garments(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="side",
            selected_references=refs(
                ("side", "model"), ("flat", "full_outfit_flat_lay"),
            ),
            garment_facts=prompt_builder.GarmentFacts(required=(), forbidden=()),
            infographic_inventory=None,
        )

        retry = prompt_builder.build_retry_prompt(
            plan, attempt=2, report=report(vision_qc.DefectCode.MISSING_SLEEVE),
        )

        for unevidenced_detail in ("cuff", "lace", "full-length"):
            self.assertNotIn(unevidenced_detail, retry.correction.lower())

    def test_open_front_retry_does_not_close_an_evidenced_open_cardigan(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="front",
            selected_references=refs(
                ("cardigan", "model"), ("knit", "upper_construction"),
            ),
            garment_facts=prompt_builder.GarmentFacts(
                required=("Preserve the evidenced open cardigan front.",),
                forbidden=("Do not add a buttoned placket.",),
            ),
            infographic_inventory=None,
        )

        retry = prompt_builder.build_retry_prompt(
            plan, attempt=2, report=report(vision_qc.DefectCode.OPEN_FRONT),
        )

        self.assertIn("Preserve the evidenced open cardigan front.", retry.base_prompt)
        self.assertNotIn("close", retry.correction.lower())
        self.assertNotIn("fully closed", retry.correction.lower())

    def test_retry_selects_one_highest_priority_defect_from_qc_report(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="front",
            selected_references=refs(
                ("front", "model"), ("flat", "full_outfit_flat_lay"),
            ),
            garment_facts=IVORY_FACTS,
            infographic_inventory=None,
        )
        initial = prompt_builder.build_prompt(plan, attempt=1)
        qc_report = report(
            vision_qc.DefectCode.BAD_OCCLUSION,
            vision_qc.DefectCode.OPEN_FRONT,
            vision_qc.DefectCode.WRONG_COLOR,
            primary=vision_qc.DefectCode.BAD_OCCLUSION,
        )

        retry = prompt_builder.build_retry_prompt(
            plan, attempt=2, report=qc_report,
        )

        self.assertEqual(retry.base_prompt, initial.base_prompt)
        self.assertEqual(retry.selected_references, initial.selected_references)
        self.assertEqual(retry.correction_code, vision_qc.DefectCode.OPEN_FRONT)
        self.assertEqual(retry.text.count("Retry correction:"), 1)
        self.assertNotIn("occlusion", retry.correction.lower())
        self.assertNotIn("color", retry.correction.lower())

    def test_attempt_contract_rejects_missing_or_initial_corrections(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="front", selected_references=refs(("model", "model")),
            garment_facts=prompt_builder.GarmentFacts(required=(), forbidden=()),
            infographic_inventory=None,
        )

        with self.assertRaises(prompt_builder.PromptPlanError):
            prompt_builder.build_prompt(plan, attempt=2)
        with self.assertRaises(prompt_builder.PromptPlanError):
            prompt_builder.build_prompt(
                plan, attempt=1, correction=vision_qc.DefectCode.OPEN_FRONT,
            )
        with self.assertRaises(prompt_builder.PromptPlanError):
            prompt_builder.build_retry_prompt(
                plan, attempt=4, report=report(vision_qc.DefectCode.OPEN_FRONT),
            )
        with self.assertRaises(prompt_builder.PromptPlanError):
            prompt_builder.build_retry_prompt(
                plan, attempt=2, report=report(),
            )


if __name__ == "__main__":
    unittest.main()
