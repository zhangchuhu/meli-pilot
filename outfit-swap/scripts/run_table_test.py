"""Behavior tests for table scheduling and bounded shared services."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from scripts import prompt_builder, safe_edit, task_state
from scripts.run_record import (
    GenerationRequest,
    QCRequest,
    RecordContext,
    RecordResult,
    RecordServices,
)
from scripts.run_table import (
    BaseField,
    BoundedBase,
    BoundedGenerator,
    BoundedQC,
    GlobalStop,
    PaidCallStopped,
    PreflightError,
    ProductionRecordServicesFactory,
    RecordBaseScope,
    RecordFinalizerAdapter,
    SeedreamGeneratorAdapter,
    ServiceLimits,
    TableConfig,
    TableResult,
    TableRuntime,
    TableScheduler,
    TableSchedulerError,
    TableSchema,
    TableScope,
    main,
    run_table,
)


class _Events:
    def append(self, _event: str, /, **_fields: object) -> None:
        return None


class _Service:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: object) -> object:
        self.calls += 1
        callback = getattr(request, "on_generate", None)
        if callback is not None:
            callback()
        return request

    def plan_target(self, *args: object) -> object:
        return args

    def artifact_path(self, *args: object) -> object:
        return args[-1]

    def review(self, request: object) -> object:
        self.calls += 1
        callback = getattr(request, "on_review", None)
        if callback is not None:
            callback()
        return request


class _Base:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def resolve_base(self, base_url: str) -> dict:
        self.calls.append(("resolve_base", {"base_url": base_url}))
        return {"ok": True}

    def list_fields(self, **kwargs: object) -> dict:
        self.calls.append(("list_fields", dict(kwargs)))
        return {"ok": True}

    def list_records(self, **kwargs: object) -> Path:
        self.calls.append(("list_records", dict(kwargs)))
        return Path("records.ndjson")

    def download_attachment(self, **kwargs: object) -> Path:
        self.calls.append(("download_attachment", dict(kwargs)))
        return Path("download.png")

    def upload_attachment(self, **kwargs: object) -> dict:
        self.calls.append(("upload_attachment", dict(kwargs)))
        return {"ok": True}

    def update_record(self, **kwargs: object) -> dict:
        self.calls.append(("update_record", dict(kwargs)))
        return {"ok": True}

    def get_record(self, **kwargs: object) -> dict:
        self.calls.append(("get_record", dict(kwargs)))
        return {"ok": True}


def _schema(*, missing: str | None = None, drift: str | None = None) -> TableSchema:
    fields = [
        BaseField("原图", "fld_source", "attachment"),
        BaseField("爆款图", "fld_target", "attachment"),
        BaseField("输出图", "fld_output", "attachment"),
        BaseField(
            "任务状态", "fld_status", "single_select",
            ("未开始", "成功", "失败"),
        ),
        BaseField("处理明细", "fld_detail", "text"),
    ]
    if missing is not None:
        fields = [field for field in fields if field.name != missing]
    if drift is not None:
        fields = [
            BaseField(field.name, field.field_id, "text", field.options)
            if field.name == drift else field
            for field in fields
        ]
    return TableSchema(tuple(fields))


def _record_scope(
        record_id: str = "rec_1", *,
        attachment_tokens: frozenset[str] = frozenset({"box_source_1"}),
        payload_root: Path = Path.cwd(),
) -> RecordBaseScope:
    return RecordBaseScope(
        table=TableScope("app_exact", "tbl_exact", "vew_exact"),
        record_id=record_id,
        attachment_tokens=attachment_tokens,
        output_field_id="fld_output",
        status_field_id="fld_status",
        detail_field_id="fld_detail",
        payload_root=payload_root,
    )


class _Adapter:
    def __init__(
            self, contexts: list[RecordContext], *, schema: TableSchema | None = None,
            auth_error: bool = False,
    ) -> None:
        self.contexts = contexts
        self.schema = schema or _schema()
        self.auth_error = auth_error
        self.trace: list[tuple] = []
        self.materialized = False
        self.stop_signal: threading.Event | None = None
        self.qc_modes: list[str] = []

    def resolve_base(self, base_url: str, base: object) -> TableScope:
        self.trace.append(("resolve", base_url, base))
        return TableScope("app_exact", "tbl_exact", "vew_exact")

    def authenticate(self, scope: TableScope, base: object) -> None:
        self.trace.append(("authenticate", scope, base))
        if self.auth_error:
            raise PreflightError("global authentication failed")

    def load_schema(self, scope: TableScope, base: object) -> TableSchema:
        self.trace.append(("schema", scope, base))
        return self.schema

    def list_records(
            self, scope: TableScope, schema: TableSchema, *, retry_failed: bool,
            qc_mode: str, base: object,
    ):
        self.trace.append((
            "records", scope, schema, retry_failed, qc_mode, base,
        ))
        for context in self.contexts:
            yield context
        self.materialized = True

    def record_services(
            self, context: RecordContext, generator: object, qc: object,
            base: object, stop_signal: threading.Event, qc_mode: str,
    ) -> RecordServices:
        del context
        self.stop_signal = stop_signal
        self.qc_modes.append(qc_mode)
        return RecordServices(
            generator=generator,
            qc=qc,
            finalizer=base,
            events=_Events(),
            stop_signal=stop_signal,
            qc_mode=qc_mode,
        )

    def record_base_scope(
            self, context: RecordContext, scope: TableScope,
            schema: TableSchema,
    ) -> RecordBaseScope:
        return RecordBaseScope(
            table=scope,
            record_id=context.record_id,
            attachment_tokens=frozenset({"box_source_1", "box_target_1"}),
            output_field_id=schema.field("输出图").field_id,
            status_field_id=schema.field("任务状态").field_id,
            detail_field_id=schema.field("处理明细").field_id,
            payload_root=context.task_dir / "generated_images",
        )


def _runtime(adapter: _Adapter, worker) -> TableRuntime:
    return TableRuntime(
        adapter=adapter,
        base=_Base(),
        generator=_Service(),
        qc=_Service(),
        worker=worker,
    )


class CLIAndPreflightTest(unittest.TestCase):
    def test_cli_defaults_record_concurrency_to_two_and_accepts_rollbacks(self) -> None:
        seen: list[TableConfig] = []
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["https://base.example/table"], execute=lambda c: (
                seen.append(c) or TableResult(0, 0, 0, 0)
            )), 0)
            self.assertEqual(main([
                "https://base.example/table", "--record-concurrency", "1",
                "--qc-mode", "shadow",
            ], execute=lambda c: (
                seen.append(c) or TableResult(0, 0, 0, 0)
            )), 0)
        self.assertEqual(seen, [
            TableConfig("https://base.example/table"),
            TableConfig(
                "https://base.example/table", record_concurrency=1,
                qc_mode="shadow",
            ),
        ])

    def test_cli_rejects_nonpositive_or_noninteger_concurrency_before_execution(self) -> None:
        for value in ("0", "-1", "1.5", "many"):
            with self.subTest(value=value):
                calls: list[TableConfig] = []
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main([
                            "https://base.example/table",
                            "--record-concurrency", value,
                        ], execute=lambda config: (
                            calls.append(config) or TableResult(0, 0, 0, 0)
                        ))
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(calls, [])

    def test_preflight_uses_one_exact_scope_and_materializes_records_before_workers(self) -> None:
        root = Path(tempfile.mkdtemp())
        contexts = [
            RecordContext(root / f"r{index}", f"rec_{index}", (0, 1))
            for index in range(3)
        ]
        adapter = _Adapter(contexts)
        worker_calls: list[tuple[str, tuple[int, ...]]] = []

        def worker(context: RecordContext, _services: RecordServices) -> RecordResult:
            self.assertTrue(adapter.materialized)
            worker_calls.append((context.record_id, context.target_indices))
            return RecordResult(context.record_id, "success", 2)

        result = TableScheduler(_runtime(adapter, worker)).run(TableConfig(
            "https://base.example/table", retry_failed=True, qc_mode="shadow",
        ))

        self.assertEqual(result, TableResult(3, 3, 0, 0))
        self.assertEqual([name for name, *_rest in adapter.trace], [
            "resolve", "authenticate", "schema", "records",
        ])
        scope = TableScope("app_exact", "tbl_exact", "vew_exact")
        self.assertEqual(adapter.trace[1][1], scope)
        self.assertEqual(adapter.trace[2][1], scope)
        self.assertEqual(adapter.trace[3][1], scope)
        self.assertEqual(adapter.trace[3][3:5], (True, "shadow"))
        self.assertCountEqual(worker_calls, [
            ("rec_0", (0, 1)), ("rec_1", (0, 1)), ("rec_2", (0, 1)),
        ])
        self.assertEqual(adapter.qc_modes, ["shadow", "shadow", "shadow"])

    def test_preflight_failures_dispatch_no_workers_or_paid_calls(self) -> None:
        cases = (
            ("auth", _Adapter([], auth_error=True)),
            ("schema drift", _Adapter([], schema=_schema(drift="输出图"))),
            ("missing field", _Adapter([], schema=_schema(missing="爆款图"))),
        )
        for label, adapter in cases:
            with self.subTest(label=label):
                worker_calls: list[str] = []
                runtime = _runtime(
                    adapter,
                    lambda context, services: worker_calls.append(context.record_id),
                )
                with self.assertRaises(PreflightError):
                    TableScheduler(runtime).run(TableConfig("https://base.example/table"))
                self.assertEqual(worker_calls, [])
                self.assertEqual(runtime.generator.calls, 0)
                self.assertEqual(runtime.qc.calls, 0)


class SchedulerTest(unittest.TestCase):
    def _observed_record_concurrency(
            self, *, configured: int, expected: int,
            limits: ServiceLimits | None = None,
    ) -> int:
        root = Path(tempfile.mkdtemp())
        contexts = [
            RecordContext(root / f"r{index}", f"rec_{index}", (0,))
            for index in range(6)
        ]
        lock = threading.Lock()
        reached = threading.Event()
        release = threading.Event()
        active = 0
        maximum = 0

        def worker(context: RecordContext, _services: RecordServices) -> RecordResult:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == expected:
                    reached.set()
            release.wait(timeout=2)
            with lock:
                active -= 1
            return RecordResult(context.record_id, "success", 1)

        scheduler = TableScheduler(_runtime(_Adapter(contexts), worker), limits=limits)
        result: list[TableResult] = []
        exit_codes: list[int] = []

        def execute(config: TableConfig) -> TableResult:
            value = scheduler.run(config)
            result.append(value)
            return value

        def invoke_cli() -> None:
            with redirect_stdout(io.StringIO()):
                exit_codes.append(main([
                    "https://base.example/table",
                    "--record-concurrency", str(configured),
                ], execute=execute))

        thread = threading.Thread(target=invoke_cli)
        thread.start()
        observed = reached.wait(timeout=1)
        time.sleep(0.02)
        release.set()
        thread.join(timeout=3)
        self.assertTrue(observed)
        self.assertEqual(exit_codes, [0])
        self.assertEqual(result, [TableResult(6, 6, 0, 0)])
        return maximum

    def test_record_concurrency_default_override_and_injected_safety_cap(self) -> None:
        self.assertEqual(self._observed_record_concurrency(
            configured=2, expected=2,
        ), 2)
        self.assertEqual(self._observed_record_concurrency(
            configured=4, expected=4,
        ), 4)
        self.assertEqual(self._observed_record_concurrency(
            configured=4, expected=2, limits=ServiceLimits(record_workers=2),
        ), 2)

    def test_worker_stopped_result_sets_global_stop_before_later_dispatch(self) -> None:
        root = Path(tempfile.mkdtemp())
        contexts = [
            RecordContext(root / f"r{index}", f"rec_{index}", (0,))
            for index in range(3)
        ]
        calls: list[str] = []

        def worker(context: RecordContext, _services: RecordServices) -> RecordResult:
            calls.append(context.record_id)
            return RecordResult(context.record_id, "stopped", 0)

        result = TableScheduler(_runtime(_Adapter(contexts), worker)).run(
            TableConfig(
                "https://base.example/table", record_concurrency=1,
            ),
        )

        self.assertEqual(calls, ["rec_0"])
        self.assertEqual(result, TableResult(3, 0, 0, 3))

    def test_records_are_bounded_targets_are_not_split_and_failures_are_independent(self) -> None:
        root = Path(tempfile.mkdtemp())
        contexts = [
            RecordContext(root / f"r{index}", f"rec_{index}", (0, 1, 2))
            for index in range(5)
        ]
        adapter = _Adapter(contexts)
        lock = threading.Lock()
        active = 0
        maximum = 0
        invocations: list[tuple[str, tuple[int, ...]]] = []

        def worker(context: RecordContext, _services: RecordServices) -> RecordResult:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                invocations.append((context.record_id, context.target_indices))
            time.sleep(0.015)
            with lock:
                active -= 1
            status = "failed" if context.record_id == "rec_1" else "success"
            return RecordResult(context.record_id, status, 0)

        scheduler = TableScheduler(_runtime(adapter, worker))
        result = scheduler.run(TableConfig(
            "https://base.example/table", record_concurrency=2,
        ))

        self.assertEqual(maximum, 2)
        self.assertEqual(result, TableResult(5, 4, 1, 0))
        self.assertCountEqual(invocations, [
            (f"rec_{index}", (0, 1, 2)) for index in range(5)
        ])
        self.assertEqual(scheduler.active_record_ids, frozenset())

    def test_public_run_table_function_uses_the_scheduler_contract(self) -> None:
        root = Path(tempfile.mkdtemp())
        context = RecordContext(root, "rec_1", (0,))
        runtime = _runtime(
            _Adapter([context]),
            lambda current, _services: RecordResult(current.record_id, "success", 1),
        )
        self.assertEqual(
            run_table(TableConfig("https://base.example/table"), runtime),
            TableResult(1, 1, 0, 0),
        )

    def test_two_scheduler_instances_have_no_cross_process_or_persistent_lock(self) -> None:
        root = Path(tempfile.mkdtemp())
        context = RecordContext(root, "same_record", (0,))
        entered = threading.Barrier(2)
        simultaneous: list[str] = []

        def worker(current: RecordContext, _services: RecordServices) -> RecordResult:
            simultaneous.append(current.record_id)
            entered.wait(timeout=1)
            return RecordResult(current.record_id, "success", 1)

        schedulers = [
            TableScheduler(_runtime(_Adapter([context]), worker)),
            TableScheduler(_runtime(_Adapter([context]), worker)),
        ]
        results: list[TableResult] = []
        threads = [threading.Thread(
            target=lambda scheduler=item: results.append(scheduler.run(
                TableConfig("https://base.example/table", record_concurrency=1),
            )),
        ) for item in schedulers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(simultaneous, ["same_record", "same_record"])
        self.assertEqual(results, [TableResult(1, 1, 0, 0)] * 2)
        self.assertEqual(list(root.glob("*run*lock*")), [])

    def test_global_stop_race_blocks_waiting_and_queued_paid_generation_ten_times(self) -> None:
        @dataclass
        class Request:
            on_generate: object

        for iteration in range(10):
            with self.subTest(iteration=iteration):
                root = Path(tempfile.mkdtemp())
                contexts = [
                    RecordContext(root / f"r{index}", f"rec_{index}", (0,))
                    for index in range(4)
                ]
                adapter = _Adapter(contexts)
                first_started = threading.Event()

                def worker(
                        context: RecordContext, services: RecordServices,
                ) -> RecordResult:
                    if context.record_id != "rec_0":
                        first_started.wait(timeout=1)
                    def stop_after_first_start() -> None:
                        first_started.set()
                        services.stop_signal.set()
                    callback = (
                        stop_after_first_start
                        if context.record_id == "rec_0" else lambda: None
                    )
                    try:
                        services.generator.generate(Request(callback))
                    except PaidCallStopped:
                        return RecordResult(context.record_id, "stopped", 0)
                    return RecordResult(context.record_id, "success", 0)

                runtime = _runtime(adapter, worker)
                result = TableScheduler(runtime, limits=ServiceLimits(
                    record_workers=2, doubao_requests=1, qc_requests=2,
                    lark_writes=1, lark_reads=2,
                )).run(TableConfig(
                    "https://base.example/table", record_concurrency=2,
                ))
                self.assertEqual(runtime.generator.calls, 1)
                self.assertEqual(result, TableResult(4, 1, 0, 3))

    def test_global_stop_race_blocks_waiting_and_queued_qc_ten_times(self) -> None:
        for iteration in range(10):
            with self.subTest(iteration=iteration):
                first_started = threading.Event()
                release_first = threading.Event()

                class InstrumentedSemaphore:
                    def __init__(self) -> None:
                        self._semaphore = threading.BoundedSemaphore(1)
                        self._lock = threading.Lock()
                        self.acquisition_attempts = 0
                        self.second_waiting = threading.Event()

                    def __enter__(self) -> "InstrumentedSemaphore":
                        with self._lock:
                            self.acquisition_attempts += 1
                            if self.acquisition_attempts == 2:
                                self.second_waiting.set()
                        self._semaphore.acquire()
                        return self

                    def __exit__(self, *_args: object) -> None:
                        self._semaphore.release()

                class QCService:
                    def __init__(self) -> None:
                        self._lock = threading.Lock()
                        self.calls = 0

                    def review(self, request: object) -> object:
                        with self._lock:
                            self.calls += 1
                            call = self.calls
                        if call == 1:
                            first_started.set()
                            if not release_first.wait(timeout=2):
                                raise AssertionError("first QC call was not released")
                        return request

                semaphore = InstrumentedSemaphore()
                raw = QCService()
                stop = GlobalStop()
                qc = BoundedQC(
                    raw, semaphore, stop, checkpoint=lambda _request: None,
                )
                outcomes: list[str] = []

                def review() -> None:
                    try:
                        qc.review(object())
                    except PaidCallStopped:
                        outcomes.append("stopped")
                    else:
                        outcomes.append("finished")

                first = threading.Thread(target=review)
                second = threading.Thread(target=review)
                first.start()
                self.assertTrue(first_started.wait(timeout=1))
                second.start()
                self.assertTrue(semaphore.second_waiting.wait(timeout=1))
                self.assertEqual(semaphore.acquisition_attempts, 2)
                self.assertEqual(raw.calls, 1)

                stop.set()
                release_first.set()
                first.join(timeout=2)
                second.join(timeout=2)

                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(raw.calls, 1)
                self.assertCountEqual(outcomes, ["finished", "stopped"])


class ProductionAdapterTest(unittest.TestCase):
    def test_record_reconciliation_accepts_the_canonical_manifest_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_dir = root / "run" / "rec_1"
            record_dir.mkdir(parents=True)
            canonical = root / "canonical" / "state.json"
            state = task_state.new_state(
                record_id="rec_1", run_id="run_1",
                source_tokens=["source_1"], target_tokens=["target_1"],
                started_at="2026-08-18T10:00:00+00:00",
            )
            task_state.save_state(canonical, state)
            manifest = record_dir / "manifest.json"
            manifest.symlink_to(canonical)

            class Base(_Base):
                def get_record(self, **kwargs: object) -> dict:
                    self.calls.append(("get_record", dict(kwargs)))
                    return {"record": {
                        "record_id": "rec_1",
                        "fields": {"输出图": [], "处理明细": None},
                    }}

            finalizer = RecordFinalizerAdapter(
                base=Base(), app_token="app_exact", table_id="tbl_exact",
                output_field_id="fld_output", detail_field_id="fld_detail",
            )

            finalizer.reconcile_record(
                RecordContext(record_dir, "rec_1", (0,)), manifest, (0,),
            )

            self.assertEqual(task_state.load_state(manifest), state)

    def test_record_finalizer_reconciles_uploaded_output_through_exact_scoped_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "manifest.json"
            state = task_state.new_state(
                record_id="rec_1", run_id="run_1",
                source_tokens=["source_1"], target_tokens=["target_1"],
                started_at="2026-08-18T10:00:00+00:00",
            )
            task_state.begin_attempt(
                state, target_token="target_1", classification="front",
                reference_tokens=["source_1"], prompt="prompt", model="model",
                updated_at="2026-08-18T10:01:00+00:00",
            )
            artifact_name = state["targets"]["target_1"]["attempt_history"][-1][
                "artifact_name"
            ]
            output_name = task_state.promoted_output_name(
                artifact_name, "target_1",
            )
            task_state.record_local_acceptance(
                state, target_token="target_1", artifact_name=artifact_name,
                name=output_name, updated_at="2026-08-18T10:02:00+00:00",
            )
            task_state.save_state(state_file, state)

            class Base(_Base):
                def get_record(self, **kwargs: object) -> dict:
                    self.calls.append(("get_record", dict(kwargs)))
                    return {"record": {
                        "record_id": "rec_1",
                        "fields": {
                            "输出图": [{
                                "file_token": "box_uploaded", "name": output_name,
                            }],
                            "处理明细": None,
                        },
                    }}

            raw = Base()
            scoped = BoundedBase(
                raw, read_semaphore=threading.BoundedSemaphore(2),
                write_semaphore=threading.BoundedSemaphore(1),
            ).scoped(_record_scope())
            finalizer = RecordFinalizerAdapter(
                base=scoped, app_token="app_exact", table_id="tbl_exact",
                output_field_id="fld_output", detail_field_id="fld_detail",
                clock=lambda: "2026-08-18T10:03:00+00:00",
            )

            finalizer.reconcile_record(
                RecordContext(root, "rec_1", (0,)), state_file, (0,),
            )

            persisted = task_state.load_state(state_file)
            self.assertEqual(persisted["targets"]["target_1"]["status"], "success")
            self.assertEqual(persisted["targets"]["target_1"]["output"], {
                "file_token": "box_uploaded", "name": output_name,
            })
            self.assertEqual(raw.calls, [("get_record", {
                "app_token": "app_exact", "table_id": "tbl_exact",
                "record_id": "rec_1",
                "field_ids": ["fld_output", "fld_detail"],
            })])

    def test_production_record_service_factory_supplies_reconciliation_adapter(self) -> None:
        root = Path(tempfile.mkdtemp())
        context = RecordContext(root, "rec_1", (0,))
        generator = object()
        qc = object()
        base = object()
        stop = threading.Event()
        events = _Events()
        factory = ProductionRecordServicesFactory(
            scope=TableScope("app_exact", "tbl_exact", "vew_exact"),
            schema=_schema(), events_factory=lambda _context: events,
        )

        services = factory(
            context, generator, qc, base, stop, "shadow",
        )

        self.assertIs(services.generator, generator)
        self.assertIs(services.qc, qc)
        self.assertIs(services.events, events)
        self.assertIs(services.stop_signal, stop)
        self.assertEqual(services.qc_mode, "shadow")
        self.assertIsInstance(services.finalizer, RecordFinalizerAdapter)
        self.assertEqual(services.finalizer.qc_mode, "shadow")

    def test_seedream_adapter_uses_only_fixed_safe_edit_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated_images").mkdir()
            doubao_script = root / "doubao_imagegen.py"
            doubao_script.write_text("# fixed transport", encoding="utf-8")
            target = root / "target.png"
            source = root / "source.png"
            target.write_bytes(b"target")
            source.write_bytes(b"source")
            output = root / "generated_images" / "attempt-01-aaaaaaaaaaaa-01.png"
            plan = prompt_builder.TargetPlan(
                classification="front",
                selected_references=(
                    prompt_builder.SelectedReference("source_1", "model"),
                ),
                garment_facts=prompt_builder.GarmentFacts((), ()),
                infographic_inventory=None,
            )
            request = GenerationRequest(
                context=RecordContext(root, "rec_1", (0,)),
                target_index=0, target_token="target_1", attempt=1,
                artifact_name=output.name, output_path=output, prompt="safe prompt",
                reference_tokens=("source_1",), plan=plan,
            )
            adapter = SeedreamGeneratorAdapter(
                doubao_script=doubao_script,
                planner=lambda *_args: plan,
                image_resolver=lambda _request: (target, source),
                approved_run_roots=(root,),
            )

            def fixed_edit(**kwargs: object) -> Path:
                prompt_file = Path(kwargs["prompt_file"])
                self.assertEqual(prompt_file.read_text(encoding="utf-8"), "safe prompt")
                self.assertEqual(kwargs["doubao_script"], doubao_script.resolve())
                self.assertEqual(kwargs["images"], [target.resolve(), source.resolve()])
                self.assertEqual(kwargs["output"], output.resolve())
                output.write_bytes(b"result")
                return output

            with mock.patch.object(safe_edit, "run_edit", side_effect=fixed_edit) as paid:
                self.assertEqual(adapter.generate(request), output.resolve())
            self.assertEqual(paid.call_count, 1)
            self.assertEqual(adapter.model, safe_edit.MODEL)
            self.assertEqual(list(root.glob(".seedream-prompt-*")), [])

    def test_seedream_adapter_resolves_artifacts_from_their_owning_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doubao_script = root / "doubao_imagegen.py"
            doubao_script.write_text("# fixed transport", encoding="utf-8")
            context = RecordContext(root / "current" / "rec_1", "rec_1", (0,))
            prior_artifact = (
                root / "prior" / "rec_1" / "generated_images"
                / "attempt-01-aaaaaaaaaaaa-01.png"
            )
            prior_artifact.parent.mkdir(parents=True)
            prior_artifact.write_bytes(b"prior candidate")
            adapter = SeedreamGeneratorAdapter(
                doubao_script=doubao_script,
                planner=lambda *_args: object(),
                image_resolver=lambda _request: (),
                artifact_resolver=lambda _context, _history: prior_artifact,
                approved_run_roots=(root,),
            )

            self.assertEqual(adapter.artifact_path(context, {
                "run_id": "prior",
                "artifact_name": prior_artifact.name,
            }), prior_artifact.resolve())

    def test_prior_run_artifacts_cannot_escape_approved_roots_or_use_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approved = root / "approved-runs"
            approved.mkdir()
            outside = root / "outside" / "attempt-01-aaaaaaaaaaaa-01.png"
            outside.parent.mkdir()
            outside.write_bytes(b"outside candidate")
            link = approved / "prior" / "rec_1" / "generated_images" / outside.name
            link.parent.mkdir(parents=True)
            link.symlink_to(outside)
            wrong_run = approved / "other" / "rec_1" / "generated_images" / outside.name
            wrong_run.parent.mkdir(parents=True)
            wrong_run.write_bytes(b"wrong owning run")
            doubao_script = root / "doubao_imagegen.py"
            doubao_script.write_text("# fixed transport", encoding="utf-8")
            context = RecordContext(approved / "current" / "rec_1", "rec_1", (0,))
            history = {"run_id": "prior", "artifact_name": outside.name}

            for label, resolved in (
                    ("outside", outside),
                    ("symlink", link),
                    ("wrong-run-identity", wrong_run),
            ):
                with self.subTest(label=label):
                    adapter = SeedreamGeneratorAdapter(
                        doubao_script=doubao_script,
                        planner=lambda *_args: object(),
                        image_resolver=lambda _request: (),
                        artifact_resolver=lambda _context, _history, value=resolved: value,
                        approved_run_roots=(approved,),
                    )
                    with self.assertRaises(TableSchedulerError):
                        adapter.artifact_path(context, history)


class SemaphoreTest(unittest.TestCase):
    class Probe:
        def __init__(self, expected: int) -> None:
            self.expected = expected
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()
            self.reached = threading.Event()
            self.release = threading.Event()

        def call(self, *_args: object, **_kwargs: object) -> object:
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == self.expected:
                    self.reached.set()
            self.release.wait(timeout=2)
            with self.lock:
                self.active -= 1
            return object()

    def _maximum(self, call, probe: "SemaphoreTest.Probe") -> int:
        threads = [threading.Thread(target=call, args=(object(),)) for _ in range(6)]
        for thread in threads:
            thread.start()
        self.assertTrue(probe.reached.wait(timeout=1))
        time.sleep(0.02)
        probe.release.set()
        for thread in threads:
            thread.join(timeout=2)
        return probe.maximum

    def test_default_paid_service_maxima(self) -> None:
        limits = ServiceLimits()
        stop = threading.Event()

        class Generator:
            def __init__(self, probe) -> None:
                self.probe = probe

            def generate(self, request) -> object:
                return self.probe.call(request)

        class QC:
            def __init__(self, probe) -> None:
                self.probe = probe

            def review(self, request) -> object:
                return self.probe.call(request)

        generator_probe = self.Probe(2)
        qc_probe = self.Probe(2)
        generator = BoundedGenerator(
            Generator(generator_probe), threading.BoundedSemaphore(limits.doubao_requests),
            stop, checkpoint=lambda _request: None,
        )
        qc = BoundedQC(
            QC(qc_probe), threading.BoundedSemaphore(limits.qc_requests),
            stop, checkpoint=lambda _request: None,
        )
        self.assertEqual(self._maximum(generator.generate, generator_probe), 2)
        self.assertEqual(self._maximum(qc.review, qc_probe), 2)

    def test_generation_and_qc_recheck_durable_state_after_semaphore_wait(self) -> None:
        root = Path(tempfile.mkdtemp())
        context = RecordContext(root, "rec_1", (0,))
        plan = prompt_builder.TargetPlan(
            classification="front",
            selected_references=(
                prompt_builder.SelectedReference("source_1", "model"),
            ),
            garment_facts=prompt_builder.GarmentFacts((), ()),
            infographic_inventory=None,
        )
        generation = GenerationRequest(
            context=context, target_index=0, target_token="target_1", attempt=1,
            artifact_name="attempt-01-aaaaaaaaaaaa-01.png",
            output_path=root / "generated_images" / "attempt-01-aaaaaaaaaaaa-01.png",
            prompt="prompt", reference_tokens=("source_1",), plan=plan,
        )
        qc_request = QCRequest(
            context=context, target_index=0, target_token="target_1", attempt=1,
            candidate=root / "attempt-01-aaaaaaaaaaaa-01.png",
            candidate_sha256="a" * 64, plan=plan,
        )
        for kind, wrapper, request, raw in (
            (
                "generation",
                lambda service, stop: BoundedGenerator(
                    service, threading.BoundedSemaphore(1), stop,
                ),
                generation,
                _Service(),
            ),
            (
                "qc",
                lambda service, stop: BoundedQC(
                    service, threading.BoundedSemaphore(1), stop,
                ),
                qc_request,
                _Service(),
            ),
        ):
            with self.subTest(kind=kind):
                stop = GlobalStop()
                bounded = wrapper(raw, stop)
                with self.assertRaises(PaidCallStopped):
                    getattr(bounded, "generate" if kind == "generation" else "review")(
                        request,
                    )
                self.assertTrue(stop.is_set())
                self.assertEqual(raw.calls, 0)

    def test_qc_recheck_rejects_candidate_bytes_changed_while_waiting(self) -> None:
        root = Path(tempfile.mkdtemp())
        generated = root / "generated_images"
        generated.mkdir()
        plan = prompt_builder.TargetPlan(
            classification="front",
            selected_references=(
                prompt_builder.SelectedReference("source_1", "model"),
            ),
            garment_facts=prompt_builder.GarmentFacts((), ()),
            infographic_inventory=None,
        )
        state = task_state.new_state(
            record_id="rec_1", run_id="run_1", source_tokens=["source_1"],
            target_tokens=["target_1"], started_at="2026-08-18T10:00:00+00:00",
        )
        task_state.record_target_plan(state, 0, json.loads(
            prompt_builder.serialize_plan(plan),
        ))
        task_state.begin_attempt(
            state, target_token="target_1", classification="front",
            reference_tokens=["source_1"], prompt="prompt", model="model",
            updated_at="2026-08-18T10:01:00+00:00",
        )
        artifact_name = state["targets"]["target_1"]["attempt_history"][-1][
            "artifact_name"
        ]
        task_state.save_state(root / "manifest.json", state)
        candidate = generated / artifact_name
        candidate.write_bytes(b"original candidate")
        original_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        request = QCRequest(
            context=RecordContext(root, "rec_1", (0,)), target_index=0,
            target_token="target_1", attempt=1, candidate=candidate,
            candidate_sha256=original_digest, plan=plan,
        )
        candidate.write_bytes(b"changed while queued")
        raw = _Service()
        stop = GlobalStop()
        qc = BoundedQC(raw, threading.BoundedSemaphore(1), stop)

        with self.assertRaises(PaidCallStopped):
            qc.review(request)

        self.assertTrue(stop.is_set())
        self.assertEqual(raw.calls, 0)

    def test_qc_recheck_allows_a_durable_candidate_from_its_owning_prior_run(self) -> None:
        root = Path(tempfile.mkdtemp())
        current = root / "current" / "rec_1"
        prior = root / "prior" / "rec_1" / "generated_images"
        current.mkdir(parents=True)
        prior.mkdir(parents=True)
        plan = prompt_builder.TargetPlan(
            classification="front",
            selected_references=(
                prompt_builder.SelectedReference("source_1", "model"),
            ),
            garment_facts=prompt_builder.GarmentFacts((), ()),
            infographic_inventory=None,
        )
        state = task_state.new_state(
            record_id="rec_1", run_id="prior", source_tokens=["source_1"],
            target_tokens=["target_1"], started_at="2026-08-18T10:00:00+00:00",
        )
        task_state.record_target_plan(state, 0, json.loads(
            prompt_builder.serialize_plan(plan),
        ))
        task_state.begin_attempt(
            state, target_token="target_1", classification="front",
            reference_tokens=["source_1"], prompt="prompt", model="model",
            updated_at="2026-08-18T10:01:00+00:00",
        )
        artifact_name = state["targets"]["target_1"]["attempt_history"][-1][
            "artifact_name"
        ]
        task_state.save_state(current / "manifest.json", state)
        candidate = prior / artifact_name
        candidate.write_bytes(b"prior run candidate")
        request = QCRequest(
            context=RecordContext(current, "rec_1", (0,)), target_index=0,
            target_token="target_1", attempt=1, candidate=candidate,
            candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
            plan=plan,
        )
        raw = _Service()
        qc = BoundedQC(raw, threading.BoundedSemaphore(1), GlobalStop())

        self.assertIs(qc.review(request), request)
        self.assertEqual(raw.calls, 1)

    def test_default_lark_read_and_write_maxima_and_exact_scope(self) -> None:
        limits = ServiceLimits()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        payload_root = Path(temporary.name)
        (payload_root / "update.json").write_text(
            '{"update_records":{"rec_1":{"处理明细":"observed"}}}',
            encoding="utf-8",
        )

        class Base:
            def __init__(self) -> None:
                self.read_probe = SemaphoreTest.Probe(2)
                self.write_probe = SemaphoreTest.Probe(1)

            def get_record(self, **kwargs: object) -> object:
                return self.read_probe.call(**kwargs)

            def update_record(self, **kwargs: object) -> object:
                return self.write_probe.call(**kwargs)

        raw = Base()
        base = BoundedBase(
            raw,
            read_semaphore=threading.BoundedSemaphore(limits.lark_reads),
            write_semaphore=threading.BoundedSemaphore(limits.lark_writes),
        ).scoped(_record_scope(payload_root=payload_root))

        read_threads = [threading.Thread(target=base.get_record, kwargs={
            "app_token": "app_exact", "table_id": "tbl_exact",
            "record_id": "rec_1", "field_ids": ["fld_output", "fld_detail"],
        }) for _ in range(6)]
        for thread in read_threads:
            thread.start()
        self.assertTrue(raw.read_probe.reached.wait(timeout=1))
        time.sleep(0.02)
        raw.read_probe.release.set()
        for thread in read_threads:
            thread.join(timeout=2)
        self.assertEqual(raw.read_probe.maximum, 2)

        write_threads = [threading.Thread(target=base.update_record, kwargs={
            "app_token": "app_exact", "table_id": "tbl_exact",
            "record_id": "rec_1", "payload": Path("update.json"),
        }) for _ in range(6)]
        for thread in write_threads:
            thread.start()
        self.assertTrue(raw.write_probe.reached.wait(timeout=1))
        time.sleep(0.02)
        raw.write_probe.release.set()
        for thread in write_threads:
            thread.join(timeout=2)
        self.assertEqual(raw.write_probe.maximum, 1)

        with self.assertRaises(PreflightError):
            base.get_record(
                app_token="app_other", table_id="tbl_exact", record_id="rec_1",
                field_ids=["fld_output", "fld_detail"],
            )
        with self.assertRaises(PreflightError):
            base.get_record(
                app_token="app_exact", table_id="tbl_exact", record_id="rec_2",
                field_ids=["fld_output", "fld_detail"],
            )

    def test_worker_base_capabilities_block_scope_escape_before_client_call(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        payload_root = Path(temporary.name)
        (payload_root / "update.json").write_text(
            '{"update_records":{"rec_1":{"处理明细":"observed"}}}',
            encoding="utf-8",
        )
        raw = _Base()
        base = BoundedBase(
            raw,
            read_semaphore=threading.BoundedSemaphore(2),
            write_semaphore=threading.BoundedSemaphore(1),
        ).scoped(_record_scope(
            attachment_tokens=frozenset({"box_source_1"}),
            payload_root=payload_root,
        ))

        base.get_record(
            app_token="app_exact", table_id="tbl_exact", record_id="rec_1",
            field_ids=["fld_output", "fld_detail"],
        )
        base.download_attachment(
            app_token="app_exact", table_id="tbl_exact", record_id="rec_1",
            token="box_source_1", output=Path("source.png"),
        )
        base.upload_attachment(
            app_token="app_exact", table_id="tbl_exact", record_id="rec_1",
            field_id="fld_output", file=Path("output.png"),
        )
        base.update_record(
            app_token="app_exact", table_id="tbl_exact", record_id="rec_1",
            payload=Path("update.json"),
        )
        self.assertEqual(len(raw.calls), 4)

        def blocked(method, **kwargs: object) -> None:
            before = len(raw.calls)
            with self.assertRaises(PreflightError):
                method(**kwargs)
            self.assertEqual(len(raw.calls), before)

        exact_read = {
            "app_token": "app_exact", "table_id": "tbl_exact",
            "record_id": "rec_1", "field_ids": ["fld_output", "fld_detail"],
        }
        for changed in (
            {**exact_read, "app_token": "app_other"},
            {**exact_read, "table_id": "tbl_other"},
            {**exact_read, "record_id": "rec_other"},
            {**exact_read, "field_ids": ["fld_output"]},
            {**exact_read, "view_id": "vew_other"},
        ):
            blocked(base.get_record, **changed)
        blocked(
            base.download_attachment,
            app_token="app_exact", table_id="tbl_exact", record_id="rec_1",
            token="box_unapproved", output=Path("source.png"),
        )
        blocked(
            base.upload_attachment,
            app_token="app_exact", table_id="tbl_exact", record_id="rec_1",
            field_id="fld_detail", file=Path("output.png"),
        )
        self.assertFalse(hasattr(base, "list_records"))
        self.assertFalse(hasattr(base, "list_fields"))
        self.assertFalse(hasattr(base, "create_field"))
        self.assertFalse(hasattr(base, "resolve_base"))

    def test_worker_update_payload_blocks_field_capability_escape_before_client_call(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = _Base()
            base = BoundedBase(
                raw,
                read_semaphore=threading.BoundedSemaphore(2),
                write_semaphore=threading.BoundedSemaphore(1),
            ).scoped(_record_scope(payload_root=root))

            def payload(name: str, value: str) -> Path:
                path = root / name
                path.write_text(value, encoding="utf-8")
                return path

            valid = payload(
                "valid.json",
                '{"update_records":{"rec_1":{"任务状态":["成功"],'
                '"处理明细":"observed"}}}',
            )
            base.update_record(
                app_token="app_exact", table_id="tbl_exact",
                record_id="rec_1", payload=Path(valid.name),
            )
            self.assertEqual(len(raw.calls), 1)

            malicious = {
                "extra root": (
                    '{"update_records":{"rec_1":{"处理明细":"x"}},'
                    '"delete_records":["rec_1"]}'
                ),
                "cross record": (
                    '{"update_records":{"rec_other":{"处理明细":"x"}}}'
                ),
                "source field": (
                    '{"update_records":{"rec_1":{"原图":[]}}}'
                ),
                "output cross field": (
                    '{"update_records":{"rec_1":{"输出图":[]}}}'
                ),
                "field id alias": (
                    '{"update_records":{"rec_1":{"fld_detail":"x"}}}'
                ),
                "nested detail": (
                    '{"update_records":{"rec_1":{"处理明细":'
                    '{"任务状态":["成功"]}}}}'
                ),
                "nested status": (
                    '{"update_records":{"rec_1":{"任务状态":[["成功"]]}}}'
                ),
                "duplicate root": (
                    '{"update_records":{"rec_1":{"处理明细":"x"}},'
                    '"update_records":{"rec_1":{"处理明细":"y"}}}'
                ),
                "duplicate record": (
                    '{"update_records":{"rec_1":{"处理明细":"x"},'
                    '"rec_1":{"处理明细":"y"}}}'
                ),
                "duplicate field": (
                    '{"update_records":{"rec_1":{"处理明细":"x",'
                    '"处理明细":"y"}}}'
                ),
            }
            for index, (label, value) in enumerate(malicious.items()):
                with self.subTest(label=label):
                    before = len(raw.calls)
                    path = payload(f"malicious-{index}.json", value)
                    with self.assertRaises(PreflightError):
                        base.update_record(
                            app_token="app_exact", table_id="tbl_exact",
                            record_id="rec_1", payload=Path(path.name),
                        )
                    self.assertEqual(len(raw.calls), before)

    def test_worker_update_uses_validated_private_snapshot_during_rename_race(
            self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "update.json"
            original.write_text(
                '{ "update_records": { "rec_1": {'
                ' "处理明细": "allowed" } } }',
                encoding="utf-8",
            )
            raw_entered = threading.Event()
            allow_raw_read = threading.Event()

            class WriteGate:
                def __init__(self) -> None:
                    self._semaphore = threading.BoundedSemaphore(1)
                    self._lock = threading.Lock()
                    self.active = False

                def __enter__(self) -> "WriteGate":
                    self._semaphore.acquire()
                    with self._lock:
                        self.active = True
                    return self

                def __exit__(self, *_args: object) -> None:
                    with self._lock:
                        self.active = False
                    self._semaphore.release()

                def is_active(self) -> bool:
                    with self._lock:
                        return self.active

            class ConsumingBase:
                def __init__(self) -> None:
                    self.payload_name: str | None = None
                    self.payload_mode: int | None = None
                    self.consumed: bytes | None = None

                def update_record(self, **kwargs: object) -> dict:
                    supplied = kwargs["payload"]
                    if not isinstance(supplied, Path):
                        raise AssertionError("payload must remain a filename")
                    self.payload_name = supplied.name
                    raw_entered.set()
                    if not allow_raw_read.wait(timeout=2):
                        raise AssertionError("raw Base read was not released")
                    path = root / supplied.name
                    self.payload_mode = path.stat().st_mode & 0o777
                    self.consumed = path.read_bytes()
                    return {"ok": True}

            gate = WriteGate()
            raw = ConsumingBase()
            base = BoundedBase(
                raw,
                read_semaphore=threading.BoundedSemaphore(2),
                write_semaphore=gate,
            ).scoped(_record_scope(payload_root=root))
            errors: list[BaseException] = []

            def update() -> None:
                try:
                    base.update_record(
                        app_token="app_exact", table_id="tbl_exact",
                        record_id="rec_1", payload=Path(original.name),
                    )
                except BaseException as error:  # pragma: no cover - asserted below
                    errors.append(error)

            thread = threading.Thread(target=update)
            thread.start()
            self.assertTrue(raw_entered.wait(timeout=1))
            self.assertTrue(gate.is_active())

            replacement = root / "forbidden.json"
            replacement.write_text(
                '{"update_records":{"rec_1":{"输出图":[]}}}',
                encoding="utf-8",
            )
            replacement.replace(original)
            allow_raw_read.set()
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertNotEqual(raw.payload_name, original.name)
            self.assertEqual(raw.payload_mode, 0o400)
            self.assertEqual(
                raw.consumed,
                '{"update_records":{"rec_1":{"处理明细":"allowed"}}}\n'.encode(),
            )
            self.assertIsNotNone(raw.payload_name)
            self.assertFalse((root / raw.payload_name).exists())
            self.assertIn("输出图", original.read_text(encoding="utf-8"))

    def test_worker_update_rejects_symlink_payload_before_client_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.json"
            outside.write_text(
                '{"update_records":{"rec_1":{"处理明细":"allowed"}}}',
                encoding="utf-8",
            )
            (root / "update.json").symlink_to(outside)
            raw = _Base()
            base = BoundedBase(
                raw,
                read_semaphore=threading.BoundedSemaphore(2),
                write_semaphore=threading.BoundedSemaphore(1),
            ).scoped(_record_scope(payload_root=root))

            with self.assertRaises(PreflightError):
                base.update_record(
                    app_token="app_exact", table_id="tbl_exact",
                    record_id="rec_1", payload=Path("update.json"),
                )
            self.assertEqual(raw.calls, [])


if __name__ == "__main__":
    unittest.main()
