# Remove Output-Size QC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every Doubao edit request pinned to `--size 2K` while making target/output dimensions and aspect ratio irrelevant to acceptance, retry, failure, and final candidate ranking.

**Architecture:** Preserve the existing transport implementation because `scripts/safe_edit.py` already emits the fixed `2K` request. Encode the behavioral change as an explicit positive QC contract in `SKILL.md` and `references/qc-and-failures.md`, guarded by a focused static contract test.

**Tech Stack:** Python 3.10+ `unittest`, Markdown skill contracts, existing `safe_edit.py` argv wrapper.

## Global Constraints

- Doubao/Seedream edit requests remain fixed at `--size 2K`.
- Generated-image pixel dimensions and aspect ratio are never compared with `爆款图` for QC.
- Dimension or aspect-ratio differences never cause rejection, retry, failure, or candidate-ranking changes.
- Complete/decodable image checks and all garment/identity/composition/accessory/text visual checks remain active.
- Existing absolute supported-image constraints remain active for input/API compatibility and artifact integrity.

---

### Task 1: Pin the size-independent QC contract

**Files:**
- Modify: `scripts/skill_contract_test.py`
- Modify: `SKILL.md`
- Modify: `references/qc-and-failures.md`

**Interfaces:**
- Consumes: `SkillContractTest.skill`, `SkillContractTest.references`, and the shipped `scripts/safe_edit.py` source.
- Produces: `SkillContractTest.test_doubao_stays_2k_and_qc_ignores_target_output_size`, plus runtime instructions that future agents apply during QC.

- [ ] **Step 1: Write the failing contract test**

Add this method to `SkillContractTest`:

```python
def test_doubao_stays_2k_and_qc_ignores_target_output_size(self) -> None:
    qc = self.references["qc-and-failures.md"]
    safe_edit = read_required(SKILL_ROOT / "scripts" / "safe_edit.py")
    self.assertIn('"--size", "2K"', safe_edit)
    self.assertIn("fixed `--size 2K`", self.skill)
    self.assertIn(
        "Never compare a generated artifact's pixel dimensions or aspect ratio "
        "with its target `爆款图`",
        qc,
    )
    self.assertIn(
        "Never reject, retry, fail, or rank candidates by a target/output "
        "dimension or aspect-ratio difference",
        qc,
    )
    self.assertIn(
        "Continue complete/decodable artifact validation and full visual QC",
        qc,
    )
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python3 -m unittest scripts.skill_contract_test.SkillContractTest.test_doubao_stays_2k_and_qc_ignores_target_output_size -v
```

Expected: `FAIL` because the new size-independent QC contract phrases are absent; the existing `safe_edit.py` 2K assertion already passes.

- [ ] **Step 3: Add the minimal runtime contract**

In `SKILL.md` workflow step 8, describe the transport as `fixed --size 2K`. In workflow step 9, add one sentence stating that output/target pixel dimensions and aspect ratio are not QC comparison, rejection, retry, failure, or ranking criteria.

At the start of `references/qc-and-failures.md` → `Rejection conditions`, add exactly these three positive contract sentences:

```markdown
Never compare a generated artifact's pixel dimensions or aspect ratio with its target `爆款图`.
Never reject, retry, fail, or rank candidates by a target/output dimension or aspect-ratio difference.
Continue complete/decodable artifact validation and full visual QC for garment fidelity and target-preservation invariants.
```

Leave the rejection list intact. Treat `composition` as visible content/crop/layout preservation, not pixel geometry.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
python3 -m unittest scripts.skill_contract_test.SkillContractTest.test_doubao_stays_2k_and_qc_ignores_target_output_size -v
```

Expected: `OK`, one test passing.

- [ ] **Step 5: Run regression verification**

Run:

```bash
python3 -m unittest scripts.safe_edit_test scripts.image_qc_test scripts.task_state_test scripts.skill_contract_test -v
python3 /Users/hugo_1/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
git diff --check
```

Expected: all tests pass, skill validation succeeds, and `git diff --check` emits no output.

- [ ] **Step 6: Commit the implementation**

```bash
git add SKILL.md references/qc-and-failures.md scripts/skill_contract_test.py docs/superpowers/plans/2026-08-17-remove-output-size-qc.md
git commit -m "fix: ignore output dimensions during outfit QC"
```
