# Outfit-swap automation and performance design

Date: 2026-08-18

## Objective

Reduce end-to-end wall time while preserving the existing 2K Seedream generation quality, three-call target budget, append-only outputs, resumability, exact-table scope, and Base readback guarantees.

The design combines five changes:

1. automatic structured visual QC through a Volcengine Ark multimodal vision model;
2. one table-level orchestrator and one target-finalization transaction;
3. dynamic selection of three or four high-value garment references;
4. stronger first-attempt garment constraints and explicit infographic text inventories;
5. configurable record-level concurrency, defaulting to two, while targets remain serial within each record.

## Non-goals

- Do not reduce Seedream output below fixed `2K` or change standard prompt optimization.
- Do not use output dimensions or aspect ratio as QC criteria.
- Do not introduce a fourth paid edit attempt.
- Do not make direct Feishu HTTP calls or replace `lark-cli`.
- Do not restore a persistent or cross-process run lock.
- Do not parallelize targets within one record.
- Do not delete or overwrite historical Base output attachments.

## Architecture

The table-level command is the only user-facing execution entry point:

```text
run_table.py
├── run_record.py
│   ├── reference_selector.py
│   ├── prompt_builder.py
│   ├── infographic_text.py
│   ├── safe_edit.py
│   ├── image_qc.py
│   ├── ark_vision_qc.py
│   ├── finalize_target.py
│   ├── lark_runner.py
│   └── task_state.py
└── table-level scheduler, semaphores, stop signal, and metrics
```

Existing Python functionality should be imported directly where practical. The Doubao CLI and `lark-cli` remain argv-array subprocesses with `shell=False`. Existing standalone CLIs remain available for tests, diagnosis, and recovery.

### `run_table.py`

`run_table.py` performs one global preflight, materializes the ordered selected-record list, and dispatches records to a bounded worker pool. It accepts `--record-concurrency N`, where `N` is a positive integer and defaults to `2`.

The scheduler keeps an in-process set of active record IDs so one invocation cannot assign the same record twice. This is queue ownership, not a persistent run lock. Two independent invocations must not process the same table simultaneously; without a cross-process lock, the system cannot guarantee that such misuse will never duplicate a paid call.

### `run_record.py`

`run_record.py` owns one record from reconciliation through terminal status. It processes targets serially in original attachment order. It performs calibration, starts paid attempts, validates artifacts, requests automatic QC, applies early acceptance or retry decisions, performs third-attempt selection, calls `finalize_target.py`, refreshes contact sheets, and writes the terminal record state.

Before any paid call it checks the global stop signal and the durable state checkpoint.

### `ark_vision_qc.py`

The initial automatic-QC backend uses Volcengine Ark multimodal `ChatCompletions`, separate from the Seedream image-generation endpoint. It reuses `ARK_API_KEY` and requires `ARK_VISION_MODEL`, the provisioned Ark vision Model ID. The model ID is configuration, not a hardcoded marketing model name.

Official references:

- [Volcengine Ark ChatCompletions API](https://api.volcengine.com/api-docs/view?action=Create3DGenerationsTasks&serviceCode=ark&version=2024-01-01)
- [Volcengine Ark multimodal understanding](https://www.volcengine.com/docs/82379/2377589?lang=zh)

The backend converts local images to supported Base64 image inputs, requests fixed-schema JSON text, parses it locally, and rejects missing fields, unknown enums, out-of-range values, Markdown wrappers, trailing prose, truncated responses, or content-filter responses. It never logs credentials, authorization headers, or raw Base64.

The backend makes decisions only. It does not mutate task state, create paid attempts, upload files, or write Base data.

### `finalize_target.py`

`finalize_target.py` accepts an already selected, complete, decodable candidate and performs the target commit sequence:

```text
revalidate candidate
→ promote deterministic output
→ accept-local
→ upload to Base
→ success mapping
→ compact detail
→ Base detail update
→ Base readback and exact verification
```

It must be idempotent and resumable:

- after promotion but before upload, resume from `accepted-local`;
- after upload but before detail update, reconcile the attachment token and do not upload again;
- on readback mismatch, stop immediately and retain the durable state;
- when the current successful mapping already exists, return success without a duplicate upload.

### `lark_runner.py`

All file-backed `lark-cli` calls run with the file's validated parent as `cwd` and a relative filename:

```text
--file ./look-09-....png
--json @./record-update.json
--output ./records-0.ndjson
```

The wrapper rejects absolute file arguments, `..`, path separators in the filename, and symlink resolution outside the expected task directory. It uses `subprocess.run(..., cwd=..., shell=False)` and never constructs a shell `cd` command.

## Reference selection

Ordinary front and front-three-quarter targets normally receive one target image plus three or four garment references:

1. closest-angle model reference, always primary;
2. upper-garment construction or closure detail;
3. complete outfit flat lay;
4. skirt construction or hem detail.

The selector is deterministic. It removes redundant, low-information references and never treats a size chart as an angle reference. Back and side targets prefer the closest matching back or side evidence. Infographics select references that cover their enumerated garment instances, normally within the same three-or-four-reference budget.

A fifth garment reference is allowed only when it provides non-redundant evidence that none of the first four contains; the target plan records the reason.

## Target plans and prompt construction

Before generation, every pending target receives an immutable structured target plan containing:

- classification and ordered garment instances;
- selected reference tokens and roles;
- visible garment facts;
- forbidden inferred structures;
- information-panel definitions and literal visible text where applicable.

Garment-specific first-attempt constraints are derived only from visible evidence. For the ivory lace set that motivated this design, the facts include:

```json
{
  "collar": "small pointed collar",
  "front_closure": "continuous pearl-button placket, fully closed",
  "neckline_forbidden": ["V neckline", "cardigan opening"],
  "undergarment_visibility": "no visible straps or neckline",
  "sleeves": "two complete full-length lace sleeves with cuffs"
}
```

The resulting initial prompt explicitly requires the blouse to close from the small collar down the full pearl-button placket, prohibits a V or cardigan opening and exposed camisole straps, and retains both sleeves and cuffs. These requirements are not global defaults for unrelated garments.

For a retry, the vision model returns an enumerated defect code. `prompt_builder.py`, not the model, maps the highest-priority defect to one controlled correction template. A retry adds only that one correction and preserves the base prompt and reference order.

## Infographic text and panel inventory

Before an infographic generation, `infographic_text.py` extracts a literal visible-text list and panel/instance inventory. The text inventory is an independent artifact, not prose remembered by the prompt author. The generation prompt may quote only values from this inventory.

For example:

```json
{
  "visible_text": ["FULL SWEEP", "FLOWY HEM", "LACE DETAIL"],
  "panels": [
    "dominant upper skirt panel",
    "lower-left full outfit panel",
    "lower-right lace detail panel"
  ]
}
```

Low-confidence extraction is repeated on the same target. If the results disagree, one adjudication request resolves the literal inventory before any paid image call. The generated candidate is checked for missing, added, or altered text, missing garment instances, panel-count changes, and material layout changes.

## Automatic QC contract

File completeness and full decoding remain deterministic `image_qc.py` checks and occur before visual QC. Pixel dimensions and target/output aspect-ratio differences are never supplied as decision features.

The vision backend returns strict JSON equivalent to:

```json
{
  "schema_version": 1,
  "candidate": "attempt-09-...-02.png",
  "scores": {
    "garment_construction": 96,
    "color_material": 94,
    "garment_details": 93,
    "target_preservation": 92,
    "text_layout": null
  },
  "critical_defects": [],
  "primary_defect": null,
  "evidence": [],
  "confidence": 0.94,
  "decision": "accept"
}
```

Allowed primary-defect codes include:

```text
wrong_collar
open_front
missing_sleeve
wrong_skirt_shape
wrong_color
original_clothing_remains
identity_changed
accessory_changed
bad_occlusion
anatomy_distortion
missing_infographic_instance
text_changed
layout_changed
```

Attempts one and two pass early only when all applicable conditions hold:

```text
garment_construction >= 90
color_material >= 88
garment_details >= 88
target_preservation >= 90
confidence >= 0.85
critical_defects is empty
```

An infographic also requires `text_layout >= 95`, exact literal text, no added or missing text, all garment instances present, and materially preserved panel layout.

The highest-priority defect determines the next targeted correction:

```text
garment construction
→ original clothing or missing instance
→ infographic text or layout
→ identity, pose, accessory, or background preservation
→ anatomy or occlusion
→ color and material
→ secondary garment details
```

An invalid or low-confidence QC result is retried on the same candidate and does not consume a paid generation attempt. If two valid decisions disagree, one adjudication request returns the final structured decision. Persistent QC-service failure preserves the candidate, starts no new generation, records a sanitized external-service failure, and resumes by checking the same artifact.

## Third-attempt selection

After the third initiated generation call, every complete, decodable candidate from the current cycle enters one comparative QC request. Selection follows the existing lexicographic garment-first contract rather than a weighted sum:

1. garment construction and silhouette;
2. color and material;
3. closures, seams, trim, decoration, and other garment details;
4. target preservation, including text and layout for infographics;
5. earlier attempt when still tied.

The selected candidate may be an earlier attempt. If at least one complete, decodable candidate exists, the highest-ranked candidate is finalized without a fourth generation. If none exists, the record takes the existing terminal external-call path.

Each QC result and final ranking is append-only under the record QC directory. Compact Base detail retains only the current outcome and pointers/digests; full evidence stays local.

## Parallel scheduling

Records run in parallel; targets within a record remain serial. Default resource limits are:

```text
record workers:       2
Doubao requests:      2
vision QC requests:   2
lark writes:          1
lark reads:           2
```

`--record-concurrency` controls record workers and defaults to `2`. External-service semaphores independently protect generation, QC, and Base operations. Rate-limit handling may temporarily reduce effective throughput without mutating the configured value.

Completion order across records is not guaranteed. Attachment order and persistence order within each record remain guaranteed.

## Failure handling and global stop

Record data faults and exhausted per-record generation failures stop only their record. A target-level visual rejection or first/second generation failure follows the existing target budget.

Systemic faults such as invalid global authentication, schema drift, missing required fields, persistent Base write/readback failure, or confirmed service-wide failure set a global stop event:

- do not dispatch new records;
- do not start new paid generations;
- allow already running generation requests to finish and save their artifacts;
- retain inspected artifacts as `accepted-local` where applicable;
- have workers exit at their next safe checkpoint.

Global preflight completes before workers start, so normal schema and credential errors incur no paid calls.

## Recovery order

Every invocation recovers in this order:

1. reconcile current Base attachments;
2. drain validated `accepted-local` uploads;
3. inspect an existing active artifact;
4. run deterministic validation and automatic QC on that artifact;
5. start a new generation only when no recoverable work exists and budget remains.

An absent artifact from an initiated attempt conservatively spends that attempt. An exhausted third-attempt checkpoint may only select an existing candidate or record terminal external-call.

## State compatibility

Canonical state advances from schema version 2 to 3 and adds a target plan plus QC report metadata. The reader migrates version 2 in memory and preserves all attempt history.

No Base schema changes are introduced. The required fields, three task statuses, deterministic output names, append-only output policy, and stable target-token identity remain unchanged. Existing task-state and image-QC CLIs remain supported.

## Performance observability

Each record writes sanitized append-only `events.ndjson` entries for:

- download and classification duration;
- selected reference count and total input bytes;
- Doubao duration and initiated-call count;
- visual-QC duration, retries, and adjudications;
- upload, detail update, and readback duration;
- early accepts and retry defect codes;
- total record duration.

The table summary reports record, target, generation-call, early-accept, average-reference, wall-time, Doubao-time, QC-time, and Lark-time totals. It never includes credentials, authorization headers, raw data URLs, or unsanitized external diagnostics.

## Test strategy

### Unit tests

- Reference selection is deterministic, chooses the closest-angle primary, covers complementary garment regions, excludes size charts, and normally caps references at four.
- Prompt construction adds closure and sleeve constraints only when supported by garment facts and adds exactly one controlled retry correction.
- Infographic inventory preserves exact literals such as `FLOWY HEM` and adjudicates disagreement before generation.
- QC schema validation rejects malformed responses, unknown defects, invalid ranges, or missing fields.
- Early acceptance uses the specified thresholds and no dimension criteria.
- Third-attempt ranking is lexicographic and chooses the earlier candidate only on a true tie.
- Finalization is idempotent across promotion, upload, detail update, and readback failure boundaries.
- Lark file arguments are relative to a validated `cwd` and cannot escape it.
- Scheduler default concurrency is two, configuration is honored, each record remains serial, and the global stop blocks new paid calls.

### Integration tests

Use fake Doubao, Ark vision, and Lark executables to cover:

- first-attempt early acceptance;
- rejection followed by second-attempt acceptance;
- three generated candidates followed by garment-best selection;
- invalid QC responses and adjudication without an extra generation;
- upload success followed by detail-write failure and recovery without regeneration or duplicate upload;
- two records running concurrently while each preserves target order;
- record-scoped failure isolation;
- global authentication failure stopping new paid work.

### Historical shadow replay

Before automatic decisions control uploads, replay stored artifacts from the observed real run. The QC backend must detect the open-front or exposed-camisole errors in targets 6, 7, and 9, and the changed `FLOWY HEM` text and layout in target 8. Previously accepted ordinary targets should not suffer an excessive false-rejection rate.

Shadow-mode exit criteria are:

- zero missed critical garment-construction defects in the replay set;
- zero missed infographic text changes;
- no more than ten percent false rejection among accepted ordinary images;
- every response either validates or follows the specified same-candidate review/adjudication path.

## Delivery phases

1. Implement `lark_runner.py` and `finalize_target.py` without changing QC decisions.
2. Implement reference selection, garment-fact prompt construction, and infographic inventories.
3. Implement Ark visual QC and run historical shadow replay until exit criteria pass.
4. Implement `run_record.py` and validate recovery with record concurrency one.
5. Implement `run_table.py`, external-service semaphores, default concurrency two, and global stopping.
6. Update skill contracts and contract tests, reinstall the skill, and validate a small real-data batch before a full-table run.

Rollback controls are `--record-concurrency 1` and `--qc-mode shadow`. If automatic QC becomes unavailable, the system preserves existing candidates and resumes their QC later rather than regenerating them.

## Acceptance criteria

- Default record concurrency is configurable and equals two.
- Ordinary generation calls normally use no more than four garment references.
- Known evidence-backed closure and sleeve constraints appear in the first prompt rather than only after a failure.
- Infographic literal text comes from a separately persisted inventory and is checked after generation.
- No `lark-cli` file argument uses an absolute path.
- Target finalization is one idempotent internal transaction with immediate Base readback.
- Automatic QC decisions are schema-valid, auditable, and dimension-independent.
- No target exceeds three initiated paid calls in one cycle.
- Default-concurrency whole-table wall time improves by at least 35 percent on comparable data without weakening image quality or persistence checks.
