# Outfit Swap External-Call Retry Design

## Goal

Allow a target to continue after a Doubao/Seedream generation failure while preserving the existing conservative paid-edit accounting. Each target has one unified budget of five initiated Doubao calls per current source identity and explicit retry cycle.

## Scope

This change applies only to failures of the Doubao edit command or to a returned artifact that is absent, incomplete, corrupt, or undecodable. Attachment upload failures and critical Feishu Base write/readback failures remain immediate run-stopping `external-call` failures because retrying those operations can create duplicate attachments or divergent durable state.

## Retry and state rules

1. Call `scripts/task_state.py attempt` before every Doubao request. The initiated request consumes one attempt even if no usable artifact is returned.
2. When Doubao fails on attempts 1–4, retain any partial artifact for evidence, write a sanitized diagnostic file, and call `scripts/task_state.py failure`. The target returns to `pending`; immediately start its next attempt before moving to another target.
3. Do not add a visual prompt correction when no complete image exists. Reuse the latest resolved prompt and ordered reference set for the retry.
4. Visual QC rejection and Doubao failure share the same five-attempt counter. A mix of both failure types can therefore consume the budget.
5. If attempt 5 produces a complete decodable bitmap, apply the existing forced-acceptance rule: promote it, persist local acceptance, upload it, and mark it successful without visual rejection.
6. If attempt 5 does not produce a complete decodable bitmap, persist `record-error --code external-call`, mark the current record `失败` when Base remains writable, and stop the entire run.
7. Restart recovery remains conservative: revalidate an existing active artifact first. An absent or invalid active artifact consumes the already-recorded attempt and follows the same attempt-1–4 retry or attempt-5 terminal rule without repeating that initiated call.

## Contract changes

- `SKILL.md` workflow steps 8–9 will state the Doubao retry loop and distinguish it from upload/Base failures.
- `references/qc-and-failures.md` will split generation failures from other external-call failures and define the unified five-attempt transition table.
- `references/base-contract.md` will continue to require immediate stop for critical Base write/readback failure; no Base command shape changes.
- `scripts/task_state.py` already supports `attempt` followed by `failure`, returning a target to `pending` below five and leaving it failed at five. No new state field or command is required unless the RED test proves a missing transition.

## Testing

Add contract tests before editing the skill documents. The tests must initially fail and then prove:

- Doubao failures on attempts 1–4 retry the same target instead of creating `record-error` or stopping the run.
- Every initiated Doubao call consumes the shared five-attempt budget.
- Attempt 5 with no valid bitmap becomes terminal `external-call` and stops the run.
- Attempt 5 with a valid bitmap remains forced accepted.
- Upload and critical Base failures still stop immediately.
- Restart handling does not repeat an already initiated call whose artifact is missing or invalid.

Run the focused contract/state tests, the complete unittest suite, and the skill validator after implementation.
