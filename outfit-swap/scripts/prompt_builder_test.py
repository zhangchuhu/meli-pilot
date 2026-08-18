import dataclasses
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
import prompt_builder
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


class TargetPlanTest(unittest.TestCase):
    def test_plan_is_deeply_immutable_and_serializes_canonically(self) -> None:
        inventory = {
            "visible_text": ["FLOWY HEM"],
            "panels": ["main panel"],
            "garment_instances": ["skirt"],
        }
        plan = prompt_builder.TargetPlan(
            classification="infographic",
            selected_references=("model", "hem"),
            garment_facts=prompt_builder.GarmentFacts(required=(), forbidden=()),
            infographic_inventory=inventory,
        )
        inventory["visible_text"][0] = "changed later"

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.classification = "front"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            plan.infographic_inventory["panels"] = ()  # type: ignore[index,union-attr]
        self.assertEqual(
            prompt_builder.serialize_plan(plan),
            '{"classification":"infographic","garment_facts":{"forbidden":[],"required":[]},'
            '"infographic_inventory":{"garment_instances":["skirt"],"panels":["main panel"],'
            '"visible_text":["FLOWY HEM"]},"schema_version":1,'
            '"selected_references":["model","hem"]}',
        )
        self.assertEqual(
            prompt_builder.plan_digest(plan),
            "a2272908a9c785d3d04f2490ed08c98346e92ce29479ed179cacbbfb10acc144",
        )


class PromptConstructionTest(unittest.TestCase):
    def test_initial_prompt_includes_only_evidenced_ivory_lace_guards(self) -> None:
        ivory_plan = prompt_builder.TargetPlan(
            classification="front",
            selected_references=("front", "collar", "flat", "hem"),
            garment_facts=IVORY_FACTS,
            infographic_inventory=None,
        )

        artifact = prompt_builder.build_prompt(ivory_plan, attempt=1)

        for clause in (*IVORY_FACTS.required, *IVORY_FACTS.forbidden):
            self.assertIn(clause, artifact.text)
        self.assertEqual(artifact.text, artifact.base_prompt)
        self.assertEqual(artifact.selected_references, ivory_plan.selected_references)
        self.assertIsNone(artifact.correction)

        unrelated = prompt_builder.build_prompt(prompt_builder.TargetPlan(
            classification="front",
            selected_references=("other-model", "other-flat"),
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
            selected_references=("front", "upper", "flat"),
            garment_facts=IVORY_FACTS,
            infographic_inventory=None,
        )
        initial = prompt_builder.build_prompt(plan, attempt=1)
        retry = prompt_builder.build_prompt(
            plan, attempt=2, correction=vision_qc.DefectCode.OPEN_FRONT,
        )

        expected = (
            "Retry correction: Fully close the front opening exactly as required "
            "by the evidenced garment facts."
        )
        self.assertEqual(retry.base_prompt, initial.base_prompt)
        self.assertEqual(retry.selected_references, initial.selected_references)
        self.assertEqual(retry.correction, expected)
        self.assertEqual(retry.text, initial.text + "\n" + expected)
        self.assertEqual(retry.text.count("Retry correction:"), 1)

    def test_attempt_three_uses_only_the_requested_defect_mapping(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="side",
            selected_references=("side", "upper", "flat"),
            garment_facts=IVORY_FACTS,
            infographic_inventory=None,
        )

        retry = prompt_builder.build_prompt(
            plan, attempt=3, correction=vision_qc.DefectCode.MISSING_SLEEVE,
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
            selected_references=("side", "flat"),
            garment_facts=prompt_builder.GarmentFacts(required=(), forbidden=()),
            infographic_inventory=None,
        )

        retry = prompt_builder.build_prompt(
            plan, attempt=2, correction=vision_qc.DefectCode.MISSING_SLEEVE,
        )

        for unevidenced_detail in ("cuff", "lace", "full-length"):
            self.assertNotIn(unevidenced_detail, retry.correction.lower())

    def test_attempt_contract_rejects_missing_or_initial_corrections(self) -> None:
        plan = prompt_builder.TargetPlan(
            classification="front", selected_references=("model",),
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
            prompt_builder.build_prompt(
                plan, attempt=4, correction=vision_qc.DefectCode.OPEN_FRONT,
            )


if __name__ == "__main__":
    unittest.main()
