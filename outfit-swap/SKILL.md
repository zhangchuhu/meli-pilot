---
name: outfit-swap
description: "Use when a user supplies one exact Feishu Base table URL or asks to transfer source garments onto every target image in a table with resumable status updates."
---

# Outfit swap

Use `scripts/run_table.py` as the table-level normal entry point. Give it one exact Feishu Base table URL; do not manually reproduce the per-record pipeline.

```bash
python3 scripts/run_table.py '<table-url>'
```

The complete interface is:

```bash
python3 scripts/run_table.py '<table-url>' [--record-concurrency N] [--retry-failed] [--qc-mode automatic|shadow]
```

Use automatic Ark QC by default. `--record-concurrency N` accepts a positive integer and defaults to `2`. Use `--retry-failed` only when the user explicitly requests failed-record retry. Use `--qc-mode shadow` only as the documented rollback control. Never select `成功` records for regeneration.

## Authorization

Treat invocation of this skill with an exact table URL as authorization for this invocation to send the selected target-person and garment-reference images to Doubao/Seedream and the relevant target, candidate, and reference images to Ark multimodal QC. Proceed without a separate skill-level confirmation before those standard transfers. Do not pause for a separate image-transfer authorization prompt. Host approval remains authoritative; surface and obey any host-enforced approval because this skill cannot bypass runtime policy.

## Before running

Require Python 3.10 or newer, `lark-cli`, `ffmpeg`, `ffprobe`, `ARK_API_KEY`, `ARK_VISION_MODEL`, and the active installed `doubao-imagegen` skill. The production entry uses the normal installed skill location; set `OUTFIT_SWAP_DOUBAO_SCRIPT` to the resolved script path only when the active catalog uses another location. `OUTFIT_SWAP_LARK_CLI`, `OUTFIT_SWAP_STATE_ROOT`, and `OUTFIT_SWAP_RUNS_ROOT` are optional non-secret executable/state discovery overrides. Do not use these variables for credentials or endpoint overrides.

Read these contracts before execution:

- [Feishu Base scope and finalization](references/feishu-base.md)
- [State, planning, recovery, and events](references/task-state.md)
- [Automatic QC and failure policy](references/qc-and-failures.md)

Stop before mutation or paid work when the exact URL, dependencies, authentication, schema, or selected-record materialization fails preflight.

## Normal workflow

1. Run the table entry once with the requested flags. Let it complete one global preflight, materialize a stable record queue, and schedule records. Do not launch a second independent invocation for the same table.
2. Let records run concurrently at the configured limit. Keep targets within each record serial in original attachment order. Do not create a persistent run lock or parallelize targets.
3. Let the pipeline reconcile state and Base first, drain accepted local uploads, inspect active artifacts, create immutable target plans, generate with fixed `--size 2K`, obtain automatic Ark decisions, select within the three-attempt budget, and call the idempotent finalizer.
4. Report the table result and sanitized event metrics. Never expose secrets, prompts, raw Base64, raw data URLs, authorization headers, or unsanitized external diagnostics.

Do not substitute direct Feishu HTTP, another image-generation path, `generate-batch`, manual per-image approval, or a sequence of target-level shell commands for the normal entry.

## Recovery and diagnosis

Re-run the same table entry to resume. Recovery always reconciles Base, drains `accepted-local` work, and checks an active candidate before starting another paid generation. An upload failure resumes through `uploads`; a later Base detail-write failure resumes through output reconciliation. Neither repeats a paid edit.

Keep these component CLIs for diagnosis and recovery, not as the normal workflow:

- [`scripts/run_record.py`](scripts/run_record.py) inspects one prepared record checkpoint.
- [`scripts/qc_replay.py`](scripts/qc_replay.py) replays historical QC read-only by default; live Ark requires `--live-ark`.
- [`scripts/task_state.py`](scripts/task_state.py) supports `bind`, `init`, `init-error`, `reconcile`, `reconcile-error`, `retry`, `record-error`, `attempt`, `accept-local`, `success`, `failure`, `pending`, `uploads`, `compact`, and `summary`. `scripts/task_state.py retry` must reset only current non-success targets. Use `--resumable-artifacts-json` during diagnostic reconciliation and locate pending uploads by their owning `run_id` before any new edit.
- [`scripts/image_qc.py`](scripts/image_qc.py) validates artifacts, builds labeled contact sheets, and promotes selected outputs.
- [`scripts/safe_edit.py`](scripts/safe_edit.py) provides argv-safe Seedream transport with `--prompt-file`.

Pass diagnostics through `--error-file`; never put raw diagnostics in argv.

## Runtime map

Use these modules through `run_table.py`; read their source only for diagnosis or extension:

- [`scripts/run_table.py`](scripts/run_table.py): preflight, bounded record scheduling, service limits, and global stop
- [`scripts/production_runtime.py`](scripts/production_runtime.py): concrete Lark/Seedream/Ark materialization and service assembly used by bare `run_table.py`
- [`scripts/run_record.py`](scripts/run_record.py): serial target orchestration and recovery
- [`scripts/reference_selector.py`](scripts/reference_selector.py): deterministic three/four-reference selection and evidenced fifth exception
- [`scripts/prompt_builder.py`](scripts/prompt_builder.py): immutable target plans and controlled corrections
- [`scripts/infographic_text.py`](scripts/infographic_text.py): literal text/panel/instance inventory gate
- [`scripts/safe_edit.py`](scripts/safe_edit.py): installed Seedream edit transport
- [`scripts/image_qc.py`](scripts/image_qc.py): deterministic file validation and promotion
- [`scripts/ark_vision_qc.py`](scripts/ark_vision_qc.py): exact Ark ChatCompletions transport and same-candidate review
- [`scripts/vision_qc.py`](scripts/vision_qc.py): strict reports, thresholds, corrections, and garment-first ranking
- [`scripts/finalize_target.py`](scripts/finalize_target.py): one resumable target transaction
- [`scripts/lark_runner.py`](scripts/lark_runner.py): typed relative-file `lark-cli` transport
- [`scripts/task_state.py`](scripts/task_state.py): canonical schema-versioned state and compatibility migration
- [`scripts/event_log.py`](scripts/event_log.py): sanitized NDJSON events and metrics
- [`scripts/qc_replay.py`](scripts/qc_replay.py): offline/live opt-in shadow validation

Initialize local record state before attachment validation can fail. Validate every image only after the state is bound. Use `scripts/task_state.py bind` for the canonical state under `~/.codex/state/outfit-swap/tables`; use `reconcile-error`, not `init-error`, for an existing record with missing required attachment lists, and preserve its target entries and attempt histories. A source attachment identity changes invalidates current mappings and resets the working budget while preserving append-only history.

Persist selected artifacts with `scripts/task_state.py accept-local` before finalization. A later Base detail-write failure resumes through output reconciliation, and accepted outputs are never regenerated because another target fails.
