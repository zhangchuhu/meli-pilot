import io
import hashlib
import json
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).parent))
import task_state


class TaskStateTest(unittest.TestCase):
    def make_state(
            self, targets: list[str] | None = None,
            sources: list[str] | None = None,
    ) -> dict:
        return task_state.new_state(
            record_id="rec_1",
            run_id="run_1",
            source_tokens=sources or ["box_s1"],
            target_tokens=targets or ["box_t1"],
            started_at="2026-08-15T10:00:00+08:00",
        )

    def begin(
            self, state: dict, token: str = "box_t1",
            references: list[str] | None = None,
    ) -> None:
        task_state.begin_attempt(
            state,
            target_token=token,
            classification="front",
            reference_tokens=references or ["box_s1"],
            prompt="put the outfit on the person",
            model="image-model",
            updated_at="2026-08-15T10:01:00+08:00",
        )

    def reconcile(
            self, state: dict, *, target_tokens: object, outputs: object,
            updated_at: str,
    ) -> None:
        task_state.reconcile(
            state, source_tokens=state["source_tokens"],
            target_tokens=target_tokens, outputs=outputs,
            run_id=state["run_id"], started_at=state["started_at"],
            updated_at=updated_at,
        )

    def accept_local(self, state: dict, token: str = "box_t1") -> None:
        active = state["targets"][token]["attempt_history"][-1]
        task_state.record_local_acceptance(
            state, target_token=token, artifact_name=active["artifact_name"],
            name=task_state.output_name(state["target_tokens"].index(token) + 1, token),
            updated_at="2026-08-15T10:01:30+08:00",
        )

    def legacy_finished_history(self, count: int, outcome: str) -> list[dict]:
        entries = []
        for number in range(1, count + 1):
            prompt = f"legacy prompt {number}"
            entries.append({
                "attempt": number,
                "artifact_ordinal": number,
                "artifact_name": task_state.attempt_output_name(1, "box_t1", number),
                "run_id": "run_1",
                "classification": "front",
                "reference_tokens": ["box_s1"],
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "model": "image-model",
                "started_at": f"2026-08-16T10:0{number}:00+08:00",
                "finished_at": f"2026-08-16T10:0{number}:30+08:00",
                "outcome": outcome,
                "error": "legacy rejection" if outcome == "failed" else None,
                "output": None,
            })
        return entries

    def make_first_of_three_locally_accepted(self) -> tuple[dict, str]:
        state = self.make_state()
        self.begin(state)
        first = state["targets"]["box_t1"]["attempt_history"][-1]["artifact_name"]
        task_state.record_failure(
            state, target_token="box_t1", error="reject 1",
            updated_at="2026-08-17T10:01:30+08:00",
        )
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error="reject 2",
            updated_at="2026-08-17T10:02:30+08:00",
        )
        self.begin(state)
        task_state.record_local_acceptance(
            state, target_token="box_t1", artifact_name=first,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T10:03:30+08:00",
        )
        return state, first

    def assert_persisted_state_rejected_without_mutation(self, state: dict) -> None:
        serialized = json.dumps(state, sort_keys=True)
        before = json.loads(serialized)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(serialized, encoding="utf-8")
            with self.assertRaisesRegex(task_state.TaskStateError, "attempt history"):
                task_state.load_state(path)
            self.assertEqual(path.read_text(encoding="utf-8"), serialized)
        self.assertEqual(state, before)

    def test_output_name_is_stable_and_indexed(self) -> None:
        self.assertEqual(
            task_state.output_name(2, "box_target_123"),
            "look-02-2f76eb8ea9d4.png",
        )

    def test_attempt_output_names_are_unique_and_never_accepted_names(self) -> None:
        names = [
            task_state.attempt_output_name(2, "box_target_123", attempt)
            for attempt in (1, 2, 3)
        ]
        self.assertEqual(names, [
            "attempt-02-2f76eb8ea9d4-01.png",
            "attempt-02-2f76eb8ea9d4-02.png",
            "attempt-02-2f76eb8ea9d4-03.png",
        ])
        self.assertEqual(len(set(names)), 3)
        self.assertNotIn(task_state.output_name(2, "box_target_123"), names)

    def test_third_attempt_can_accept_first_current_cycle_artifact(self) -> None:
        state = self.make_state()
        self.begin(state)
        first = state["targets"]["box_t1"]["attempt_history"][-1]["artifact_name"]
        task_state.record_failure(
            state, target_token="box_t1", error="visual reject 1",
            updated_at="2026-08-17T10:01:30+08:00",
        )
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error="visual reject 2",
            updated_at="2026-08-17T10:02:30+08:00",
        )
        self.begin(state)
        task_state.record_local_acceptance(
            state, target_token="box_t1", artifact_name=first,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T10:03:30+08:00",
        )
        target = state["targets"]["box_t1"]
        self.assertEqual(target["local_acceptance"]["artifact_name"], first)
        self.assertEqual(
            [entry["outcome"] for entry in target["attempt_history"]],
            ["accepted-local", "failed", "failed"],
        )
        self.assertIsNone(state["current_target"])

    def test_success_updates_selected_history_not_latest_history(self) -> None:
        state, _first = self.make_first_of_three_locally_accepted()
        task_state.record_success(
            state, target_token="box_t1", file_token="box_out",
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T10:04:00+08:00",
        )
        history = state["targets"]["box_t1"]["attempt_history"]
        self.assertEqual(
            [entry["outcome"] for entry in history],
            ["success", "failed", "failed"],
        )
        self.assertEqual(history[0]["output"]["file_token"], "box_out")
        self.assertIsNone(history[-1]["output"])

    def test_persisted_second_attempt_historical_acceptance_is_rejected(self) -> None:
        state, _first = self.make_first_of_three_locally_accepted()
        target = state["targets"]["box_t1"]
        target["attempts"] = 2
        target["attempt_history"] = target["attempt_history"][:2]

        self.assert_persisted_state_rejected_without_mutation(state)

    def test_persisted_historical_acceptance_allows_interrupted_later_attempt(self) -> None:
        state, _first = self.make_first_of_three_locally_accepted()
        target = state["targets"]["box_t1"]
        target["attempt_history"][-1]["outcome"] = "interrupted"
        target["attempt_history"][-1]["error"] = "interrupted after initiation"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            loaded = task_state.load_state(path)

        self.assertEqual(loaded, state)

    def test_interrupted_middle_attempt_does_not_poison_failed_candidate_selection(self) -> None:
        state = self.make_state()
        self.begin(state)
        first = state["targets"]["box_t1"]["attempt_history"][-1]["artifact_name"]
        task_state.record_failure(
            state, target_token="box_t1", error="first visual rejection",
            updated_at="2026-08-17T10:01:30+08:00",
        )
        self.begin(state)
        self.reconcile(
            state, target_tokens=["box_t1"], outputs=[],
            updated_at="2026-08-17T10:02:30+08:00",
        )
        self.begin(state)

        task_state.record_local_acceptance(
            state, target_token="box_t1", artifact_name=first,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T10:03:30+08:00",
        )

        target = state["targets"]["box_t1"]
        self.assertEqual(target["status"], "accepted-local")
        self.assertEqual(
            [entry["outcome"] for entry in target["attempt_history"]],
            ["accepted-local", "interrupted", "failed"],
        )
        self.assertEqual(target["local_acceptance"]["artifact_name"], first)

    def test_revalidated_interrupted_candidate_is_eligible_for_selection(self) -> None:
        state = self.make_state()
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error="first visual rejection",
            updated_at="2026-08-17T10:01:30+08:00",
        )
        self.begin(state)
        interrupted = state["targets"]["box_t1"]["attempt_history"][-1][
            "artifact_name"
        ]
        self.reconcile(
            state, target_tokens=["box_t1"], outputs=[],
            updated_at="2026-08-17T10:02:30+08:00",
        )
        self.begin(state)

        task_state.record_local_acceptance(
            state, target_token="box_t1", artifact_name=interrupted,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T10:03:30+08:00",
        )

        target = state["targets"]["box_t1"]
        self.assertEqual(target["status"], "accepted-local")
        self.assertEqual(
            [entry["outcome"] for entry in target["attempt_history"]],
            ["failed", "accepted-local", "failed"],
        )
        self.assertEqual(target["local_acceptance"]["artifact_name"], interrupted)

    def test_persisted_historical_selection_rejects_conflicting_later_outcomes(self) -> None:
        for outcome in ("running", "accepted-local", "success"):
            with self.subTest(outcome=outcome):
                state, _first = self.make_first_of_three_locally_accepted()
                later = state["targets"]["box_t1"]["attempt_history"][-1]
                later["outcome"] = outcome
                if outcome == "running":
                    later["finished_at"] = None
                    later["error"] = None
                elif outcome == "accepted-local":
                    later["error"] = None
                else:
                    later["error"] = None
                    later["output"] = {
                        "file_token": "box_conflict",
                        "name": task_state.output_name(1, "box_t1"),
                    }

                self.assert_persisted_state_rejected_without_mutation(state)

    def test_attempt_two_and_three_reject_ordered_reference_drift_without_mutation(self) -> None:
        state = self.make_state(sources=["box_s1", "box_s2"])
        expected_references = ["box_s1", "box_s2"]
        self.begin(state, references=expected_references)
        task_state.record_failure(
            state, target_token="box_t1", error="first rejection",
            updated_at="2026-08-17T10:01:30+08:00",
        )

        before_second = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "reference"):
            self.begin(state, references=["box_s2", "box_s1"])
        self.assertEqual(state, before_second)

        self.begin(state, references=expected_references)
        task_state.record_failure(
            state, target_token="box_t1", error="second rejection",
            updated_at="2026-08-17T10:02:30+08:00",
        )
        before_third = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "reference"):
            self.begin(state, references=["box_s2", "box_s1"])
        self.assertEqual(state, before_third)
        self.assertEqual(state["targets"]["box_t1"]["attempts"], 2)

    def test_live_historical_selection_rejects_mismatched_ordered_references_without_mutation(self) -> None:
        state = self.make_state(sources=["box_s1", "box_s2"])
        references = ["box_s1", "box_s2"]
        for attempt in range(2):
            self.begin(state, references=references)
            task_state.record_failure(
                state, target_token="box_t1", error=f"rejection {attempt + 1}",
                updated_at=f"2026-08-17T10:0{attempt + 1}:30+08:00",
            )
        self.begin(state, references=references)
        target = state["targets"]["box_t1"]
        selected = target["attempt_history"][0]
        selected["reference_tokens"] = ["box_s2", "box_s1"]
        before = json.loads(json.dumps(state))

        with self.assertRaisesRegex(task_state.TaskStateError, "attempt history"):
            task_state.record_local_acceptance(
                state, target_token="box_t1",
                artifact_name=selected["artifact_name"],
                name=task_state.output_name(1, "box_t1"),
                updated_at="2026-08-17T10:03:30+08:00",
            )

        self.assertEqual(state, before)

    def test_persisted_historical_acceptance_and_success_reject_mismatched_ordered_references(self) -> None:
        for terminal_status in ("accepted-local", "success"):
            with self.subTest(terminal_status=terminal_status):
                state = self.make_state(sources=["box_s1", "box_s2"])
                references = ["box_s1", "box_s2"]
                self.begin(state, references=references)
                first = state["targets"]["box_t1"]["attempt_history"][-1][
                    "artifact_name"
                ]
                task_state.record_failure(
                    state, target_token="box_t1", error="first rejection",
                    updated_at="2026-08-17T10:01:30+08:00",
                )
                self.begin(state, references=references)
                task_state.record_failure(
                    state, target_token="box_t1", error="second rejection",
                    updated_at="2026-08-17T10:02:30+08:00",
                )
                self.begin(state, references=references)
                task_state.record_local_acceptance(
                    state, target_token="box_t1", artifact_name=first,
                    name=task_state.output_name(1, "box_t1"),
                    updated_at="2026-08-17T10:03:30+08:00",
                )
                if terminal_status == "success":
                    task_state.record_success(
                        state, target_token="box_t1", file_token="box_out",
                        name=task_state.output_name(1, "box_t1"),
                        updated_at="2026-08-17T10:04:00+08:00",
                    )
                state["targets"]["box_t1"]["attempt_history"][0][
                    "reference_tokens"
                ] = ["box_s2", "box_s1"]
                serialized = json.dumps(state, sort_keys=True)

                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "state.json"
                    path.write_text(serialized, encoding="utf-8")
                    with self.assertRaisesRegex(task_state.TaskStateError, "attempt history"):
                        task_state.load_state(path)
                    self.assertEqual(path.read_text(encoding="utf-8"), serialized)

    def test_new_retry_and_source_change_cycles_establish_new_ordered_references(self) -> None:
        retry_state = self.make_state(sources=["box_s1", "box_s2"])
        self.begin(retry_state, references=["box_s1", "box_s2"])
        task_state.record_failure(
            retry_state, target_token="box_t1", error="old retry cycle",
            updated_at="2026-08-17T10:01:30+08:00",
        )
        task_state.prepare_retry(
            retry_state, updated_at="2026-08-17T11:00:00+08:00",
        )
        self.begin(retry_state, references=["box_s2", "box_s1"])
        retry_cycle = retry_state["targets"]["box_t1"]["attempt_history"][-1]
        self.assertEqual(retry_cycle["attempt"], 1)
        self.assertEqual(retry_cycle["reference_tokens"], ["box_s2", "box_s1"])

        source_state = self.make_state(sources=["box_s1", "box_s2"])
        self.begin(source_state, references=["box_s1", "box_s2"])
        task_state.record_failure(
            source_state, target_token="box_t1", error="old source cycle",
            updated_at="2026-08-17T10:01:30+08:00",
        )
        task_state.reconcile(
            source_state, source_tokens=["box_s1", "box_s3"],
            target_tokens=["box_t1"], outputs=[], run_id="run_2",
            started_at="2026-08-17T12:00:00+08:00",
            updated_at="2026-08-17T12:00:01+08:00",
        )
        self.begin(source_state, references=["box_s3", "box_s1"])
        source_cycle = source_state["targets"]["box_t1"]["attempt_history"][-1]
        self.assertEqual(source_cycle["attempt"], 1)
        self.assertEqual(source_cycle["reference_tokens"], ["box_s3", "box_s1"])

    def test_latest_selection_remains_readable_when_older_cycle_references_differ(self) -> None:
        for terminal_status in ("accepted-local", "success"):
            with self.subTest(terminal_status=terminal_status):
                state = self.make_state(sources=["box_s1", "box_s2"])
                references = ["box_s1", "box_s2"]
                for attempt in range(2):
                    self.begin(state, references=references)
                    task_state.record_failure(
                        state, target_token="box_t1",
                        error=f"legacy rejection {attempt + 1}",
                        updated_at=f"2026-08-17T10:0{attempt + 1}:30+08:00",
                    )
                self.begin(state, references=references)
                self.accept_local(state)
                if terminal_status == "success":
                    task_state.record_success(
                        state, target_token="box_t1", file_token="box_out",
                        name=task_state.output_name(1, "box_t1"),
                        updated_at="2026-08-17T10:04:00+08:00",
                    )
                state["targets"]["box_t1"]["attempt_history"][0][
                    "reference_tokens"
                ] = ["box_s2", "box_s1"]
                state["targets"]["box_t1"]["attempt_history"][1][
                    "reference_tokens"
                ] = ["box_s2", "box_s1"]

                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "state.json"
                    path.write_text(json.dumps(state), encoding="utf-8")
                    loaded = task_state.load_state(path)

                self.assertEqual(loaded, state)

    def test_persisted_second_attempt_historical_success_is_rejected(self) -> None:
        state, _first = self.make_first_of_three_locally_accepted()
        target = state["targets"]["box_t1"]
        output = {
            "file_token": "box_out",
            "name": task_state.output_name(1, "box_t1"),
        }
        target["attempts"] = 2
        target["attempt_history"] = target["attempt_history"][:2]
        target["attempt_history"][0]["outcome"] = "success"
        target["attempt_history"][0]["output"] = output
        target["status"] = "success"
        target["output"] = output
        target["local_acceptance"] = None

        self.assert_persisted_state_rejected_without_mutation(state)

    def test_historical_acceptance_copies_selected_metadata_to_target(self) -> None:
        state = self.make_state()
        task_state.begin_attempt(
            state, target_token="box_t1", classification="front three-quarter",
            reference_tokens=["box_s1"], prompt="chosen garment prompt",
            model="chosen-model", updated_at="2026-08-17T10:01:00+08:00",
        )
        first = state["targets"]["box_t1"]["attempt_history"][-1]["artifact_name"]
        task_state.record_failure(
            state, target_token="box_t1", error="reject 1",
            updated_at="2026-08-17T10:01:30+08:00",
        )
        task_state.begin_attempt(
            state, target_token="box_t1", classification="side",
            reference_tokens=["box_s1"], prompt="second garment prompt",
            model="second-model", updated_at="2026-08-17T10:02:00+08:00",
        )
        task_state.record_failure(
            state, target_token="box_t1", error="reject 2",
            updated_at="2026-08-17T10:02:30+08:00",
        )
        task_state.begin_attempt(
            state, target_token="box_t1", classification="back",
            reference_tokens=["box_s1"], prompt="third garment prompt",
            model="third-model", updated_at="2026-08-17T10:03:00+08:00",
        )
        task_state.record_local_acceptance(
            state, target_token="box_t1", artifact_name=first,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T10:03:30+08:00",
        )

        target = state["targets"]["box_t1"]
        self.assertEqual(target["classification"], "front three-quarter")
        self.assertEqual(target["reference_tokens"], ["box_s1"])
        self.assertEqual(
            target["prompt_sha256"],
            hashlib.sha256(b"chosen garment prompt").hexdigest(),
        )
        self.assertEqual(target["model"], "chosen-model")

    def test_historical_acceptance_rejects_artifact_from_previous_retry_cycle(self) -> None:
        state = self.make_state()
        for number in range(3):
            self.begin(state)
            old_artifact = state["targets"]["box_t1"]["attempt_history"][0]["artifact_name"]
            task_state.record_failure(
                state, target_token="box_t1", error=f"old {number}",
                updated_at=f"2026-08-17T10:0{number + 1}:30+08:00",
            )
        task_state.prepare_retry(state, updated_at="2026-08-17T11:00:00+08:00")
        self.begin(state)
        before = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "current attempt cycle"):
            task_state.record_local_acceptance(
                state, target_token="box_t1", artifact_name=old_artifact,
                name=task_state.output_name(1, "box_t1"),
                updated_at="2026-08-17T11:01:00+08:00",
            )
        self.assertEqual(state, before)

    def test_early_active_acceptance_stops_before_attempt_two(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        self.assertEqual(state["targets"]["box_t1"]["attempts"], 1)
        with self.assertRaisesRegex(task_state.TaskStateError, "not pending"):
            self.begin(state)

    def test_historical_acceptance_rejects_artifact_before_source_change(self) -> None:
        state = self.make_state()
        self.begin(state)
        old_artifact = state["targets"]["box_t1"]["attempt_history"][-1]["artifact_name"]
        task_state.record_failure(
            state, target_token="box_t1", error="old source",
            updated_at="2026-08-17T10:01:30+08:00",
        )
        task_state.reconcile(
            state, source_tokens=["box_new_source"], target_tokens=["box_t1"],
            outputs=[], run_id="run_2", started_at="2026-08-17T11:00:00+08:00",
            updated_at="2026-08-17T11:00:01+08:00",
        )
        self.begin(state)
        before = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "current attempt cycle"):
            task_state.record_local_acceptance(
                state, target_token="box_t1", artifact_name=old_artifact,
                name=task_state.output_name(1, "box_t1"),
                updated_at="2026-08-17T11:01:00+08:00",
            )
        self.assertEqual(state, before)

    def test_legacy_running_fourth_attempt_can_select_existing_cycle_without_new_call(self) -> None:
        state = self.make_state()
        history = self.legacy_finished_history(4, outcome="failed")
        history[-1].update({
            "outcome": "running", "finished_at": None, "error": None, "output": None,
        })
        target = state["targets"]["box_t1"]
        target.update({
            "status": "running", "classification": "front",
            "reference_tokens": ["box_s1"], "attempts": 4,
            "prompt_sha256": history[-1]["prompt_sha256"],
            "model": "image-model", "error": None,
            "updated_at": history[-1]["started_at"], "attempt_history": history,
        })
        state["current_target"] = "box_t1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            loaded = task_state.load_state(path)
        with self.assertRaisesRegex(task_state.TaskStateError, "not pending"):
            self.begin(loaded)
        first = history[0]["artifact_name"]
        task_state.record_local_acceptance(
            loaded, target_token="box_t1", artifact_name=first,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T11:01:00+08:00",
        )
        self.assertEqual(
            loaded["targets"]["box_t1"]["local_acceptance"]["artifact_name"], first,
        )
        self.assertEqual(loaded["targets"]["box_t1"]["attempts"], 4)

    def test_new_state_preserves_ordered_tokens(self) -> None:
        state = task_state.new_state(
            record_id="rec_1",
            run_id="run_1",
            source_tokens=["box_s1", "box_s2"],
            target_tokens=["box_t1", "box_t2"],
            started_at="2026-08-15T10:00:00+08:00",
        )
        self.assertEqual(state["schema_version"], 4)
        self.assertEqual(state["target_tokens"], ["box_t1", "box_t2"])
        self.assertEqual(state["targets"]["box_t1"]["status"], "pending")

    def test_new_state_initializes_schema_v3_qc_checkpoints(self) -> None:
        state = self.make_state()

        self.assertEqual(state["targets"]["box_t1"], {
            "status": "pending",
            "classification": None,
            "reference_tokens": [],
            "attempts": 0,
            "output": None,
            "local_acceptance": None,
            "prompt_sha256": None,
            "model": None,
            "error": None,
            "stale_output_tokens": [],
            "updated_at": "2026-08-15T10:00:00+08:00",
            "attempt_history": [],
            "target_plan": None,
            "qc_reports": [],
            "selection_reason": None,
            "selection_reason_history": [],
        })

    def test_load_migrates_v2_checkpoints_without_changing_attempt_history(self) -> None:
        legacy = self.make_state()
        self.begin(legacy)
        task_state.record_failure(
            legacy, target_token="box_t1", error="needs visual review",
            updated_at="2026-08-15T10:02:00+08:00",
        )
        legacy["schema_version"] = 2
        target = legacy["targets"]["box_t1"]
        target.pop("target_plan", None)
        target.pop("qc_reports", None)
        target.pop("selection_reason", None)
        history = json.loads(json.dumps(target["attempt_history"]))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-v2.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            migrated = task_state.load_state(path)
            path.write_text(json.dumps(migrated), encoding="utf-8")
            reloaded = task_state.load_state(path)

        migrated_target = migrated["targets"]["box_t1"]
        self.assertEqual(migrated["schema_version"], 4)
        self.assertEqual(migrated_target["attempt_history"], history)
        self.assertIsNone(migrated_target["target_plan"])
        self.assertEqual(migrated_target["qc_reports"], [])
        self.assertIsNone(migrated_target["selection_reason"])
        self.assertEqual(reloaded, migrated)

    def test_target_plan_is_immutable_but_identical_replay_is_safe(self) -> None:
        state = self.make_state()
        plan = {
            "classification": "front",
            "reference_tokens": ["box_s1"],
            "roles": ["primary"],
        }

        task_state.record_target_plan(state, 0, plan)
        plan["roles"].append("mutated-after-write")
        task_state.record_target_plan(state, 0, {
            "classification": "front",
            "reference_tokens": ["box_s1"],
            "roles": ["primary"],
        })

        self.assertEqual(state["targets"]["box_t1"]["target_plan"], {
            "classification": "front",
            "reference_tokens": ["box_s1"],
            "roles": ["primary"],
        })
        before = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "target plan"):
            task_state.record_target_plan(state, 0, {"classification": "side"})
        self.assertEqual(state, before)

    def test_source_reconciliation_preserves_an_immutable_target_plan(self) -> None:
        state = self.make_state()
        plan = {"classification": "front", "reference_tokens": ["box_s1"]}
        task_state.record_target_plan(state, 0, plan)

        task_state.reconcile(
            state, source_tokens=["box_s2"], target_tokens=["box_t1"], outputs=[],
            run_id="run_2", started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
        )

        self.assertEqual(state["targets"]["box_t1"]["target_plan"], plan)
        with self.assertRaisesRegex(task_state.TaskStateError, "target plan"):
            task_state.record_target_plan(state, 0, {"classification": "side"})

    def test_checkpoint_payloads_reject_non_string_keys_recursively(self) -> None:
        state = self.make_state()
        before = json.loads(json.dumps(state))

        with self.assertRaisesRegex(task_state.TaskStateError, "string keys"):
            task_state.record_target_plan(
                state, 0, {"references": [{1: "box_s1"}]},
            )

        self.assertEqual(state, before)

    def test_checkpoint_payloads_reject_cyclic_dicts_and_lists_atomically(self) -> None:
        cyclic_dict: dict[str, object] = {}
        cyclic_dict["self"] = cyclic_dict
        cyclic_list: list[object] = []
        cyclic_list.append(cyclic_list)

        def timeout(_signum: int, _frame: object) -> None:
            raise TimeoutError("cycle validation timed out")

        for cycle in (cyclic_dict, cyclic_list):
            with self.subTest(cycle=type(cycle).__name__):
                state = self.make_state()
                before = json.loads(json.dumps(state))
                previous_handler = signal.signal(signal.SIGALRM, timeout)
                signal.setitimer(signal.ITIMER_REAL, 0.1)
                try:
                    with self.assertRaisesRegex(task_state.TaskStateError, "cycle"):
                        task_state.record_target_plan(state, 0, {"value": cycle})
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, previous_handler)
                self.assertEqual(state, before)

    def test_qc_reports_append_with_artifact_digest_and_attempt_number(self) -> None:
        state = self.make_state()
        first = {
            "attempt": 1,
            "artifact_sha256": "a" * 64,
            "decision": "retry",
        }
        second = {
            "attempt": 2,
            "artifact_sha256": "b" * 64,
            "decision": "accept",
        }

        task_state.record_qc_report(state, 0, first)
        task_state.record_qc_report(state, 0, second)

        self.assertEqual(state["targets"]["box_t1"]["qc_reports"], [first, second])
        before = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "artifact digest"):
            task_state.record_qc_report(state, 0, {"attempt": 3, "decision": "retry"})
        self.assertEqual(state, before)

    def test_current_attempt_cycle_returns_only_the_current_cycle_as_a_copy(self) -> None:
        state = self.make_state()
        for attempt in range(3):
            self.begin(state)
            task_state.record_failure(
                state, target_token="box_t1", error=f"old failure {attempt}",
                updated_at="2026-08-18T10:00:00+08:00",
            )
        task_state.prepare_retry(
            state, updated_at="2026-08-18T11:00:00+08:00",
        )
        self.begin(state)

        cycle = task_state.current_attempt_cycle(state, 0)
        cycle[0]["prompt"] = "mutated copy"

        self.assertEqual(len(cycle), 1)
        self.assertEqual(cycle[0]["attempt"], 1)
        self.assertNotEqual(
            state["targets"]["box_t1"]["attempt_history"][-1]["prompt"],
            "mutated copy",
        )

    def test_recoverable_qc_failure_preserves_active_artifact_and_budget(self) -> None:
        state = self.make_state()
        self.begin(state)
        active = json.loads(json.dumps(
            state["targets"]["box_t1"]["attempt_history"][-1],
        ))

        task_state.record_qc_failure(
            state, target_token="box_t1", error="Ark QC unavailable",
            updated_at="2026-08-18T10:02:00+08:00",
        )

        target = state["targets"]["box_t1"]
        self.assertEqual((target["status"], target["attempts"]), ("running", 1))
        self.assertEqual(target["attempt_history"][-1], active)
        self.assertEqual(target["error"], "Ark QC unavailable")
        self.assertEqual(state["current_target"], "box_t1")

    def test_selection_reason_is_immutable_after_it_is_recorded(self) -> None:
        state = self.make_state()
        reason = {"artifact_sha256": "c" * 64, "reason": "best garment construction"}

        task_state.record_selection_reason(state, 0, reason)
        task_state.record_selection_reason(state, 0, {
            "artifact_sha256": "c" * 64,
            "reason": "best garment construction",
        })

        before = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "selection reason"):
            task_state.record_selection_reason(
                state, 0, {"artifact_sha256": "d" * 64, "reason": "different"},
            )
        self.assertEqual(state, before)

    def test_retry_archives_selection_reason_and_opens_new_cycle_slot(self) -> None:
        state = self.make_state()
        state["targets"]["box_t1"]["status"] = "failed"
        reason = {"artifact_sha256": "c" * 64, "reason": "cycle one"}
        task_state.record_selection_reason(state, 0, reason)

        task_state.prepare_retry(state, updated_at="2026-08-18T10:03:00+08:00")
        target = state["targets"]["box_t1"]
        self.assertIsNone(target["selection_reason"])
        self.assertEqual(target["selection_reason_history"], [reason])
        task_state.record_selection_reason(
            state, 0, {"artifact_sha256": "d" * 64, "reason": "cycle two"},
        )
        self.assertEqual(
            state["targets"]["box_t1"]["selection_reason"]["reason"], "cycle two",
        )

    def test_cli_persists_schema_v3_qc_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            plan_path = root / "plan.json"
            report_path = root / "report.json"
            reason_path = root / "reason.json"
            state_path.write_text(json.dumps(self.make_state()), encoding="utf-8")
            plan_path.write_text(
                '{"classification":"front","reference_tokens":["box_s1"]}',
                encoding="utf-8",
            )
            report_path.write_text(
                '{"attempt":1,"artifact_sha256":"'
                + "a" * 64 + '","decision":"accept"}',
                encoding="utf-8",
            )
            reason_path.write_text(
                '{"artifact_sha256":"'
                + "a" * 64 + '","reason":"accepted automatically"}',
                encoding="utf-8",
            )
            for command, payload in (
                ("target-plan", plan_path),
                ("qc-report", report_path),
                ("selection-reason", reason_path),
            ):
                with self.subTest(command=command), redirect_stderr(io.StringIO()), \
                        redirect_stdout(io.StringIO()):
                    code = task_state.main([
                        command, "--state", str(state_path), "--target-index", "0",
                        "--payload-json", str(payload),
                    ])
                self.assertEqual(code, 0)

            target = task_state.load_state(state_path)["targets"]["box_t1"]
            self.assertEqual(target["target_plan"]["classification"], "front")
            self.assertEqual(target["qc_reports"][0]["attempt"], 1)
            self.assertEqual(target["selection_reason"]["reason"], "accepted automatically")

    def test_new_state_rejects_empty_record_run_or_start_identity(self) -> None:
        defaults = {
            "record_id": "rec_1", "run_id": "run_1",
            "source_tokens": ["box_s1"], "target_tokens": ["box_t1"],
            "started_at": "2026-08-15T10:00:00+08:00",
        }
        for key in ("record_id", "run_id", "started_at"):
            with self.subTest(key=key):
                with self.assertRaises(task_state.TaskStateError):
                    task_state.new_state(**(defaults | {key: ""}))

    def test_success_is_skipped_only_when_output_is_present(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        task_state.record_success(
            state, target_token="box_t1", file_token="box_out",
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-15T10:02:00+08:00",
        )
        self.reconcile(
            state, target_tokens=["box_t1"],
            outputs=[{"file_token": "box_out", "name": task_state.output_name(1, "box_t1")}],
            updated_at="2026-08-15T10:03:00+08:00",
        )
        self.assertEqual(task_state.pending_targets(state), [])

    def test_missing_output_changes_success_back_to_pending(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        task_state.record_success(
            state, target_token="box_t1", file_token="box_out",
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-15T10:02:00+08:00",
        )
        self.reconcile(state, target_tokens=["box_t1"], outputs=[],
                       updated_at="2026-08-15T10:03:00+08:00")
        self.assertEqual(task_state.pending_targets(state), ["box_t1"])
        self.assertIsNone(state["targets"]["box_t1"]["output"])

    def test_replaced_target_becomes_pending_without_deleting_history(self) -> None:
        state = self.make_state()
        self.reconcile(state, target_tokens=["box_t2"], outputs=[],
                       updated_at="2026-08-15T10:03:00+08:00")
        self.assertEqual(state["target_tokens"], ["box_t2"])
        self.assertEqual(state["targets"]["box_t2"]["status"], "pending")
        self.assertIn("box_t1", state["targets"])

    def test_deterministic_filename_recovers_uploaded_output(self) -> None:
        state = self.make_state()
        self.reconcile(
            state, target_tokens=["box_t1"],
            outputs=[{"file_token": "box_out", "name": task_state.output_name(1, "box_t1")}],
            updated_at="2026-08-15T10:03:00+08:00",
        )
        self.assertEqual(state["targets"]["box_t1"]["output"]["file_token"], "box_out")
        self.assertEqual(state["targets"]["box_t1"]["status"], "success")

    def test_reordered_targets_recover_outputs_by_token_digest(self) -> None:
        state = self.make_state(["box_t1", "box_t2"])
        self.reconcile(
            state,
            target_tokens=["box_t2", "box_t1"],
            outputs=[
                {"file_token": "box_out_1", "name": "look-01-9a9af8b7a89a.png"},
                {"file_token": "box_out_2", "name": "look-02-c92b9a0f2b8e.png"},
            ],
            updated_at="2026-08-15T10:03:00+08:00",
        )
        self.assertEqual(
            state["targets"]["box_t2"]["output"],
            {"file_token": "box_out_2", "name": "look-02-c92b9a0f2b8e.png"},
        )
        self.assertEqual(
            state["targets"]["box_t1"]["output"],
            {"file_token": "box_out_1", "name": "look-01-9a9af8b7a89a.png"},
        )
        self.assertEqual(task_state.pending_targets(state), [])
        task_state.compact_detail(state)

    def test_three_attempts_make_target_terminal_failed(self) -> None:
        state = self.make_state()
        for attempt in range(2):
            self.begin(state)
            task_state.record_failure(
                state, target_token="box_t1", error=f"failure {attempt}",
                updated_at="2026-08-17T10:02:00+08:00",
            )
            self.assertEqual(state["targets"]["box_t1"]["status"], "pending")
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error="failure 2",
            updated_at="2026-08-17T10:03:00+08:00",
        )
        self.assertEqual(state["targets"]["box_t1"]["attempts"], 3)
        self.assertEqual(state["targets"]["box_t1"]["status"], "failed")
        with self.assertRaisesRegex(task_state.TaskStateError, "exhausted"):
            self.begin(state)

    def test_load_preserves_legacy_success_with_five_attempts(self) -> None:
        state = self.make_state()
        target = state["targets"]["box_t1"]
        target["attempts"] = 5
        target["status"] = "success"
        target["output"] = {
            "file_token": "box_out", "name": task_state.output_name(1, "box_t1"),
        }
        target["attempt_history"] = self.legacy_finished_history(5, outcome="failed")
        target["attempt_history"][-1]["outcome"] = "success"
        target["attempt_history"][-1]["error"] = None
        target["attempt_history"][-1]["output"] = target["output"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            loaded = task_state.load_state(path)
        self.assertEqual(loaded["targets"]["box_t1"]["status"], "success")
        self.assertEqual(loaded["targets"]["box_t1"]["attempts"], 5)

    def test_load_terminalizes_legacy_pending_without_new_call(self) -> None:
        state = self.make_state()
        target = state["targets"]["box_t1"]
        target["attempts"] = 4
        target["status"] = "pending"
        target["attempt_history"] = self.legacy_finished_history(4, outcome="failed")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            loaded = task_state.load_state(path)
        self.assertEqual(loaded["targets"]["box_t1"]["status"], "failed")
        with self.assertRaisesRegex(task_state.TaskStateError, "not pending"):
            self.begin(loaded)

    def test_begin_attempt_requires_pending_and_known_classification(self) -> None:
        state = self.make_state()
        self.begin(state)
        with self.assertRaisesRegex(task_state.TaskStateError, "not pending"):
            self.begin(state)

        unknown = self.make_state()
        with self.assertRaisesRegex(task_state.TaskStateError, "classification"):
            task_state.begin_attempt(
                unknown, target_token="box_t1", classification="casual",
                reference_tokens=["box_s1"], prompt="prompt", model="model",
                updated_at="2026-08-15T10:01:00+08:00",
            )

    def test_begin_attempt_rejects_a_second_concurrent_target(self) -> None:
        state = self.make_state(["box_t1", "box_t2"])
        self.begin(state, "box_t1")
        with self.assertRaisesRegex(task_state.TaskStateError, "already running"):
            self.begin(state, "box_t2")
        self.assertEqual(state["current_target"], "box_t1")
        self.assertEqual(state["targets"]["box_t2"]["status"], "pending")

    def test_pending_excludes_the_current_running_target(self) -> None:
        state = self.make_state(["box_t1", "box_t2"])
        self.begin(state, "box_t1")
        self.assertEqual(task_state.pending_targets(state), ["box_t2"])

    def test_begin_attempt_invalid_inputs_leave_state_unchanged(self) -> None:
        cases = (
            {"reference_tokens": []},
            {"prompt": None},
            {"model": ""},
            {"updated_at": ""},
        )
        defaults = {
            "target_token": "box_t1", "classification": "front",
            "reference_tokens": ["box_s1"], "prompt": "prompt",
            "model": "model", "updated_at": "2026-08-15T10:01:00+08:00",
        }
        for override in cases:
            with self.subTest(override=override):
                state = self.make_state()
                before = json.loads(json.dumps(state))
                with self.assertRaises(task_state.TaskStateError):
                    task_state.begin_attempt(state, **(defaults | override))
                self.assertEqual(state, before)

    def test_success_and_failure_require_a_running_attempt(self) -> None:
        pending = self.make_state()
        pending["targets"]["box_t1"]["attempts"] = 1
        with self.assertRaisesRegex(task_state.TaskStateError, "local acceptance"):
            task_state.record_success(
                pending, target_token="box_t1", file_token="box_out",
                name=task_state.output_name(1, "box_t1"),
                updated_at="2026-08-15T10:02:00+08:00",
            )
        with self.assertRaisesRegex(task_state.TaskStateError, "not running"):
            task_state.record_failure(
                pending, target_token="box_t1", error="late failure",
                updated_at="2026-08-15T10:02:00+08:00",
            )

        successful = self.make_state()
        self.begin(successful)
        self.accept_local(successful)
        task_state.record_success(
            successful, target_token="box_t1", file_token="box_out",
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-15T10:02:00+08:00",
        )
        with self.assertRaisesRegex(task_state.TaskStateError, "not running"):
            task_state.record_failure(
                successful, target_token="box_t1", error="late failure",
                updated_at="2026-08-15T10:03:00+08:00",
            )
        self.assertEqual(successful["targets"]["box_t1"]["status"], "success")
        self.assertIsNotNone(successful["targets"]["box_t1"]["output"])

    def test_terminal_failure_cannot_be_changed_to_success(self) -> None:
        state = self.make_state()
        for attempt in range(3):
            self.begin(state)
            task_state.record_failure(
                state, target_token="box_t1", error=f"failure {attempt}",
                updated_at="2026-08-15T10:02:00+08:00",
            )
        with self.assertRaisesRegex(task_state.TaskStateError, "local acceptance"):
            task_state.record_success(
                state, target_token="box_t1", file_token="box_out",
                name=task_state.output_name(1, "box_t1"),
                updated_at="2026-08-15T10:03:00+08:00",
            )
        self.assertEqual(state["targets"]["box_t1"]["status"], "failed")

    def test_new_state_rejects_empty_source_or_target_inputs(self) -> None:
        for sources, targets, message in (
            ([], ["box_t1"], "source_tokens"),
            (["box_s1"], [], "target_tokens"),
        ):
            with self.subTest(sources=sources, targets=targets):
                with self.assertRaisesRegex(task_state.TaskStateError, message):
                    task_state.new_state(
                        record_id="rec_1", run_id="run_1",
                        source_tokens=sources, target_tokens=targets,
                        started_at="2026-08-15T10:00:00+08:00",
                    )

    def test_non_success_output_and_empty_current_targets_are_invalid(self) -> None:
        state = self.make_state()
        state["targets"]["box_t1"]["output"] = {
            "file_token": "box_out", "name": task_state.output_name(1, "box_t1"),
        }
        with self.assertRaisesRegex(task_state.TaskStateError, "non-successful target"):
            task_state.aggregate_status(state)

        empty = self.make_state()
        empty["target_tokens"] = []
        with self.assertRaisesRegex(task_state.TaskStateError, "target_tokens"):
            task_state.aggregate_status(empty)

    def test_restart_does_not_close_exhausted_selection_when_earlier_candidate_is_valid(self) -> None:
        state = self.make_state()
        self.begin(state)
        first = state["targets"]["box_t1"]["attempt_history"][-1]
        task_state.record_failure(
            state, target_token="box_t1", error="first visual rejection",
            updated_at="2026-08-15T10:01:30+08:00",
        )
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error="second visual rejection",
            updated_at="2026-08-15T10:02:30+08:00",
        )
        self.begin(state)

        task_state.reconcile(
            state, source_tokens=["box_s1"], target_tokens=["box_t1"],
            outputs=[], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
            resumable_artifacts=[{
                "run_id": first["run_id"],
                "artifact_name": first["artifact_name"],
            }],
        )

        target = state["targets"]["box_t1"]
        self.assertEqual(target["status"], "running")
        self.assertEqual(state["current_target"], "box_t1")
        self.assertEqual(target["attempts"], 3)
        self.assertEqual(
            [entry["outcome"] for entry in target["attempt_history"]],
            ["failed", "failed", "running"],
        )
        self.assertEqual(task_state.pending_targets(state), [])

        task_state.record_local_acceptance(
            state, target_token="box_t1", artifact_name=first["artifact_name"],
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-15T11:01:00+08:00",
        )
        target = state["targets"]["box_t1"]
        self.assertEqual(target["status"], "accepted-local")
        self.assertEqual(
            target["local_acceptance"]["artifact_name"], first["artifact_name"],
        )
        self.assertEqual(
            [entry["outcome"] for entry in target["attempt_history"]],
            ["accepted-local", "failed", "failed"],
        )

    def test_restart_without_candidates_keeps_checkpoint_until_external_call(self) -> None:
        state = self.make_state()
        for attempt in range(2):
            self.begin(state)
            task_state.record_failure(
                state, target_token="box_t1", error=f"failure {attempt}",
                updated_at="2026-08-15T10:02:00+08:00",
            )
        self.begin(state)
        self.assertEqual(
            (state["targets"]["box_t1"]["status"],
             state["targets"]["box_t1"]["attempts"]),
            ("running", 3),
        )

        self.reconcile(
            state, target_tokens=["box_t1"], outputs=[],
            updated_at="2026-08-15T10:03:00+08:00",
        )
        checkpoint = json.loads(json.dumps(state))
        self.reconcile(
            state, target_tokens=["box_t1"], outputs=[],
            updated_at="2026-08-15T10:03:00+08:00",
        )
        self.assertEqual(state, checkpoint)
        self.assertEqual(
            (state["targets"]["box_t1"]["status"],
             state["targets"]["box_t1"]["attempts"]),
            ("running", 3),
        )
        self.assertEqual(task_state.pending_targets(state), [])
        with self.assertRaisesRegex(task_state.TaskStateError, "exhausted"):
            self.begin(state)

        task_state.record_error(
            state, code="external-call", error="no valid current-cycle candidate",
            updated_at="2026-08-15T10:04:00+08:00",
        )
        target = state["targets"]["box_t1"]
        self.assertEqual(target["status"], "failed")
        self.assertEqual(target["attempt_history"][-1]["outcome"], "failed")
        self.assertEqual(state["record_error"], {
            "code": "external-call",
            "message": "no valid current-cycle candidate",
            "updated_at": "2026-08-15T10:04:00+08:00",
        })
        self.assertIsNone(state["current_target"])

    def test_restart_does_not_close_legacy_exhausted_selection_checkpoint(self) -> None:
        for attempts in (4, 5):
            with self.subTest(attempts=attempts):
                state = self.make_state()
                history = self.legacy_finished_history(attempts, outcome="failed")
                history[-1].update({
                    "outcome": "running", "finished_at": None,
                    "error": None, "output": None,
                })
                target = state["targets"]["box_t1"]
                target.update({
                    "status": "running", "classification": "front",
                    "reference_tokens": ["box_s1"], "attempts": attempts,
                    "prompt_sha256": history[-1]["prompt_sha256"],
                    "model": "image-model", "error": None,
                    "updated_at": history[-1]["started_at"],
                    "attempt_history": history,
                })
                state["current_target"] = "box_t1"

                self.reconcile(
                    state, target_tokens=["box_t1"], outputs=[],
                    updated_at="2026-08-17T11:00:01+08:00",
                )

                target = state["targets"]["box_t1"]
                self.assertEqual(target["status"], "running")
                self.assertEqual(state["current_target"], "box_t1")
                self.assertEqual(target["attempts"], attempts)
                self.assertEqual(len(target["attempt_history"]), attempts)
                self.assertEqual(target["attempt_history"][-1]["outcome"], "running")
                with self.assertRaisesRegex(
                        task_state.TaskStateError, "exhausted|not pending",
                ):
                    self.begin(state)

    def test_interrupted_attempt_gets_a_new_immutable_artifact_identity(self) -> None:
        state = self.make_state()
        self.begin(state)
        interrupted_name = state["targets"]["box_t1"]["attempt_history"][-1][
            "artifact_name"
        ]

        self.reconcile(
            state, target_tokens=["box_t1"], outputs=[],
            updated_at="2026-08-15T10:03:00+08:00",
        )
        self.assertEqual(state["targets"]["box_t1"]["attempts"], 1)
        self.begin(state)
        resumed = state["targets"]["box_t1"]["attempt_history"][-1]

        self.assertEqual(resumed["attempt"], 2, "an initiated edit may have been billed")
        self.assertEqual(resumed["artifact_ordinal"], 2)
        self.assertNotEqual(resumed["artifact_name"], interrupted_name)
        self.assertTrue(resumed["artifact_name"].endswith("-02.png"))

    def test_reconcile_preserves_a_valid_paid_artifact_for_reinspection(self) -> None:
        state = self.make_state()
        self.begin(state)
        active = state["targets"]["box_t1"]["attempt_history"][-1]
        task_state.reconcile(
            state, source_tokens=["box_s1"], target_tokens=["box_t1"],
            outputs=[], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
            resumable_artifacts=[{
                "run_id": active["run_id"], "artifact_name": active["artifact_name"],
            }],
        )
        self.assertEqual(state["current_target"], "box_t1")
        self.assertEqual(state["targets"]["box_t1"]["status"], "running")
        self.assertEqual(state["targets"]["box_t1"]["attempts"], 1)
        self.assertEqual(task_state.pending_targets(state), [])

    def test_upload_failure_resumes_local_acceptance_without_new_generation(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        accepted = state["targets"]["box_t1"]["local_acceptance"]
        history_count = len(state["targets"]["box_t1"]["attempt_history"])
        task_state.record_error(
            state, code="external-call", error="upload unavailable",
            updated_at="2026-08-15T10:02:00+08:00",
        )
        task_state.reconcile(
            state, source_tokens=["box_s1"], target_tokens=["box_t1"],
            outputs=[], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
            resumable_artifacts=[{
                "run_id": accepted["run_id"],
                "artifact_name": accepted["artifact_name"],
            }],
        )
        self.assertEqual(task_state.pending_targets(state), [])
        self.assertEqual(len(state["targets"]["box_t1"]["attempt_history"]), history_count)
        self.assertEqual(
            task_state.pending_uploads(state)[0]["artifact_name"],
            state["targets"]["box_t1"]["local_acceptance"]["artifact_name"],
        )

    def test_missing_local_acceptance_artifact_is_not_stranded(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)

        task_state.reconcile(
            state, source_tokens=["box_s1"], target_tokens=["box_t1"],
            outputs=[], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
            resumable_artifacts=[],
        )

        target = state["targets"]["box_t1"]
        self.assertEqual(target["status"], "pending")
        self.assertIsNone(target["local_acceptance"])
        self.assertEqual(target["attempts"], 1, "a paid edit must still spend budget")
        self.assertEqual(task_state.pending_targets(state), ["box_t1"])

    def test_retry_preserves_a_resumable_pending_upload(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        before = json.loads(json.dumps(state["targets"]["box_t1"]))

        task_state.prepare_retry(
            state, updated_at="2026-08-15T10:03:00+08:00",
        )

        self.assertEqual(state["targets"]["box_t1"], before)
        self.assertEqual(task_state.pending_targets(state), [])
        self.assertEqual(len(task_state.pending_uploads(state)), 1)

    def test_reordered_accepted_local_upload_keeps_its_display_index(self) -> None:
        state = self.make_state(["box_t1", "box_t2"])
        self.begin(state, "box_t1")
        self.accept_local(state, "box_t1")
        accepted = state["targets"]["box_t1"]["local_acceptance"]

        task_state.reconcile(
            state, source_tokens=["box_s1"],
            target_tokens=["box_t2", "box_t1"], outputs=[], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
            resumable_artifacts=[{
                "run_id": accepted["run_id"],
                "artifact_name": accepted["artifact_name"],
            }],
        )
        upload = task_state.pending_uploads(state)[0]
        task_state.record_success(
            state, target_token="box_t1", file_token="box_out",
            name=upload["name"], updated_at="2026-08-15T11:01:00+08:00",
        )

        self.assertEqual(upload["name"], task_state.output_name(1, "box_t1"))
        self.assertEqual(state["targets"]["box_t1"]["status"], "success")

    def test_reordered_running_artifact_promotes_with_its_original_display_index(self) -> None:
        state = self.make_state(["box_t1", "box_t2"])
        self.begin(state, "box_t1")
        active = state["targets"]["box_t1"]["attempt_history"][-1]
        task_state.reconcile(
            state, source_tokens=["box_s1"],
            target_tokens=["box_t2", "box_t1"], outputs=[], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
            resumable_artifacts=[{
                "run_id": active["run_id"],
                "artifact_name": active["artifact_name"],
            }],
        )
        accepted_name = task_state.promoted_output_name(
            active["artifact_name"], "box_t1",
        )
        task_state.record_local_acceptance(
            state, target_token="box_t1",
            artifact_name=active["artifact_name"], name=accepted_name,
            updated_at="2026-08-15T11:01:00+08:00",
        )

        self.assertEqual(accepted_name, task_state.output_name(1, "box_t1"))
        self.assertEqual(
            state["targets"]["box_t1"]["status"], "accepted-local",
        )

    def test_reconcile_recovers_an_uploaded_running_attempt_without_regeneration(self) -> None:
        state = self.make_state()
        self.begin(state)
        uploaded = {
            "file_token": "box_out",
            "name": task_state.output_name(1, "box_t1"),
        }

        task_state.reconcile(
            state, source_tokens=["box_s1"], target_tokens=["box_t1"],
            outputs=[uploaded], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
            resumable_artifacts=[],
        )

        target = state["targets"]["box_t1"]
        self.assertEqual(target["status"], "success")
        self.assertEqual(target["output"], uploaded)
        self.assertEqual(target["attempt_history"][-1]["outcome"], "success")
        self.assertIsNone(state["current_target"])

    def test_uploaded_success_requires_a_persisted_local_acceptance(self) -> None:
        state = self.make_state()
        self.begin(state)
        with self.assertRaisesRegex(task_state.TaskStateError, "local acceptance"):
            task_state.record_success(
                state, target_token="box_t1", file_token="box_out",
                name=task_state.output_name(1, "box_t1"),
                updated_at="2026-08-15T10:02:00+08:00",
            )

    def test_attempt_history_keeps_the_artifact_owning_run_id(self) -> None:
        state = self.make_state()
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error="first rejection",
            updated_at="2026-08-15T10:02:00+08:00",
        )
        task_state.reconcile(
            state, source_tokens=["box_s1"], target_tokens=["box_t1"],
            outputs=[], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
        )
        self.begin(state)
        history = state["targets"]["box_t1"]["attempt_history"]
        self.assertEqual([entry["run_id"] for entry in history], ["run_1", "run_2"])

    def test_record_success_rejects_invalid_output_mapping(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        with self.assertRaises(task_state.TaskStateError):
            task_state.record_success(
                state, target_token="box_t1", file_token="box_out",
                name=task_state.output_name(1, "another_target"),
                updated_at="2026-08-15T10:02:00+08:00",
            )

    def test_record_command_success_persists_receipt_without_file_token(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        name = task_state.output_name(1, "box_t1")

        task_state.record_command_success(
            state, target_token="box_t1", receipt_sha256="a" * 64,
            name=name, updated_at="2026-08-15T10:02:00+08:00",
        )

        self.assertEqual(state["targets"]["box_t1"]["status"], "success")
        self.assertEqual(state["targets"]["box_t1"]["output"], {
            "confirmation": "command-success",
            "receipt_sha256": "a" * 64,
            "name": name,
        })
        self.assertEqual(task_state.aggregate_status(state), "成功")

    def test_reconcile_does_not_overturn_command_success_without_base_output(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        task_state.record_command_success(
            state, target_token="box_t1", receipt_sha256="b" * 64,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-15T10:02:00+08:00",
        )
        expected = json.loads(json.dumps(state["targets"]["box_t1"]["output"]))

        self.reconcile(
            state, target_tokens=["box_t1"], outputs=[],
            updated_at="2026-08-15T10:03:00+08:00",
        )

        self.assertEqual(state["targets"]["box_t1"]["status"], "success")
        self.assertEqual(state["targets"]["box_t1"]["output"], expected)
        replayed = task_state.reconcile_target_output(
            state, target_index=0, outputs=[],
            updated_at="2026-08-15T10:04:00+08:00",
        )
        self.assertEqual(replayed, expected)

    def test_command_success_receipt_rejects_malformed_digest(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)

        with self.assertRaises(task_state.TaskStateError):
            task_state.record_command_success(
                state, target_token="box_t1", receipt_sha256="not-a-digest",
                name=task_state.output_name(1, "box_t1"),
                updated_at="2026-08-15T10:02:00+08:00",
            )

    def test_persisted_success_requires_matching_output_identity(self) -> None:
        for output in (
            {"file_token": "box_out", "name": "unrelated.png"},
            {"file_token": "", "name": task_state.output_name(1, "box_t1")},
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                state = self.make_state()
                state["targets"]["box_t1"]["status"] = "success"
                state["targets"]["box_t1"]["output"] = output
                path = Path(directory) / "state.json"
                path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(task_state.TaskStateError):
                    task_state.load_state(path)

    def test_reconcile_accepts_tuple_and_generator_inputs(self) -> None:
        state = self.make_state()
        outputs = ({"file_token": "box_out", "name": task_state.output_name(1, "box_t1")}
                   for _ in range(1))
        self.reconcile(
            state, target_tokens=("box_t1",), outputs=outputs,
            updated_at="2026-08-15T10:03:00+08:00",
        )
        self.assertEqual(state["targets"]["box_t1"]["status"], "success")
        with self.assertRaises(task_state.TaskStateError):
            task_state.record_success(
                state, target_token="box_t1", file_token="",
                name=task_state.output_name(1, "box_t1"),
                updated_at="2026-08-15T10:02:00+08:00",
            )

    def test_terminal_failed_target_is_not_retryable_pending(self) -> None:
        state = self.make_state()
        for attempt in range(3):
            self.begin(state)
            task_state.record_failure(
                state, target_token="box_t1", error=f"failure {attempt}",
                updated_at="2026-08-15T10:02:00+08:00",
            )
        self.assertEqual(task_state.pending_targets(state), [])
        self.assertEqual(task_state.aggregate_status(state), "失败")

    def test_explicit_retry_resets_only_current_non_success_targets(self) -> None:
        state = self.make_state(["box_t1", "box_t2", "box_t3"])
        self.begin(state, "box_t1")
        self.accept_local(state, "box_t1")
        task_state.record_success(
            state, target_token="box_t1", file_token="box_out_1",
            name="look-01-9a9af8b7a89a.png",
            updated_at="2026-08-15T10:02:00+08:00",
        )
        for token in ("box_t2", "box_t3"):
            for attempt in range(3):
                self.begin(state, token)
                task_state.record_failure(
                    state, target_token=token, error=f"{token} failure {attempt}",
                    updated_at="2026-08-15T10:02:00+08:00",
                )
        self.reconcile(
            state, target_tokens=["box_t1", "box_t2"],
            outputs=[{"file_token": "box_out_1", "name": "look-01-9a9af8b7a89a.png"}],
            updated_at="2026-08-15T10:03:00+08:00",
        )
        retained_success = json.loads(json.dumps(state["targets"]["box_t1"]))
        retained_historical_failure = json.loads(json.dumps(state["targets"]["box_t3"]))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = task_state.main([
                    "retry", "--state", str(path),
                    "--updated-at", "2026-08-15T10:04:00+08:00",
                ])
            self.assertEqual(code, 0, stderr.getvalue())
            retried = task_state.load_state(path)

        self.assertEqual(retried["targets"]["box_t1"], retained_success)
        self.assertEqual(retried["targets"]["box_t3"], retained_historical_failure)
        self.assertEqual(retried["targets"]["box_t2"]["status"], "pending")
        self.assertEqual(retried["targets"]["box_t2"]["attempts"], 0)
        self.assertIsNone(retried["targets"]["box_t2"]["output"])
        self.assertIsNone(retried["targets"]["box_t2"]["error"])
        self.assertEqual(task_state.pending_targets(retried), ["box_t2"])

    def test_aggregate_requires_every_current_target_success(self) -> None:
        state = self.make_state(["box_t1", "box_t2"])
        self.begin(state, "box_t1")
        self.accept_local(state, "box_t1")
        task_state.record_success(
            state, target_token="box_t1", file_token="box_out_1",
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-15T10:02:00+08:00",
        )
        self.assertEqual(task_state.aggregate_status(state), "失败")
        self.begin(state, "box_t2")
        self.accept_local(state, "box_t2")
        task_state.record_success(
            state, target_token="box_t2", file_token="box_out_2",
            name=task_state.output_name(2, "box_t2"),
            updated_at="2026-08-15T10:02:00+08:00",
        )
        self.assertEqual(task_state.aggregate_status(state), "成功")

    def test_compact_omits_full_prompt_and_keeps_digest(self) -> None:
        state = self.make_state()
        self.begin(state)
        detail = task_state.compact_detail(state)
        target_detail = json.loads(detail)["targets"]["box_t1"]
        self.assertNotIn("prompt", target_detail)
        self.assertIn("prompt_sha256", target_detail)
        self.assertEqual(target_detail["prompt_sha256"],
                         state["targets"]["box_t1"]["prompt_sha256"])

    def test_reconcile_refreshes_active_run_and_current_sources(self) -> None:
        state = self.make_state()
        task_state.reconcile(
            state,
            source_tokens=["box_s2", "box_s3"],
            target_tokens=["box_t1"], outputs=[],
            run_id="run_2", started_at="2026-08-15T11:00:00+08:00",
            updated_at="2026-08-15T11:00:01+08:00",
        )
        self.assertEqual(state["run_id"], "run_2")
        self.assertEqual(state["started_at"], "2026-08-15T11:00:00+08:00")
        self.assertEqual(state["source_tokens"], ["box_s2", "box_s3"])
        self.assertIsNone(state["current_target"])

        task_state.begin_attempt(
            state, target_token="box_t1", classification="front",
            reference_tokens=["box_s2"], prompt="new prompt", model="model",
            updated_at="2026-08-15T11:01:00+08:00",
        )
        self.assertEqual(state["current_target"], "box_t1")
        task_state.record_failure(
            state, target_token="box_t1", error="visual rejection",
            updated_at="2026-08-15T11:02:00+08:00",
        )
        self.assertIsNone(state["current_target"])

    def test_changed_source_invalidates_and_never_recovers_old_output(self) -> None:
        state = self.make_state()
        self.begin(state)
        accepted = {
            "file_token": "box_old_output",
            "name": task_state.output_name(1, "box_t1"),
        }
        self.accept_local(state)
        task_state.record_success(
            state, target_token="box_t1", file_token=accepted["file_token"],
            name=accepted["name"], updated_at="2026-08-15T10:02:00+08:00",
        )

        for updated_at in (
            "2026-08-15T10:03:00+08:00",
            "2026-08-15T10:04:00+08:00",
        ):
            task_state.reconcile(
                state, source_tokens=["box_new_source"],
                target_tokens=["box_t1"], outputs=[accepted],
                run_id="run_2", started_at="2026-08-15T10:03:00+08:00",
                updated_at=updated_at,
            )
            self.assertEqual(state["targets"]["box_t1"]["status"], "pending")
            self.assertIsNone(state["targets"]["box_t1"]["output"])
        self.assertIn(
            "box_old_output",
            state["targets"]["box_t1"]["stale_output_tokens"],
        )

    def test_changed_source_resets_budget_but_keeps_monotonic_artifact_history(self) -> None:
        state = self.make_state()
        for attempt in range(2):
            self.begin(state)
            task_state.record_failure(
                state, target_token="box_t1", error=f"failure {attempt}",
                updated_at="2026-08-15T10:02:00+08:00",
            )
        self.begin(state)
        accepted = {
            "file_token": "box_old_output",
            "name": task_state.output_name(1, "box_t1"),
        }
        self.accept_local(state)
        task_state.record_success(
            state, target_token="box_t1", file_token=accepted["file_token"],
            name=accepted["name"], updated_at="2026-08-15T10:02:00+08:00",
        )

        task_state.reconcile(
            state, source_tokens=["box_new_source"], target_tokens=["box_t1"],
            outputs=[accepted], run_id="run_2",
            started_at="2026-08-15T10:03:00+08:00",
            updated_at="2026-08-15T10:03:01+08:00",
        )
        self.assertEqual(state["targets"]["box_t1"]["attempts"], 0)
        self.begin(state)
        current = state["targets"]["box_t1"]
        self.assertEqual(current["attempts"], 1)
        self.assertEqual(current["attempt_history"][-1]["artifact_ordinal"], 4)

    def test_compact_omits_local_stale_output_history(self) -> None:
        state = self.make_state()
        state["targets"]["box_t1"]["stale_output_tokens"] = ["box_stale"]
        self.assertNotIn("stale_output_tokens", task_state.compact_detail(state))

    def test_reconcile_target_output_promotes_accepted_mapping_once(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        output = {
            "file_token": "box_uploaded", "name": task_state.output_name(1, "box_t1"),
        }

        reconciled = task_state.reconcile_target_output(
            state, target_index=0, outputs=[output],
            updated_at="2026-08-18T10:02:00+08:00",
        )
        replayed = task_state.reconcile_target_output(
            state, target_index=0, outputs=[{**output, "size": 123}],
            updated_at="2026-08-18T10:03:00+08:00",
        )

        self.assertEqual(reconciled, output)
        self.assertEqual(replayed, output)
        self.assertEqual(state["targets"]["box_t1"]["status"], "success")
        self.assertEqual(
            [entry["outcome"] for entry in state["targets"]["box_t1"]["attempt_history"]],
            ["success"],
        )

    def test_reconcile_success_uses_file_token_when_remote_name_differs(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        logical = {
            "file_token": "box_uploaded",
            "name": task_state.output_name(1, "box_t1"),
        }
        task_state.record_success(
            state, target_token="box_t1", file_token=logical["file_token"],
            name=logical["name"], updated_at="2026-08-18T10:02:00+08:00",
        )

        replayed = task_state.reconcile_target_output(
            state, target_index=0,
            outputs=[{"file_token": "box_uploaded", "name": "server-name.png"}],
            updated_at="2026-08-18T10:03:00+08:00",
        )

        self.assertEqual(replayed, logical)
        self.assertEqual(state["targets"]["box_t1"]["output"], logical)

    def test_table_reconcile_preserves_success_by_file_token(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        logical_name = task_state.output_name(1, "box_t1")
        task_state.record_success(
            state, target_token="box_t1", file_token="box_uploaded",
            name=logical_name, updated_at="2026-08-18T10:02:00+08:00",
        )

        self.reconcile(
            state, target_tokens=["box_t1"],
            outputs=[{"file_token": "box_uploaded", "name": "server-name.png"}],
            updated_at="2026-08-18T10:03:00+08:00",
        )

        self.assertEqual(state["targets"]["box_t1"]["status"], "success")
        self.assertEqual(
            state["targets"]["box_t1"]["output"],
            {"file_token": "box_uploaded", "name": logical_name},
        )

    def test_reconcile_target_output_without_match_does_not_mutate(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        before = json.loads(json.dumps(state))

        reconciled = task_state.reconcile_target_output(
            state, target_index=0, outputs=[],
            updated_at="2026-08-18T10:02:00+08:00",
        )

        self.assertIsNone(reconciled)
        self.assertEqual(state, before)

    def test_reconcile_target_output_rejects_ambiguous_current_attachments(self) -> None:
        state = self.make_state()
        self.begin(state)
        self.accept_local(state)
        name = task_state.output_name(1, "box_t1")

        with self.assertRaisesRegex(task_state.TaskStateError, "ambiguous"):
            task_state.reconcile_target_output(
                state, target_index=0,
                outputs=[
                    {"file_token": "box_one", "name": name},
                    {"file_token": "box_two", "name": name},
                ],
                updated_at="2026-08-18T10:02:00+08:00",
            )

    def test_source_reorder_preserves_current_success(self) -> None:
        state = task_state.new_state(
            record_id="rec_1", run_id="run_1",
            source_tokens=["box_s1", "box_s2"], target_tokens=["box_t1"],
            started_at="2026-08-15T10:00:00+08:00",
        )
        self.begin(state)
        accepted = {
            "file_token": "box_out",
            "name": task_state.output_name(1, "box_t1"),
        }
        self.accept_local(state)
        task_state.record_success(
            state, target_token="box_t1", file_token=accepted["file_token"],
            name=accepted["name"], updated_at="2026-08-15T10:02:00+08:00",
        )
        task_state.reconcile(
            state, source_tokens=["box_s2", "box_s1"],
            target_tokens=["box_t1"], outputs=[accepted], run_id="run_2",
            started_at="2026-08-15T10:03:00+08:00",
            updated_at="2026-08-15T10:03:01+08:00",
        )
        self.assertEqual(state["targets"]["box_t1"]["status"], "success")

    def test_attempt_history_is_local_only_and_preserves_every_attempt(self) -> None:
        state = self.make_state()
        task_state.begin_attempt(
            state, target_token="box_t1", classification="front",
            reference_tokens=["box_s1"], prompt="first full prompt", model="model",
            updated_at="2026-08-15T10:01:00+08:00",
        )
        task_state.record_failure(
            state, target_token="box_t1",
            error="Bearer secret-token data:image/png;base64,AAAA https://x.test/a?sig=secret",
            updated_at="2026-08-15T10:02:00+08:00",
        )
        task_state.begin_attempt(
            state, target_token="box_t1", classification="front",
            reference_tokens=["box_s1"], prompt="second full prompt", model="model",
            updated_at="2026-08-15T10:03:00+08:00",
        )
        self.accept_local(state)
        task_state.record_success(
            state, target_token="box_t1", file_token="box_out",
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-15T10:04:00+08:00",
        )

        history = state["targets"]["box_t1"]["attempt_history"]
        self.assertEqual([entry["prompt"] for entry in history],
                         ["first full prompt", "second full prompt"])
        self.assertEqual([entry["outcome"] for entry in history], ["failed", "success"])
        self.assertNotIn("secret-token", history[0]["error"])
        self.assertNotIn("data:image", history[0]["error"])
        self.assertNotIn("sig=secret", history[0]["error"])
        self.assertIsNone(state["current_target"])

        compact = task_state.compact_detail(state)
        self.assertNotIn("attempt_history", compact)
        self.assertNotIn("first full prompt", compact)
        self.assertNotIn("second full prompt", compact)

    def test_explicit_retry_preserves_local_attempt_history(self) -> None:
        state = self.make_state()
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error="first rejection",
            updated_at="2026-08-15T10:02:00+08:00",
        )
        history = json.loads(json.dumps(
            state["targets"]["box_t1"]["attempt_history"],
        ))
        task_state.prepare_retry(
            state, updated_at="2026-08-15T10:03:00+08:00",
        )
        self.assertEqual(state["targets"]["box_t1"]["attempt_history"], history)
        self.assertEqual(state["targets"]["box_t1"]["attempts"], 0)

    def test_explicit_retry_rejects_an_active_attempt_without_mutation(self) -> None:
        state = self.make_state()
        self.begin(state)
        before = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "active attempt"):
            task_state.prepare_retry(
                state, updated_at="2026-08-15T10:03:00+08:00",
            )
        self.assertEqual(state, before)

    def test_retry_invalid_timestamp_leaves_state_unchanged(self) -> None:
        state = self.make_state()
        before = json.loads(json.dumps(state))
        with self.assertRaises(task_state.TaskStateError):
            task_state.prepare_retry(state, updated_at="")
        self.assertEqual(state, before)

    def test_retry_rejects_missing_current_attachments_without_mutation(self) -> None:
        state = task_state.new_record_error_state(
            record_id="rec_1", run_id="run_1", source_tokens=[],
            target_tokens=["box_t1"], started_at="2026-08-15T10:00:00+08:00",
            code="missing-source", error="source is missing",
            updated_at="2026-08-15T10:00:01+08:00",
        )
        before = json.loads(json.dumps(state))
        with self.assertRaisesRegex(task_state.TaskStateError, "reconcile"):
            task_state.prepare_retry(
                state, updated_at="2026-08-15T10:03:00+08:00",
            )
        self.assertEqual(state, before)

    def test_cli_retry_failure_leaves_manifest_bytes_unchanged(self) -> None:
        state = task_state.new_record_error_state(
            record_id="rec_1", run_id="run_1", source_tokens=[],
            target_tokens=["box_t1"], started_at="2026-08-15T10:00:00+08:00",
            code="missing-source", error="source is missing",
            updated_at="2026-08-15T10:00:01+08:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            before = state_path.read_bytes()
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                code = task_state.main([
                    "retry", "--state", str(state_path),
                    "--updated-at", "2026-08-15T10:03:00+08:00",
                ])
            self.assertEqual(code, 1)
            self.assertEqual(state_path.read_bytes(), before)

    def test_missing_source_record_has_sanitized_compact_failure(self) -> None:
        state = task_state.new_record_error_state(
            record_id="rec_1", run_id="run_1",
            source_tokens=[], target_tokens=["box_t1"],
            started_at="2026-08-15T10:00:00+08:00",
            code="missing-source",
            error="missing source; Bearer secret-token data:image/png;base64,AAAA",
            updated_at="2026-08-15T10:00:01+08:00",
        )
        self.assertEqual(task_state.aggregate_status(state), "失败")
        detail = json.loads(task_state.compact_detail(state))
        self.assertEqual(detail["record_error"]["code"], "missing-source")
        self.assertIn("missing source", detail["record_error"]["message"])
        self.assertNotIn("secret-token", detail["record_error"]["message"])
        self.assertNotIn("data:image", detail["record_error"]["message"])
        self.assertIsNone(detail["current_target"])

    def test_existing_history_survives_missing_source_reconciliation(self) -> None:
        state = self.make_state()
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error="first rejection",
            updated_at="2026-08-15T10:02:00+08:00",
        )
        history = json.loads(json.dumps(
            state["targets"]["box_t1"]["attempt_history"],
        ))

        task_state.reconcile_error(
            state,
            source_tokens=[], target_tokens=["box_t1"],
            outputs=[],
            run_id="run_2", started_at="2026-08-15T11:00:00+08:00",
            code="missing-source", error="source attachment is missing",
            updated_at="2026-08-15T11:00:01+08:00",
        )

        self.assertEqual(state["source_tokens"], [])
        self.assertEqual(state["target_tokens"], ["box_t1"])
        self.assertEqual(state["targets"]["box_t1"]["attempt_history"], history)
        self.assertEqual(state["record_error"]["code"], "missing-source")
        self.assertEqual(state["run_id"], "run_2")

    def test_source_change_error_stales_an_unrecorded_base_output(self) -> None:
        state = self.make_state()
        old_output = {
            "file_token": "box_old_source_output",
            "name": task_state.output_name(1, "box_t1"),
        }

        task_state.reconcile_error(
            state, source_tokens=["box_new_source"], target_tokens=[],
            outputs=[old_output], run_id="run_2",
            started_at="2026-08-15T11:00:00+08:00",
            code="missing-target", error="target attachment is missing",
            updated_at="2026-08-15T11:00:01+08:00",
        )
        task_state.reconcile(
            state, source_tokens=["box_new_source"], target_tokens=["box_t1"],
            outputs=[old_output], run_id="run_3",
            started_at="2026-08-15T12:00:00+08:00",
            updated_at="2026-08-15T12:00:01+08:00",
        )

        target = state["targets"]["box_t1"]
        self.assertEqual(target["status"], "pending")
        self.assertIsNone(target["output"])
        self.assertIn(old_output["file_token"], target["stale_output_tokens"])

    def test_invalid_record_error_leaves_active_attempt_unchanged(self) -> None:
        state = self.make_state()
        self.begin(state)
        before = json.loads(json.dumps(state))
        with self.assertRaises(task_state.TaskStateError):
            task_state.record_error(
                state, code="unknown", error="bad source",
                updated_at="2026-08-15T10:02:00+08:00",
            )
        self.assertEqual(state, before)

    def test_loaded_running_state_requires_active_history(self) -> None:
        state = self.make_state()
        self.begin(state)
        state["targets"]["box_t1"]["attempt_history"] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(task_state.TaskStateError, "history"):
                task_state.load_state(path)

    def test_loaded_running_state_requires_a_budgeted_matching_attempt(self) -> None:
        state = self.make_state()
        self.begin(state)
        state["targets"]["box_t1"]["attempts"] = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(task_state.TaskStateError, "running attempt"):
                task_state.load_state(path)

    def test_cli_reconcile_refreshes_run_and_source_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            sources = root / "sources.json"
            targets = root / "targets.json"
            outputs = root / "outputs.json"
            artifacts = root / "artifacts.json"
            state_path.write_text(json.dumps(self.make_state()), encoding="utf-8")
            sources.write_text('["box_s2"]', encoding="utf-8")
            targets.write_text('["box_t1"]', encoding="utf-8")
            outputs.write_text('[]', encoding="utf-8")
            artifacts.write_text('[]', encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = task_state.main([
                    "reconcile", "--state", str(state_path),
                    "--source-tokens-json", str(sources),
                    "--target-tokens-json", str(targets),
                    "--outputs-json", str(outputs), "--run-id", "run_2",
                    "--resumable-artifacts-json", str(artifacts),
                    "--started-at", "2026-08-15T11:00:00+08:00",
                    "--updated-at", "2026-08-15T11:00:01+08:00",
                ])
            self.assertEqual(code, 0, stderr.getvalue())
            reconciled = task_state.load_state(state_path)
            self.assertEqual(reconciled["source_tokens"], ["box_s2"])
            self.assertEqual(reconciled["run_id"], "run_2")

    def test_cli_init_error_supports_missing_source_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            sources = root / "sources.json"
            targets = root / "targets.json"
            error_path = root / "error.txt"
            sources.write_text('[]', encoding="utf-8")
            targets.write_text('["box_t1"]', encoding="utf-8")
            error_path.write_text("source attachment is missing", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = task_state.main([
                    "init-error", "--state", str(state_path), "--record-id", "rec_1",
                    "--run-id", "run_1", "--started-at", "2026-08-15T10:00:00+08:00",
                    "--source-tokens-json", str(sources),
                    "--target-tokens-json", str(targets), "--code", "missing-source",
                    "--error-file", str(error_path),
                    "--updated-at", "2026-08-15T10:00:01+08:00",
                ])
            self.assertEqual(code, 0, stderr.getvalue())
            failed = task_state.load_state(state_path)
            self.assertEqual(failed["record_error"]["code"], "missing-source")

    def test_cli_record_error_sanitizes_an_initialized_corrupt_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            error_path = root / "error.txt"
            state_path.write_text(json.dumps(self.make_state()), encoding="utf-8")
            error_path.write_text(
                "decode failed?secret=raw-value", encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = task_state.main([
                    "record-error", "--state", str(state_path),
                    "--code", "corrupt-source",
                    "--error-file", str(error_path),
                    "--updated-at", "2026-08-15T10:02:00+08:00",
                ])
            self.assertEqual(code, 0)
            persisted = task_state.load_state(state_path)
            self.assertEqual(persisted["record_error"]["code"], "corrupt-source")
            self.assertNotIn("raw-value", persisted["record_error"]["message"])
            self.assertEqual(task_state.aggregate_status(persisted), "失败")

    def test_raw_error_text_is_not_accepted_as_a_cli_argument(self) -> None:
        parser = task_state._parser()
        subparsers = next(
            action for action in parser._actions
            if hasattr(action, "choices") and action.choices
        )
        for command in ("init-error", "reconcile-error", "record-error", "failure"):
            with self.subTest(command=command):
                command_help = subparsers.choices[command].format_help()
                self.assertIn("--error-file", command_help)
                self.assertNotRegex(command_help, r"--error(?:\s|=)")

    def test_mutating_cli_stdout_never_exposes_local_attempt_history_or_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            references = root / "references.json"
            prompt = root / "prompt.txt"
            state_path.write_text(json.dumps(self.make_state()), encoding="utf-8")
            references.write_text('["box_s1"]', encoding="utf-8")
            prompt.write_text("PRIVATE FULL PROMPT", encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = task_state.main([
                    "attempt", "--state", str(state_path),
                    "--target-token", "box_t1", "--classification", "front",
                    "--references-json", str(references),
                    "--prompt-file", str(prompt), "--model", "image-model",
                    "--updated-at", "2026-08-15T10:01:00+08:00",
                ])

            self.assertEqual(code, 0)
            emitted = stdout.getvalue()
            self.assertNotIn("PRIVATE FULL PROMPT", emitted)
            self.assertNotIn("attempt_history", emitted)
            self.assertIn(
                "PRIVATE FULL PROMPT",
                state_path.read_text(encoding="utf-8"),
                "the full audit trail must remain in the local manifest",
            )

    def test_unknown_schema_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = self.make_state()
            state["schema_version"] = 999
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(
                task_state.TaskStateError, "unsupported schema version",
            ):
                task_state.load_state(path)

    def test_baseline_v1_manifest_is_deterministically_migrated(self) -> None:
        legacy = {
            "schema_version": 1, "record_id": "rec_1", "run_id": "run_1",
            "started_at": "2026-08-15T10:00:00+08:00",
            "source_tokens": ["box_s1"], "target_tokens": ["box_t1", "box_t2"],
            "targets": {
                "box_t1": {
                    "status": "success", "classification": "front",
                    "reference_tokens": ["box_s1"], "attempts": 1,
                    "output": {"file_token": "box_out", "name": task_state.output_name(1, "box_t1")},
                    "prompt_sha256": "digest", "model": "image-model",
                    "error": None, "updated_at": "2026-08-15T10:02:00+08:00",
                },
                "box_t2": {
                    "status": "pending", "classification": None,
                    "reference_tokens": [], "attempts": 0, "output": None,
                    "prompt_sha256": None, "model": None,
                    "error": (
                        "Bearer legacy-token secret=raw-value "
                        "https://example.invalid/a?sig=raw "
                        "data:image/png;base64,AAAA"
                    ),
                    "updated_at": "2026-08-15T10:00:00+08:00",
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            migrated = task_state.load_state(path)
        self.assertEqual(migrated["schema_version"], 4)
        self.assertEqual(migrated["targets"]["box_t1"]["status"], "success")
        self.assertEqual(migrated["targets"]["box_t1"]["output"]["file_token"], "box_out")
        self.assertEqual(migrated["targets"]["box_t1"]["attempt_history"], [])
        self.assertEqual(migrated["targets"]["box_t1"]["stale_output_tokens"], [])
        self.assertIsNone(migrated["current_target"])
        compact = task_state.compact_detail(migrated)
        for secret in ("legacy-token", "raw-value", "sig=raw", "data:image"):
            self.assertNotIn(secret, compact)

    def test_canonical_manifest_path_is_stable_across_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_1_manifest = root / "runs" / "run_1" / "rec" / "manifest.json"
            run_2_manifest = root / "runs" / "run_2" / "rec" / "manifest.json"
            state_1 = task_state.bind_manifest(
                state_root=root / "state", base_token="app_x", table_id="tbl_x",
                record_id="rec_1", run_manifest=run_1_manifest,
            )
            state_2 = task_state.bind_manifest(
                state_root=root / "state", base_token="app_x", table_id="tbl_x",
                record_id="rec_1", run_manifest=run_2_manifest,
            )
            self.assertEqual(state_1, state_2)
            task_state._atomic_write(state_1, self.make_state())
            active = task_state.load_state(run_1_manifest)
            self.begin(active)
            task_state._atomic_write(state_1, active)
            persisted = task_state.load_state(run_2_manifest)
            self.assertEqual(
                len(persisted["targets"]["box_t1"]["attempt_history"]), 1,
            )

    def test_atomic_manifest_write_syncs_file_then_rename_then_directory(self) -> None:
        events: list[str] = []
        real_replace = task_state.os.replace

        def replace(source: str, destination: str) -> None:
            events.append("replace")
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    task_state.os, "fsync",
                    side_effect=lambda _descriptor: events.append("file-fsync"),
                ), \
                mock.patch.object(task_state.os, "replace", side_effect=replace), \
                mock.patch.object(
                    task_state, "_fsync_directory",
                    side_effect=lambda _path: events.append("directory-fsync"),
                ):
            task_state._atomic_write(
                Path(directory) / "state.json", self.make_state(),
            )

        self.assertEqual(events, ["file-fsync", "replace", "directory-fsync"])

    def test_bind_cli_does_not_allow_a_caller_selected_state_root(self) -> None:
        parser = task_state._parser()
        subparsers = next(
            action for action in parser._actions
            if hasattr(action, "choices") and action.choices
        )
        self.assertNotIn(
            "--state-root", subparsers.choices["bind"].format_help(),
        )

    def test_durable_metadata_rejects_lone_surrogates(self) -> None:
        state = self.make_state()
        with self.assertRaises(task_state.TaskStateError):
            task_state.record_selection_reason(
                state, 0, {"reason": "bad\ud800text"},
            )

    def test_cli_argument_errors_use_task_state_contract(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = task_state.main(["pending"])
        self.assertEqual(code, 1)
        self.assertTrue(stderr.getvalue().startswith("task-state error: "))

    def test_cli_malformed_json_uses_task_state_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            targets = root / "targets.json"
            source.write_text("{", encoding="utf-8")
            targets.write_text("[]", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = task_state.main([
                    "init", "--state", str(root / "state.json"), "--record-id", "rec",
                    "--run-id", "run", "--started-at", "2026-08-15T10:00:00+08:00",
                    "--source-tokens-json", str(source), "--target-tokens-json", str(targets),
                ])
            self.assertEqual(code, 1)
            self.assertTrue(stderr.getvalue().startswith("task-state error: "))

    def test_cli_malformed_state_uses_task_state_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = self.make_state()
            state["targets"]["box_t1"] = {"status": "pending"}
            path.write_text(json.dumps(state), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = task_state.main(["pending", "--state", str(path)])
            self.assertEqual(code, 1)
            self.assertTrue(stderr.getvalue().startswith("task-state error: "))

    def test_cli_object_references_json_uses_task_state_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            references = root / "references.json"
            prompt = root / "prompt.txt"
            state_path.write_text(json.dumps(self.make_state()), encoding="utf-8")
            references.write_text('{"box_s1": true}', encoding="utf-8")
            prompt.write_text("prompt", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = task_state.main([
                    "attempt", "--state", str(state_path), "--target-token", "box_t1",
                    "--classification", "casual", "--references-json", str(references),
                    "--prompt-file", str(prompt), "--model", "image-model",
                    "--updated-at", "2026-08-15T10:01:00+08:00",
                ])
            self.assertEqual(code, 1)
            self.assertTrue(stderr.getvalue().startswith("task-state error: "))


if __name__ == "__main__":
    unittest.main()
