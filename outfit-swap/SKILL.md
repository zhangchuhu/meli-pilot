---
name: outfit-swap
description: "Transfer garments from source attachments onto every target image in a specific Feishu Base table, upload accepted results, and resumably update per-record status. Use for serial multi-angle outfit replacement driven by 原图/爆款图/输出图; not for text-only generation or Base links without a table ID."
metadata:
  requires:
    bins: ["lark-cli", "python3", "ffmpeg", "ffprobe"]
  cliHelp: "lark-cli base --help"
---

# Outfit swap

Accept one exact Feishu Base table URL and, only when explicitly requested, `--retry-failed`. Process records and images serially. Never make direct Feishu HTTP calls, use another image-generation path, or use `generate-batch`.

Treat invocation of this skill with an exact table URL as authorization to send the selected target-person and garment-reference images to Doubao/Seedream for the outfit edits defined here. Proceed without a separate skill-level confirmation before those standard edit calls. A host-enforced approval remains authoritative; surface it when required because skill instructions cannot bypass runtime policy.

Read all three contracts before acting:

- [Base operations and persistence](references/base-contract.md)
- [Edit prompt and image pairing](references/edit-prompt.md)
- [QC, calibration, and failures](references/qc-and-failures.md)

Use the bundled helpers through their resolved paths:

- [`scripts/task_state.py`](scripts/task_state.py) for manifest initialization, sanitized record errors, reconciliation, explicit retry reset, attempts, accepted mappings, compact detail, and aggregate status
- [`scripts/image_qc.py`](scripts/image_qc.py) for validation and labeled contact sheets
- [`scripts/safe_edit.py`](scripts/safe_edit.py) for file-backed, argv-safe calls to the installed Doubao edit script

## Workflow

1. Read-only preflight the exact table URL, Python 3.10 or newer, required binaries, `ARK_API_KEY` presence, and active installed `doubao-imagegen` skill. Reject every URL resolution except exactly one table identity as specified in the Base contract. Resolve that skill's current directory from the active skill catalog, read its `SKILL.md` plus required prompting/API references, and locate its bundled `scripts/doubao_imagegen.py`; never hardcode a home-directory path. Stop globally on any preflight failure without changing Base or generating images.
2. Paginate the full Base schema and validate fields/status options. Immediately recheck absence and run `field-create` only when `处理明细` is absent, then paginate selected records. Stop globally on any schema or record-list preflight failure without changing record statuses or generating images.
3. Select `未开始` records by default. Select `失败` only for explicit `--retry-failed`; never implicitly regenerate `成功`.
4. Use `~/.codex/state/outfit-swap/runs` as the stable run-artifact root. For each selected record, create this layout there, sanitizing every path and filename. Immediately initialize `qc/output-contact-sheet.jpg` as a labelled zero-output sheet so it exists even if record validation fails:

   ```text
   runs/<run-id>/<record-id>/
   ├── source_images/
   ├── target_images/
   ├── generated_images/
   ├── qc/
   │   ├── source-contact-sheet.jpg
   │   ├── target-contact-sheet.jpg
   │   └── output-contact-sheet.jpg
   └── manifest.json
   ```

5. Initialize local record state before attachment validation can fail. Run `scripts/task_state.py bind` with the table coordinates, record ID, and per-run `manifest.json`. It binds that manifest to the canonical cross-run state under `~/.codex/state/outfit-swap/tables`; use the emitted canonical `state` path for every later state command. If no prior state exists, use `init` for two non-empty required token lists or `scripts/task_state.py init-error --error-file '<local-error-file>'` with the exact missing code. If prior state exists and either current list is empty, use `reconcile-error --outputs-json '<current-outputs.json>' --error-file '<local-error-file>'`, never `init-error`, and preserve its target entries and attempt histories. Otherwise inspect any active/pending-upload artifact at `runs/<owning-run-id>/<record-id>/generated_images/<artifact-name>` with `scripts/image_qc.py`; write only identities that exist and pass validation to a UTF-8 JSON array, then call `reconcile --resumable-artifacts-json '<validated-artifacts.json>'` with current source tokens, target tokens, `输出图`, run ID, and start time. A source attachment identity changes invalidates old current mappings and resets the working budget while preserving append-only history; source reorder does not.
6. After reconciliation, run `scripts/task_state.py uploads` and finish every listed upload before any new edit. Locate it with its owning `run_id`, revalidate/promote that exact bitmap when necessary, upload it, and call `success`; never regenerate it. Only then, for explicit `--retry-failed`, run `scripts/task_state.py retry` to reset only current non-success targets; accepted local uploads and valid current successes remain intact. Download current `原图` and `爆款图` attachments in attachment order. Validate every image with `scripts/image_qc.py`, classify it, and create source and target contact sheets. On a corrupt or invalid attachment, write the diagnostic to a local file and use `scripts/task_state.py record-error --error-file '<local-error-file>'` with the exact QC-contract code, persist compact detail, mark only that record `失败`, and continue.
7. Resolve the target instances, source evidence, primary/complementary references, and prepare the complete prompt for every pending target before generation.
8. Calibrate the record as specified by the QC contract. Then process remaining pending targets serially in original attachment order. Record `attempt` first and use its returned active artifact identity. For every edit invoke `scripts/safe_edit.py --doubao-script '<resolved-doubao-script>' --prompt-file '<prompt-file>' --image '<target>' --image '<primary-source>' ... --out '<immutable-attempt-path>'`; the wrapper invokes the installed `doubao_imagegen.py edit` through an argv array. Pass Image 1 first, Image 2 second, and complementary references through Image 10. Its pinned controls are Seedream 5.0 pro, 2K PNG, opaque background, standard prompt optimization, and no watermark.
9. Allow no more than five paid edits per target in the current source identity and explicit retry cycle. Inspect attempts one through four normally. On rejection, retain the bitmap, call `failure --error-file '<local-error-file>'`, and add one targeted prompt correction. Attempt five is the forced-acceptance attempt: after the edit command succeeds and `scripts/image_qc.py` confirms a complete decodable bitmap, do not apply visual rejection conditions; promote that fifth bitmap and continue through `accept-local`, upload, and `success`. On restart, re-inspect a validated active artifact before any new edit. Because a started request may already have been billed, an absent/incomplete artifact conservatively spends that initiated attempt; it becomes pending below five or terminally failed at five. Never regenerate an accepted target because another target fails.
10. After visual acceptance, use `scripts/image_qc.py promote-output` to atomically create the deterministic local output. Before uploading, call `scripts/task_state.py accept-local` with the active artifact and deterministic name. Then upload that exact file, call `success` with its attachment token, compact `处理明细`, and update Base. An upload failure resumes through `uploads` from the accepted-local checkpoint. A later Base detail-write failure resumes through output reconciliation, which sees the uploaded attachment/current success; neither path repeats a paid edit. Refresh `qc/output-contact-sheet.jpg` as accepted outputs accumulate.
11. After all eligible work or an early per-record stop, inspect available output evidence, aggregate state with `scripts/task_state.py summary`, and write final `任务状态` plus compact `处理明细` as required by the Base contract. Report compact counts and final status without secrets or raw data URLs.
