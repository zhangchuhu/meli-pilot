"""Concrete production assembly for the standalone outfit-swap table runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scripts import (
    ark_vision_qc,
    event_log,
    image_qc,
    infographic_text,
    prompt_builder,
    reference_selector,
    task_state,
)
from scripts.lark_runner import LarkBaseClient, RecordPage
from scripts.run_record import QCRequest, RecordContext, RecordResult, RecordServices, run_record
from scripts.run_table import (
    BaseField,
    PreflightError,
    ProductionRecordServicesFactory,
    RecordBaseScope,
    SeedreamGeneratorAdapter,
    TableConfig,
    TableResult,
    TableRuntime,
    TableSchema,
    TableSchedulerError,
    TableScope,
    run_table,
)


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ORDINARY = frozenset({
    "front", "front three-quarter", "side", "back three-quarter", "back",
})
_CLASSIFICATIONS = _ORDINARY | frozenset({"detail or flat lay", "infographic"})
_DETAIL_DEFINITION = {
    "name": "处理明细", "type": "text", "style": {"type": "plain"},
}


class RecordMaterializationError(ValueError):
    """Raised for one record's missing, corrupt, or unsupported input bytes."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _directory_from_env(environ: Mapping[str, str], name: str, default: Path) -> Path:
    raw = environ.get(name)
    path = Path(raw).expanduser() if isinstance(raw, str) and raw.strip() else default
    resolved = path.resolve(strict=False)
    if resolved.exists() and (not resolved.is_dir() or resolved.is_symlink()):
        raise TableSchedulerError(f"{name} is not a safe directory")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    return resolved


def _file_from_env(environ: Mapping[str, str], name: str, default: str) -> Path:
    raw = environ.get(name, default)
    path = Path(raw).expanduser().resolve(strict=False)
    if not path.is_file() or path.is_symlink():
        raise TableSchedulerError(f"{name} does not identify a regular file")
    return path


def _executable_from_env(
        environ: Mapping[str, str], name: str, default: str,
) -> str:
    raw = environ.get(name, default)
    if not isinstance(raw, str) or not raw.strip():
        raise TableSchedulerError(f"{name} does not identify an executable")
    candidate = raw.strip()
    resolved = shutil.which(candidate)
    if resolved is None:
        raise TableSchedulerError(f"{name} does not identify an executable")
    return resolved


def _require_dependencies(environ: Mapping[str, str]) -> str:
    for name in ("ARK_API_KEY", "ARK_VISION_MODEL"):
        value = environ.get(name)
        if not isinstance(value, str) or not value.strip():
            raise TableSchedulerError(f"missing required environment variable: {name}")
    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise TableSchedulerError(f"missing required executable: {executable}")
    return _executable_from_env(environ, "OUTFIT_SWAP_LARK_CLI", "lark-cli")


def _make_ark_client() -> ark_vision_qc.ArkVisionClient:
    return ark_vision_qc.ArkVisionClient()


class ProductionLarkService:
    """Route safe relative file transports to each owning task directory."""

    def __init__(self, *, run_dir: Path, executable: str | Path) -> None:
        self._run_dir = Path(run_dir).resolve()
        self._executable = str(executable)
        self._records: dict[str, Path] = {}
        self._root = self._client(self._run_dir)

    def _client(self, directory: Path) -> LarkBaseClient:
        resolved = Path(directory).resolve()
        try:
            resolved.relative_to(self._run_dir)
        except ValueError:
            raise TableSchedulerError("Lark task directory escaped the current run") from None
        return LarkBaseClient(task_dir=resolved, executable=self._executable)

    def register_record(self, record_id: str, generated_dir: Path) -> None:
        if record_id in self._records:
            raise PreflightError("record materialization contains duplicates")
        self._records[record_id] = Path(generated_dir).resolve()

    def resolve_base(self, base_url: str) -> dict:
        return self._root.resolve_base(base_url)

    def list_fields(self, **kwargs: object) -> dict:
        return self._root.list_fields(**kwargs)

    def create_field(self, **kwargs: object) -> dict:
        return self._root.create_field(**kwargs)

    def list_records_page(self, **kwargs: object) -> RecordPage:
        return self._root.list_records_page(**kwargs)

    def list_records(self, **kwargs: object) -> Path:
        return self._root.list_records(**kwargs)

    def download_attachment(self, **kwargs: object) -> Path:
        output = Path(kwargs.pop("output"))
        if not output.is_absolute():
            raise TableSchedulerError("materialized download path is not absolute")
        client = self._client(output.parent)
        return client.download_attachment(output=Path(output.name), **kwargs)

    def upload_attachment(self, **kwargs: object) -> dict:
        record_id = str(kwargs.get("record_id", ""))
        return self._record_client(record_id).upload_attachment(**kwargs)

    def update_record(self, **kwargs: object) -> dict:
        record_id = str(kwargs.get("record_id", ""))
        return self._record_client(record_id).update_record(**kwargs)

    def update_record_canonical(self, **kwargs: object) -> dict:
        record_id = str(kwargs.get("record_id", ""))
        return self._record_client(record_id).update_record_canonical(**kwargs)

    def get_record(self, **kwargs: object) -> dict:
        return self._root.get_record(**kwargs)

    def _record_client(self, record_id: str) -> LarkBaseClient:
        directory = self._records.get(record_id)
        if directory is None:
            raise TableSchedulerError("record Lark scope was not materialized")
        return self._client(directory)


def _object(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise TableSchedulerError(f"Ark {label} response has an invalid shape")
    return value


def _string_list(value: Any, label: str, *, allow_empty: bool) -> tuple[str, ...]:
    if (not isinstance(value, list) or (not allow_empty and not value)
            or not all(isinstance(item, str) and item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise TableSchedulerError(f"Ark {label} response is invalid")
    return tuple(value)


def _strict_json(raw: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (TypeError, ValueError):
        raise TableSchedulerError("Ark planning response was not strict JSON") from None


class ArkPlanner:
    """Create immutable target plans from typed Ark visual evidence."""

    def __init__(self, adapter: "ProductionTableAdapter", client: object) -> None:
        self._adapter = adapter
        self._client = client

    def plan_target(
            self, context: RecordContext, target_index: int, target_token: str,
    ) -> prompt_builder.TargetPlan:
        target = self._adapter.image_path(context.record_id, target_token, "target")
        sources = self._adapter.source_paths(context.record_id)
        classification = self._classify(target)
        inventory = None
        instances: Sequence[str] = ()
        if classification == "infographic":
            inventory = infographic_text.settle_inventory(
                target_token,
                read=lambda token: self._read_inventory(token, target),
                adjudicate=lambda token, first, second: self._adjudicate_inventory(
                    token, target, first, second,
                ),
            )
            instances = inventory.garment_instances
        evidence, facts, unique = self._source_evidence(sources, instances=instances)
        if classification in _ORDINARY or classification == "infographic":
            selection = reference_selector.select_references(
                evidence, classification=classification,
                garment_instances=instances, unique_requirement=unique,
            )
            chosen = list(selection.selected)
            roles = list(selection.roles)
            fifth_reason = selection.fifth_reference_reason
        else:
            chosen = sorted(
                (item for item in evidence if item.information_score > 0
                 and "size_chart" not in item.roles),
                key=lambda item: (-item.information_score, item.token),
            )[:4]
            roles = ["garment_evidence"] * len(chosen)
            fifth_reason = None
        selected_tokens = {item.token for item in chosen}
        for item in sorted(
                evidence, key=lambda value: (-value.information_score, value.token),
        ):
            if len(chosen) >= 3:
                break
            if (item.token not in selected_tokens and item.information_score > 0
                    and "size_chart" not in item.roles):
                chosen.append(item)
                roles.append("supporting_evidence")
                selected_tokens.add(item.token)
        if not 3 <= len(chosen) <= 5:
            raise TableSchedulerError("target planning requires three or four references")
        if len(chosen) == 5 and fifth_reason is None:
            raise TableSchedulerError("the fifth reference lacks unique evidence")
        return prompt_builder.TargetPlan(
            classification=classification,
            selected_references=tuple(
                prompt_builder.SelectedReference(item.token, role)
                for item, role in zip(chosen, roles, strict=True)
            ),
            garment_facts=facts,
            infographic_inventory=inventory,
            fifth_reference_reason=fifth_reason,
        )

    def _complete(self, *, system: str, user: str, images: Sequence[Path]) -> Any:
        complete = getattr(self._client, "complete_json", None)
        if not callable(complete):
            raise TableSchedulerError("Ark client has no JSON completion boundary")
        return _strict_json(complete(
            system_prompt=system, user_prompt=user, images=tuple(images),
        ))

    def _classify(self, target: Path) -> str:
        value = _object(self._complete(
            system=(
                "Determine one target classification from visible content. Return "
                "strict JSON with schema_version and classification only."
            ),
            user=(
                "Choose exactly one of: front, front three-quarter, side, back "
                "three-quarter, back, detail or flat lay, infographic."
            ),
            images=(target,),
        ), frozenset({"schema_version", "classification"}), "target classification")
        classification = value["classification"]
        if value["schema_version"] != 1 or classification not in _CLASSIFICATIONS:
            raise TableSchedulerError("Ark target classification response is invalid")
        return classification

    def _source_evidence(
            self, sources: tuple[tuple[str, Path], ...], *,
            instances: Sequence[str],
    ) -> tuple[
        tuple[reference_selector.SourceEvidence, ...],
        prompt_builder.GarmentFacts,
        reference_selector.UniqueEvidenceRequirement | None,
    ]:
        tokens = [token for token, _path in sources]
        value = _object(self._complete(
            system=(
                "Extract source garment evidence from visible content. Return strict "
                "JSON for every supplied opaque token and garment facts."
            ),
            user=(
                "Tokens in image order: " + json.dumps(tokens, separators=(",", ":"))
                + ". Roles may include model, upper_construction, "
                  "full_outfit_flat_lay, skirt_hem, size_chart, and instance:<literal>. "
                + "Required infographic instances: "
                + json.dumps(list(instances), ensure_ascii=False, separators=(",", ":"))
                + "."
            ),
            images=tuple(path for _token, path in sources),
        ), frozenset({
            "schema_version", "sources", "garment_facts", "unique_requirement",
        }), "source garment evidence")
        if value["schema_version"] != 1 or not isinstance(value["sources"], list):
            raise TableSchedulerError("Ark source garment evidence response is invalid")
        paths = dict(sources)
        evidence: list[reference_selector.SourceEvidence] = []
        returned: list[str] = []
        for raw in value["sources"]:
            item = _object(raw, frozenset({
                "token", "angle", "roles", "information_score",
            }), "source evidence item")
            token = item["token"]
            score = item["information_score"]
            roles = _string_list(item["roles"], "source roles", allow_empty=False)
            if (token not in paths or item["angle"] not in _CLASSIFICATIONS
                    or not isinstance(score, int) or isinstance(score, bool)
                    or not 0 <= score <= 100):
                raise TableSchedulerError("Ark source evidence item is invalid")
            returned.append(token)
            evidence.append(reference_selector.SourceEvidence(
                token=token, path=paths[token], angle=item["angle"],
                roles=frozenset(roles), information_score=score,
            ))
        if returned != tokens:
            raise TableSchedulerError("Ark source evidence changed attachment order")
        facts_value = _object(
            value["garment_facts"], frozenset({"required", "forbidden"}),
            "garment facts",
        )
        facts = prompt_builder.GarmentFacts(
            required=_string_list(
                facts_value["required"], "required garment facts", allow_empty=True,
            ),
            forbidden=_string_list(
                facts_value["forbidden"], "forbidden garment facts", allow_empty=True,
            ),
        )
        unique_value = value["unique_requirement"]
        unique = None
        if unique_value is not None:
            unique_object = _object(
                unique_value, frozenset({"role", "reason"}), "unique requirement",
            )
            unique = reference_selector.UniqueEvidenceRequirement(
                role=unique_object["role"], reason=unique_object["reason"],
            )
        return tuple(evidence), facts, unique

    def _read_inventory(
            self, target_token: str, target: Path,
    ) -> infographic_text.InventoryReading:
        value = self._inventory_response(
            target, "Read every literal text item, panel, and garment instance once."
        )
        return infographic_text.InventoryReading(target_token=target_token, **value)

    def _adjudicate_inventory(
            self, target_token: str, target: Path,
            first: infographic_text.InventoryReading,
            second: infographic_text.InventoryReading,
    ) -> infographic_text.InventoryReading:
        evidence = json.dumps({
            "first": {
                "visible_text": first.visible_text, "panels": first.panels,
                "garment_instances": first.garment_instances,
            },
            "second": {
                "visible_text": second.visible_text, "panels": second.panels,
                "garment_instances": second.garment_instances,
            },
        }, ensure_ascii=False, separators=(",", ":"))
        value = self._inventory_response(
            target, "Adjudicate these two readings against the same image: " + evidence,
        )
        return infographic_text.InventoryReading(target_token=target_token, **value)

    def _inventory_response(self, target: Path, user: str) -> dict[str, tuple[str, ...]]:
        value = _object(self._complete(
            system=(
                "Return an exact infographic inventory as strict JSON with "
                "visible_text, panels, and garment_instances only."
            ), user=user, images=(target,),
        ), frozenset({"visible_text", "panels", "garment_instances"}), "inventory")
        return {
            key: _string_list(value[key], key, allow_empty=False)
            for key in ("visible_text", "panels", "garment_instances")
        }


class ArkQCAdapter:
    def __init__(self, adapter: "ProductionTableAdapter", client: object) -> None:
        self._adapter = adapter
        self._client = client

    def review(self, request: QCRequest) -> ark_vision_qc.QCReviewResult:
        if not isinstance(request, QCRequest):
            raise TableSchedulerError("QC request is invalid")
        images = self._adapter.qc_images(request)
        infographic = request.plan.classification == "infographic"
        user = (
            "Review the candidate against the target and ordered garment references. "
            + ("Verify every settled literal and panel. " if infographic else "")
            + "Return the candidate field exactly as '"
            + request.candidate.name + "'."
        )
        return ark_vision_qc.review_candidate(
            self._client,
            system_prompt=(
                "You are the visual quality reviewer. Return exactly one JSON object "
                "using visual-QC schema version 1. Base every score and defect only "
                "on visible image content."
            ),
            user_prompt=user,
            images=images,
            candidate=request.candidate.name,
            infographic=infographic,
        )


class ProductionTableAdapter:
    def __init__(
            self, *, run_id: str, run_dir: Path, runs_root: Path,
            state_root: Path, base_service: ProductionLarkService,
            ark_client: object,
    ) -> None:
        self._run_id = run_id
        self._run_dir = Path(run_dir).resolve()
        self._runs_root = Path(runs_root).resolve()
        self._state_root = Path(state_root).resolve()
        self._base = base_service
        self._ark_client = ark_client
        self._scope: TableScope | None = None
        self._schema: TableSchema | None = None
        self._attachments: dict[str, dict[str, dict[str, Path]]] = {}
        self._table_events = event_log.EventLog(self._run_dir / "events.ndjson")

    def resolve_base(self, base_url: str, base: object) -> TableScope:
        response = base.resolve_base(base_url)
        if not isinstance(response, dict) or _contains_key(response, "record_id"):
            raise PreflightError("Base URL did not resolve to an exact table view")
        payload = response.get("data", response)
        if not isinstance(payload, dict):
            raise PreflightError("Base URL did not resolve to an exact table view")
        values = tuple(payload.get(name) for name in (
            "base_token", "table_id", "view_id",
        ))
        if not all(isinstance(value, str) and value for value in values):
            raise PreflightError("Base URL did not resolve to an exact table view")
        scope = TableScope(*values)
        self._scope = scope
        self._table_events.append(
            "table_started", table_id=scope.table_id, status="running",
        )
        return scope

    def authenticate(self, scope: TableScope, base: object) -> None:
        if scope != self._scope or base is None:
            raise PreflightError("Base authentication scope changed")

    def load_schema(self, scope: TableScope, base: object) -> TableSchema:
        fields = self._all_fields(scope, base)
        if not any(field.name == "处理明细" for field in fields):
            if any(field.name == "处理明细" for field in self._all_fields(scope, base)):
                raise PreflightError("Base schema changed during detail-field preflight")
            base.create_field(
                app_token=scope.app_token, table_id=scope.table_id,
                definition=dict(_DETAIL_DEFINITION),
            )
            fields = self._all_fields(scope, base)
        schema = TableSchema(tuple(fields))
        self._schema = schema
        return schema

    def _all_fields(self, scope: TableScope, base: object) -> list[BaseField]:
        fields: list[BaseField] = []
        offset = 0
        while True:
            response = base.list_fields(
                app_token=scope.app_token, table_id=scope.table_id,
                limit=200, offset=offset,
            )
            items, has_more = _page_items(response)
            fields.extend(_base_field(item) for item in items)
            if not has_more:
                return fields
            if not items:
                raise PreflightError("Base field pagination did not advance")
            offset += len(items)

    def list_records(
            self, scope: TableScope, schema: TableSchema, *, retry_failed: bool,
            qc_mode: str, base: object,
    ) -> Iterable[RecordContext]:
        del qc_mode
        filter_name = "status-filter.json"
        filter_path = self._run_dir / filter_name
        selected_status = "失败" if retry_failed else "未开始"
        filter_path.write_text(json.dumps({
            "logic": "and",
            "conditions": [["任务状态", "intersects", [selected_status]]],
        }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        offset = 0
        contexts: list[RecordContext] = []
        field_ids = [schema.field(name).field_id for name in (
            "原图", "爆款图", "输出图", "任务状态", "处理明细",
        )]
        while True:
            output = Path(f"records-{offset}.ndjson")
            page = base.list_records_page(
                app_token=scope.app_token, table_id=scope.table_id,
                view_id=scope.view_id, field_ids=field_ids,
                filter_payload=Path(filter_name), output=output,
                limit=2000, offset=offset, retry_failed=retry_failed,
            )
            if not isinstance(page, RecordPage):
                raise PreflightError("Base record page summary is invalid")
            records = _read_records(page.path)
            if len(records) != page.records_count:
                raise PreflightError("Base record page count does not match its artifact")
            for record in records:
                fields = record.get("fields")
                status = fields.get("任务状态") if isinstance(fields, dict) else None
                if status == ["成功"]:
                    continue
                contexts.append(self._materialize(
                    scope, schema, record, retry_failed=retry_failed, base=base,
                ))
            if not page.has_more:
                return tuple(contexts)
            if page.records_count <= 0:
                raise PreflightError("Base record pagination did not advance")
            offset += page.records_count

    def _materialize(
            self, scope: TableScope, schema: TableSchema, record: dict[str, Any], *,
            retry_failed: bool, base: object,
    ) -> RecordContext:
        record_id = record.get("record_id")
        fields = record.get("fields")
        if (not isinstance(record_id, str) or _SAFE_ID.fullmatch(record_id) is None
                or not isinstance(fields, dict)):
            raise PreflightError("Base record identity or fields are invalid")
        sources = _attachments(fields.get("原图"))
        targets = _attachments(fields.get("爆款图"))
        outputs = _outputs(fields.get("输出图"))
        record_dir = self._run_dir / record_id
        generated = record_dir / "generated_images"
        source_dir = record_dir / "source_images"
        target_dir = record_dir / "target_images"
        for directory in (record_dir, generated, source_dir, target_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = record_dir / "manifest.json"
        state_path = task_state.bind_manifest(
            state_root=self._state_root, base_token=scope.app_token,
            table_id=scope.table_id, record_id=record_id,
            run_manifest=manifest,
        )
        started = _now()
        source_tokens = [item["file_token"] for item in sources]
        target_tokens = [item["file_token"] for item in targets]
        if not state_path.exists():
            state = (
                task_state.new_state(
                    record_id=record_id, run_id=self._run_id,
                    source_tokens=source_tokens, target_tokens=target_tokens,
                    started_at=started,
                )
                if sources and targets else task_state.new_record_error_state(
                    record_id=record_id, run_id=self._run_id,
                    source_tokens=source_tokens, target_tokens=target_tokens,
                    started_at=started,
                    code="missing-source" if not sources else "missing-target",
                    error="required attachment field is empty", updated_at=started,
                )
            )
            task_state.save_state(manifest, state)
        else:
            state = task_state.load_state(manifest)
            if not sources or not targets:
                task_state.reconcile_error(
                    state, source_tokens=source_tokens, target_tokens=target_tokens,
                    outputs=outputs, run_id=self._run_id, started_at=started,
                    code="missing-source" if not sources else "missing-target",
                    error="required attachment field is empty", updated_at=started,
                )
            else:
                task_state.reconcile(
                    state, source_tokens=source_tokens, target_tokens=target_tokens,
                    outputs=outputs, run_id=self._run_id, started_at=started,
                    updated_at=started,
                    resumable_artifacts=self._resumable_artifacts(state),
                )
            if retry_failed and sources and targets:
                task_state.prepare_retry(state, updated_at=started)
            task_state.save_state(manifest, state)
        image_qc.build_empty_contact_sheet(
            record_dir / "output-contact-sheet.jpg", "no accepted outputs",
        )
        self._base.register_record(record_id, generated)
        paths = {"source": {}, "target": {}}
        self._attachments[record_id] = paths
        if sources and targets:
            try:
                self._download_set(
                    scope, record_id, sources, source_dir, "source", paths, base,
                )
                self._download_set(
                    scope, record_id, targets, target_dir, "target", paths, base,
                )
            except RecordMaterializationError as error:
                state = task_state.load_state(manifest)
                task_state.record_error(
                    state, code=error.code, error="attachment validation failed",
                    updated_at=_now(),
                )
                task_state.save_state(manifest, state)
        return RecordContext(
            task_dir=record_dir, record_id=record_id,
            target_indices=tuple(range(len(target_tokens))),
        )

    def _download_set(
            self, scope: TableScope, record_id: str,
            attachments: tuple[dict[str, str], ...], directory: Path,
            role: str, paths: dict[str, dict[str, Path]],
            base: object,
    ) -> None:
        for index, item in enumerate(attachments, start=1):
            token = item["file_token"]
            digest = hashlib.sha256(token.encode()).hexdigest()[:12]
            provisional = directory / f"{role}-{index:02d}-{digest}.png"
            base.download_attachment(
                app_token=scope.app_token, table_id=scope.table_id,
                record_id=record_id, token=token, output=provisional,
            )
            try:
                info = image_qc.validate_decodable_raster(provisional)
            except image_qc.ImageQCError:
                raise RecordMaterializationError(f"corrupt-{role}") from None
            suffixes = {
                "mjpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif",
            }
            suffix = suffixes.get(info.codec_name)
            if suffix is None:
                raise RecordMaterializationError(f"invalid-{role}")
            output = provisional.with_suffix(suffix)
            if output != provisional:
                if output.exists() or output.is_symlink():
                    raise TableSchedulerError("canonical attachment path already exists")
                provisional.rename(output)
            paths[role][token] = output

    def _resumable_artifacts(self, state: dict[str, Any]) -> tuple[dict[str, str], ...]:
        result: list[dict[str, str]] = []
        record_id = state["record_id"]
        for target in state["targets"].values():
            for history in target["attempt_history"]:
                path = (
                    self._runs_root / history["run_id"] / record_id
                    / "generated_images" / history["artifact_name"]
                )
                if path.is_file() and not path.is_symlink():
                    result.append({
                        "run_id": history["run_id"],
                        "artifact_name": history["artifact_name"],
                    })
        return tuple(result)

    def record_base_scope(
            self, context: RecordContext, scope: TableScope,
            schema: TableSchema,
    ) -> RecordBaseScope:
        tokens = frozenset(
            token for role in self._attachments[context.record_id].values()
            for token in role
        )
        return RecordBaseScope(
            table=scope, record_id=context.record_id,
            attachment_tokens=tokens,
            output_field_id=schema.field("输出图").field_id,
            status_field_id=schema.field("任务状态").field_id,
            detail_field_id=schema.field("处理明细").field_id,
            payload_root=context.task_dir / "generated_images",
        )

    def record_services(
            self, context: RecordContext, generator: object, qc: object,
            base: object, stop_signal: object, qc_mode: str,
    ) -> RecordServices:
        if self._scope is None or self._schema is None:
            raise TableSchedulerError("record services precede table preflight")
        factory = ProductionRecordServicesFactory(
            scope=self._scope, schema=self._schema,
            events_factory=lambda current: event_log.EventLog(
                current.task_dir / "events.ndjson",
            ),
        )
        return factory(context, generator, qc, base, stop_signal, qc_mode)

    def image_path(self, record_id: str, token: str, role: str) -> Path:
        try:
            return self._attachments[record_id][role][token]
        except KeyError:
            raise TableSchedulerError("attachment token was not materialized") from None

    def source_paths(self, record_id: str) -> tuple[tuple[str, Path], ...]:
        return tuple(self._attachments[record_id]["source"].items())

    def generation_images(self, request: object) -> tuple[Path, ...]:
        context = getattr(request, "context", None)
        target_token = getattr(request, "target_token", None)
        references = getattr(request, "reference_tokens", None)
        if (not isinstance(context, RecordContext) or not isinstance(target_token, str)
                or not isinstance(references, tuple)):
            raise TableSchedulerError("generation image request is invalid")
        return (
            self.image_path(context.record_id, target_token, "target"),
            *(self.image_path(context.record_id, token, "source") for token in references),
        )

    def artifact_path(
            self, context: RecordContext, history: dict[str, object],
    ) -> Path:
        run_id = history.get("run_id")
        name = history.get("artifact_name")
        if not isinstance(run_id, str) or not isinstance(name, str):
            raise TableSchedulerError("artifact identity is invalid")
        return self._runs_root / run_id / context.record_id / "generated_images" / name

    def qc_images(self, request: QCRequest) -> tuple[Path, ...]:
        return (
            request.candidate,
            self.image_path(request.context.record_id, request.target_token, "target"),
            *(self.image_path(request.context.record_id, token, "source")
              for token in request.plan.reference_tokens),
        )


def _page_items(response: object) -> tuple[list[dict[str, Any]], bool]:
    value = response.get("data", response) if isinstance(response, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise PreflightError("Base field page is invalid")
    items = value["items"]
    if not all(isinstance(item, dict) for item in items):
        raise PreflightError("Base field page is invalid")
    has_more = value.get("has_more", len(items) == 200)
    if not isinstance(has_more, bool):
        raise PreflightError("Base field page is invalid")
    return items, has_more


def _contains_key(value: object, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(
            _contains_key(item, target) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False


def _base_field(value: dict[str, Any]) -> BaseField:
    name = value.get("field_name", value.get("name"))
    field_id = value.get("field_id")
    kind = value.get("type")
    options_value = value.get("options", ())
    if isinstance(options_value, list):
        options = tuple(
            option.get("name") if isinstance(option, dict) else option
            for option in options_value
        )
    else:
        options = ()
    try:
        return BaseField(name, field_id, kind, options)
    except (PreflightError, TypeError):
        raise PreflightError("Base field definition is invalid") from None


def _read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError
            records.append(value)
    except (OSError, UnicodeError, ValueError):
        raise PreflightError("Base record artifact is invalid") from None
    return records


def _attachments(value: Any) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PreflightError("Base attachment field is invalid")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise PreflightError("Base attachment field is invalid")
        token = item.get("file_token", item.get("token"))
        if not isinstance(token, str) or not token:
            raise PreflightError("Base attachment field is invalid")
        result.append({"file_token": token, "name": "opaque"})
    if len({item["file_token"] for item in result}) != len(result):
        raise PreflightError("Base attachment tokens are duplicated")
    return tuple(result)


def _outputs(value: Any) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PreflightError("Base output field is invalid")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise PreflightError("Base output field is invalid")
        token = item.get("file_token", item.get("token"))
        name = item.get("name")
        if (not isinstance(token, str) or not token
                or not isinstance(name, str) or Path(name).name != name):
            raise PreflightError("Base output field is invalid")
        result.append({"file_token": token, "name": name})
    return tuple(result)


def _terminal_worker(context: RecordContext, services: RecordServices) -> RecordResult:
    try:
        result = run_record(context, services)
    except Exception:
        setter = getattr(services.stop_signal, "set", None)
        if callable(setter):
            setter()
        result = RecordResult(context.record_id, "failed", 0)
    finalizer = services.finalizer
    terminal = getattr(finalizer, "terminalize_record", None)
    if not callable(terminal):
        raise TableSchedulerError("finalizer has no terminal record boundary")
    try:
        terminal(context, result.status)
    except Exception:
        setter = getattr(services.stop_signal, "set", None)
        if callable(setter):
            setter()
        raise
    return result


def execute(config: TableConfig, *, environ: Mapping[str, str] | None = None) -> TableResult:
    """Assemble and run the production scheduler for one documented invocation."""
    env = os.environ if environ is None else environ
    executable = _require_dependencies(env)
    state_root = _directory_from_env(
        env, "OUTFIT_SWAP_STATE_ROOT",
        Path.home() / ".codex" / "state" / "outfit-swap" / "tables",
    )
    runs_root = _directory_from_env(
        env, "OUTFIT_SWAP_RUNS_ROOT",
        Path.home() / ".codex" / "state" / "outfit-swap" / "runs",
    )
    run_id = _run_id()
    run_dir = runs_root / run_id
    run_dir.mkdir(mode=0o700)
    doubao = _file_from_env(
        env, "OUTFIT_SWAP_DOUBAO_SCRIPT",
        str(Path.home() / ".codex" / "skills" / "image-gen-ark"
            / "doubao-imagegen" / "scripts" / "doubao_imagegen.py"),
    )
    base = ProductionLarkService(run_dir=run_dir, executable=executable)
    ark = _make_ark_client()
    adapter = ProductionTableAdapter(
        run_id=run_id, run_dir=run_dir, runs_root=runs_root,
        state_root=state_root, base_service=base, ark_client=ark,
    )
    planner = ArkPlanner(adapter, ark)
    generator = SeedreamGeneratorAdapter(
        doubao_script=doubao, planner=planner,
        image_resolver=adapter.generation_images,
        artifact_resolver=adapter.artifact_path,
        approved_run_roots=(runs_root,),
    )
    runtime = TableRuntime(
        adapter=adapter, base=base, generator=generator,
        qc=ArkQCAdapter(adapter, ark), worker=_terminal_worker,
    )
    result = run_table(config, runtime)
    scope = adapter._scope
    if scope is not None:
        adapter._table_events.append(
            "table_finished", table_id=scope.table_id,
            status=(
                "stopped" if result.stopped
                else "failed" if result.failed else "success"
            ),
        )
    return result
