# Three-Attempt Best-Garment Selection Design

## Goal

Replace the five-attempt forced-last-image policy with a maximum of three initiated Doubao edits per target. Accept an early candidate immediately when it passes normal QC. If the third attempt is reached, choose the valid candidate from the current cycle that is most similar to the garment references instead of automatically accepting the last image.

## Budget and stopping rules

1. Each target has one budget of three initiated Doubao calls for the current source identity and explicit retry cycle: one initial call plus at most two retries.
2. Record `attempt` before every Doubao call. A started request consumes one attempt even when the command fails or no usable artifact is returned.
3. Inspect attempts one and two with the existing full QC rules. When either passes, promote and accept it immediately; do not spend the remaining budget.
4. When attempt one or two is rejected, retain its immutable artifact, call `failure --error-file`, add one correction for the observed defect, and retry the same target.
5. A command failure or absent, incomplete, corrupt, or undecodable artifact on attempt one or two also calls `failure --error-file` and retries the same target, but adds no visual prompt correction.
6. After attempt three, compare every complete decodable candidate produced in the current three-attempt cycle. Select and accept the candidate with the highest garment-reference similarity. Do not require the selected candidate to be the third artifact.
7. If attempt three is invalid but an earlier valid candidate exists, select the best earlier candidate. If the cycle contains no complete decodable candidate, persist terminal `external-call`, mark the record failed when Base remains writable, and stop the run.
8. Upload and critical Base write/readback failures remain immediate `external-call` stops and do not enter the generation retry loop.

## Candidate ranking

Rank valid candidates by direct comparative inspection against the ordered garment references:

1. garment construction and silhouette;
2. color and material appearance;
3. closures, seams, trim, decoration, logos, and other visible garment details;
4. when garment similarity is tied, preservation of the target person, pose, crop, composition, accessories, background, lighting, and text/layout;
5. when the visual result is still tied, choose the earlier attempt to avoid an arbitrary preference for later cost.

Missing, partial, corrupt, or undecodable artifacts never participate. Attempts one and two still require all existing rejection conditions to pass for early acceptance. The comparative fallback applies only after the third initiated attempt.

## State-machine changes

- Change `MAX_ATTEMPTS` from five to three and keep the existing attempt counter and immutable ordinal history.
- Keep `failure` as the transition for rejected or unusable attempts. Attempts one and two return to `pending`; attempt three becomes terminal only when no valid candidate can be accepted.
- Extend `accept-local` so it can accept either the active artifact or an earlier valid artifact from the contiguous current three-attempt cycle.
- Historical selection is legal only while attempt three is the active attempt. The selected artifact must belong to the same target, source identity, reference selection, and explicit retry cycle. Artifacts from an older source identity or retry cycle remain in history but cannot be selected.
- When an earlier artifact wins, finish the active third history entry as not selected, change the selected history entry to `accepted-local`, and store that selected artifact in `local_acceptance`. After upload, `success` updates the selected history entry rather than assuming the latest entry won.
- Preserve the current state schema. Identify the current cycle by the contiguous history suffix whose attempt numbers are `1`, `2`, `3`; explicit retry and source-identity reset begin a new suffix at attempt one.
- Reconciliation continues to revalidate active and accepted-local artifacts before any new paid call. A recovered third attempt follows the same comparison rule without repeating the call.

## Skill-contract changes

- `SKILL.md` describes the three-call budget, early acceptance, generation-failure retries on attempts one and two, and third-attempt comparative selection.
- `references/qc-and-failures.md` replaces fifth-attempt forced acceptance with the ranking rules and distinguishes generation failures from upload/Base failures.
- `references/edit-prompt.md` changes the budget language to three attempts and keeps one targeted correction only after an inspected visual defect.
- `references/base-contract.md` retains all existing Base command shapes and immediate critical-write failure behavior.

## Testing

Use test-first development and verify each new test fails for the expected old behavior before changing production code.

- Attempts one and two return to `pending`; a fourth attempt is impossible.
- A passing first or second attempt can be accepted immediately and prevents another call.
- On the third attempt, `accept-local` can select attempt one or two from the current cycle.
- It rejects artifacts from an earlier explicit retry cycle, changed source identity, another target, or a non-contiguous history segment.
- Upload success updates the selected history entry, not automatically the latest entry.
- Invalid attempts are excluded; an invalid third attempt can still select a valid earlier candidate.
- Three invalid artifacts create terminal `external-call`; upload and critical Base failures still stop immediately.
- Skill pressure tests produce early acceptance when an early candidate passes and garment-first comparative selection only after the third attempt.
- Run focused state/contract tests, the complete helper test suite, `git diff --check`, and the skill validator.

## Out of scope

- Automated embedding or pixel-similarity scoring.
- Numeric similarity scores in durable state.
- State-schema or Base-field changes.
- Parallel generation, batch generation, deletion of historical outputs, or regeneration of already accepted targets.
