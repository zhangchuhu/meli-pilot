# Quality control and failure handling

## Classification and local evidence

Classify every useful source and target image as exactly one of:

- front
- front three-quarter
- side
- back three-quarter
- back
- detail or flat lay
- infographic

`infographic` includes detail callouts, color grids, size charts, and comparison layouts. A source size chart is not an angle reference. Target details, flat lays, and infographics still require their own outputs.

For every initialized record, run `scripts/image_qc.py empty-contact-sheet` to create `qc/output-contact-sheet.jpg` with a concise empty/error label before any output is accepted and before record validation can fail. After validation and classification, create both `qc/source-contact-sheet.jpg` and `qc/target-contact-sheet.jpg`. Refresh the output contact sheet as accepted outputs accumulate. Inspect all three source/target/output contact sheets during final completeness and consistency review; never upload them to `输出图`.

## Per-record calibration and ordering

1. From `scripts/task_state.py pending`, choose the first pending ordinary single-model target in original attachment order. Never choose a reconciled current success.
2. Generate and inspect that target alone as calibration.
3. Continue only after it passes. Do not repeat the accepted calibration output.
4. Process every other pending target serially in original attachment order.
5. If no pending ordinary target remains, skip calibration and process any pending details, flat lays, or infographics serially. This includes retries where an earlier ordinary calibration target is already a valid current success.

Every target has three attempts in the current source identity and explicit retry cycle: one initial edit plus at most two one-change corrections. A user-authorized later `--retry-failed` cycle starts a fresh budget for current non-success targets. Inspect the returned bitmap after every attempt; never infer acceptance from a successful command.

## Rejection conditions

Reject an output when any of these occur:

- target identity, pose, composition, scene, or an important accessory changed
- any original target clothing remains visible
- garment color or key construction is wrong
- front and back construction is inconsistent with the selected references
- hands, feet, hair, bags, phones, or jewelry have bad occlusion
- limbs are distorted, fingers are added, buttons float, or unwanted text or logos appear
- an infographic retains an original garment instance that was required to change
- a multi-model grid changes only one cell when all clothing instances were required to change
- visible text or infographic layout changes materially

Record each attempt through `scripts/task_state.py`. Accept only after direct inspection confirms both garment fidelity and all target-preservation invariants.

## Failure classes

Use only this exact record-error mapping. Put the sanitized diagnostic in a local UTF-8 file and pass it with `--error-file`; never place a raw diagnostic in argv.

| Condition | Command | `--code` |
|---|---|---|
| current `原图` token list empty | `init-error` for new state, otherwise `reconcile-error` | `missing-source` |
| current `爆款图` token list empty | `init-error` for new state, otherwise `reconcile-error` | `missing-target` |
| both required token lists empty | same new/existing rule; message states both are empty | `missing-source` |
| downloaded `原图` is corrupt | `record-error` | `corrupt-source` |
| downloaded `爆款图` is corrupt | `record-error` | `corrupt-target` |
| decodable `原图` violates image constraints | `record-error` | `invalid-source` |
| decodable `爆款图` violates image constraints | `record-error` | `invalid-target` |
| other record-scoped data fault whose role cannot be attributed | `record-error` | `record-data` |
| Doubao, upload, or critical Base-write failure while local state is writable | `record-error` | `external-call` |

These are the only record-error codes. A rejected target attempt uses `failure`, not a record error.

### Global preflight failure

An invalid table URL, schema mismatch, missing dependency/key, or occupied local lock stops the entire run before generation. Leave every record status unchanged and incur no image cost.

### Record data failure

Missing source/target attachments, a corrupt image, or invalid image constraints marks only that record `失败` with a precise compact detail entry. Continue with the next record.

Persist the `record-error` state, compact and summarize it, and write terminal `任务状态: 失败` plus `处理明细` before continuing, even when current targets remain pending. This terminal Base write is required for every record-level stop.

### Calibration exhaustion

If calibration is rejected on all three attempts, mark the record `失败`, skip all remaining targets in that record, and continue with the next record.

Before continuing, compact and summarize the manifest and write terminal `任务状态: 失败` plus `处理明细`. This terminal Base write is required even though skipped targets remain pending.

### Later visual exhaustion

If a later target is rejected on all three attempts, record that target failure and continue processing the other targets in the same record so accepted work is preserved. Finish the record as `失败`.

### External call failure

A Doubao tool/API failure, attachment-upload failure, or critical Base-write failure stops the entire run immediately. Mark the current record `失败` when Base remains writable. Persist an already-inspected bitmap with `accept-local` before upload; after restart, reconcile its validated identity and drain `scripts/task_state.py uploads` before calibration or generation. If Feishu cannot be updated, retain local artifacts and manifest state for recovery and leave later records untouched.

Always release an owned lock on normal and handled abnormal exits through the holder's authenticated control request. The permanent mutation guard serializes inspection/reclaim/release. Reclaim a same-machine lock only when the control channel is absent and its PID plus OS process-start identity prove the holder is gone or the PID was reused; timeouts and ambiguous identity fail closed. Elapsed time alone never overrides a live holder.
