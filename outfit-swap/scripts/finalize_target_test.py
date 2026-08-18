"""Restart and failure tests for transactional target finalization."""

from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from scripts import task_state
from scripts.finalize_target import FinalizeError, FinalizeRequest, TargetFinalizer


def write_png(path: Path, width: int = 64, height: int = 64) -> None:
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


class FakeBase:
    def __init__(self, task_dir: Path) -> None:
        self.task_dir = task_dir
        self.outputs: list[dict[str, str]] = []
        self.detail: str | None = None
        self.upload_calls = 0
        self.update_calls = 0
        self.get_calls = 0
        self.get_field_ids: list[tuple[str, ...]] = []
        self.fail_upload = False
        self.fail_update = False
        self.drop_update = False

    def upload_attachment(self, **kwargs: object) -> dict:
        self.upload_calls += 1
        if self.fail_upload:
            raise RuntimeError("upload unavailable")
        attachment = {
            "file_token": "box_uploaded_1", "name": Path(kwargs["file"]).name,
        }
        self.outputs.append(attachment)
        return {"data": {"attachments": {
            "rec_1": {"fld_output": [{**attachment, "size": 123}]},
        }}}

    def update_record(self, **kwargs: object) -> dict:
        self.update_calls += 1
        if self.fail_update:
            raise RuntimeError("detail unavailable")
        payload = json.loads(
            (self.task_dir / Path(kwargs["payload"]).name).read_text(encoding="utf-8"),
        )
        if not self.drop_update:
            self.detail = payload["update_records"]["rec_1"]["处理明细"]
        return {"ok": True}

    def get_record(self, **kwargs: object) -> dict:
        self.get_calls += 1
        self.get_field_ids.append(tuple(kwargs["field_ids"]))
        return {"data": {
            "fields": ["输出图", "处理明细"],
            "data": [[list(self.outputs), self.detail]],
            "record_id_list": ["rec_1"],
        }}


class FinalizeTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state_file = self.root / "state.json"
        self.target_token = "box_target_1"
        self.state = task_state.new_state(
            record_id="rec_1", run_id="run_1", source_tokens=["box_source_1"],
            target_tokens=[self.target_token], started_at="2026-08-18T10:00:00+08:00",
        )
        task_state.begin_attempt(
            self.state, target_token=self.target_token, classification="front",
            reference_tokens=["box_source_1"], prompt="swap garment",
            model="image-model", updated_at="2026-08-18T10:01:00+08:00",
        )
        self.candidate_name = self.state["targets"][self.target_token][
            "attempt_history"
        ][-1]["artifact_name"]
        self.candidate = self.root / self.candidate_name
        write_png(self.candidate)
        self.digest = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        task_state.save_state(self.state_file, self.state)
        self.base = FakeBase(self.root)
        self.finalizer = TargetFinalizer(
            base=self.base, app_token="app_token", table_id="tbl_1",
            output_field_id="fld_output", detail_field_id="fld_detail",
            clock=lambda: "2026-08-18T10:02:00+08:00",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def request(self) -> FinalizeRequest:
        return FinalizeRequest(
            task_dir=self.root, state_file=self.state_file, record_id="rec_1",
            target_index=0, candidate=self.candidate,
            candidate_sha256=self.digest,
        )

    def output_path(self) -> Path:
        return self.root / task_state.output_name(1, self.target_token)

    def accept_locally(self) -> None:
        from scripts import image_qc
        image_qc.promote_output(self.candidate, self.output_path())
        task_state.record_local_acceptance(
            self.state, target_token=self.target_token,
            artifact_name=self.candidate.name, name=self.output_path().name,
            updated_at="2026-08-18T10:01:30+08:00",
        )
        task_state.save_state(self.state_file, self.state)

    def mark_success(self, token: str = "box_uploaded_1") -> None:
        task_state.record_success(
            self.state, target_token=self.target_token, file_token=token,
            name=self.output_path().name,
            updated_at="2026-08-18T10:01:45+08:00",
        )
        task_state.save_state(self.state_file, self.state)

    def test_candidate_checkpoint_promotes_accepts_uploads_and_verifies(self) -> None:
        result = self.finalizer.finalize(self.request())
        persisted = task_state.load_state(self.state_file)
        self.assertEqual(result.resumed_from, "candidate")
        self.assertEqual(result.output_path, self.output_path().resolve())
        self.assertEqual(result.attachment_token, "box_uploaded_1")
        self.assertEqual(persisted["targets"][self.target_token]["status"], "success")
        self.assertEqual(self.base.upload_calls, 1)
        self.assertEqual(self.base.update_calls, 1)
        self.assertEqual(self.base.detail, task_state.compact_detail(persisted))
        self.assertEqual(
            self.base.get_field_ids,
            [("fld_output", "fld_detail"), ("fld_output", "fld_detail")],
        )

    def test_accepted_local_checkpoint_does_not_reaccept_or_regenerate(self) -> None:
        self.accept_locally()
        result = self.finalizer.finalize(self.request())
        self.assertEqual(result.resumed_from, "accepted-local")
        self.assertEqual(self.base.upload_calls, 1)
        self.assertEqual(task_state.load_state(self.state_file)["targets"][
            self.target_token
        ]["status"], "success")

    def test_uploaded_attachment_is_reconciled_before_upload(self) -> None:
        self.accept_locally()
        self.base.outputs = [{
            "file_token": "box_preexisting", "name": self.output_path().name,
        }]
        result = self.finalizer.finalize(self.request())
        self.assertEqual(result.resumed_from, "uploaded")
        self.assertEqual(result.attachment_token, "box_preexisting")
        self.assertEqual(self.base.upload_calls, 0)
        self.assertEqual(self.base.update_calls, 1)

    def test_matching_success_and_base_readback_returns_without_writes(self) -> None:
        self.accept_locally()
        self.mark_success()
        persisted = task_state.load_state(self.state_file)
        self.base.outputs = [persisted["targets"][self.target_token]["output"]]
        self.base.detail = task_state.compact_detail(persisted)
        result = self.finalizer.finalize(self.request())
        self.assertEqual(result.resumed_from, "verified")
        self.assertEqual(self.base.upload_calls, 0)
        self.assertEqual(self.base.update_calls, 0)
        self.assertEqual(self.base.get_calls, 1)

    def test_success_from_prior_run_can_finish_missing_detail_write(self) -> None:
        self.accept_locally()
        self.mark_success()
        persisted = task_state.load_state(self.state_file)
        persisted["run_id"] = "run_2"
        task_state.save_state(self.state_file, persisted)
        self.base.outputs = [persisted["targets"][self.target_token]["output"]]

        result = self.finalizer.finalize(self.request())

        self.assertEqual(result.resumed_from, "success")
        self.assertEqual(self.base.upload_calls, 0)
        self.assertEqual(self.base.update_calls, 1)

    def test_corrupt_candidate_stops_before_state_or_base_side_effects(self) -> None:
        before = self.state_file.read_bytes()
        self.candidate.write_bytes(b"not an image")
        request = FinalizeRequest(
            task_dir=self.root, state_file=self.state_file, record_id="rec_1",
            target_index=0, candidate=self.candidate,
            candidate_sha256=hashlib.sha256(b"not an image").hexdigest(),
        )
        with self.assertRaisesRegex(FinalizeError, "candidate image is invalid"):
            self.finalizer.finalize(request)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(self.base.get_calls, 0)
        self.assertEqual(self.base.upload_calls, 0)

    def test_complete_candidate_ignores_comparison_dimensions_and_aspect(self) -> None:
        write_png(self.candidate, width=1, height=64)
        self.digest = hashlib.sha256(self.candidate.read_bytes()).hexdigest()

        result = self.finalizer.finalize(self.request())

        self.assertEqual(result.attachment_token, "box_uploaded_1")
        self.assertEqual(self.base.upload_calls, 1)

    def test_upload_failure_leaves_durable_accepted_local_checkpoint(self) -> None:
        self.base.fail_upload = True
        with self.assertRaisesRegex(FinalizeError, "attachment upload failed"):
            self.finalizer.finalize(self.request())
        persisted = task_state.load_state(self.state_file)
        self.assertEqual(persisted["targets"][self.target_token]["status"], "accepted-local")
        self.assertTrue(self.output_path().is_file())
        self.assertEqual(self.base.update_calls, 0)

    def test_detail_failure_resumes_without_duplicate_upload(self) -> None:
        self.base.fail_update = True
        with self.assertRaisesRegex(FinalizeError, "detail update failed"):
            self.finalizer.finalize(self.request())
        self.assertEqual(task_state.load_state(self.state_file)["targets"][
            self.target_token
        ]["status"], "success")
        self.base.fail_update = False
        result = self.finalizer.finalize(self.request())
        self.assertEqual(result.resumed_from, "success")
        self.assertEqual(self.base.upload_calls, 1)
        self.assertEqual(self.base.update_calls, 2)

    def test_readback_mismatch_stops_with_success_mapping_intact(self) -> None:
        self.base.drop_update = True
        with self.assertRaisesRegex(FinalizeError, "Base readback mismatch"):
            self.finalizer.finalize(self.request())
        persisted = task_state.load_state(self.state_file)
        self.assertEqual(persisted["targets"][self.target_token]["status"], "success")
        self.assertEqual(self.base.upload_calls, 1)
        self.assertEqual(self.base.update_calls, 1)

    def test_duplicate_invocation_is_a_verified_noop(self) -> None:
        first = self.finalizer.finalize(self.request())
        second = self.finalizer.finalize(self.request())
        self.assertEqual(first.attachment_token, second.attachment_token)
        self.assertEqual(second.resumed_from, "verified")
        self.assertEqual(self.base.upload_calls, 1)
        self.assertEqual(self.base.update_calls, 1)

    def test_existing_different_deterministic_output_is_never_overwritten(self) -> None:
        self.output_path().write_bytes(b"historical output")
        before = self.output_path().read_bytes()
        with self.assertRaisesRegex(FinalizeError, "deterministic output conflicts"):
            self.finalizer.finalize(self.request())
        self.assertEqual(self.output_path().read_bytes(), before)
        self.assertEqual(self.base.upload_calls, 0)


if __name__ == "__main__":
    unittest.main()
