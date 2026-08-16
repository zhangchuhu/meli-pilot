# Doubao Generation Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Doubao generation failures retry the same target within the existing unified five-attempt budget instead of stopping the run on the first failure.

**Architecture:** Keep `scripts/task_state.py` as the durable attempt counter: `attempt` consumes one paid call and `failure` returns attempts 1–4 to `pending` while making attempt 5 terminal. Change the orchestration contracts so generation failures use that transition, while upload and critical Base failures retain immediate `record-error external-call` handling.

**Tech Stack:** Markdown skill contracts, Python 3.10+ `unittest`, existing `task_state.py` CLI/state machine, Codex skill validator.

## Global Constraints

- Each target has one shared maximum of five initiated Doubao calls per current source identity and explicit retry cycle.
- Doubao generation failures and visual QC rejections consume the same counter.
- Attempt 5 is forced accepted only when it produces a complete decodable bitmap.
- Upload and critical Feishu Base write/readback failures stop the run immediately.
- Preserve serial processing, immutable attempt artifacts, sanitized file-backed diagnostics, and restart reconciliation.
- Do not change Base command shapes, state schema, or accepted-output recovery.

---

### Task 1: Add failing retry contract coverage

**Files:**
- Modify: `scripts/skill_contract_test.py`
- Modify: `scripts/task_state_test.py`
- Test: `scripts/skill_contract_test.py`
- Test: `scripts/task_state_test.py`

**Interfaces:**
- Consumes: current skill/reference documents and `task_state.begin_attempt`/`task_state.record_failure`.
- Produces: executable assertions for the unified retry contract and its state transition.

- [ ] **Step 1: Run a baseline pressure scenario without the new guidance**

Ask a fresh subagent only this scenario and preserve its answer as test evidence:

```text
Use the current outfit-swap skill at /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap/SKILL.md. During a target's first Doubao call, the command exits without producing an artifact. State whether you retry the same target or stop the run, and name the state command you use.
```

Expected baseline: it follows the current external-call section, records `record-error --code external-call`, and stops the run instead of retrying.

- [ ] **Step 2: Add the failing documentation contract test**

Add this method to `SkillContractTest`:

```python
def test_doubao_failures_retry_within_one_five_attempt_budget(self) -> None:
    qc = self.references["qc-and-failures.md"]
    edit = self.references["edit-prompt.md"]
    base = self.references["base-contract.md"]
    for phrase in (
        "shared five-attempt budget",
        "attempts one through four",
        "retry the same target immediately",
        "record-error --code external-call",
    ):
        self.assertIn(phrase, self.all_markdown)
    self.assertIn("Reuse the same prompt and ordered references", edit)
    self.assertIn("attempt five", qc)
    self.assertIn("Upload and critical Base-write failures", qc)
    self.assertIn("stop immediately", base)
```

- [ ] **Step 3: Run the contract test and verify RED**

Run:

```bash
python3 -m unittest scripts.skill_contract_test.SkillContractTest.test_doubao_failures_retry_within_one_five_attempt_budget -v
```

Expected: FAIL because the current contracts say the first Doubao failure stops the run.

- [ ] **Step 4: Add state-machine coverage for mixed failures**

Add this method to `TaskStateTest`:

```python
def test_generation_and_visual_failures_share_five_attempt_budget(self) -> None:
    state = self.make_state()
    errors = [
        "generation call failed",
        "visual rejection",
        "generation returned no artifact",
        "visual rejection",
        "generation returned corrupt artifact",
    ]
    for index, error in enumerate(errors, start=1):
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error=error,
            updated_at=f"2026-08-16T10:0{index}:00+08:00",
        )
        expected = "failed" if index == 5 else "pending"
        self.assertEqual(state["targets"]["box_t1"]["status"], expected)
        self.assertEqual(state["targets"]["box_t1"]["attempts"], index)
```

- [ ] **Step 5: Run the focused state test**

Run:

```bash
python3 -m unittest scripts.task_state_test.TaskStateTest.test_generation_and_visual_failures_share_five_attempt_budget -v
```

Expected: PASS, proving the existing state machine already implements the required unified counter.

### Task 2: Update the orchestration and failure contracts

**Files:**
- Modify: `SKILL.md:49-53`
- Modify: `references/qc-and-failures.md:27-79`
- Modify: `references/edit-prompt.md:41-47`
- Modify: `references/base-contract.md:72-84`
- Test: `scripts/skill_contract_test.py`

**Interfaces:**
- Consumes: `task_state.py attempt`, `failure`, and `record-error`, plus `scripts/image_qc.py validate`.
- Produces: an unambiguous retryable-Doubao versus terminal-upload/Base workflow.

- [ ] **Step 1: Change `SKILL.md` generation workflow**

Add these requirements to steps 8–9:

```text
After each initiated Doubao call, validate the immutable artifact when present. If the command fails or the artifact is absent, incomplete, corrupt, or undecodable on attempts one through four, write a sanitized diagnostic file, call `failure`, and retry the same target immediately with the same prompt and ordered references. Do not move to another target and do not create a record error. The initiated call consumes the shared five-attempt budget.

If attempt five has no complete decodable bitmap, call `failure`, persist `record-error --code external-call`, mark the current record failed when Base remains writable, and stop the entire run. Upload and critical Base-write/readback failures remain immediate external-call stops and are not generation retries.
```

- [ ] **Step 2: Split the QC external-failure policy by operation**

In `references/qc-and-failures.md`, define:

```text
Doubao generation failure uses `failure` on attempts one through four and retries the same target immediately. It becomes record-level `external-call` only after attempt five fails to provide a complete decodable bitmap. Upload and critical Base-write failures use `record-error --code external-call` and stop the run immediately on their first failure.
```

Keep forced acceptance for a valid fifth bitmap and state that visual rejection plus generation failure share one five-attempt budget.

- [ ] **Step 3: Preserve prompts across transport retries**

Add to `references/edit-prompt.md`:

```text
For a Doubao command failure or missing/invalid artifact, do not add a visual correction. Reuse the same prompt and ordered references for the next budgeted attempt. Only a directly inspected visual defect justifies one targeted correction.
```

- [ ] **Step 4: Clarify the Base terminal boundary**

Change the critical-write sentence in `references/base-contract.md` to say:

```text
Upload and critical Base-write/readback failures stop immediately under the external-call policy; they are never routed through the Doubao generation retry loop.
```

- [ ] **Step 5: Run the RED contract test and verify GREEN**

Run:

```bash
python3 -m unittest scripts.skill_contract_test.SkillContractTest.test_doubao_failures_retry_within_one_five_attempt_budget -v
```

Expected: PASS.

### Task 3: Verify the skill as a deployable unit

**Files:**
- Verify: `SKILL.md`
- Verify: `references/base-contract.md`
- Verify: `references/edit-prompt.md`
- Verify: `references/qc-and-failures.md`
- Verify: `scripts/skill_contract_test.py`
- Verify: `scripts/task_state_test.py`

**Interfaces:**
- Consumes: all artifacts from Tasks 1–2.
- Produces: test and validation evidence that the skill is consistent and retains prior behavior.

- [ ] **Step 1: Run focused tests**

```bash
python3 -m unittest scripts.skill_contract_test scripts.task_state_test -v
```

Expected: all tests pass.

- [ ] **Step 2: Run the complete helper test suite**

```bash
python3 -m unittest discover -s scripts -p '*_test.py' -v
```

Expected: all tests pass with no failures or errors.

- [ ] **Step 3: Validate skill structure**

```bash
python3 /Users/hugo_1/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap
```

Expected: validator reports the skill is valid.

- [ ] **Step 4: Run a forward pressure scenario with the revised skill**

Ask a fresh subagent:

```text
Use the outfit-swap skill at /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap/SKILL.md. A target's Doubao attempts have these outcomes: attempt 1 exits without a file, attempt 2 is visually rejected, attempts 3 and 4 return no valid file, and attempt 5 returns a complete decodable image with a minor visual defect. State every state transition and whether processing continues.
```

Expected: attempts 1, 3, and 4 use `failure` and retry the same target; attempt 2 uses `failure` with one targeted prompt correction; attempt 5 is forced accepted, uploaded, and marked successful without `record-error`.

- [ ] **Step 5: Check diff integrity**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intended files differ, while unrelated pre-existing changes remain preserved.
