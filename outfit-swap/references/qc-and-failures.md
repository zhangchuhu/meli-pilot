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

Every target has three initiated calls in the current source identity and explicit retry cycle: one initial call plus at most two retries. A user-authorized later `--retry-failed` cycle starts a fresh budget for current non-success targets. Attempts one and two use the normal full-QC rejection conditions. When either passes, stop immediately after an early full-QC pass, then promote, durably accept, upload, and mark that artifact successful. For a visual rejection on attempt one or two, retain the immutable artifact, call `failure --error-file`, add one targeted correction, and retry the same target. For a command failure or absent, incomplete, corrupt, or undecodable artifact on attempt one or two, call `failure --error-file` and retry the same target with no visual prompt correction. After attempt three, use the third-attempt garment-best selection below.

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

Record each attempt through `scripts/task_state.py`. For attempts one and two, accept only after direct inspection confirms both garment fidelity and all target-preservation invariants. After attempt three, compare every complete decodable candidate from the current cycle; the selected candidate may be earlier than the third artifact.

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
| attempt three has no complete decodable current-cycle candidate | `record-error` | `external-call` |
| attachment upload or critical Base-write/readback failure | `record-error` | `external-call` |

These are the only record-error codes. A Doubao failure on attempt one or two never uses `record-error`; use `failure --error-file`, retry the same target, and add no visual prompt correction. A rejected target attempt likewise uses `failure`, not a record error. Only a third attempt with no complete decodable current-cycle candidate becomes a generation-related `record-error --code external-call`; upload and critical Base-write/readback failures use that code immediately.

### Global preflight failure

An invalid table URL, schema mismatch, or missing dependency/key stops the entire run before generation. Leave every record status unchanged and incur no image cost.

### Record data failure

Missing source/target attachments, a corrupt image, or invalid image constraints marks only that record `失败` with a precise compact detail entry. Continue with the next record.

Persist the `record-error` state, compact and summarize it, and write terminal `任务状态: 失败` plus `处理明细` before continuing, even when current targets remain pending. This terminal Base write is required for every record-level stop.

### Third-attempt garment-best selection

After the third initiated call, compare every complete decodable candidate from the contiguous current three-attempt cycle against the ordered garment references, including a candidate previously visually rejected on attempt one or two. Exclude missing, partial, corrupt, and undecodable artifacts. At this final garment-best selection, garment fidelity outranks the earlier visual-rejection rationale. Rank eligible candidates by direct comparative inspection, in this order:

1. garment construction and silhouette;
2. color and material appearance;
3. closures, seams, trim, decoration, logos, and other visible garment details;
4. when garment similarity is tied, preservation of the target person, pose, crop, composition, accessories, background, lighting, and text/layout;
5. when the visual result is still tied, choose the earlier attempt.

Promote the selected artifact and pass its artifact name to `scripts/task_state.py accept-local`; this may select an earlier eligible artifact while attempt three remains active. If the third artifact is invalid but an earlier eligible candidate exists, select the best earlier candidate. If no complete decodable candidate exists, call `record-error --code external-call --error-file '<local-error-file>'` directly while attempt three is active, mark the record failed when Base remains writable, and stop the run without exposing another pending attempt.

### External call failure

A Doubao tool/API failure during attempt one or two uses the generation retry rule above. Upload and critical Base-write/readback failures stop the entire run immediately, are not generation retries, and mark the current record `失败` when Base remains writable. Persist an already-inspected bitmap with `accept-local` before upload; after restart, reconcile its validated identity and drain `scripts/task_state.py uploads` before calibration or generation. If Feishu cannot be updated, retain local artifacts and manifest state for recovery and leave later records untouched.
