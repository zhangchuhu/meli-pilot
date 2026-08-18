# Outfit Swap Final Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the final five production blockers and three scoped durability/validation minors without weakening the existing safety or migration contracts.

**Architecture:** Extend the persisted target plan and strict QC schemas compatibly, keep all model output behind typed validators and local decision policy, and instrument actual transport boundaries. Preserve schema-v3 state by migrating older serialized target plans at deserialization and by archiving prior selection reasons rather than deleting them.

**Tech Stack:** Python 3.10+, `unittest`, dataclasses, JSON state, FFprobe-backed image validation, existing Ark/Lark/Seedream adapters.

**Spec:** `.superpowers/sdd/2026-08-18-outfit-swap-automation-performance/task-11-brief.md` plus final whole-branch review findings received 2026-08-19.

## Global Constraints

- Keep direct Feishu HTTP forbidden and the Ark endpoint exact.
- Keep targets serial, record concurrency configurable, and generation capped at three attempts.
- Keep generated-candidate/finalizer validation decode-only; apply full constraints only to downloaded inputs.
- Persist no secrets, prompts, paths, Base64, or free-form evidence in events.
- Preserve schema-v3 state and accept older target-plan payloads through explicit migration.

---

### Task 1: Ordered garment instances and full local prompt

**Files:**
- Modify: `scripts/prompt_builder.py`
- Modify: `scripts/production_runtime.py`
- Test: `scripts/prompt_builder_test.py`, `scripts/production_runtime_test.py`

**Interfaces:**
- Produces: `TargetPlan.garment_instances: tuple[str, ...]`, migrated serialization, and the full local replacement/preservation prompt.

- [ ] Write failing ordinary multi-garment plan round-trip and prompt tests with literal expected numbered instances and preservation clauses.
- [ ] Run the focused tests and confirm missing field/template failures.
- [ ] Add bounded ordered instances to `TargetPlan`, migrate schema-2 plans, collect ordinary instances from strict Ark evidence, and render the local prompt.
- [ ] Run focused tests GREEN.

### Task 2: Strict infographic QC schema and gates

**Files:**
- Modify: `scripts/vision_qc.py`
- Modify: `scripts/ark_vision_qc.py`
- Modify: `scripts/production_runtime.py`
- Modify: `scripts/run_record.py`
- Test: `scripts/vision_qc_test.py`, `scripts/ark_vision_qc_test.py`, `scripts/production_runtime_test.py`

**Interfaces:**
- Produces: explicit infographic text/instance/panel gate fields in `QCReport`; `early_accept` consumes only validated report gates.

- [ ] Add failing parser/prompt tests for complete schema instructions and one missing/added literal or panel mismatch.
- [ ] Confirm RED due absent fields/default-true behavior.
- [ ] Extend the strict schema, require infographic gates and ordinary null policy, include settled inventory in the production request, and reject any failed gate locally.
- [ ] Run focused tests GREEN.

### Task 3: One comparative third-attempt Ark request

**Files:**
- Modify: `scripts/run_record.py`
- Modify: `scripts/production_runtime.py`
- Modify: `scripts/task_state.py`
- Test: `scripts/run_record_test.py`, `scripts/production_runtime_test.py`, `scripts/task_state_test.py`

**Interfaces:**
- Produces: a strict opaque-alias comparison report persisted in state; one `compare_candidates` QC boundary; local garment-first order verification.

- [ ] Add failing tests for one target/all candidates/ordered refs call, exact alias set, inconsistent rank rejection, persisted replay, and no fourth generation.
- [ ] Confirm RED because selection currently reuses independent reports only.
- [ ] Implement the shared-gated comparative request, strict parser, durable report, local lexicographic verification, and idempotent replay.
- [ ] Run focused tests GREEN.

### Task 4: Full input validation

**Files:**
- Modify: `scripts/production_runtime.py`
- Test: `scripts/production_runtime_test.py`

**Interfaces:**
- Consumes: `image_qc.validate_image` for downloaded source/target only.

- [ ] Add failing tests for misleading extensions with valid bytes and tiny/oversized/extreme inputs producing record-local precise codes before paid work.
- [ ] Confirm RED because downloads currently call decode-only validation.
- [ ] Switch post-canonicalization input validation to `validate_image` and preserve code mapping.
- [ ] Run focused tests GREEN.

### Task 5: Actual Ark metrics and non-overlapping totals

**Files:**
- Modify: `scripts/event_log.py`
- Modify: `scripts/run_record.py`
- Modify: `scripts/production_runtime.py`
- Test: `scripts/event_log_test.py`, `scripts/production_runtime_test.py`

**Interfaces:**
- Produces: actual Ark request count fields, comparative request count, non-overlapping service totals, and valid input-byte telemetry above 100 MiB aggregate.

- [ ] Add failing metrics tests using `QCReviewResult.review_count/adjudicated`, comparative calls, nested finalize phases, and a >100 MiB valid aggregate.
- [ ] Confirm RED against phase-count and cap behavior.
- [ ] Emit bounded actual-request counts, sum them, exclude outer finalize from Lark totals, and raise the aggregate telemetry bound safely.
- [ ] Run focused tests GREEN.

### Task 6: Scoped durability minors and final verification

**Files:**
- Modify: `scripts/task_state.py`
- Modify: `scripts/vision_qc.py` and touched validators
- Test: `scripts/task_state_test.py`, `scripts/vision_qc_test.py`
- Modify: `SKILL.md`, named references, Task 11 report/ledger

**Interfaces:**
- Produces: archived selection reasons across retry; true-integer schema versions; surrogate-safe normalization with cleaned temporaries.

- [ ] Add failing retry/archive, `1.0` schema, and lone-surrogate state tests.
- [ ] Implement minimal compatibility-preserving fixes and run focused tests GREEN.
- [ ] Update contracts/report/ledger.
- [ ] Run full unittest discovery, compileall, diff check, and skill quick validation.
- [ ] Commit one coherent final-fix wave.
