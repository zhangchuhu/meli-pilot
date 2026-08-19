"""Behavior tests for the sanitized append-only performance event log."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from scripts.event_log import EventLog, EventLogError, summarize_events


def _append_events_from_process(path: str, run: int, worker: int) -> None:
    """Write a private worker's events through a separately opened EventLog."""
    event_log = EventLog(Path(path))
    for index in range(20):
        event_log.append(
            "record_finished",
            record_id=f"process-{run}-{worker}-{index}",
            status="success",
            duration_ms=index + 1,
        )


class EventLogTest(unittest.TestCase):
    def test_target_event_accepts_nine_references_but_not_ten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = EventLog(Path(temporary) / "events.ndjson", clock_ms=lambda: 1)
            event = log.append(
                "target_started", record_id="rec-1", target_id="target-1",
                reference_count=9,
            )
            self.assertEqual(event["reference_count"], 9)
            with self.assertRaises(EventLogError):
                log.append(
                    "target_started", record_id="rec-1", target_id="target-2",
                    reference_count=10,
                )

    def test_append_writes_one_durable_stable_schema_ndjson_event(self) -> None:
        """Removing append/fsync or adding an unapproved field breaks the contract."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.ndjson"
            clock_values = iter((1_000, 1_001))
            event_log = EventLog(path, clock_ms=lambda: next(clock_values))

            with patch("scripts.event_log.os.fsync") as fsync:
                first = event_log.append(
                    "record_started", record_id="rec-1", concurrency=2,
                )
                second = event_log.append(
                    "record_finished", record_id="rec-1", status="success",
                    duration_ms=17,
                )

            self.assertEqual(fsync.call_count, 2)
            self.assertEqual(first, {
                "schema_version": 1,
                "event": "record_started",
                "timestamp_ms": 1_000,
                "record_id": "rec-1",
                "concurrency": 2,
            })
            self.assertEqual(second["timestamp_ms"], 1_001)
            self.assertEqual(
                [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()],
                [first, second],
            )

    def test_partial_short_write_completes_before_the_next_append(self) -> None:
        """A short write must be completed before another event can be appended."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.ndjson"
            event_log = EventLog(path, clock_ms=lambda: 1)
            baseline = event_log.append("record_started", record_id="rec-1")
            real_write = os.write

            calls = 0

            def short_write(descriptor: int, payload: bytes) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_write(descriptor, payload[:len(payload) // 2])
                return real_write(descriptor, payload)

            with patch("scripts.event_log.os.write", side_effect=short_write):
                short_event = event_log.append(
                    "record_finished", record_id="rec-1", status="success",
                )

            after_recovery = event_log.append("record_finished", record_id="rec-1", status="success")
            self.assertEqual(
                [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()],
                [baseline, short_event, after_recovery],
            )

    def test_write_error_after_partial_append_rolls_back_before_the_next_append(self) -> None:
        """An I/O error on a continuation write must restore the previous file end."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.ndjson"
            event_log = EventLog(path, clock_ms=lambda: 1)
            baseline = event_log.append("record_started", record_id="rec-1")
            real_write = os.write
            calls = 0

            def partial_then_error(descriptor: int, payload: bytes) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_write(descriptor, payload[:len(payload) // 2])
                raise OSError("simulated write failure")

            with patch("scripts.event_log.os.write", side_effect=partial_then_error):
                with self.assertRaises(EventLogError):
                    event_log.append("record_finished", record_id="rec-1", status="success")

            after_recovery = event_log.append("record_finished", record_id="rec-1", status="success")
            self.assertEqual(
                [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()],
                [baseline, after_recovery],
            )

    def test_append_rejects_unknown_and_sensitive_fields_before_writing(self) -> None:
        """Metrics remain a bounded closed schema; payloads use diagnostic output."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.ndjson"
            event_log = EventLog(path, clock_ms=lambda: 1)
            rejected = (
                {"prompt": "change the dress"},
                {"api_key": "secret"},
                {"authorization": "Bearer secret"},
                {"image_base64": "aGVsbG8="},
                {"exception": "Traceback: raw remote response"},
                {"error_category": "remote error: Bearer secret"},
            )

            for fields in rejected:
                with self.subTest(fields=fields):
                    with self.assertRaises(EventLogError):
                        event_log.append("generation_finished", **fields)

            self.assertFalse(path.exists())

    def test_append_rejects_unknown_event_and_invalid_enums(self) -> None:
        """Permitting arbitrary event names or defect values defeats the closed schema."""
        with tempfile.TemporaryDirectory() as temporary:
            event_log = EventLog(Path(temporary) / "events.ndjson", clock_ms=lambda: 1)

            with self.assertRaises(EventLogError):
                event_log.append("debug_dump", record_id="rec-1")
            with self.assertRaises(EventLogError):
                event_log.append("qc_finished", defect="the entire remote response")
            with self.assertRaises(EventLogError):
                event_log.append("target_finished", status="maybe")

    def test_concurrent_appends_remain_complete_ndjson_across_repeated_runs(self) -> None:
        """Dropping the append lock or split-writing an event corrupts worker telemetry."""
        for run in range(5):
            with self.subTest(run=run), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "events.ndjson"
                event_log = EventLog(path)

                def append(worker: int, index: int) -> None:
                    event_log.append(
                        "record_finished",
                        record_id=f"rec-{run}-{worker}-{index}",
                        status="success",
                        duration_ms=index + 1,
                    )

                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures = [
                        pool.submit(append, worker, index)
                        for worker in range(8)
                        for index in range(25)
                    ]
                    for future in futures:
                        future.result()

                events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(events), 200)
                self.assertEqual(
                    {event["record_id"] for event in events},
                    {f"rec-{run}-{worker}-{index}" for worker in range(8) for index in range(25)},
                )
                self.assertTrue(all(event["event"] == "record_finished" for event in events))

    @unittest.skipUnless(os.name == "posix", "flock contract is Unix-specific")
    def test_multiprocess_appends_remain_complete_ndjson_across_repeated_runs(self) -> None:
        """Independent process writers must not interleave or leave partial NDJSON."""
        context = multiprocessing.get_context("fork")
        for run in range(3):
            with self.subTest(run=run), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "events.ndjson"
                processes = [
                    context.Process(target=_append_events_from_process, args=(str(path), run, worker))
                    for worker in range(4)
                ]
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(timeout=10)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)

                events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(events), 80)
                self.assertEqual(
                    {event["record_id"] for event in events},
                    {f"process-{run}-{worker}-{index}" for worker in range(4) for index in range(20)},
                )

    def test_summary_reports_rates_counts_latency_percentiles_and_service_totals(self) -> None:
        """Changing metric definitions or phase attribution breaks comparable run reports."""
        events = [
            {"schema_version": 1, "event": "table_started", "timestamp_ms": 1_000,
             "table_id": "tbl-1"},
            {"schema_version": 1, "event": "target_started", "timestamp_ms": 1_010,
             "record_id": "rec-1", "target_id": "target-1", "reference_count": 3,
             "input_bytes": 300},
            {"schema_version": 1, "event": "generation_started", "timestamp_ms": 1_020,
             "record_id": "rec-1", "target_id": "target-1", "attempt": 1},
            {"schema_version": 1, "event": "generation_finished", "timestamp_ms": 1_030,
             "record_id": "rec-1", "target_id": "target-1", "attempt": 1,
             "duration_ms": 10, "status": "success"},
            {"schema_version": 1, "event": "qc_started", "timestamp_ms": 1_031,
             "record_id": "rec-1", "target_id": "target-1", "attempt": 1},
            {"schema_version": 1, "event": "qc_finished", "timestamp_ms": 1_040,
             "record_id": "rec-1", "target_id": "target-1", "attempt": 1,
             "duration_ms": 5, "status": "early_accept", "score": 99},
            {"schema_version": 1, "event": "target_finished", "timestamp_ms": 1_050,
             "record_id": "rec-1", "target_id": "target-1", "attempt": 1,
             "status": "success", "duration_ms": 40},
            {"schema_version": 1, "event": "target_started", "timestamp_ms": 1_060,
             "record_id": "rec-1", "target_id": "target-2", "reference_count": 4,
             "input_bytes": 500},
            {"schema_version": 1, "event": "generation_started", "timestamp_ms": 1_070,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 1},
            {"schema_version": 1, "event": "generation_finished", "timestamp_ms": 1_090,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 1,
             "duration_ms": 20, "status": "success"},
            {"schema_version": 1, "event": "qc_started", "timestamp_ms": 1_091,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 1},
            {"schema_version": 1, "event": "qc_finished", "timestamp_ms": 1_100,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 1,
             "duration_ms": 10, "status": "retry", "defect": "open_front"},
            {"schema_version": 1, "event": "retry_decided", "timestamp_ms": 1_101,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 1,
             "status": "retry", "defect": "open_front"},
            {"schema_version": 1, "event": "generation_started", "timestamp_ms": 1_110,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 2},
            {"schema_version": 1, "event": "generation_finished", "timestamp_ms": 1_140,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 2,
             "duration_ms": 30, "status": "success"},
            {"schema_version": 1, "event": "qc_started", "timestamp_ms": 1_141,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 2},
            {"schema_version": 1, "event": "qc_finished", "timestamp_ms": 1_161,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 2,
             "duration_ms": 20, "status": "success", "score": 95},
            {"schema_version": 1, "event": "target_finished", "timestamp_ms": 1_170,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 2,
             "status": "success", "duration_ms": 110},
            {"schema_version": 1, "event": "target_started", "timestamp_ms": 1_180,
             "record_id": "rec-1", "target_id": "target-3", "reference_count": 2,
             "input_bytes": 200},
            {"schema_version": 1, "event": "retry_decided", "timestamp_ms": 1_190,
             "record_id": "rec-1", "target_id": "target-3", "attempt": 1,
             "status": "retry", "defect": "wrong_color"},
            {"schema_version": 1, "event": "target_finished", "timestamp_ms": 1_200,
             "record_id": "rec-1", "target_id": "target-3", "attempt": 1,
             "status": "failed", "duration_ms": 20, "error_category": "external-call"},
            {"schema_version": 1, "event": "finalize_finished", "timestamp_ms": 1_205,
             "record_id": "rec-1", "target_id": "target-2", "duration_ms": 12,
             "phase": "lark_write", "status": "success"},
            {"schema_version": 1, "event": "table_finished", "timestamp_ms": 11_000,
             "table_id": "tbl-1", "status": "success"},
        ]

        summary = summarize_events(events)

        self.assertEqual(summary["total_wall_time_ms"], 10_000)
        self.assertEqual(summary["targets"], 3)
        self.assertEqual(summary["accepted_targets"], 2)
        self.assertEqual(summary["paid_generation_calls"], 3)
        self.assertEqual(summary["paid_generations_per_accepted_target"], 1.5)
        self.assertEqual(summary["qc_calls"], 3)
        self.assertEqual(summary["early_pass_rate"], 0.5)
        self.assertEqual(summary["retry_rate"], 2 / 3)
        self.assertEqual(summary["failure_rate"], 1 / 3)
        self.assertEqual(summary["reference_count"], {"total": 9, "average_per_target": 3.0})
        self.assertEqual(summary["input_bytes"], {"total": 1_000})
        self.assertEqual(summary["service_totals_ms"], {
            "doubao": 60,
            "qc": 35,
            "lark": 12,
        })
        self.assertEqual(summary["phase_latency_ms"]["generation"], {
            "count": 3, "p50": 20.0, "p95": 29.0,
        })
        self.assertEqual(summary["phase_latency_ms"]["qc"], {
            "count": 3, "p50": 10.0, "p95": 19.0,
        })

    def test_summary_counts_actual_ark_requests_and_large_valid_aggregate_inputs(self) -> None:
        events = [
            {"schema_version": 1, "event": "target_started", "timestamp_ms": 1,
             "record_id": "rec-1", "target_id": "target-1", "input_bytes": 140_000_000},
            {"schema_version": 1, "event": "qc_request_accounted", "timestamp_ms": 2,
             "record_id": "rec-1", "target_id": "target-1", "attempt": 1,
             "status": "success", "ark_request_count": 2},
            {"schema_version": 1, "event": "comparative_qc_finished", "timestamp_ms": 3,
             "record_id": "rec-1", "target_id": "target-1", "status": "success",
             "phase": "qc", "ark_request_count": 1},
        ]
        summary = summarize_events(events)
        self.assertEqual(summary["input_bytes"]["total"], 140_000_000)
        self.assertEqual(summary["qc_calls"], 3)
        self.assertEqual(summary["comparative_qc_calls"], 1)

    def test_summary_counts_accepted_and_accepted_local_targets_consistently(self) -> None:
        """Excluding accepted-local targets understates acceptance and early-pass metrics."""
        events = [
            {"schema_version": 1, "event": "target_started", "timestamp_ms": 1,
             "record_id": "rec-1", "target_id": "target-1"},
            {"schema_version": 1, "event": "generation_started", "timestamp_ms": 2,
             "record_id": "rec-1", "target_id": "target-1", "attempt": 1},
            {"schema_version": 1, "event": "qc_finished", "timestamp_ms": 3,
             "record_id": "rec-1", "target_id": "target-1", "attempt": 1,
             "status": "early_accept"},
            {"schema_version": 1, "event": "target_finished", "timestamp_ms": 4,
             "record_id": "rec-1", "target_id": "target-1", "status": "accepted-local"},
            {"schema_version": 1, "event": "target_started", "timestamp_ms": 5,
             "record_id": "rec-1", "target_id": "target-2"},
            {"schema_version": 1, "event": "generation_started", "timestamp_ms": 6,
             "record_id": "rec-1", "target_id": "target-2", "attempt": 1},
            {"schema_version": 1, "event": "target_finished", "timestamp_ms": 7,
             "record_id": "rec-1", "target_id": "target-2", "status": "accepted"},
        ]

        summary = summarize_events(events)

        self.assertEqual(summary["accepted_targets"], 2)
        self.assertEqual(summary["paid_generations_per_accepted_target"], 1.0)
        self.assertEqual(summary["early_pass_rate"], 0.5)

    def test_summary_is_pure_and_rejects_unsanitized_input(self) -> None:
        """A summary must not silently process unsafe historical event payloads."""
        unsafe = [{
            "schema_version": 1,
            "event": "record_finished",
            "timestamp_ms": 1,
            "record_id": "rec-1",
            "status": "success",
            "diagnostic": "raw traceback",
        }]

        with self.assertRaises(EventLogError):
            summarize_events(unsafe)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
