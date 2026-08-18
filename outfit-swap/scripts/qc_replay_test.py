"""Behavior tests for read-only historical visual-QC replay."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import qc_replay


def report(
        candidate: str, *, decision: str = "accept", confidence: float = 0.94,
        defects: tuple[str, ...] = (), infographic: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate": candidate,
        "scores": {
            "garment_construction": 70 if defects else 96,
            "color_material": 94,
            "garment_details": 93,
            "target_preservation": 92,
            "text_layout": 70 if infographic and defects else (96 if infographic else None),
        },
        "critical_defects": list(defects),
        "primary_defect": defects[0] if defects else None,
        "evidence": [],
        "confidence": confidence,
        "decision": decision,
    }


def candidate(
        attempt: int, name: str, expected: str, responses: list[object], *,
        defects: tuple[str, ...] = (), changed_text: tuple[str, ...] = (),
        text_exact: bool = True, panels_exact: bool = True,
) -> dict[str, object]:
    return {
        "attempt": attempt,
        "name": name,
        "images": [f"artifacts/{name}"],
        "expected_outcome": expected,
        "expected_defects": list(defects),
        "changed_text": list(changed_text),
        "text_exact": text_exact,
        "panels_exact": panels_exact,
        "system_prompt": "Return the strict visual-QC JSON schema.",
        "user_prompt": "Compare the same candidate with its approved references.",
        "offline_responses": responses,
    }


def target(
        target_id: str, candidates: list[dict[str, object]], *,
        expected_attempt: int, infographic: bool = False,
) -> dict[str, object]:
    return {
        "id": target_id,
        "infographic": infographic,
        "expected_accepted_attempt": expected_attempt,
        "candidates": candidates,
    }


def manifest(*targets: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "targets": list(targets)}


def load_payload(payload: dict[str, object]) -> qc_replay.ReplayManifest:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return qc_replay.load_manifest(path)


class ManifestValidationTest(unittest.TestCase):
    def test_loads_the_fixed_schema_and_resolves_images_from_manifest_directory(self) -> None:
        payload = manifest(target(
            "ordinary-1",
            [candidate(1, "attempt-01.png", "accept", [report("attempt-01.png")])],
            expected_attempt=1,
        ))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history" / "manifest.json"
            path.parent.mkdir()
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = qc_replay.load_manifest(path)

        self.assertEqual(loaded.targets[0].target_id, "ordinary-1")
        self.assertEqual(
            loaded.targets[0].candidates[0].images,
            ((path.parent / "artifacts" / "attempt-01.png").resolve(),),
        )

    def test_rejects_unknown_malformed_or_dimension_and_aspect_fields_at_any_depth(self) -> None:
        valid = manifest(target(
            "ordinary-1",
            [candidate(1, "attempt-01.png", "accept", [report("attempt-01.png")])],
            expected_attempt=1,
        ))
        malformed = json.loads(json.dumps(valid))
        malformed["targets"][0]["candidates"][0]["expected_outcome"] = "maybe"
        duplicate_attempt = json.loads(json.dumps(valid))
        duplicate_attempt["targets"][0]["candidates"].append(
            candidate(1, "duplicate.png", "retry", [report("duplicate.png")]),
        )
        dimension = json.loads(json.dumps(valid))
        dimension["targets"][0]["candidates"][0]["dimensions"] = [1024, 1024]
        aspect = json.loads(json.dumps(valid))
        aspect["targets"][0]["aspect_ratio"] = "1:1"
        fourth_attempt = manifest(target(
            "ordinary-1",
            [
                candidate(
                    attempt, f"attempt-{attempt}.png",
                    "accept" if attempt == 4 else "retry",
                    [report(f"attempt-{attempt}.png")],
                )
                for attempt in range(1, 5)
            ],
            expected_attempt=4,
        ))

        for payload in (
                malformed, duplicate_attempt, dimension, aspect, fourth_attempt,
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(qc_replay.ReplayError):
                    load_payload(payload)


class OfflineReplayTest(unittest.TestCase):
    def test_candidate_order_is_deterministic_by_attempt_then_name(self) -> None:
        loaded = load_payload(manifest(target(
            "ordinary-1",
            [
                candidate(2, "z-attempt.png", "accept", [report("z-attempt.png")]),
                candidate(1, "a-attempt.png", "retry", [report(
                    "a-attempt.png", decision="retry", defects=("open_front",),
                )], defects=("open_front",)),
            ],
            expected_attempt=2,
        )))

        result = qc_replay.replay_manifest(loaded)

        self.assertEqual(
            [item.name for item in result.target_results[0].candidates],
            ["a-attempt.png", "z-attempt.png"],
        )
        self.assertEqual(result.target_results[0].predicted_accepted_attempt, 2)

    def test_compares_expected_accepted_attempt_and_accounts_false_accept_and_retry(self) -> None:
        loaded = load_payload(manifest(
            target("ordinary-a", [
                candidate(1, "a-1.png", "retry", [report("a-1.png")]),
                candidate(2, "a-2.png", "accept", [report(
                    "a-2.png", decision="retry", defects=("wrong_color",),
                )]),
            ], expected_attempt=2),
            target("ordinary-b", [
                candidate(1, "b-1.png", "accept", [report("b-1.png")]),
            ], expected_attempt=1),
        ))

        result = qc_replay.replay_manifest(loaded)

        self.assertEqual(result.summary, qc_replay.ReplaySummary(
            targets=2,
            agreement_rate=0.5,
            false_accept_rate=1.0,
            false_retry_rate=0.5,
            mean_qc_calls=1.5,
        ))
        self.assertEqual(result.mean_predicted_paid_attempts, 1.0)
        self.assertEqual(result.target_results[0].expected_accepted_attempt, 2)
        self.assertEqual(result.target_results[0].predicted_accepted_attempt, 1)

    def test_invalid_and_disagreeing_responses_use_same_candidate_review_and_adjudication(self) -> None:
        invalid_then_valid = candidate(
            1, "ordinary.png", "accept",
            [{"not": "the QC schema"}, report("ordinary.png")],
        )
        disagreement = candidate(
            1, "infographic.png", "retry",
            [
                report(
                    "infographic.png", decision="retry", confidence=0.5,
                    defects=("text_changed", "layout_changed"), infographic=True,
                ),
                report("infographic.png", decision="accept", infographic=True),
                report(
                    "infographic.png", decision="retry",
                    defects=("text_changed", "layout_changed"), infographic=True,
                ),
            ],
            defects=("text_changed", "layout_changed"),
            changed_text=("FLOWY HEM",), text_exact=False, panels_exact=False,
        )
        loaded = load_payload(manifest(
            target("ordinary-1", [invalid_then_valid], expected_attempt=1),
            target("8", [
                disagreement,
                candidate(
                    2, "infographic-accepted.png", "accept",
                    [report("infographic-accepted.png", infographic=True)],
                ),
            ], expected_attempt=2, infographic=True),
        ))

        result = qc_replay.replay_manifest(loaded)

        ordinary, infographic = result.target_results
        self.assertEqual(ordinary.candidates[0].qc_calls, 2)
        self.assertFalse(ordinary.candidates[0].adjudicated)
        self.assertEqual(infographic.candidates[0].qc_calls, 3)
        self.assertTrue(infographic.candidates[0].adjudicated)
        self.assertTrue(result.gates.response_paths_valid)

    def test_critical_and_infographic_misses_are_explicit_shadow_gate_failures(self) -> None:
        targets = []
        for target_id in ("6", "7", "9"):
            targets.append(target(
                target_id,
                [
                    candidate(
                        1, f"target-{target_id}-bad.png", "retry",
                        [report(f"target-{target_id}-bad.png")],
                        defects=("open_front",),
                    ),
                    candidate(
                        2, f"target-{target_id}-accepted.png", "accept",
                        [report(f"target-{target_id}-accepted.png")],
                    ),
                ],
                expected_attempt=2,
            ))
        targets.append(target(
            "8",
            [
                candidate(
                    1, "target-8-bad.png", "retry",
                    [report("target-8-bad.png", infographic=True)],
                    defects=("text_changed", "layout_changed"),
                    changed_text=("FLOWY HEM",), text_exact=False, panels_exact=False,
                ),
                candidate(
                    2, "target-8-accepted.png", "accept",
                    [report("target-8-accepted.png", infographic=True)],
                ),
            ],
            expected_attempt=2,
            infographic=True,
        ))

        result = qc_replay.replay_manifest(load_payload(manifest(*targets)))

        self.assertEqual(len(result.gates.missed_critical_defects), 3)
        self.assertEqual(len(result.gates.missed_infographic_text_changes), 2)
        self.assertFalse(result.gates.passed)

    def test_default_cli_is_read_only_and_does_not_construct_a_live_ark_client(self) -> None:
        fixture = (
            Path(__file__).parents[1]
            / "tests" / "fixtures" / "qc-replay" / "manifest.example.json"
        )
        before = {
            path.relative_to(fixture.parent): path.read_bytes()
            for path in fixture.parent.rglob("*") if path.is_file()
        }
        with patch(
                "scripts.qc_replay.ark_vision_qc.ArkVisionClient",
                side_effect=AssertionError("offline replay must not construct Ark transport"),
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = qc_replay.main([str(fixture)])
        after = {
            path.relative_to(fixture.parent): path.read_bytes()
            for path in fixture.parent.rglob("*") if path.is_file()
        }

        self.assertEqual(exit_code, 0)
        self.assertEqual(before, after)
        rendered = json.loads(stdout.getvalue())
        self.assertEqual(rendered["mode"], "offline")
        self.assertNotEqual(
            rendered["summary"]["mean_qc_calls"],
            rendered["mean_predicted_paid_attempts"],
        )
        self.assertNotIn("images", stdout.getvalue())
        self.assertNotIn("prompt", stdout.getvalue())

    def test_live_ark_requires_the_explicit_flag_and_can_use_an_injected_client(self) -> None:
        loaded = load_payload(manifest(target(
            "ordinary-1",
            [candidate(1, "attempt-01.png", "accept", [{"offline": "invalid"}])],
            expected_attempt=1,
        )))

        class LiveClient:
            def complete_json(self, **request: object) -> str:
                self_request = request
                self.assert_request(self_request)
                return json.dumps(report("attempt-01.png"))

            @staticmethod
            def assert_request(request: dict[str, object]) -> None:
                if not request["images"]:
                    raise AssertionError("live replay must include the configured images")

        with self.assertRaises(qc_replay.ReplayError):
            qc_replay.replay_manifest(loaded, client=LiveClient())

        result = qc_replay.replay_manifest(
            loaded, live_ark=True, client=LiveClient(),
        )

        self.assertEqual(result.mode, "live-ark")
        self.assertEqual(result.target_results[0].predicted_accepted_attempt, 1)


if __name__ == "__main__":
    unittest.main()
