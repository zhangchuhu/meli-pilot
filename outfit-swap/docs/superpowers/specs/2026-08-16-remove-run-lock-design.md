# Remove outfit-swap run locking

## Goal

Remove the outfit-swap same-machine run-lock feature completely. The workflow must not start, inspect, reclaim, release, document, or test a table-scoped run lock.

## Workflow design

After resolving and validating one exact Feishu Base table URL, proceed directly to full schema pagination and validation. If `处理明细` is absent, immediately repeat the absence check and create the field once. This second check reduces stale-read risk but does not provide mutual exclusion; concurrent runs may still race.

All record-state reconciliation, pending-upload recovery, serial image processing, attachment validation, generation budgets, and Base persistence behavior remain unchanged.

## Complete removal boundary

- Remove `scripts/run_lock.py` and `scripts/run_lock_test.py`.
- Remove the helper entry and hold/release steps from `SKILL.md`, then renumber the workflow.
- Remove lock-holder, held-lock, lock-root, occupied-lock, release, reclaim, control-channel, and mutation-guard guidance from the Base and QC references.
- Remove `scripts/run_lock.py` from the runtime-helper contract and delete lock-order assertions.
- Add a static contract that rejects any remaining `run_lock`, `run-lock`, `run lock`, or `运行锁` reference in runtime skill sources (`SKILL.md`, `references/`, and retained `scripts/`). Process design documents are outside this runtime contract.

## Concurrency consequence

Two Codex runs may process the same table concurrently after this change. No replacement mutual-exclusion mechanism, feature flag, or compatibility shim will be introduced.

## Verification

First change the skill contract test so it fails against the current lock-bearing skill. Then remove the lock implementation and references until the focused contract test passes. Run the complete Python test suite, validate the skill folder with the Skill Creator validator, and forward-test the updated skill in a read-only scenario to confirm it proceeds from URL validation directly to schema pagination without invoking a lock helper.
