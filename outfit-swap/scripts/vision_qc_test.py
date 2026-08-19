import dataclasses
import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
import vision_qc


def report_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "candidate": "attempt-01.png",
        "scores": {
            "garment_construction": 96,
            "color_material": 94,
            "garment_details": 93,
            "target_preservation": 92,
            "text_layout": None,
        },
        "critical_defects": [],
        "primary_defect": None,
        "evidence": [],
        "confidence": 0.94,
        "decision": "accept",
        "exact_text": None,
        "added_text": None,
        "missing_text": None,
        "instances_exact": None,
        "panel_count_exact": None,
        "panel_layout_exact": None,
    }
    payload.update(changes)
    return payload


def make_report(
        candidate: str, *, garment: int = 96, color: int = 94,
        details: int = 93, preservation: int = 92,
        text_layout: int | None = None, critical: tuple[vision_qc.DefectCode, ...] = (),
        primary: vision_qc.DefectCode | None = None, confidence: float = 0.94,
        decision: str = "accept",
) -> vision_qc.QCReport:
    return vision_qc.QCReport(
        candidate=candidate,
        scores=vision_qc.Scores(
            garment_construction=garment,
            color_material=color,
            garment_details=details,
            target_preservation=preservation,
            text_layout=text_layout,
        ),
        critical_defects=critical,
        primary_defect=primary,
        confidence=confidence,
        decision=decision,
    )


class ParseReportTest(unittest.TestCase):
    def parse(self, payload: dict[str, object], *, infographic: bool = False) -> vision_qc.QCReport:
        if infographic:
            payload = dict(payload)
            payload.update({
                "exact_text": True, "added_text": [], "missing_text": [],
                "instances_exact": True, "panel_count_exact": True,
                "panel_layout_exact": True,
            })
        return vision_qc.parse_report(json.dumps(payload), infographic=infographic)

    def test_parses_only_the_complete_strict_schema(self) -> None:
        report = self.parse(report_payload(
            critical_defects=["open_front"], primary_defect="open_front",
        ))

        self.assertEqual(report.candidate, "attempt-01.png")
        self.assertEqual(report.scores.garment_construction, 96)
        self.assertEqual(report.critical_defects, (vision_qc.DefectCode.OPEN_FRONT,))
        self.assertEqual(report.primary_defect, vision_qc.DefectCode.OPEN_FRONT)
        self.assertEqual(report.confidence, 0.94)

    def test_parses_all_preservation_and_secondary_detail_defect_codes(self) -> None:
        for defect in (
            "pose_changed",
            "background_changed",
            "secondary_garment_details_changed",
        ):
            with self.subTest(defect=defect):
                report = self.parse(report_payload(
                    critical_defects=[defect], primary_defect=defect,
                ))
                self.assertEqual(report.critical_defects[0].value, defect)
                self.assertEqual(report.primary_defect.value, defect)  # type: ignore[union-attr]

    def test_rejects_markdown_fences_and_trailing_prose(self) -> None:
        raw = json.dumps(report_payload())
        for invalid in (f"```json\n{raw}\n```", f"{raw}\nassessment complete"):
            with self.subTest(invalid=invalid[:12]):
                with self.assertRaises(vision_qc.VisionQCError):
                    vision_qc.parse_report(invalid, infographic=False)

    def test_rejects_missing_or_unknown_envelope_and_score_fields(self) -> None:
        missing_envelope = report_payload()
        del missing_envelope["evidence"]
        unknown_envelope = report_payload(unexpected=True)
        missing_score = report_payload()
        del missing_score["scores"]["garment_details"]  # type: ignore[index]
        unknown_score = report_payload()
        unknown_score["scores"]["extra"] = 1  # type: ignore[index]

        for payload in (missing_envelope, unknown_envelope, missing_score, unknown_score):
            with self.subTest(payload=payload):
                with self.assertRaises(vision_qc.VisionQCError):
                    self.parse(payload)

    def test_rejects_unknown_defect_codes(self) -> None:
        for field, value in (
            ("critical_defects", ["does_not_exist"]),
            ("primary_defect", "does_not_exist"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(vision_qc.VisionQCError):
                    self.parse(report_payload(**{field: value}))

    def test_requires_text_layout_nullability_for_the_candidate_kind(self) -> None:
        ordinary = report_payload()
        ordinary["scores"]["text_layout"] = 95  # type: ignore[index]
        infographic = report_payload()
        infographic["scores"]["text_layout"] = None  # type: ignore[index]

        with self.assertRaises(vision_qc.VisionQCError):
            self.parse(ordinary, infographic=False)
        with self.assertRaises(vision_qc.VisionQCError):
            self.parse(infographic, infographic=True)

    def test_rejects_out_of_range_scores_and_confidence(self) -> None:
        invalid_score = report_payload()
        invalid_score["scores"]["color_material"] = 101  # type: ignore[index]
        invalid_confidence = report_payload(confidence=1.01)
        boolean_score = report_payload()
        boolean_score["scores"]["garment_construction"] = True  # type: ignore[index]

        for payload in (invalid_score, invalid_confidence, boolean_score):
            with self.subTest(payload=payload):
                with self.assertRaises(vision_qc.VisionQCError):
                    self.parse(payload)

    def test_evidence_requires_nonempty_string_items(self) -> None:
        for evidence in (["visible seam", 7], [""], [{}]):
            with self.subTest(evidence=evidence):
                with self.assertRaisesRegex(
                        vision_qc.VisionQCError,
                        "evidence must be an array of non-empty strings",
                ):
                    self.parse(report_payload(evidence=evidence))

    def test_schema_version_must_be_a_true_integer(self) -> None:
        with self.assertRaises(vision_qc.VisionQCError):
            self.parse(report_payload(schema_version=1.0))


class EarlyAcceptTest(unittest.TestCase):
    def test_accepts_an_ordinary_candidate_at_each_threshold(self) -> None:
        report = make_report(
            "ordinary", garment=90, color=88, details=88, preservation=90,
            confidence=0.85,
        )

        self.assertTrue(vision_qc.early_accept(report, infographic=False))

    def test_rejects_ordinary_candidate_when_any_early_accept_condition_fails(self) -> None:
        cases = (
            make_report("construction", garment=89),
            make_report("color", color=87),
            make_report("details", details=87),
            make_report("preservation", preservation=89),
            make_report("confidence", confidence=0.84),
            make_report("critical", critical=(vision_qc.DefectCode.OPEN_FRONT,)),
        )

        for report in cases:
            with self.subTest(candidate=report.candidate):
                self.assertFalse(vision_qc.early_accept(report, infographic=False))

    def test_uses_local_thresholds_even_when_remote_decision_is_retry(self) -> None:
        report = vision_qc.parse_report(
            json.dumps(report_payload(candidate="remote-retry", decision="retry")),
            infographic=False,
        )

        self.assertTrue(vision_qc.early_accept(report, infographic=False))

    def test_infographic_requires_every_explicit_exactness_gate(self) -> None:
        report = dataclasses.replace(
            make_report("info", text_layout=95), exact_text=True,
            added_text=(), missing_text=(), instances_exact=True,
            panel_count_exact=True, panel_layout_exact=True,
        )

        self.assertTrue(vision_qc.early_accept(report, infographic=True))
        self.assertFalse(vision_qc.early_accept(
            make_report("low-text", text_layout=94), infographic=True,
        ))
        for change in (
            {"exact_text": False}, {"added_text": ("SALE",)},
            {"missing_text": ("FLOWY HEM",)}, {"instances_exact": False},
            {"panel_count_exact": False}, {"panel_layout_exact": False},
        ):
            with self.subTest(change=change):
                self.assertFalse(vision_qc.early_accept(
                    dataclasses.replace(report, **change), infographic=True,
                ))
        self.assertFalse(vision_qc.early_accept(
            make_report("missing-text", text_layout=None), infographic=True,
        ))


class CorrectionAndSelectionTest(unittest.TestCase):
    def test_comparative_report_requires_exact_alias_set_and_local_order(self) -> None:
        def item(alias: str, garment: int) -> dict[str, object]:
            return report_payload(
                candidate=alias,
                scores={
                    "garment_construction": garment, "color_material": 90,
                    "garment_details": 90, "target_preservation": 90,
                    "text_layout": None,
                },
            )
        valid = {"schema_version": 1, "candidates": [item("candidate_1", 90), item("candidate_2", 95)],
                 "ranking": ["candidate_2", "candidate_1"], "selected_alias": "candidate_2"}
        parsed = vision_qc.parse_comparative_report(
            json.dumps(valid), aliases=("candidate_1", "candidate_2"), infographic=False,
        )
        self.assertEqual(parsed.selected_alias, "candidate_2")
        for changed in (
            {**valid, "ranking": ["candidate_1", "candidate_2"], "selected_alias": "candidate_1"},
            {**valid, "ranking": ["candidate_2", "candidate_3"]},
        ):
            with self.assertRaises(vision_qc.VisionQCError):
                vision_qc.parse_comparative_report(
                    json.dumps(changed), aliases=("candidate_1", "candidate_2"), infographic=False,
                )

    def test_correction_uses_the_highest_priority_reported_defect(self) -> None:
        report = make_report(
            "priority",
            critical=(
                vision_qc.DefectCode.BAD_OCCLUSION,
                vision_qc.DefectCode.WRONG_COLOR,
                vision_qc.DefectCode.OPEN_FRONT,
            ),
            primary=vision_qc.DefectCode.BAD_OCCLUSION,
        )

        self.assertEqual(
            vision_qc.correction_for(report), vision_qc.DefectCode.OPEN_FRONT,
        )

    def test_correction_covers_remaining_priority_groups_and_primary_defect(self) -> None:
        self.assertEqual(
            vision_qc.correction_for(make_report(
                "clothing", critical=(
                    vision_qc.DefectCode.LAYOUT_CHANGED,
                    vision_qc.DefectCode.ORIGINAL_CLOTHING_REMAINS,
                ),
            )),
            vision_qc.DefectCode.ORIGINAL_CLOTHING_REMAINS,
        )
        self.assertEqual(
            vision_qc.correction_for(make_report(
                "text", critical=(
                    vision_qc.DefectCode.IDENTITY_CHANGED,
                    vision_qc.DefectCode.TEXT_CHANGED,
                ),
            )),
            vision_qc.DefectCode.TEXT_CHANGED,
        )
        self.assertEqual(
            vision_qc.correction_for(make_report(
                "primary", primary=vision_qc.DefectCode.WRONG_COLOR,
            )),
            vision_qc.DefectCode.WRONG_COLOR,
        )
        self.assertIsNone(vision_qc.correction_for(make_report("none")))

    def test_correction_represents_and_orders_preservation_and_secondary_details(self) -> None:
        preservation = make_report(
            "preservation",
            critical=(
                vision_qc.DefectCode.SECONDARY_GARMENT_DETAILS_CHANGED,
                vision_qc.DefectCode.ANATOMY_DISTORTION,
                vision_qc.DefectCode.BACKGROUND_CHANGED,
            ),
        )
        color = make_report(
            "color",
            critical=(
                vision_qc.DefectCode.SECONDARY_GARMENT_DETAILS_CHANGED,
                vision_qc.DefectCode.WRONG_COLOR,
            ),
        )
        pose = make_report(
            "pose",
            primary=vision_qc.DefectCode.POSE_CHANGED,
        )

        self.assertEqual(
            vision_qc.correction_for(preservation),
            vision_qc.DefectCode.BACKGROUND_CHANGED,
        )
        self.assertEqual(
            vision_qc.correction_for(color), vision_qc.DefectCode.WRONG_COLOR,
        )
        self.assertEqual(
            vision_qc.correction_for(pose), vision_qc.DefectCode.POSE_CHANGED,
        )

    def test_select_best_is_garment_first_not_a_weighted_sum(self) -> None:
        garment_winner = make_report("garment", garment=91, color=1, details=1, preservation=1)
        otherwise_stronger = make_report("otherwise", garment=90, color=100, details=100, preservation=100)

        selected = vision_qc.select_best(
            (otherwise_stronger, garment_winner), {"garment": 2, "otherwise": 1},
        )

        self.assertIs(selected, garment_winner)

    def test_select_best_applies_each_remaining_score_before_attempt_order(self) -> None:
        color_winner = make_report("color", color=95, details=1, preservation=1)
        detail_winner = make_report("detail", color=94, details=100, preservation=100)
        preservation_winner = make_report("preservation", details=94, preservation=95)
        lower_preservation = make_report("lower-preservation", details=94, preservation=94)

        self.assertIs(
            vision_qc.select_best((detail_winner, color_winner), {"color": 2, "detail": 1}),
            color_winner,
        )
        self.assertIs(
            vision_qc.select_best((lower_preservation, preservation_winner), {
                "preservation": 2, "lower-preservation": 1,
            }),
            preservation_winner,
        )

    def test_select_best_uses_text_layout_before_attempt_and_earlier_attempt_only_on_a_true_tie(self) -> None:
        earlier = make_report("earlier", text_layout=95)
        later_better_text = make_report("later-text", text_layout=96)
        later_tied = make_report("later-tied", text_layout=95)

        self.assertIs(
            vision_qc.select_best((earlier, later_better_text), {"earlier": 1, "later-text": 2}),
            later_better_text,
        )
        self.assertIs(
            vision_qc.select_best((later_tied, earlier), {"earlier": 1, "later-tied": 2}),
            earlier,
        )


if __name__ == "__main__":
    unittest.main()
