"""End-to-end unit tests for one resumable serial record worker."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts import prompt_builder, task_state, vision_qc
from scripts.event_log import EventLog
from scripts.finalize_target import TargetFinalizer
from scripts.run_record import (
    GenerationRequest,
    QCRequest,
    RecordContext,
    RecordServices,
    main,
    run_record,
)


def write_png(path: Path, shade: int = 128) -> None:
    width = height = 32
    raw = b"".join(
        b"\x00" + bytes((shade, shade, shade)) * width for _ in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


class FakeClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"2026-08-18T10:00:{self.calls:02d}+00:00"


class FakeStopSignal:
    def __init__(self, stopped: bool = False) -> None:
        self.stopped = stopped
        self.checks = 0
        self.on_check = None

    def is_set(self) -> bool:
        self.checks += 1
        if self.on_check is not None:
            callback = self.on_check
            self.on_check = None
            callback()
        return self.stopped

    def set(self) -> None:
        self.stopped = True


class RecordingEvents:
    def __init__(self, trace: list[tuple] | None = None) -> None:
        self.items: list[dict] = []
        self.trace = trace

    def append(self, event: str, /, **fields: object) -> dict:
        item = {"event": event, **fields}
        self.items.append(item)
        if self.trace is not None and event in {
                "generation_started", "qc_started", "finalize_started",
        }:
            self.trace.append((event, fields.get("target_id"), fields.get("attempt")))
        return item


class FakeBase:
    def __init__(self, trace: list[tuple], task_dir: Path) -> None:
        self.trace = trace
        self.task_dir = task_dir
        self.outputs: list[dict[str, str]] = []
        self.detail: str | None = None
        self.upload_calls = 0
        self.update_calls = 0
        self.get_calls = 0

    def upload_attachment(self, **kwargs: object) -> dict:
        self.upload_calls += 1
        name = Path(kwargs["file"]).name
        mapping = {"file_token": f"box_output_{self.upload_calls}", "name": name}
        self.outputs.append(mapping)
        return {"data": {"attachments": {
            kwargs["record_id"]: {kwargs["field_id"]: [mapping]},
        }}}

    def update_record(self, **kwargs: object) -> dict:
        self.update_calls += 1
        payload_path = self.task_dir / Path(kwargs["payload"])
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.detail = payload["update_records"][kwargs["record_id"]]["处理明细"]
        return {"ok": True}

    def get_record(self, **kwargs: object) -> dict:
        self.get_calls += 1
        return {"data": {
            "fields": ["输出图", "处理明细"],
            "data": [[list(self.outputs), self.detail]],
            "record_id_list": [kwargs["record_id"]],
        }}


class FakeRecordFinalizer:
    """Record-level adapter around the real idempotent target finalizer."""

    def __init__(
            self, base: FakeBase, clock: FakeClock, trace: list[tuple],
    ) -> None:
        self.base = base
        self.trace = trace
        self.calls = []
        self.fail_reconcile = False
        self.target = TargetFinalizer(
            base=base, app_token="app_1", table_id="tbl_1",
            output_field_id="fld_output", detail_field_id="fld_detail",
            clock=clock,
        )

    def reconcile_record(
            self, context: RecordContext, state_file: Path,
            target_indices: tuple[int, ...],
    ) -> None:
        self.trace.append(("base-reconcile",))
        if self.fail_reconcile:
            raise RuntimeError("Base unavailable")
        state = task_state.load_state(state_file)
        changed = False
        for index in target_indices:
            token = state["target_tokens"][index]
            if state["targets"][token]["status"] != "accepted-local":
                continue
            mapping = task_state.reconcile_target_output(
                state, target_index=index, outputs=self.base.outputs,
                updated_at="2026-08-18T10:00:00+00:00",
            )
            changed = changed or mapping is not None
        if changed:
            task_state.save_state(state_file, state)

    def finalize(self, request: object) -> object:
        self.calls.append(request)
        self.trace.append(("finalize", request.target_index, request.candidate.name))
        return self.target.finalize(request)


def plan() -> prompt_builder.TargetPlan:
    return prompt_builder.TargetPlan(
        classification="front",
        selected_references=(
            prompt_builder.SelectedReference("box_source_1", "model"),
        ),
        garment_facts=prompt_builder.GarmentFacts(
            required=("closed front supported by evidence",),
            forbidden=("open cardigan front",),
        ),
        infographic_inventory=None,
    )


class FakeGenerator:
    model = "doubao-seedream-5-0-pro-260628"

    def __init__(self, trace: list[tuple]) -> None:
        self.trace = trace
        self.calls: list[GenerationRequest] = []
        self.plan_calls: list[int] = []
        self.plan_errors: dict[int, BaseException] = {}
        self.outcomes: dict[tuple[int, int], str] = {}
        self.transport_calls: list[tuple[int, int, int]] = []

    def plan_target(
            self, context: RecordContext, target_index: int,
            target_token: str,
    ) -> prompt_builder.TargetPlan:
        del context, target_token
        self.plan_calls.append(target_index)
        self.trace.append(("plan", target_index))
        if target_index in self.plan_errors:
            raise self.plan_errors[target_index]
        return plan()

    def artifact_path(self, context: RecordContext, history: dict) -> Path:
        return context.task_dir / "generated_images" / history["artifact_name"]

    def generate(self, request: GenerationRequest) -> Path:
        self.calls.append(request)
        self.trace.append(("generate", request.target_index, request.attempt))
        outcome = self.outcomes.get((request.target_index, request.attempt), "valid")
        if outcome == "invalid":
            request.output_path.write_bytes(b"not a decodable image")
        elif outcome == "transport-retry":
            self.transport_calls.extend([
                (request.target_index, request.attempt, 1),
                (request.target_index, request.attempt, 2),
            ])
            write_png(request.output_path, 100 + request.attempt)
        elif outcome == "error":
            raise RuntimeError("seedream transport unavailable")
        else:
            self.transport_calls.append((request.target_index, request.attempt, 1))
            write_png(request.output_path, 100 + request.attempt)
        return request.output_path


def report(
        candidate: str, *, garment: int = 96, color: int = 94,
        details: int = 93, preservation: int = 92,
        defects: tuple[vision_qc.DefectCode, ...] = (),
        decision: str = "accept",
) -> vision_qc.QCReport:
    return vision_qc.QCReport(
        candidate=candidate,
        scores=vision_qc.Scores(
            garment_construction=garment,
            color_material=color,
            garment_details=details,
            target_preservation=preservation,
            text_layout=None,
        ),
        critical_defects=defects,
        primary_defect=defects[-1] if defects else None,
        confidence=0.95,
        decision=decision,
    )


class FakeQC:
    def __init__(self, trace: list[tuple]) -> None:
        self.trace = trace
        self.calls: list[QCRequest] = []
        self.responses: dict[tuple[int, int], object] = {}

    def review(self, request: QCRequest) -> vision_qc.QCReport:
        self.calls.append(request)
        self.trace.append(("qc", request.target_index, request.attempt))
        value = self.responses.get((request.target_index, request.attempt))
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(request)
        if isinstance(value, vision_qc.QCReport):
            return value
        return report(request.candidate.name)


class RunRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "generated_images").mkdir()
        self.state_file = self.root / "manifest.json"
        self.trace: list[tuple] = []
        self.clock = FakeClock()
        self.stop = FakeStopSignal()
        self.events = RecordingEvents(self.trace)
        self.generator = FakeGenerator(self.trace)
        self.qc = FakeQC(self.trace)
        self.base = FakeBase(self.trace, self.root / "generated_images")
        self.finalizer = FakeRecordFinalizer(
            self.base, self.clock, self.trace,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def initialize(self, targets: int = 1) -> RecordContext:
        tokens = [f"box_target_{index}" for index in range(1, targets + 1)]
        state = task_state.new_state(
            record_id="rec_1", run_id="run_1",
            source_tokens=["box_source_1"], target_tokens=tokens,
            started_at=self.clock(),
        )
        task_state.save_state(self.state_file, state)
        return RecordContext(
            task_dir=self.root, record_id="rec_1",
            target_indices=tuple(range(targets)),
        )

    def services(self, *, events: object | None = None) -> RecordServices:
        return RecordServices(
            generator=self.generator, qc=self.qc, finalizer=self.finalizer,
            events=self.events if events is None else events,
            stop_signal=self.stop, clock=self.clock,
        )

    def active_artifact(self, state: dict, index: int = 0) -> Path:
        token = state["target_tokens"][index]
        history = state["targets"][token]["attempt_history"][-1]
        return self.root / "generated_images" / history["artifact_name"]

    def begin(self, state: dict, index: int = 0, prompt: str = "initial") -> Path:
        token = state["target_tokens"][index]
        task_state.record_target_plan(
            state, index, json.loads(prompt_builder.serialize_plan(plan())),
        )
        task_state.begin_attempt(
            state, target_token=token, classification="front",
            reference_tokens=["box_source_1"], prompt=prompt,
            model=self.generator.model, updated_at=self.clock(),
        )
        path = self.active_artifact(state, index)
        task_state.save_state(self.state_file, state)
        return path

    @staticmethod
    def qc_payload(
            attempt: int, digest: str, value: vision_qc.QCReport,
    ) -> dict:
        return {
            "attempt": attempt,
            "artifact_name": value.candidate,
            "artifact_sha256": digest,
            "report": {
                "schema_version": 1,
                "candidate": value.candidate,
                "scores": {
                    "garment_construction": value.scores.garment_construction,
                    "color_material": value.scores.color_material,
                    "garment_details": value.scores.garment_details,
                    "target_preservation": value.scores.target_preservation,
                    "text_layout": value.scores.text_layout,
                },
                "critical_defects": [item.value for item in value.critical_defects],
                "primary_defect": (
                    None if value.primary_defect is None
                    else value.primary_defect.value
                ),
                "evidence": [],
                "confidence": value.confidence,
                "decision": value.decision,
            },
        }

    def test_two_targets_remain_serial_with_early_then_corrected_acceptance(self) -> None:
        context = self.initialize(targets=2)
        self.qc.responses[(1, 1)] = lambda request: report(
            request.candidate.name, garment=70,
            defects=(
                vision_qc.DefectCode.WRONG_COLOR,
                vision_qc.DefectCode.OPEN_FRONT,
            ),
            decision="retry",
        )

        result = run_record(context, self.services())

        state = task_state.load_state(self.state_file)
        self.assertEqual((result.status, result.accepted_targets), ("success", 2))
        self.assertEqual(
            [(call.target_index, call.attempt) for call in self.generator.calls],
            [(0, 1), (1, 1), (1, 2)],
        )
        self.assertEqual(
            [state["targets"][token]["attempts"] for token in state["target_tokens"]],
            [1, 2],
        )
        retry_prompt = self.generator.calls[-1].prompt
        self.assertIn("front opening", retry_prompt)
        self.assertNotIn("Match the evidenced garment color", retry_prompt)
        self.assertTrue(all(
            state["targets"][token]["status"] == "success"
            for token in state["target_tokens"]
        ))

    def test_three_rejections_select_the_garment_best_candidate(self) -> None:
        context = self.initialize()
        rankings = {
            1: (91, 98, 98, 98),
            2: (97, 80, 80, 80),
            3: (95, 99, 99, 99),
        }
        for attempt, scores in rankings.items():
            self.qc.responses[(0, attempt)] = lambda request, values=scores: report(
                request.candidate.name, garment=values[0], color=values[1],
                details=values[2], preservation=values[3],
                defects=(vision_qc.DefectCode.WRONG_COLOR,), decision="reject",
            )

        result = run_record(context, self.services())

        state = task_state.load_state(self.state_file)
        selected = state["targets"]["box_target_1"]["selection_reason"]
        self.assertEqual(result.status, "success")
        self.assertEqual(selected["attempt"], 2)
        self.assertEqual(self.finalizer.calls[-1].candidate.name, selected["artifact_name"])
        self.assertEqual(len(self.generator.calls), 3)

    def test_true_final_selection_tie_chooses_the_earlier_attempt(self) -> None:
        context = self.initialize()
        for attempt in (1, 2, 3):
            self.qc.responses[(0, attempt)] = lambda request: report(
                request.candidate.name, garment=80, color=80, details=80,
                preservation=80,
                defects=(vision_qc.DefectCode.WRONG_COLOR,), decision="reject",
            )

        run_record(context, self.services())

        selected = task_state.load_state(self.state_file)["targets"][
            "box_target_1"
        ]["selection_reason"]
        self.assertEqual(selected["attempt"], 1)
        self.assertTrue(selected["artifact_name"].endswith("-01.png"))

    def test_invalid_artifact_spends_attempt_while_transport_retries_stay_inside_next_attempt(self) -> None:
        context = self.initialize()
        self.generator.outcomes[(0, 1)] = "invalid"
        self.generator.outcomes[(0, 2)] = "transport-retry"

        result = run_record(context, self.services())

        target = task_state.load_state(self.state_file)["targets"]["box_target_1"]
        self.assertEqual(result.status, "success")
        self.assertEqual(target["attempts"], 2)
        self.assertEqual([entry["outcome"] for entry in target["attempt_history"]], [
            "failed", "success",
        ])
        self.assertEqual(
            self.generator.transport_calls,
            [(0, 2, 1), (0, 2, 2)],
        )
        self.assertEqual([(call.attempt) for call in self.qc.calls], [2])

    def test_artifact_integrity_failure_reuses_prompt_without_visual_correction(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state, prompt="unchanged transport prompt")
        write_png(candidate)
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        stale_visual_report = report(
            candidate.name, garment=70,
            defects=(vision_qc.DefectCode.OPEN_FRONT,), decision="retry",
        )
        task_state.record_qc_report(
            state, 0, self.qc_payload(1, digest, stale_visual_report),
        )
        task_state.save_state(self.state_file, state)
        candidate.write_bytes(b"artifact bytes changed after QC")

        result = run_record(context, self.services())

        self.assertEqual(result.status, "success")
        self.assertEqual([(call.attempt) for call in self.generator.calls], [2])
        self.assertEqual(
            self.generator.calls[0].prompt, "unchanged transport prompt",
        )
        self.assertNotIn("Retry correction", self.generator.calls[0].prompt)

    def test_restart_from_active_artifact_runs_pending_qc_without_generation(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state)
        write_png(candidate)

        result = run_record(context, self.services())

        self.assertEqual(result.status, "success")
        self.assertEqual(len(self.generator.calls), 0)
        self.assertEqual([(call.attempt) for call in self.qc.calls], [1])

    def test_stop_before_pending_qc_preserves_artifact_without_reviewer_call(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state)
        write_png(candidate)
        self.stop.stopped = True

        result = run_record(context, self.services())

        target = task_state.load_state(self.state_file)["targets"]["box_target_1"]
        self.assertEqual(result.status, "stopped")
        self.assertEqual((target["status"], target["attempts"]), ("running", 1))
        self.assertEqual(len(self.qc.calls), 0)
        self.assertTrue(candidate.is_file())

    def test_stale_current_cycle_before_final_selection_qc_calls_no_reviewer(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        for attempt in (1, 2):
            candidate = self.begin(state, prompt=f"attempt {attempt}")
            write_png(candidate, 100 + attempt)
            task_state.record_failure(
                state, target_token="box_target_1", error="visual rejection",
                updated_at=self.clock(),
            )
        self.begin(state, prompt="attempt 3")

        def stale_current_cycle() -> None:
            stale = task_state.load_state(self.state_file)
            task_state.record_error(
                stale, code="external-call", error="concurrent state change",
                updated_at=self.clock(),
            )
            task_state.save_state(self.state_file, stale)

        self.stop.on_check = stale_current_cycle

        result = run_record(context, self.services())

        self.assertEqual(result.status, "stopped")
        self.assertEqual(len(self.qc.calls), 0)
        self.assertEqual(
            task_state.load_state(self.state_file)["record_error"]["code"],
            "external-call",
        )

    def test_restart_from_persisted_qc_decision_does_not_repeat_qc(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state)
        write_png(candidate)
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        persisted = report(
            candidate.name, garment=70,
            defects=(vision_qc.DefectCode.OPEN_FRONT,), decision="retry",
        )
        task_state.record_qc_report(state, 0, self.qc_payload(1, digest, persisted))
        task_state.save_state(self.state_file, state)

        result = run_record(context, self.services())

        target = task_state.load_state(self.state_file)["targets"]["box_target_1"]
        self.assertEqual(result.status, "success")
        self.assertEqual([(call.attempt) for call in self.generator.calls], [2])
        self.assertEqual([(call.attempt) for call in self.qc.calls], [2])
        self.assertEqual(target["attempts"], 2)

    def test_restart_drains_accepted_local_before_generation(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state)
        write_png(candidate)
        task_state.record_local_acceptance(
            state, target_token="box_target_1", artifact_name=candidate.name,
            name=task_state.promoted_output_name(candidate.name, "box_target_1"),
            updated_at=self.clock(),
        )
        task_state.save_state(self.state_file, state)

        result = run_record(context, self.services())

        self.assertEqual(result.status, "success")
        self.assertEqual(len(self.generator.calls), 0)
        self.assertEqual(len(self.qc.calls), 0)
        self.assertEqual(len(self.finalizer.calls), 1)

    def test_base_reconciliation_recovers_an_uploaded_attachment_first(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state)
        write_png(candidate)
        name = task_state.promoted_output_name(candidate.name, "box_target_1")
        task_state.record_local_acceptance(
            state, target_token="box_target_1", artifact_name=candidate.name,
            name=name, updated_at=self.clock(),
        )
        task_state.save_state(self.state_file, state)
        self.base.outputs.append({"file_token": "box_already_uploaded", "name": name})

        result = run_record(context, self.services())

        target = task_state.load_state(self.state_file)["targets"]["box_target_1"]
        self.assertEqual(result.status, "success")
        self.assertEqual(target["output"]["file_token"], "box_already_uploaded")
        self.assertEqual(len(self.finalizer.calls), 0)
        self.assertEqual(len(self.generator.calls), 0)
        self.assertEqual(self.trace[0], ("base-reconcile",))

    def test_terminal_record_is_a_generation_noop_after_reconciliation(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state)
        write_png(candidate)
        task_state.record_local_acceptance(
            state, target_token="box_target_1", artifact_name=candidate.name,
            name=task_state.promoted_output_name(candidate.name, "box_target_1"),
            updated_at=self.clock(),
        )
        task_state.record_success(
            state, target_token="box_target_1", file_token="box_done",
            name=task_state.promoted_output_name(candidate.name, "box_target_1"),
            updated_at=self.clock(),
        )
        task_state.save_state(self.state_file, state)
        self.base.outputs.append(state["targets"]["box_target_1"]["output"])

        result = run_record(context, self.services())

        self.assertEqual((result.status, result.accepted_targets), ("success", 1))
        self.assertEqual(len(self.generator.calls), 0)
        self.assertEqual(len(self.qc.calls), 0)
        self.assertEqual(len(self.finalizer.calls), 0)
        self.assertEqual(self.trace[0], ("base-reconcile",))

    def test_recovery_order_drains_acceptance_then_qcs_active_artifact(self) -> None:
        context = self.initialize(targets=2)
        state = task_state.load_state(self.state_file)
        first = self.begin(state, 0)
        write_png(first)
        task_state.record_local_acceptance(
            state, target_token="box_target_1", artifact_name=first.name,
            name=task_state.promoted_output_name(first.name, "box_target_1"),
            updated_at=self.clock(),
        )
        second = self.begin(state, 1)
        write_png(second)

        result = run_record(context, self.services())

        self.assertEqual(result.status, "success")
        key_steps = [item for item in self.trace if item[0] in {
            "base-reconcile", "finalize", "qc", "generate",
        }]
        self.assertEqual(key_steps[0], ("base-reconcile",))
        self.assertEqual(key_steps[1][0:2], ("finalize", 0))
        self.assertEqual(key_steps[2], ("qc", 1, 1))
        self.assertEqual(key_steps[3][0:2], ("finalize", 1))
        self.assertFalse(any(item[0] == "generate" for item in key_steps))

    def test_later_planning_failure_waits_for_earlier_active_recovery(self) -> None:
        context = self.initialize(targets=2)
        state = task_state.load_state(self.state_file)
        first = self.begin(state, 0)
        write_png(first)
        self.generator.plan_errors[1] = RuntimeError("second plan unavailable")

        result = run_record(context, self.services())

        persisted = task_state.load_state(self.state_file)
        self.assertEqual(result.status, "failed")
        self.assertEqual(persisted["targets"]["box_target_1"]["status"], "success")
        self.assertEqual(persisted["targets"]["box_target_2"]["status"], "pending")
        finalize_position = next(
            index for index, item in enumerate(self.trace)
            if item[0:2] == ("finalize", 0)
        )
        second_plan_position = self.trace.index(("plan", 1))
        self.assertLess(finalize_position, second_plan_position)

    def test_persisted_plan_recovers_active_artifact_without_planner(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state)
        write_png(candidate)
        self.generator.plan_calls.clear()
        self.generator.plan_errors[0] = RuntimeError("planner must not run")

        result = run_record(context, self.services())

        self.assertEqual(result.status, "success")
        self.assertEqual(self.generator.plan_calls, [])
        self.assertEqual(len(self.qc.calls), 1)
        self.assertEqual(
            task_state.load_state(self.state_file)["targets"]["box_target_1"][
                "status"
            ],
            "success",
        )

    def test_missing_active_artifact_spends_attempt_before_new_generation(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        self.begin(state)

        result = run_record(context, self.services())

        target = task_state.load_state(self.state_file)["targets"]["box_target_1"]
        self.assertEqual(result.status, "success")
        self.assertEqual(target["attempts"], 2)
        self.assertEqual(target["attempt_history"][0]["outcome"], "failed")
        self.assertEqual([(call.attempt) for call in self.generator.calls], [2])

    def test_stop_observed_before_paid_call_leaves_target_pending(self) -> None:
        context = self.initialize()
        self.stop.stopped = True

        result = run_record(context, self.services())

        target = task_state.load_state(self.state_file)["targets"]["box_target_1"]
        self.assertEqual(result.status, "stopped")
        self.assertEqual(target["status"], "pending")
        self.assertEqual(target["attempts"], 0)
        self.assertEqual(len(self.generator.calls), 0)
        self.assertTrue(any(item["event"] == "stop_observed" for item in self.events.items))

    def test_persistent_qc_failure_preserves_candidate_and_starts_no_generation(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        candidate = self.begin(state)
        write_png(candidate)
        before = candidate.read_bytes()
        self.qc.responses[(0, 1)] = RuntimeError("ark unavailable")

        result = run_record(context, self.services())

        target = task_state.load_state(self.state_file)["targets"]["box_target_1"]
        self.assertEqual(result.status, "stopped")
        self.assertEqual((target["status"], target["attempts"]), ("running", 1))
        self.assertEqual(candidate.read_bytes(), before)
        self.assertIn("QC", target["error"])
        self.assertEqual(len(self.generator.calls), 0)

    def test_exhausted_missing_attempt_three_never_starts_attempt_four(self) -> None:
        context = self.initialize()
        state = task_state.load_state(self.state_file)
        for attempt in (1, 2):
            candidate = self.begin(state, prompt=f"attempt {attempt}")
            task_state.record_failure(
                state, target_token="box_target_1", error="unusable artifact",
                updated_at=self.clock(),
            )
            self.assertFalse(candidate.exists())
        self.begin(state, prompt="attempt 3")

        result = run_record(context, self.services())

        persisted = task_state.load_state(self.state_file)
        target = persisted["targets"]["box_target_1"]
        self.assertEqual(result.status, "failed")
        self.assertEqual(target["attempts"], 3)
        self.assertEqual(len(self.generator.calls), 0)
        self.assertEqual(persisted["record_error"]["code"], "external-call")

    def test_worker_emits_only_events_accepted_by_the_real_allowlist(self) -> None:
        context = self.initialize()
        event_path = self.root / "events.ndjson"
        milliseconds = iter(range(1, 100))
        events = EventLog(event_path, clock_ms=lambda: next(milliseconds))

        result = run_record(context, self.services(events=events))

        self.assertEqual(result.status, "success")
        decoded = [json.loads(line) for line in event_path.read_text().splitlines()]
        self.assertTrue(decoded)
        self.assertEqual(decoded[0]["event"], "record_started")
        self.assertEqual(decoded[-1]["event"], "record_finished")
        self.assertEqual(
            [item["event"] for item in decoded].count("target_started"), 1,
        )

    def test_base_reconciliation_failure_is_durable_and_stops_paid_work(self) -> None:
        context = self.initialize()
        self.finalizer.fail_reconcile = True

        result = run_record(context, self.services())

        state = task_state.load_state(self.state_file)
        self.assertEqual(result.status, "failed")
        self.assertEqual(state["record_error"]["code"], "external-call")
        self.assertEqual(len(self.generator.calls), 0)

    def test_diagnostic_cli_is_read_only_and_rejects_negative_target_indices(self) -> None:
        self.initialize()
        before = self.state_file.read_bytes()
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            status = main([
                "--task-dir", str(self.root), "--record-id", "rec_1",
                "--target-index", "0",
            ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(self.state_file.read_bytes(), before)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            invalid = main([
                "--task-dir", str(self.root), "--record-id", "rec_1",
                "--target-index", "-1",
            ])
        self.assertEqual(invalid, 1)
        self.assertEqual(self.state_file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
