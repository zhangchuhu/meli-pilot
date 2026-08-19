# Automatic QC and failure handling

Use this reference for artifact validation, Ark multimodal QC, retry decisions, third-attempt selection, shadow rollback, and failure classification. Automatic QC owns normal visual decisions; do not add a manual per-image approval or agent-inspection gate.

## Classification and deterministic evidence

Classify every useful source and target as exactly one of:

- front
- front three-quarter
- side
- back three-quarter
- back
- detail or flat lay
- infographic

Treat size charts as infographic evidence, never angle evidence. Process detail, flat-lay, and infographic targets like every other target.

Use full `scripts/image_qc.py validate` constraints for every downloaded source and target before any paid call. Derive canonical codec/MIME from decoded bytes, not the remote filename; a misleading suffix remains valid when its raster bytes are supported. Record tiny, oversized, extreme-aspect, or otherwise invalid inputs as precise record-local `invalid-source`/`invalid-target` failures. Generated candidates and finalization use complete decode-only validation and never compare their dimensions/aspect with the target. For every initialized record, run `scripts/image_qc.py empty-contact-sheet` before any output is accepted and before record validation can fail. Build source/target contact sheets after classification and refresh the output contact sheet after acceptance; never upload contact sheets to `输出图`.

Never compare a generated artifact's pixel dimensions or aspect ratio with its target `爆款图`. Never reject, retry, fail, or rank candidates by a target/output dimension or aspect-ratio difference. Continue complete/decodable artifact validation and full visual QC for garment fidelity and target-preservation invariants.

## Ark multimodal QC

Use `scripts/ark_vision_qc.py` with `ARK_API_KEY` and the configured `ARK_VISION_MODEL`. Its only direct HTTP target is `https://ark.cn-beijing.volces.com/api/v3/chat/completions`. Send QC images in exact target, candidate, ordered-reference order. Ordinary diagnostics may log complete prompts and request bodies, including raw Base64 and raw data URLs. First pass structured headers and payloads through `scripts/payload_security.py`; never log credentials, API keys, tokens, cookies, signatures, or authorization headers or values.

Archive every received approved-endpoint Ark HTTP response before JSON or schema parsing. Store the exact bounded response bytes in the current run's private `ark-responses/<sequence>.body` and a `0600` sidecar with HTTP status, byte count, timestamp, approved endpoint, and credential-redacted response headers. Keep the directory at `0700`, assign sequence numbers thread-safely, hold a no-follow directory capability while writing, and use atomic file replacement. Archive HTTP error bodies when present. A timeout or connection failure has no response body, so retain only the existing sanitized transport classification. If a response body echoes the exact request credential, refuse to persist it and stop with a sanitized archive error; redact exact request credentials even when an unrelated response-header name carries them. Do not copy response archives into `events.ndjson`, Base `处理明细`, metrics, or ordinary terminal output. Live `qc_replay.py --live-ark` requests use a separate private archive below the configured runs root.

Format each ChatCompletions request as one `user` multimodal message with all image items before the final text item. Combine the locally controlled instructions and task text in that final item. For every structured response that carries a schema version, require the unquoted JSON integer `1`, show an exact typed JSON example in the task, and retain strict local type validation.

Every structured Ark request explicitly disables deep thinking. The request timeout defaults to 120 seconds and may be overridden only with a positive finite `OUTFIT_SWAP_ARK_TIMEOUT_SECONDS` value. Validate an invalid timeout before creating run/state directories.

Route every Ark classification, source-evidence, infographic-inventory, adjudication, and QC request through the same stop-aware bounded gate. The default shared Ark concurrency is two even when record concurrency is higher. Recheck the global stop and durable record ownership after acquiring the gate and before transport. Any Ark transport exception, including authentication/model rejection, sets the global stop before another queued request or paid call can begin.

During target planning, distinguish Ark transport or timeout failures from returned planning-data defects. A transport failure returns a recoverable `stopped` result, preserves the target as `pending` with zero new generation attempts, and leaves `record_error` unset; it must never be rewritten as `record-data`. A later explicit `--retry-failed` invocation resumes planning. A completed Ark response that fails the strict classification, evidence, inventory, or reference-selection contract remains a record-local `record-data` failure.

Before strict source-evidence validation, apply only the finite alias map `front-close`, `front close-up`, and `close-up front` to the existing `detail or flat lay` angle. Preserve source `roles` arrays unchanged. Any other unknown angle remains invalid. Do not use a garment-fact enumeration whitelist: accept new fact categories and values when each is one lowercase ASCII `category:value` code, each side contains only letters, digits, underscore, or hyphen, the category is at most 48 characters, the value is at most 80 characters, and there are at most 64 unique codes across `required` and `forbidden`. Reject category-only role names, spaces, prose, instructions, control characters, punctuation, duplicate codes, and malformed or oversized values. Render known facts through the existing local templates and unknown safe facts through a generic local evidence template; never relay raw Ark-authored prose. No second Ark repair request is allowed.

Validate that the key and model are nonempty before directory creation. Ark offers no separate no-cost authentication boundary here, so actual credentials can only be authenticated by the first invocation-authorized image-bearing request; never add a speculative paid probe. Treat an authentication/model failure at that first request as systemic, not as one failure per record.

Require one strict schema-version-1 JSON report with the exact candidate identity, five named scores, enumerated critical defects, optional primary defect, evidence array of non-empty strings, confidence in the decimal range 0 through 1, decision, and all six exactness fields: `exact_text`, `added_text`, `missing_text`, `instances_exact`, `panel_count_exact`, and `panel_layout_exact`. The prompt must include a complete typed ordinary-report example. Infographics require explicit booleans and exact literal arrays; ordinary reports require nulls. Define `target_preservation` only as preservation of the target person's identity/body/pose/crop/composition/hands/feet/shoes/carried objects/background/lighting/shadows; the target's original clothing is intentionally replaced and its absence must never reduce that score. Before strict validation, normalize only two observed bounded deviations: convert a non-empty evidence string to a one-element array and convert a finite numeric confidence greater than 1 and at most 100 to the corresponding decimal. Apply the same compatibility step to each comparative candidate. Never normalize candidate identity, field names, enum values, empty evidence, mixed evidence arrays, or any other value; use only the local diagnostic codes `candidate_mismatch`, `evidence_wrong_type`, `confidence_wrong_scale`, and `schema_invalid`, and do not make an extra Ark repair request. Never default an absent exactness gate to true. Reject Markdown wrappers, trailing prose, duplicate/missing/unknown fields, wrong candidates, unknown defect codes, non-finite/out-of-range values, truncation, and content-filter responses.

Retry an invalid or low-confidence response on the same candidate; this does not consume a Seedream generation attempt. If two valid same-candidate decisions disagree, adjudicate once on that same candidate. Persist only the validated structured report and sanitized metadata. If Ark remains unavailable, preserve the active candidate and return a recoverable QC stop; do not generate a replacement.

## Early acceptance and correction

Attempts one and two may pass early. Accept only when all applicable gates hold:

```text
garment_construction >= 90
color_material >= 88
garment_details >= 88
target_preservation >= 90
confidence >= 0.85
critical_defects is empty
```

For an infographic also require:

```text
text_layout >= 95
literal text is exact with nothing added or missing
all planned garment instances are present
panel count and material layout are preserved
```

When attempt one or two clears every gate, stop immediately after an early full-QC pass and finalize that artifact. Otherwise retain the candidate, append the QC report, record `failure --error-file`, and add exactly one controlled correction for the highest-priority defect. The correction priority is construction; original clothing/missing instances; text/layout; identity/pose/accessory/background; anatomy/occlusion; color/material; secondary garment details.

For a Seedream command failure or absent/incomplete/corrupt/undecodable artifact on attempt one or two, use `failure --error-file`, retry the same target, and apply no visual prompt correction.

Each current source-identity and explicit retry cycle has one initial call plus at most two retries. No fourth paid generation exists.

## Third-attempt garment-best selection

Use third-attempt garment-first selection. After the third initiated generation call, send the target, the verified nonempty subset of complete current-cycle candidates, and ordered references in one shared-gated Ark comparative request, including a candidate previously visually rejected on attempt one or two. Missing or corrupt earlier artifacts may be omitted; aliases retain actual attempt numbers such as `candidate_2` and `candidate_3`. Require each supplied path and digest to match its durable attempt and require the response alias set exactly. Persist the validated comparative reports, claimed ranking, and locally verified ranking as the durable `selection_reason` before finalization; reuse that checkpoint after a crash without another Ark call. Reject a missing/extra alias or any claimed order that conflicts with local garment-first ranking. Garment fidelity outranks the earlier visual-rejection rationale. Compare every complete decodable candidate lexicographically in this order:

1. garment construction and silhouette;
2. color and material appearance;
3. closures, seams, trim, decoration, logos, and other garment details;
4. target preservation, including identity, pose, crop, composition, accessories, background, lighting, text, and layout;
5. earlier attempt when all prior criteria tie.

Do not use a weighted sum. The selected artifact may come from attempt one or two. Persist its `selection_reason`, promote it, and pass its artifact name to `scripts/task_state.py accept-local`.

If attempt three is invalid but an earlier candidate is valid, select the best earlier candidate. If no complete decodable current-cycle candidate exists, call `record-error --code external-call --error-file '<local-error-file>'` while the final checkpoint remains active.

On restart from an exhausted checkpoint, preserve a non-callable final-selection checkpoint. Revalidate all current-cycle artifacts, then choose an existing revalidated current-cycle candidate with `accept-local`; otherwise record terminal `record-error --code external-call` without another `attempt`. Never call `failure`, `retry`, or `attempt` from that exhausted checkpoint.

A comparative transport or strict-schema failure is recoverable: set the shared stop, preserve the exhausted running checkpoint and artifacts, return stopped, and issue no fourth generation. A later invocation compares the same surviving artifacts again.

## Shadow rollback

Use `--qc-mode shadow` as the automatic-decision rollback control. Shadow mode still calls Ark, validates/persists the report, and records the Ark observation and scores. It does not let that observation reject or retry the candidate; it deterministically finalizes the current complete decodable candidate. It is not manual QC. Return to `--qc-mode automatic` only after the shadow gates pass.

Use `scripts/qc_replay.py` to validate shadow gates read-only by default. Require zero missed critical construction defects, zero missed infographic text/layout changes, false retry at or below the configured limit, and valid same-candidate response/adjudication paths. Live Ark replay requires explicit `--live-ark`.

## Failure classes

### Global preflight failure

Treat an invalid/ambiguous table URL, missing dependency/key/model ID, schema mismatch, or invalid exact-view record inventory as global preflight failure. Mutate no record and incur no image cost. Treat the first real Ark authentication/model rejection as a systemic runtime stop because credentials cannot be authenticated without an authorized image-bearing request.

### Record data failure

Treat missing required attachments, corrupt inputs, invalid input constraints, or target-planning evidence gaps as record-local data failures. Persist the `record-error` state, compact and summarize it, and perform the terminal Base write when Base is writable. This terminal Base write is required even when skipped targets remain pending. Continue other already-eligible records unless a systemic failure set the global stop.

Use only this exact mapping:

| Condition | Command | `--code` |
| --- | --- | --- |
| current `原图` token list empty | `init-error` for new state, otherwise `reconcile-error` | `missing-source` |
| current `爆款图` token list empty | `init-error` for new state, otherwise `reconcile-error` | `missing-target` |
| both required token lists empty | same new/existing rule; message states both are empty | `missing-source` |
| downloaded `原图` is corrupt | `record-error` | `corrupt-source` |
| downloaded `爆款图` is corrupt | `record-error` | `corrupt-target` |
| decodable `原图` violates input constraints | `record-error` | `invalid-source` |
| decodable `爆款图` violates input constraints | `record-error` | `invalid-target` |
| other attributable record data/planning fault | `record-error` | `record-data` |
| attempt three has no complete decodable current-cycle candidate | `record-error` | `external-call` |
| attachment upload or critical Base-write/readback failure | `record-error` | `external-call` |

These are the only record-error codes. A Doubao failure on attempt one or two never uses `record-error`; use `failure --error-file`, retry the same target, and add no visual prompt correction.

### Third-attempt garment-best selection

Use the selection contract above. A target with at least one valid candidate finalizes one of those candidates; a target with none takes terminal `external-call`.

### External call failure

Treat persistent Ark failure as a recoverable same-candidate QC stop: preserve state and start no new generation. Treat upload and critical Base-write/readback failures as immediate global stops, not generation retries. Mark the current record `external-call` when Base remains writable; otherwise preserve local state for re-entry and leave later records untouched.
