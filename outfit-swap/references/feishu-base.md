# Feishu Base transport and finalization

Use this reference when validating table scope, Base fields, Lark transport, finalization, or write recovery. Keep `scripts/run_table.py` as the normal entry; use the component interfaces below only for diagnosis or implementation work.

## Scope and schema

Accept one exact table URL that resolves to one `base_token`, `table_id`, and view scope. Resolve it before mutation. Reject any resolver result containing `record_id`. Reject Base-only, record-share, BaseApp, Wiki, indirect, or ambiguous links; never guess or broaden the table.

Require these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `原图` | multi-value attachment | ordered garment evidence |
| `爆款图` | multi-value attachment | ordered edit targets |
| `输出图` | multi-value attachment | append-only accepted/historical outputs |
| `任务状态` | single select | exactly `未开始`, `成功`, `失败` |
| `处理明细` | multiline text | compact versioned state |

Treat a missing/mistyped field or status option as a global preflight failure. The only schema mutation allowed is creating absent `处理明细`. Immediately before creating absent `处理明细`, repeat the full absence check.

Select `未开始` by default:

```json
{"logic":"and","conditions":[["任务状态","intersects",["未开始"]]]}
```

Select `失败` only for explicit `--retry-failed`:

```json
{"logic":"and","conditions":[["任务状态","intersects",["失败"]]]}
```

Skip `成功` in both modes. While `has_more` is true, advance listing offset by the returned record count, preserve every page artifact, and retain cross-page record order. Treat 2,000 as the per-page cap, not the run cap.

## Typed Lark interface

Use `scripts/lark_runner.py:LarkBaseClient` for Base transport. Its implemented operations are `resolve_base`, `list_fields`, `create_field`, `list_records`, `list_records_page`, `download_attachment`, `upload_attachment`, `update_record`, and `get_record`. `scripts/production_runtime.py` assembles these operations for bare `scripts/run_table.py`; no host-supplied dependency injection is required. Authenticate as the user. Never make direct Feishu HTTP calls.

For every file-backed call, pass one task-local basename. Run `lark-cli` with the file's validated parent as `cwd`. Every file-backed argument is relative to that `cwd`:

```text
--file ./look-<index>-<target-digest>.png
--json @./record-update.json
--output ./records-<offset>.ndjson
```

Reject absolute file arguments, `..`, path separators, pre-existing output files, and symlink escapes. Keep argv as a list and `shell=False`; never construct `cd`, shell interpolation, or a shell pipeline.

Treat remote attachment names as untrusted metadata. Build local names only from the opaque role, attachment order, and token digest. Download to a safe provisional raster basename, validate the actual bytes, and use a canonical suffix derived from the decoded codec before Seedream or Ark transfer. A path-like remote name or an extension that disagrees with the bytes must neither escape the record directory nor become a MIME-label mismatch.

Use these record-keyed CellValue envelopes internally:

```json
{"update_records":{"<record-id>":{"处理明细":"<compact-json>"}}}
```

```json
{"update_records":{"<record-id>":{"任务状态":["成功"],"处理明细":"<compact-json>"}}}
```

Use `失败` in the terminal envelope for a failed record. Canonicalize update bytes and transport them through the private relative `@./record-update.json` file.

## One target transaction

Call `scripts/finalize_target.py:TargetFinalizer` after automatic acceptance or third-attempt selection. Treat finalization as one idempotent finalization transaction:

```text
revalidate candidate digest
→ promote deterministic output
→ persist accepted-local
→ read current Base fields
→ upload only when not already present
→ persist success mapping
→ write compact detail
→ exact Base readback
```

The exact Base readback must contain both the accepted attachment mapping and the exact compact detail. On mismatch, upload failure, update failure, or readback failure, stop immediately; do not turn a persistence failure into a generation retry.

Resume from the durable checkpoint:

- `running`: promote and persist `accepted-local` before upload.
- `accepted-local`: revalidate the candidate and reconcile/upload it without generation.
- uploaded but detail not written: reconcile the deterministic attachment, persist success, and continue the same transaction without duplicate upload.
- `success`: verify the current mapping and detail; return without duplicate upload when exact.

Outputs are append-only. Never delete or overwrite historical Base attachments. Match recovery by the target-token digest independently of current attachment order; the ordered index is display-only.

## Terminal record writes

After all eligible targets complete or record processing stops early, summarize durable state and persist `任务状态` with the latest compact detail, even when skipped targets remain `pending`. A record-level data failure requires this terminal write when Base is writable. A systemic Base write/readback failure sets the global stop and leaves later records untouched.
