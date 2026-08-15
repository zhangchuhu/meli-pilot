# Remove Run Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the outfit-swap same-machine run-lock implementation, workflow instructions, failure semantics, and tests without changing the remaining resumable image-processing behavior.

**Architecture:** The workflow will move directly from exact table URL validation to schema pagination and validation. A static contract will protect the runtime skill surface from reintroducing lock helpers or lock instructions; the existing task-state and image-processing components remain untouched.

**Tech Stack:** Markdown skill contracts, Python 3 standard library, `unittest`, Skill Creator validation

## Global Constraints

- Remove `scripts/run_lock.py` and `scripts/run_lock_test.py` completely.
- Do not introduce a replacement mutex, compatibility shim, feature flag, or lock cleanup path.
- Keep the immediate second `处理明细` absence check before field creation.
- Keep record reconciliation, pending-upload recovery, serial processing, generation budgets, attachment validation, and Base persistence unchanged.
- Runtime lock-reference enforcement covers `SKILL.md`, `references/`, and retained `scripts/`; process documents under `docs/` are outside that runtime contract.

---

### Task 1: Remove the run-lock feature from the runtime skill

**Files:**
- Modify: `scripts/skill_contract_test.py:17-25`
- Modify: `scripts/skill_contract_test.py:137-143`
- Modify: `scripts/skill_contract_test.py:224-250`
- Modify: `SKILL.md:19-54`
- Modify: `references/base-contract.md:1-38`
- Modify: `references/qc-and-failures.md:63-87`
- Delete: `scripts/run_lock.py`
- Delete: `scripts/run_lock_test.py`

**Interfaces:**
- Consumes: the existing runtime skill surface consisting of `SKILL.md`, `references/*.md`, and the retained Python helpers in `RUNTIME_SCRIPTS`
- Produces: a lock-free workflow whose first Base operation after URL resolution is schema pagination, plus a static `test_run_lock_feature_is_absent` regression contract

- [ ] **Step 1: Write the failing static contract**

Remove `"scripts/run_lock.py"` from `RUNTIME_SCRIPTS`. Add this test to `SkillContractTest` after `test_skill_links_every_runtime_reference_and_script`:

```python
def test_run_lock_feature_is_absent(self) -> None:
    runtime_sources = {
        "SKILL.md": self.skill,
        **{
            f"references/{name}": source
            for name, source in self.references.items()
        },
        **{
            script: read_required(SKILL_ROOT / script)
            for script in RUNTIME_SCRIPTS
        },
    }
    pattern = re.compile(r"(?i)(?:run[_ -]?lock|运行锁)")
    findings = [
        name for name, source in runtime_sources.items() if pattern.search(source)
    ]
    self.assertEqual(findings, [])
    self.assertFalse((SKILL_ROOT / "scripts" / "run_lock.py").exists())
    self.assertFalse((SKILL_ROOT / "scripts" / "run_lock_test.py").exists())
```

In `test_base_contract_closes_scope_pagination_and_cell_values`, replace the holder-ready assertion and hold-before-create ordering assertion with:

```python
self.assertIn("Immediately before creating absent `处理明细`, repeat", base)
self.assertLess(
    base.index("lark-cli base +field-list"),
    base.index("lark-cli base +field-create"),
)
```

- [ ] **Step 2: Run the new contract and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest scripts.skill_contract_test.SkillContractTest.test_run_lock_feature_is_absent -v
```

Expected: FAIL because the current runtime skill still references `scripts/run_lock.py`, and both lock files still exist.

- [ ] **Step 3: Remove lock instructions from the workflow and references**

In `SKILL.md`:

- delete the `scripts/run_lock.py` helper bullet;
- change workflow step 1 from “pre-lock failure” to “preflight failure”;
- replace step 2 with direct full-schema pagination, schema validation, immediate absence recheck and optional `处理明细` creation, followed by selected-record pagination;
- delete cleanup step 12, leaving steps 1 through 11 contiguous.

In `references/base-contract.md`:

- change the opening to require URL resolution before any mutation;
- remove the long-lived holder paragraph and all “under lock” wording;
- state `Immediately before creating absent \`处理明细\`, repeat the complete absence check, then create it once.`

In `references/qc-and-failures.md`:

- change global preflight failures to URL, schema, dependency, or key failures only;
- delete the final release, reclaim, control-channel, and mutation-guard paragraph.

- [ ] **Step 4: Delete the implementation and dedicated tests**

Delete exactly:

```text
scripts/run_lock.py
scripts/run_lock_test.py
```

Do not change `task_state.py`, `image_qc.py`, `safe_edit.py`, or their tests.

- [ ] **Step 5: Run the focused skill contract and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest scripts.skill_contract_test -v
```

Expected: all skill contract tests pass and the lock-absence test reports no runtime references or files.

- [ ] **Step 6: Validate the skill and run the complete test suite**

Run:

```bash
python3 /Users/hugo_1/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts -p '*_test.py' -v
```

Expected: Skill Creator validation succeeds and every retained Python test passes.

- [ ] **Step 7: Forward-test the updated skill read-only**

Dispatch a fresh agent with this exact task:

```text
Read and use the skill at /Users/hugo_1/Workspace/PythonProject/meli-pilot/outfit-swap/SKILL.md. A user provides a valid Feishu Base table URL and asks you to process it with the outfit-swap workflow. Explain only the exact local/preflight execution sequence through the point immediately before downloading attachments. Do not call external services, do not run commands, and do not modify files. Return the ordered sequence and name every bundled helper you would invoke.
```

Expected: the sequence proceeds from exact URL validation directly to schema pagination and does not mention or invoke a run lock.

- [ ] **Step 8: Inspect and commit the intended deletion**

Run:

```bash
git diff --check
git status --short
git diff -- SKILL.md references/base-contract.md references/qc-and-failures.md scripts/skill_contract_test.py scripts/run_lock.py scripts/run_lock_test.py
git add SKILL.md references/base-contract.md references/qc-and-failures.md scripts/skill_contract_test.py scripts/run_lock.py scripts/run_lock_test.py docs/superpowers/plans/2026-08-16-remove-run-lock.md
git commit -m "refactor: remove outfit-swap run lock"
```
