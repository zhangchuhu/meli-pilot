# Remove output-size QC

## Goal

Keep Doubao/Seedream edit requests pinned to `--size 2K`, while preventing generated-image dimensions or aspect ratio from affecting visual acceptance or retry decisions.

## Behavior

- Continue invoking `safe_edit.py` with the fixed `--size 2K` control.
- Continue validating that target, source, and generated artifacts are complete, decodable supported images within the existing absolute image constraints.
- Do not compare a generated artifact's pixel dimensions or aspect ratio with its `爆款图` input.
- Do not reject, retry, rank, or fail an otherwise valid generated artifact because its dimensions or aspect ratio differ from the target.
- Continue full visual QC for garment fidelity, target identity, pose, composition, scene, accessories, occlusion, anatomy, and visible text/layout.
- After three attempts, continue choosing the garment-best complete decodable candidate under the existing selection rules; size similarity is not a ranking factor.

## Implementation surface

- Update `references/qc-and-failures.md` to state the positive acceptance contract and explicitly exclude target/output dimension comparison.
- Update `SKILL.md` so the fixed `2K` transport control and size-independent QC behavior are visible in the core workflow.
- Add contract tests that fail unless both requirements are present and remain non-contradictory.
- Do not change `scripts/safe_edit.py`, because it already passes `--size 2K` deterministically.
- Do not remove `scripts/image_qc.py` absolute validity checks; they protect API compatibility and artifact integrity rather than comparing target and output dimensions.

## Verification

- Run the new focused contract test and observe it fail before documentation changes.
- Apply the minimal contract changes and rerun the focused test.
- Run the complete skill contract, state, safe-edit, and image-QC test suites.
- Run the skill validator and `git diff --check`.
