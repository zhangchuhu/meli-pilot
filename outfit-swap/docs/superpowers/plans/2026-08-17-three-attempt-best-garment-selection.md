# Three-Attempt Best-Garment Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Limit each target to three initiated Doubao edits, accept an early full-QC pass immediately, and after attempt three accept the valid current-cycle candidate most similar to the garment references.

**Architecture:** Keep schema version 2 and the immutable attempt history. Separate the new-call budget from the largest readable legacy attempt number, then teach `accept-local` and recovery to identify the accepted history entry by artifact identity rather than assuming the latest attempt won. Keep visual comparison in the skill workflow: the state helper validates cycle ownership and persists the chosen artifact but does not invent numeric similarity scores.

**Tech Stack:** Python 3.10+ standard library, `unittest`, Markdown skill contracts, existing `task_state.py` and `image_qc.py`, Codex skill validator.

## Global Constraints

- Each target has exactly three initiated Doubao calls at most per current source identity and explicit retry cycle: one initial call plus at most two retries.
- Attempts one and two stop early when full QC passes; no unused call may be made after acceptance.
- After attempt three, select the complete decodable current-cycle candidate with the highest garment-reference similarity; garment construction and silhouette rank first.
- Missing, partial, corrupt, or undecodable artifacts never participate in selection.
- Generation failures consume the same three-call budget; upload and critical Base write/readback failures stop immediately.
- Preserve serial processing, immutable attempt artifacts, sanitized file-backed diagnostics, restart reconciliation, accepted-output recovery, Base command shapes, and state schema version 2.
- Existing attempt-four/five history remains readable and can never authorize another paid call.
- Do not add automated embeddings, numeric similarity state, parallel generation, batch generation, or output deletion.

---

### Task 1: Enforce the three-call budget without breaking legacy state

**Files:**
- Modify: `scripts/task_state.py:14-18,350-475,592-598,880-925,1011-1030`
- Modify: `scripts/task_state_test.py:160-190`
- Test: `scripts/task_state_test.py`

**Interfaces:**
- Consumes: schema-version-2 target state and immutable `attempt_history` entries.
- Produces: `MAX_ATTEMPTS = 3`, `MAX_RECORDED_ATTEMPT = 5`, and `load_state()` normalization for legacy pending targets.

- [ ] **Step 1: Add failing three-call and legacy-read tests**

Replace the five-attempt terminal test and add legacy coverage with these behaviors:

```python
def test_three_attempts_make_target_terminal_failed(self) -> None:
    state = self.make_state()
    for attempt in range(2):
        self.begin(state)
        task_state.record_failure(
            state, target_token="box_t1", error=f"failure {attempt}",
            updated_at="2026-08-17T10:02:00+08:00",
        )
        self.assertEqual(state["targets"]["box_t1"]["status"], "pending")
    self.begin(state)
    task_state.record_failure(
        state, target_token="box_t1", error="failure 2",
        updated_at="2026-08-17T10:03:00+08:00",
    )
    self.assertEqual(state["targets"]["box_t1"]["attempts"], 3)
    self.assertEqual(state["targets"]["box_t1"]["status"], "failed")
    with self.assertRaisesRegex(task_state.TaskStateError, "exhausted"):
        self.begin(state)

def test_load_preserves_legacy_success_with_five_attempts(self) -> None:
    state = self.make_state()
    target = state["targets"]["box_t1"]
    target["attempts"] = 5
    target["status"] = "success"
    target["output"] = {
        "file_token": "box_out", "name": task_state.output_name(1, "box_t1"),
    }
    target["attempt_history"] = self.legacy_finished_history(5, outcome="failed")
    target["attempt_history"][-1]["outcome"] = "success"
    target["attempt_history"][-1]["error"] = None
    target["attempt_history"][-1]["output"] = target["output"]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        loaded = task_state.load_state(path)
    self.assertEqual(loaded["targets"]["box_t1"]["status"], "success")
    self.assertEqual(loaded["targets"]["box_t1"]["attempts"], 5)

def test_load_terminalizes_legacy_pending_without_new_call(self) -> None:
    state = self.make_state()
    target = state["targets"]["box_t1"]
    target["attempts"] = 4
    target["status"] = "pending"
    target["attempt_history"] = self.legacy_finished_history(4, outcome="failed")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        loaded = task_state.load_state(path)
    self.assertEqual(loaded["targets"]["box_t1"]["status"], "failed")
    with self.assertRaisesRegex(task_state.TaskStateError, "not pending"):
        self.begin(loaded)
```

Add this test-only helper; do not add a production fixture builder:

```python
def legacy_finished_history(self, count: int, outcome: str) -> list[dict]:
    entries = []
    for number in range(1, count + 1):
        prompt = f"legacy prompt {number}"
        entries.append({
            "attempt": number,
            "artifact_ordinal": number,
            "artifact_name": task_state.attempt_output_name(1, "box_t1", number),
            "run_id": "run_1",
            "classification": "front",
            "reference_tokens": ["box_s1"],
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "model": "image-model",
            "started_at": f"2026-08-16T10:0{number}:00+08:00",
            "finished_at": f"2026-08-16T10:0{number}:30+08:00",
            "outcome": outcome,
            "error": "legacy rejection" if outcome == "failed" else None,
            "output": None,
        })
    return entries
```

Add `import hashlib` to the test module.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest \
  scripts.task_state_test.TaskStateTest.test_three_attempts_make_target_terminal_failed \
  scripts.task_state_test.TaskStateTest.test_load_preserves_legacy_success_with_five_attempts \
  scripts.task_state_test.TaskStateTest.test_load_terminalizes_legacy_pending_without_new_call -v
```

Expected: the three-attempt test reports `pending` after attempt three under the old budget; legacy tests fail once the test fixture expects separate new/legacy limits.

- [ ] **Step 3: Implement separate active and legacy limits**

Use these constants and predicates:

```python
MAX_ATTEMPTS = 3
MAX_RECORDED_ATTEMPT = 5
```

- Validate persisted `target["attempts"]` and history-entry `attempt` against `MAX_RECORDED_ATTEMPT`.
- Keep `begin_attempt()` blocked when `target["attempts"] >= MAX_ATTEMPTS`.
- Make `record_failure()` terminal when `target["attempts"] >= MAX_ATTEMPTS`.
- Add `_normalize_legacy_budget(state)` after migration and validation in `load_state()`. Change only a `pending` target with attempts `>= MAX_ATTEMPTS` to `failed`, set a concise non-secret error such as `legacy attempt budget exceeds current three-call limit`, and leave `success`, `accepted-local`, `running`, and existing history unchanged.
- Validate the normalized state again before returning it.

```python
def load_state(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            state = _validate_state(_migrate_v1_state(json.load(handle)))
        state = _normalize_legacy_budget(state)
        return _validate_state(state)
    except (OSError, json.JSONDecodeError) as error:
        raise TaskStateError(f"cannot load state: {error}") from error
```

- [ ] **Step 4: Run the state suite and verify GREEN**

Run:

```bash
python3 -m unittest scripts.task_state_test -v
```

Expected: all state tests pass; no test starts a fourth call.

- [ ] **Step 5: Commit**

```bash
git add scripts/task_state.py scripts/task_state_test.py
git commit -m "feat: enforce three-attempt edit budget"
```

---

### Task 2: Accept the garment-best artifact from the current cycle

**Files:**
- Modify: `scripts/task_state.py:350-490,630-780,932-1010`
- Modify: `scripts/task_state_test.py`
- Test: `scripts/task_state_test.py`

**Interfaces:**
- Consumes: `record_local_acceptance(state, target_token, artifact_name, name, updated_at)` and contiguous history suffixes whose attempt numbers begin at one.
- Produces: `_current_cycle_history(target)`, `_accepted_history_entry(target)`, historical-artifact acceptance, selected-entry upload/reconciliation.

- [ ] **Step 1: Add failing current-cycle selection tests**

Add behavioral tests that use real transitions:

```python
def test_third_attempt_can_accept_first_current_cycle_artifact(self) -> None:
    state = self.make_state()
    self.begin(state)
    first = state["targets"]["box_t1"]["attempt_history"][-1]["artifact_name"]
    task_state.record_failure(state, target_token="box_t1", error="visual reject 1",
                              updated_at="2026-08-17T10:01:30+08:00")
    self.begin(state)
    task_state.record_failure(state, target_token="box_t1", error="visual reject 2",
                              updated_at="2026-08-17T10:02:30+08:00")
    self.begin(state)
    task_state.record_local_acceptance(
        state, target_token="box_t1", artifact_name=first,
        name=task_state.output_name(1, "box_t1"),
        updated_at="2026-08-17T10:03:30+08:00",
    )
    target = state["targets"]["box_t1"]
    self.assertEqual(target["local_acceptance"]["artifact_name"], first)
    self.assertEqual([entry["outcome"] for entry in target["attempt_history"]],
                     ["accepted-local", "failed", "failed"])
    self.assertIsNone(state["current_target"])

def test_success_updates_selected_history_not_latest_history(self) -> None:
    state, first = self.make_first_of_three_locally_accepted()
    task_state.record_success(
        state, target_token="box_t1", file_token="box_out",
        name=task_state.output_name(1, "box_t1"),
        updated_at="2026-08-17T10:04:00+08:00",
    )
    history = state["targets"]["box_t1"]["attempt_history"]
    self.assertEqual([entry["outcome"] for entry in history],
                     ["success", "failed", "failed"])
    self.assertEqual(history[0]["output"]["file_token"], "box_out")
    self.assertIsNone(history[-1]["output"])

def test_historical_acceptance_rejects_artifact_from_previous_retry_cycle(self) -> None:
    state = self.make_state()
    for number in range(3):
        self.begin(state)
        old_artifact = state["targets"]["box_t1"]["attempt_history"][0]["artifact_name"]
        task_state.record_failure(state, target_token="box_t1", error=f"old {number}",
                                  updated_at=f"2026-08-17T10:0{number + 1}:30+08:00")
    task_state.prepare_retry(state, updated_at="2026-08-17T11:00:00+08:00")
    self.begin(state)
    before = json.loads(json.dumps(state))
    with self.assertRaisesRegex(task_state.TaskStateError, "current attempt cycle"):
        task_state.record_local_acceptance(
            state, target_token="box_t1", artifact_name=old_artifact,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T11:01:00+08:00",
        )
    self.assertEqual(state, before)
```

Define the helper referenced by the success test:

```python
def make_first_of_three_locally_accepted(self) -> tuple[dict, str]:
    state = self.make_state()
    self.begin(state)
    first = state["targets"]["box_t1"]["attempt_history"][-1]["artifact_name"]
    task_state.record_failure(state, target_token="box_t1", error="reject 1",
                              updated_at="2026-08-17T10:01:30+08:00")
    self.begin(state)
    task_state.record_failure(state, target_token="box_t1", error="reject 2",
                              updated_at="2026-08-17T10:02:30+08:00")
    self.begin(state)
    task_state.record_local_acceptance(
        state, target_token="box_t1", artifact_name=first,
        name=task_state.output_name(1, "box_t1"),
        updated_at="2026-08-17T10:03:30+08:00",
    )
    return state, first
```

Add exact boundary tests for early acceptance and source reset:

```python
def test_early_active_acceptance_stops_before_attempt_two(self) -> None:
    state = self.make_state()
    self.begin(state)
    self.accept_local(state)
    self.assertEqual(state["targets"]["box_t1"]["attempts"], 1)
    with self.assertRaisesRegex(task_state.TaskStateError, "not pending"):
        self.begin(state)

def test_historical_acceptance_rejects_artifact_before_source_change(self) -> None:
    state = self.make_state()
    self.begin(state)
    old_artifact = state["targets"]["box_t1"]["attempt_history"][-1]["artifact_name"]
    task_state.record_failure(state, target_token="box_t1", error="old source",
                              updated_at="2026-08-17T10:01:30+08:00")
    task_state.reconcile(
        state, source_tokens=["box_new_source"], target_tokens=["box_t1"],
        outputs=[], run_id="run_2", started_at="2026-08-17T11:00:00+08:00",
        updated_at="2026-08-17T11:00:01+08:00",
    )
    self.begin(state)
    before = json.loads(json.dumps(state))
    with self.assertRaisesRegex(task_state.TaskStateError, "current attempt cycle"):
        task_state.record_local_acceptance(
            state, target_token="box_t1", artifact_name=old_artifact,
            name=task_state.output_name(1, "box_t1"),
            updated_at="2026-08-17T11:01:00+08:00",
        )
    self.assertEqual(state, before)
```

Add a legacy running-attempt test:

```python
def test_legacy_running_fourth_attempt_can_select_existing_cycle_without_new_call(self) -> None:
    state = self.make_state()
    history = self.legacy_finished_history(4, outcome="failed")
    history[-1].update({
        "outcome": "running", "finished_at": None, "error": None, "output": None,
    })
    target = state["targets"]["box_t1"]
    target.update({
        "status": "running", "classification": "front",
        "reference_tokens": ["box_s1"], "attempts": 4,
        "prompt_sha256": history[-1]["prompt_sha256"],
        "model": "image-model", "error": None,
        "updated_at": history[-1]["started_at"], "attempt_history": history,
    })
    state["current_target"] = "box_t1"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        loaded = task_state.load_state(path)
    with self.assertRaisesRegex(task_state.TaskStateError, "not pending"):
        self.begin(loaded)
    first = history[0]["artifact_name"]
    task_state.record_local_acceptance(
        loaded, target_token="box_t1", artifact_name=first,
        name=task_state.output_name(1, "box_t1"),
        updated_at="2026-08-17T11:01:00+08:00",
    )
    self.assertEqual(
        loaded["targets"]["box_t1"]["local_acceptance"]["artifact_name"], first,
    )
    self.assertEqual(loaded["targets"]["box_t1"]["attempts"], 4)
```

- [ ] **Step 2: Run the selection tests and verify RED**

Run:

```bash
python3 -m unittest \
  scripts.task_state_test.TaskStateTest.test_third_attempt_can_accept_first_current_cycle_artifact \
  scripts.task_state_test.TaskStateTest.test_success_updates_selected_history_not_latest_history \
  scripts.task_state_test.TaskStateTest.test_historical_acceptance_rejects_artifact_from_previous_retry_cycle -v
```

Expected: old `accept-local` rejects the first artifact because it is not the active artifact; the success path assumes the latest entry.

- [ ] **Step 3: Implement cycle and accepted-entry lookup helpers**

Implement private helpers with these contracts:

```python
def _current_cycle_history(target: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the final contiguous 1..N attempt suffix for the current cycle."""

def _accepted_history_entry(target: dict[str, Any]) -> dict[str, Any]:
    """Return the sole history entry matching local_acceptance artifact identity."""
```

`_current_cycle_history` scans backward to the most recent entry whose `attempt == 1`, requires literal attempts `1..N`, requires `N == target["attempts"]`, and rejects a missing or non-contiguous suffix. `_accepted_history_entry` requires exactly one `accepted-local` entry and matches both `run_id` and `artifact_name` from `local_acceptance`.

- [ ] **Step 4: Extend acceptance without changing the CLI or schema**

Keep the existing `accept-local --artifact-name` command shape. In `record_local_acceptance()`:

- Preserve active-artifact acceptance on attempt one or two.
- Permit a non-active artifact only when the active attempt count is at least three and the artifact belongs to `_current_cycle_history(target)` with outcome `failed`.
- Work on a deep copy. If an earlier artifact wins, finish the active entry as `failed` with sanitized error `not selected; lower garment-reference similarity`, convert the selected entry to `accepted-local`, clear its error/output, and copy its classification, references, prompt digest, and model to the target-level compact fields.
- Store the selected entry's `run_id` and `artifact_name` in `local_acceptance`, clear `current_target`, validate the candidate, then replace the original state atomically.
- Update `_validate_state()` so accepted-local and success history may be anywhere in the current-cycle suffix, while still requiring exactly one matching entry.
- Update `record_success()` and the accepted-local branches in `reconcile()` to use `_accepted_history_entry()` instead of `attempt_history[-1]`.

- [ ] **Step 5: Run focused and full state tests**

Run:

```bash
python3 -m unittest scripts.task_state_test -v
```

Expected: all state tests pass, including recovery of a selected earlier artifact and rejection of prior-cycle artifacts.

- [ ] **Step 6: Commit**

```bash
git add scripts/task_state.py scripts/task_state_test.py
git commit -m "feat: accept best artifact from current cycle"
```

---

### Task 3: Rewrite the outfit-swap orchestration and QC contracts

**Files:**
- Modify: `SKILL.md:47-55`
- Modify: `references/edit-prompt.md:41-47`
- Modify: `references/qc-and-failures.md:20-79`
- Modify: `references/base-contract.md:70-84`
- Modify: `scripts/skill_contract_test.py:205-230,330-360`
- Test: `scripts/skill_contract_test.py`

**Interfaces:**
- Consumes: three-call state transitions and historical `accept-local` selection from Tasks 1-2.
- Produces: unambiguous early-pass, generation-retry, garment-first fallback, and immediate upload/Base failure instructions.

- [ ] **Step 1: Record a RED pressure scenario before editing the skill**

Give a fresh agent only the current installed skill and this scenario; save its answer in the plan's SDD workspace:

```text
Use the outfit-swap skill at /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap/SKILL.md. For one target: attempt 1 is a valid bitmap but visually rejected, attempt 2 is also rejected, and attempt 3 has the best person preservation while attempt 1 is visibly closest to the garment references. State whether you generate more images and which artifact you upload.
```

Expected RED: the old contract continues toward attempt five or selects the fifth bitmap; it does not stop at three and choose attempt one.

- [ ] **Step 2: Add failing contract tests**

Replace fifth-attempt assertions with explicit three-attempt behavior:

```python
def test_three_attempt_early_pass_and_garment_best_fallback_contract(self) -> None:
    qc = self.references["qc-and-failures.md"]
    edit = self.references["edit-prompt.md"]
    runtime_markdown = "\n".join([self.skill, *self.references.values()]).lower()
    self.assertIn("one initial call plus at most two retries", qc)
    self.assertIn("stop immediately after an early full-QC pass", qc)
    self.assertIn("compare every complete decodable candidate", qc)
    self.assertIn("garment construction and silhouette", qc)
    self.assertIn("accept-local", qc)
    self.assertIn("Reuse the same prompt and ordered references", edit)
    for obsolete in ("five attempts", "attempt five", "fifth bitmap"):
        self.assertNotIn(obsolete, runtime_markdown)

def test_generation_failures_retry_but_upload_and_base_failures_stop(self) -> None:
    qc = self.references["qc-and-failures.md"]
    base = self.references["base-contract.md"]
    self.assertIn("attempt one or two", qc)
    self.assertIn("retry the same target", qc)
    self.assertIn("no visual prompt correction", qc)
    self.assertIn("Upload and critical Base-write/readback failures", qc)
    self.assertIn("stop immediately", base)
```

- [ ] **Step 3: Run the contract tests and verify RED**

Run:

```bash
python3 -m unittest \
  scripts.skill_contract_test.SkillContractTest.test_three_attempt_early_pass_and_garment_best_fallback_contract \
  scripts.skill_contract_test.SkillContractTest.test_generation_failures_retry_but_upload_and_base_failures_stop -v
```

Expected: FAIL because the shipped skill still specifies five attempts and fifth-image forced acceptance.

- [ ] **Step 4: Update the skill and references**

Write the following behavior without changing Base commands:

- `SKILL.md`: attempts one/two use full QC and stop immediately on a pass; failures retain artifacts and retry the same target; attempt three triggers comparison across valid current-cycle artifacts; promote and pass the selected artifact name to `accept-local`.
- When the third artifact is unusable and no earlier valid candidate exists, call `record-error --code external-call --error-file` directly while the third attempt is active; do not expose another pending attempt.
- `qc-and-failures.md`: define the ranking order verbatim from the design—construction/silhouette, color/material, visible details, target preservation, then earlier-attempt tie-break. Exclude invalid artifacts. Replace the fifth-attempt section with `Third-attempt garment-best selection`.
- `edit-prompt.md`: state total budget three; visual rejection adds one targeted correction; transport/missing-artifact retry reuses the same prompt and ordered references with no visual correction.
- `base-contract.md`: state that upload and critical Base-write/readback failures stop immediately and are not generation retries.
- Preserve the authorization sentence, serial processing, untrusted-content boundary, exact prompt template, and all existing lark-cli command shapes.

- [ ] **Step 5: Run contract tests and verify GREEN**

Run:

```bash
python3 -m unittest scripts.skill_contract_test -v
```

Expected: all contract tests pass and no obsolete five-attempt wording remains in shipped Markdown/Python sources except migration-specific legacy compatibility text.

- [ ] **Step 6: Commit**

```bash
git add SKILL.md references/base-contract.md references/edit-prompt.md \
  references/qc-and-failures.md scripts/skill_contract_test.py
git commit -m "docs: select garment-best result within three attempts"
```

---

### Task 4: Verify recovery, deployability, and forward behavior

**Files:**
- Verify: `SKILL.md`
- Verify: `references/base-contract.md`
- Verify: `references/edit-prompt.md`
- Verify: `references/qc-and-failures.md`
- Verify: `scripts/task_state.py`
- Verify: `scripts/task_state_test.py`
- Verify: `scripts/skill_contract_test.py`

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: green full-suite, validator, diff-integrity, and independent forward-test evidence.

- [ ] **Step 1: Run focused suites**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  scripts.task_state_test scripts.skill_contract_test -v
```

Expected: all focused tests pass with no warnings or errors.

- [ ] **Step 2: Run the complete helper suite**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts -p '*_test.py' -v
```

Expected: all tests pass; the baseline before implementation is 128 tests.

- [ ] **Step 3: Validate the skill structure**

```bash
/opt/homebrew/bin/python3.13 \
  /Users/hugo_1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap
```

Expected: `Skill is valid!`

- [ ] **Step 4: Run two fresh forward pressure scenarios**

Scenario A:

```text
Use the outfit-swap skill at /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap/SKILL.md. Attempt 1 passes every QC condition. State whether another Doubao request is made and list the state transitions through upload.
```

Expected: no attempt two; promote attempt one, `accept-local`, upload, `success`.

Scenario B:

```text
Use the outfit-swap skill at /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap/SKILL.md. Attempts 1 and 2 are visually rejected. Attempt 3 is valid, but attempt 1 is visibly closest to the garment references while attempt 3 preserves the person better. State which artifact is selected, every state transition, and whether attempt 4 is allowed.
```

Expected: select attempt one because garment fidelity is primary; accept it from current-cycle history; no attempt four.

- [ ] **Step 5: Check source and diff integrity**

```bash
git diff --check
git status --short
rg -n "attempt five|fifth-attempt|five-attempt budget|MAX_ATTEMPTS = 5" \
  SKILL.md references scripts
```

Expected: `git diff --check` is silent; status contains only intended tracked files or known generated bytecode dirt; obsolete runtime/contract wording is absent, while explicitly labeled legacy compatibility may still mention historical attempt four/five.

- [ ] **Step 6: Commit verification-only adjustments if required**

If verification required no source change, record no empty commit. If it exposed a test or wording defect, fix it test-first, rerun Steps 1-5, then stage only the relevant files from this exact list and commit:

```bash
git add SKILL.md references/base-contract.md references/edit-prompt.md \
  references/qc-and-failures.md scripts/task_state.py \
  scripts/task_state_test.py scripts/skill_contract_test.py
git commit -m "test: verify three-attempt garment selection"
```
