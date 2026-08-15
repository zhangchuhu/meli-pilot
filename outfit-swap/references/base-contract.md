# Feishu Base contract

Use `lark-cli` exclusively for Base operations and always authenticate as the user. The input must be one exact table URL that resolves directly to both a `base_token` and `table_id`. Resolve and validate the URL before any mutation, then reject any resolver result containing `record_id`: record-share scope is not table scope. Reject Base-only, record-share, BaseApp, Wiki, indirect, or otherwise ambiguous links, and never guess or broaden a table.

## Required fields

| Field | Required type | Contract |
|---|---|---|
| 原图 | Multi-value attachment | Source garment evidence |
| 爆款图 | Multi-value attachment | Every current attachment is a target |
| 输出图 | Multi-value attachment | Append-only accepted and historical outputs |
| 任务状态 | Single select | Options are exactly `未开始`, `成功`, `失败` |
| 处理明细 | Multiline text | Compact versioned JSON; create only when absent |

Any missing/mistyped required field or missing status option is a global preflight failure. The only schema mutation allowed is creating absent `处理明细`.

## Exact command shapes

Resolve the URL read-only:

```bash
lark-cli base +url-resolve --url '<table-url>' --as user
```

After accepting exactly one table identity, enumerate every field page. Start at offset zero, request 200, and advance the offset by the returned item count until a page contains fewer than 200 items:

```bash
lark-cli base +field-list --base-token '<base-token>' --table-id '<table-id>' --limit 200 --offset '<offset>' --as user
```

Immediately before creating absent `处理明细`, repeat the complete absence check, then create it once:

```bash
lark-cli base +field-create --base-token '<base-token>' --table-id '<table-id>' --json '{"name":"处理明细","type":"text","style":{"type":"plain"}}' --as user
```

Write exactly one of these UTF-8 filter files; do not interpolate Base values into a shell command:

```json
{"logic":"and","conditions":[["任务状态","intersects",["未开始"]]]}
```

For explicit `--retry-failed`, use instead:

```json
{"logic":"and","conditions":[["任务状态","intersects",["失败"]]]}
```

Project all five fields and collect page artifacts with this exact shape:

```bash
lark-cli base +record-list --base-token '<base-token>' --table-id '<table-id>' --field-id '<原图-field-id>' --field-id '<爆款图-field-id>' --field-id '<输出图-field-id>' --field-id '<任务状态-field-id>' --field-id '<处理明细-field-id>' --filter-json @'<status-filter.json>' --format ndjson --limit 2000 --offset '<offset>' --output '<run-dir>/records-<offset>.ndjson' --minimal-stdout --as user
```

Read each command's minimal summary. While `has_more` is true, add `records_count` to the prior offset and write a new offset-named artifact; never overwrite a prior page. Stop only on `has_more: false`. The 2,000-record CLI limit is a page cap, not a run cap. Preserve the returned record order across pages. Skip `成功` in both modes.

For each attachment in returned attachment order, validate its supplied suffix and sanitize the role and index. Compute `file-token-digest` as the first 12 lowercase hex characters of SHA-256 over the exact attachment token. Download exactly one token to one explicit, non-existing path under `source_images/` or `target_images/`; duplicate remote filenames therefore cannot collide:

```bash
lark-cli base +record-download-attachment --base-token '<base-token>' --table-id '<table-id>' --record-id '<record-id>' --file-token '<file-token>' --output '<record-dir>/<role>-<ordered-index>-<file-token-digest>.<validated-suffix>' --as user
```

After visual acceptance, upload exactly one deterministic output and then persist the new mapping immediately:

```bash
lark-cli base +record-upload-attachment --base-token '<base-token>' --table-id '<table-id>' --record-id '<record-id>' --field-id '<输出图-field-id>' --file '<accepted-output>' --as user
lark-cli base +record-batch-update --base-token '<base-token>' --table-id '<table-id>' --json @'<record-update.json>' --as user
```

Use the batch-update shape for incremental `处理明细` and terminal `任务状态` writes; do not wait until the record is finished to map an accepted upload.

`record-update.json` must use the external CLI's record-keyed CellValue envelope. `处理明细` is a JSON string CellValue and a single-select value is a one-element array. Immediately after each accepted attachment upload, write only the new compact mapping/detail:

```json
{"update_records":{"<record-id>":{"处理明细":"<compact-json>"}}}
```

After all current targets reach a terminal state or record processing stops early, write the status and latest detail together. For an early record/calibration stop, compact and summarize the durable manifest, then perform this terminal write even when skipped targets remain `pending`. Success is exactly:

```json
{"update_records":{"<record-id>":{"任务状态":["成功"],"处理明细":"<compact-json>"}}}
```

Failure uses the same shape with `["失败"]`. Batch updates may contain at most 200 record entries, although this workflow writes one current record at a time. Read the record back after each critical detail/status write; on mismatch or write failure, stop under the external-call policy rather than generating further images.

## Compact processing detail

`处理明细` is the compact JSON emitted by `scripts/task_state.py compact`. It contains:

- `schema_version`, `record_id`, `run_id`, and `started_at`
- ordered current `source_tokens` and `target_tokens`
- per-target `status`, `classification`, selected `reference_tokens`, `attempts`, uploaded `output` identity or pending `local_acceptance` artifact identity, `prompt_sha256`, `model`, concise `error`, and `updated_at`

The per-run `manifest.json` is a link created by `scripts/task_state.py bind` to the canonical cross-run record state under `~/.codex/state/outfit-swap/tables`. Keep full prompts, immutable attempt history, and stale output tokens only there. Never log credentials, API keys, raw diagnostic arguments, or raw data URLs; pass diagnostic text through `--error-file`.

The target attachment token is the stable work identity. A target counts as accepted only when its current token maps to a successful output whose attachment token is still present in `输出图`. Output filenames remain deterministic (`look-<ordered-index>-<target-token-digest>.png`), but recovery matches the target-token digest independently of current attachment order; the ordered index is display-only and never part of stable identity. Reconcile current Base attachments before retrying so reordered targets and uploads whose following detail write failed avoid another generation call.

Outputs are append-only: never delete historical attachments, including outputs for removed or replaced targets. Historical files do not count toward current success. After each acceptance, save locally, upload individually, update the mapping/detail, and update `manifest.json` before moving to the next target.
