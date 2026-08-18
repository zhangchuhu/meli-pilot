# Outfit-swap Automation and Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resumable, automatically visually reviewed outfit-swap runner that finalizes targets transactionally, sends only three or four useful garment references, and processes records concurrently with a configurable default of two.

**Architecture:** A table scheduler performs global preflight and dispatches independent record workers. Each record worker keeps targets serial, uses a deterministic target plan plus Volcengine Ark multimodal QC, and hands accepted candidates to one idempotent finalizer. Existing state, image validation, Seedream transport, and `lark-cli` remain the only authorities for their current responsibilities.

**Tech Stack:** Python 3.10+ standard library, `unittest`, Volcengine Ark `ChatCompletions`, existing Seedream CLI, `lark-cli`, FFmpeg/FFprobe.

**Spec:** `docs/superpowers/specs/2026-08-18-outfit-swap-automation-performance-design.md`

## Global implementation constraints

- Keep Seedream `--size 2K`; never use output dimensions or aspect ratio as QC inputs.
- Allow no more than three initiated paid generations per target cycle. Attempts one and two may pass early; after attempt three, choose the best complete candidate and never create attempt four.
- Process records concurrently but targets inside one record serially. `--record-concurrency` defaults to `2` and must reject non-positive values.
- Keep all Feishu operations behind `lark-cli`; do not add direct Feishu HTTP calls.
- Do not restore a persistent or cross-process run lock. The scheduler may maintain only an in-process active-record set.
- Invoke external CLIs as argv arrays with `shell=False`. File-backed `lark-cli` calls must run in the file directory and use `./basename`.
- Automatic QC uses the provisioned `ARK_VISION_MODEL`, never a hardcoded marketing model name, and must not log keys, authorization headers, or image Base64.
- Preserve append-only artifacts, exact Base scope, resumability, terminal stop behavior, and readback verification.

## File map

New runtime modules:

- `scripts/lark_runner.py`
- `scripts/finalize_target.py`
- `scripts/vision_qc.py`
- `scripts/ark_vision_qc.py`
- `scripts/reference_selector.py`
- `scripts/prompt_builder.py`
- `scripts/infographic_text.py`
- `scripts/event_log.py`
- `scripts/run_record.py`
- `scripts/run_table.py`
- `scripts/qc_replay.py`

New tests mirror those modules with `_test.py`. Modify `scripts/task_state.py`, `scripts/task_state_test.py`, `scripts/skill_contract_test.py`, `SKILL.md`, and the relevant files under `references/`.

## Task 1: Upgrade durable state to schema v3

**Files:**

- Modify: `scripts/task_state.py`
- Modify: `scripts/task_state_test.py`

- [ ] Add failing tests for schema-v3 initialization, v2-to-v3 migration, and idempotent reload.

The migrated target object must add these fields without altering old attempt/history entries:

```python
{
    "target_plan": None,
    "qc_reports": [],
    "selection_reason": None,
}
```

- [ ] Add failing tests for immutable target plans and append-only QC reports.

Required Python interfaces:

```python
def record_target_plan(state: dict, target_index: int, plan: dict) -> None: ...
def record_qc_report(state: dict, target_index: int, report: dict) -> None: ...
def record_selection_reason(state: dict, target_index: int, reason: dict) -> None: ...
```

`record_target_plan` may replay an identical plan but must reject replacement with a different plan. QC reports append and include an artifact digest plus attempt number.

- [ ] Run the focused tests and confirm they fail for the intended missing behavior.

```bash
python3 -m unittest scripts.task_state_test
```

- [ ] Implement the minimal schema migration and state APIs, retaining atomic writes and existing validation.
- [ ] Add CLI subcommands `target-plan`, `qc-report`, and `selection-reason` using existing state-file conventions.
- [ ] Re-run focused tests, then all existing state tests.

```bash
python3 -m unittest scripts.task_state_test
```

- [ ] Commit.

```bash
git add scripts/task_state.py scripts/task_state_test.py
git commit -m "feat: persist automatic QC checkpoints"
```

## Task 2: Add safe relative-path Base execution

**Files:**

- Create: `scripts/lark_runner.py`
- Create: `scripts/lark_runner_test.py`

- [ ] Write tests around a fake `lark-cli` executable that records argv and cwd.

Cover:

- `--file ./candidate.png`, `--json @./update.json`, and `--output ./records.ndjson`;
- cwd equals the validated parent directory;
- argv-list invocation and `shell=False`;
- rejection of absolute names, `..`, separators, missing files, and symlinks escaping the task directory;
- sanitized failures that omit attachment tokens and local file content.

- [ ] Define the wrapper API in the tests.

```python
class LarkBaseClient:
    def resolve_base(self, base_url: str) -> dict: ...
    def list_records(self, *, app_token: str, table_id: str, view_id: str, output: Path) -> Path: ...
    def download_attachment(self, *, token: str, output: Path) -> Path: ...
    def upload_attachment(self, *, file: Path, app_token: str, table_id: str) -> dict: ...
    def update_record(self, *, app_token: str, table_id: str, record_id: str, payload: Path) -> dict: ...
    def get_record(self, *, app_token: str, table_id: str, record_id: str) -> dict: ...
```

- [ ] Run the focused test and observe failure.

```bash
python3 -m unittest scripts.lark_runner_test
```

- [ ] Implement basename validation, containment checks using resolved paths, and one private `_run` that invokes `subprocess.run` with `shell=False`.
- [ ] Re-run the focused test and existing contract test.

```bash
python3 -m unittest scripts.lark_runner_test scripts.skill_contract_test
```

- [ ] Commit.

```bash
git add scripts/lark_runner.py scripts/lark_runner_test.py
git commit -m "feat: add safe relative-path Base client"
```

## Task 3: Implement idempotent target finalization

**Files:**

- Create: `scripts/finalize_target.py`
- Create: `scripts/finalize_target_test.py`
- Modify: `scripts/task_state.py`
- Modify: `scripts/task_state_test.py`

- [ ] Write state-machine tests for these restart points:

1. candidate not yet promoted;
2. deterministic output exists and state is `accepted-local`;
3. attachment was uploaded but compact detail was not written;
4. success mapping and Base readback already match.

- [ ] Add failure tests for corrupt input, upload failure, detail-update failure, readback mismatch, and duplicate invocation.
- [ ] Define typed request/result boundaries.

```python
@dataclass(frozen=True)
class FinalizeRequest:
    task_dir: Path
    state_file: Path
    record_id: str
    target_index: int
    candidate: Path
    candidate_sha256: str

@dataclass(frozen=True)
class FinalizeResult:
    output_path: Path
    attachment_token: str
    resumed_from: str
```

- [ ] Run the focused tests and confirm the expected failures.

```bash
python3 -m unittest scripts.finalize_target_test scripts.task_state_test
```

- [ ] Implement this exact transaction using existing `image_qc.py` and state operations:

```text
revalidate → promote → accept-local → upload → success → compact
→ Base update → Base readback → exact verification
```

Promotion must be append-safe, and resumes must reconcile state/attachment identity before deciding whether upload is needed.

- [ ] Re-run focused tests and contract tests.

```bash
python3 -m unittest scripts.finalize_target_test scripts.task_state_test scripts.skill_contract_test
```

- [ ] Commit.

```bash
git add scripts/finalize_target.py scripts/finalize_target_test.py scripts/task_state.py scripts/task_state_test.py
git commit -m "feat: finalize accepted targets transactionally"
```

## Task 4: Encode the pure QC policy and third-attempt ranking

**Files:**

- Create: `scripts/vision_qc.py`
- Create: `scripts/vision_qc_test.py`

- [ ] Write parser tests for valid strict JSON and rejection of Markdown fences, trailing prose, missing/unknown fields, unknown defect codes, invalid nullability, and out-of-range scores/confidence.
- [ ] Define the policy types.

```python
class DefectCode(str, Enum): ...

@dataclass(frozen=True)
class Scores:
    garment_construction: int
    color_material: int
    garment_details: int
    target_preservation: int
    text_layout: int | None

@dataclass(frozen=True)
class QCReport:
    candidate: str
    scores: Scores
    critical_defects: tuple[DefectCode, ...]
    primary_defect: DefectCode | None
    confidence: float
    decision: str
```

- [ ] Write threshold tests for ordinary and infographic candidates. No test or implementation may inspect width, height, dimensions, or aspect ratio.
- [ ] Write correction-priority tests and third-attempt lexicographic ranking tests, including earlier-attempt tie-breaking.

Required pure functions:

```python
def parse_report(raw: str, *, infographic: bool) -> QCReport: ...
def early_accept(report: QCReport, *, infographic: bool, text_exact: bool = True, panels_exact: bool = True) -> bool: ...
def correction_for(report: QCReport) -> DefectCode | None: ...
def select_best(reports: Sequence[QCReport], attempt_by_candidate: Mapping[str, int]) -> QCReport: ...
```

- [ ] Run the test and observe failure.

```bash
python3 -m unittest scripts.vision_qc_test
```

- [ ] Implement only the pure schema, thresholds, correction ordering, and lexicographic comparator.
- [ ] Re-run the test.

```bash
python3 -m unittest scripts.vision_qc_test
```

- [ ] Commit.

```bash
git add scripts/vision_qc.py scripts/vision_qc_test.py
git commit -m "feat: define structured visual QC policy"
```

## Task 5: Add the Ark multimodal QC backend

**Files:**

- Create: `scripts/ark_vision_qc.py`
- Create: `scripts/ark_vision_qc_test.py`
- Modify: `scripts/skill_contract_test.py`

- [ ] Write mocked transport tests for endpoint, headers, model selection, image MIME/Base64 encoding, timeout behavior, content-filter responses, malformed JSON, and sanitized errors.

The only permitted direct HTTP endpoint in shipped Python is:

```python
ARK_CHAT_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
```

The client must read `ARK_API_KEY` and `ARK_VISION_MODEL` from its environment and fail before network I/O when either is absent.

- [ ] Define a narrow client boundary.

```python
class ArkVisionClient:
    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[Path],
    ) -> str: ...
```

- [ ] Write orchestration tests showing: one review normally; a second review for invalid/low-confidence output; adjudication only when two valid reports disagree; no state mutation or paid-generation call.
- [ ] Change `skill_contract_test.py` first so direct HTTP remains forbidden everywhere except `scripts/ark_vision_qc.py`, and that file is restricted to the exact Ark host/path. Continue rejecting Feishu `/open-apis` and alternative HTTP clients in every shipped file.
- [ ] Run tests and confirm the backend tests fail while the tightened safety test passes its fixture cases.

```bash
python3 -m unittest scripts.ark_vision_qc_test scripts.skill_contract_test
```

- [ ] Implement the smallest standard-library transport, strict response extraction, and same-candidate retry/adjudication coordinator. Never include raw Base64, credentials, headers, or complete remote response bodies in exceptions/events.
- [ ] Re-run focused tests.

```bash
python3 -m unittest scripts.ark_vision_qc_test scripts.vision_qc_test scripts.skill_contract_test
```

- [ ] Commit.

```bash
git add scripts/ark_vision_qc.py scripts/ark_vision_qc_test.py scripts/skill_contract_test.py
git commit -m "feat: add Ark multimodal QC backend"
```

## Task 6: Build deterministic reference, prompt, and infographic plans

**Files:**

- Create: `scripts/reference_selector.py`
- Create: `scripts/reference_selector_test.py`
- Create: `scripts/prompt_builder.py`
- Create: `scripts/prompt_builder_test.py`
- Create: `scripts/infographic_text.py`
- Create: `scripts/infographic_text_test.py`

- [ ] Write selector tests for front, three-quarter, side, back, and infographic targets.

Use explicit evidence rather than filenames alone:

```python
@dataclass(frozen=True)
class SourceEvidence:
    token: str
    path: Path
    angle: str
    roles: frozenset[str]
    information_score: int
```

Ordinary front targets must select, in stable order: closest-angle model image, upper construction detail, full-outfit flat lay, and skirt/hem detail when nonredundant. Normally return three or four. Permit a fifth only with a recorded reason proving unique evidence.

- [ ] Write prompt tests for immutable base prompts and exactly one controlled correction on attempt two or three.

```python
@dataclass(frozen=True)
class GarmentFacts:
    required: tuple[str, ...]
    forbidden: tuple[str, ...]

@dataclass(frozen=True)
class TargetPlan:
    classification: str
    selected_references: tuple[str, ...]
    garment_facts: GarmentFacts
    infographic_inventory: dict | None
```

For the evidenced ivory lace set, assert the initial prompt requires a small collar, complete closure down the continuous pearl-button placket, no V/cardigan opening or exposed straps, and two complete long sleeves with cuffs. Also assert these clauses are absent for unrelated garments without that evidence.

- [ ] Write infographic extraction tests: two same-image readings, exact literal inventory, adjudication on disagreement, `FLOWY HEM` preserved verbatim, panel/garment-instance inventory, and no paid generation before the inventory is settled.
- [ ] Run focused tests and observe failure.

```bash
python3 -m unittest scripts.reference_selector_test scripts.prompt_builder_test scripts.infographic_text_test
```

- [ ] Implement deterministic selection, plan serialization/digesting, prompt construction, one-defect correction mapping, and infographic inventory coordination.
- [ ] Re-run focused tests.

```bash
python3 -m unittest scripts.reference_selector_test scripts.prompt_builder_test scripts.infographic_text_test
```

- [ ] Commit.

```bash
git add scripts/reference_selector.py scripts/reference_selector_test.py scripts/prompt_builder.py scripts/prompt_builder_test.py scripts/infographic_text.py scripts/infographic_text_test.py
git commit -m "feat: plan compact evidence-backed outfit edits"
```

## Task 7: Add sanitized event and latency metrics

**Files:**

- Create: `scripts/event_log.py`
- Create: `scripts/event_log_test.py`

- [ ] Write tests for append-only NDJSON, fsync, stable schema, concurrent appends, and field allowlisting.

Required event families:

```text
table_started / table_finished
record_started / record_finished
target_started / target_finished
generation_started / generation_finished
qc_started / qc_finished
finalize_started / finalize_finished
retry_decided / third_attempt_selected / stop_observed
```

Events may contain IDs, attempt numbers, durations, status, defect enums, scores, candidate digest, concurrency, and error category. They must reject tokens, credentials, headers, Base64, prompts, and arbitrary exception text.

- [ ] Add summary tests for total wall time, paid generations per accepted target, QC calls, early-pass rate, retry rate, failure rate, and p50/p95 phase latency.
- [ ] Run the focused tests and observe failure.

```bash
python3 -m unittest scripts.event_log_test
```

- [ ] Implement the append writer and pure summarizer.
- [ ] Re-run focused tests, including a repeated concurrent-append case.

```bash
python3 -m unittest scripts.event_log_test
```

- [ ] Commit.

```bash
git add scripts/event_log.py scripts/event_log_test.py
git commit -m "feat: record sanitized pipeline performance events"
```

## Task 8: Implement one resumable record worker

**Files:**

- Create: `scripts/run_record.py`
- Create: `scripts/run_record_test.py`
- Modify: `scripts/task_state.py`
- Modify: `scripts/task_state_test.py`

- [ ] Create fakes for Seedream, Ark QC, Base, clock, and stop signal. Write an end-to-end unit test for a record whose first target passes on attempt one and second target passes on attempt two.
- [ ] Define dependency-injected boundaries.

```python
@dataclass(frozen=True)
class RecordContext:
    task_dir: Path
    record_id: str
    target_indices: tuple[int, ...]

@dataclass(frozen=True)
class RecordServices:
    generator: object
    qc: object
    finalizer: object
    events: object
    stop_signal: object

@dataclass(frozen=True)
class RecordResult:
    record_id: str
    status: str
    accepted_targets: int
```

- [ ] Add scenario tests for:

- first-attempt early acceptance;
- one highest-priority correction and second-attempt acceptance;
- three complete rejected candidates followed by garment-first comparative selection;
- an earlier attempt winning final selection;
- artifact validation failure and same-attempt external retry under the existing transport policy;
- restart from active artifact, pending QC, `accepted-local`, uploaded attachment, and terminal state;
- recovery order is Base reconciliation, accepted-local drain, active-artifact inspection, deterministic validation/QC, then a new generation only if no recovery work exists;
- a missing artifact from an initiated attempt conservatively spends that attempt;
- stop observed before a paid call;
- persistent QC-service failure preserving the current candidate without initiating another generation;
- no code path producing paid attempt four;
- target order remaining serial.

- [ ] Run the focused tests and confirm failure.

```bash
python3 -m unittest scripts.run_record_test
```

- [ ] Implement reconciliation, target-plan persistence, serial target loop, deterministic artifact validation, automatic QC, controlled retry, third-attempt selection, and finalizer calls.
- [ ] Expose a diagnostic CLI for one record while keeping dependencies injectable.
- [ ] Re-run focused and adjacent tests.

```bash
python3 -m unittest scripts.run_record_test scripts.finalize_target_test scripts.task_state_test
```

- [ ] Commit.

```bash
git add scripts/run_record.py scripts/run_record_test.py scripts/task_state.py scripts/task_state_test.py
git commit -m "feat: automate serial target processing per record"
```

## Task 9: Add the table scheduler and bounded concurrency

**Files:**

- Create: `scripts/run_table.py`
- Create: `scripts/run_table_test.py`

- [ ] Write CLI tests showing `--record-concurrency` defaults to `2`, accepts positive integers, and rejects zero/negative/non-integer values before Base or paid-service access.
- [ ] Define scheduler configuration and results.

```python
@dataclass(frozen=True)
class ServiceLimits:
    record_workers: int = 2
    doubao_requests: int = 2
    qc_requests: int = 2
    lark_writes: int = 1
    lark_reads: int = 2

@dataclass(frozen=True)
class TableConfig:
    base_url: str
    record_concurrency: int = 2
    retry_failed: bool = False
    qc_mode: str = "automatic"

@dataclass(frozen=True)
class TableResult:
    selected: int
    succeeded: int
    failed: int
    stopped: int
```

- [ ] Write scheduler tests for exact Base/table/view preflight, stable record materialization, an in-process active-record set, bounded record concurrency, target serialization inside each record, and independent record failures.
- [ ] Add preflight tests proving invalid global authentication, schema drift, or missing required fields dispatch zero workers and initiate zero paid calls. Support `--qc-mode shadow` and `--record-concurrency 1` as rollback controls.
- [ ] Write semaphore tests proving the default maxima: records 2, Doubao 2, QC 2, Lark writes 1, Lark reads 2.
- [ ] Add a global-stop race test: once a worker reports stop, queued workers and active workers at their next paid-call checkpoint initiate no new generation. Repeat the race test ten times.
- [ ] Add a test documenting the explicit non-guarantee: two independent processes are unsupported and no persistent/cross-process lock is created.
- [ ] Run focused tests and observe failure.

```bash
python3 -m unittest scripts.run_table_test
```

- [ ] Implement one global preflight, stable selected-record queue, `ThreadPoolExecutor`, per-service semaphores, stop propagation, and aggregate result reporting.
- [ ] Ensure every worker rechecks durable state plus stop immediately before each paid call.
- [ ] Re-run the race and record-worker suites.

```bash
python3 -m unittest scripts.run_table_test scripts.run_record_test
```

- [ ] Commit.

```bash
git add scripts/run_table.py scripts/run_table_test.py
git commit -m "feat: process outfit records concurrently"
```

## Task 10: Build historical QC replay and shadow gates

**Files:**

- Create: `scripts/qc_replay.py`
- Create: `scripts/qc_replay_test.py`
- Create: `tests/fixtures/qc-replay/manifest.example.json`

- [ ] Write tests for manifest validation, deterministic candidate ordering, expected accepted-attempt comparison, false-accept/false-retry accounting, and no mutation of source state/Base.
- [ ] Define replay output.

```python
@dataclass(frozen=True)
class ReplaySummary:
    targets: int
    agreement_rate: float
    false_accept_rate: float
    false_retry_rate: float
    mean_qc_calls: float
```

- [ ] Encode the exact shadow-mode exit gates from the design:

- zero missed critical garment-construction defects in the replay set, including targets 6, 7, and 9;
- zero missed infographic text changes, including target 8's changed `FLOWY HEM` text/layout;
- no more than ten percent false rejection among previously accepted ordinary images;
- every response either validates or follows the same-candidate review/adjudication path;
- no dimension/aspect-ratio input in replay manifests or requests;
- average QC call count and predicted paid attempts reported separately.

- [ ] Run the focused tests and observe failure.

```bash
python3 -m unittest scripts.qc_replay_test
```

- [ ] Implement offline manifest replay plus an opt-in live Ark mode. The default command must be read-only and make no Base writes or Seedream calls.
- [ ] Re-run focused tests. If credentials and approved fixtures are available during execution, run the live shadow replay and archive only sanitized reports.

```bash
python3 -m unittest scripts.qc_replay_test
```

- [ ] Commit.

```bash
git add scripts/qc_replay.py scripts/qc_replay_test.py tests/fixtures/qc-replay/manifest.example.json
git commit -m "test: add automatic QC shadow replay"
```

## Task 11: Switch the skill contract to the automated pipeline

**Files:**

- Modify: `SKILL.md`
- Modify: `references/qc-and-failures.md`
- Modify: `references/task-state.md`
- Modify: `references/feishu-base.md`
- Modify: `scripts/skill_contract_test.py`

- [ ] Add failing contract assertions for the new normal entry point and exact behavior.

The contract must state:

- `scripts/run_table.py` is the table-level normal entry point;
- record concurrency is configurable and defaults to two;
- records may run concurrently, targets within a record are serial;
- automatic visual decisions use Ark multimodal QC with the fixed thresholds and third-attempt garment-first selection;
- attempts one and two may pass early; no fourth paid generation exists;
- normal reference input is three or four images, with an evidenced fifth exception;
- infographic literal text inventory is created before generation;
- dimensions/aspect ratio never cause visual rejection or retry;
- finalization is one idempotent transaction with exact Base readback;
- all file-backed `lark-cli` arguments are relative to cwd;
- there is no persistent/cross-process run lock and simultaneous independent invocations are unsupported.

- [ ] Extend the shipped-runtime inventory in `skill_contract_test.py` to include every new Python module and enforce:

- no direct Feishu HTTP;
- no shell invocation;
- no destructive filesystem operation;
- direct HTTP exception only for the exact Ark ChatCompletions endpoint in `ark_vision_qc.py`;
- secrets/Base64 never enter event or error payloads;
- no dimension/aspect-ratio QC criteria;
- no paid attempt four.

- [ ] Document authorization boundaries precisely: the user authorizes source/target image transmission to Seedream for generation and the relevant target/candidate/reference images to Ark for automatic QC for this invocation; host approval remains authoritative.
- [ ] Run the contract test and observe it fail before documentation updates.

```bash
python3 -m unittest scripts.skill_contract_test
```

- [ ] Update the skill and references to match the implemented commands, state schema, recovery points, metrics, retry policy, and failure categories. Remove obsolete manual per-image QC workflow from the normal path while retaining diagnostic/recovery commands.
- [ ] Run the full test suite and byte-compile all runtime scripts.

```bash
python3 -m unittest discover -s scripts -p '*_test.py'
python3 -m compileall -q scripts
git diff --check
```

- [ ] Commit.

```bash
git add SKILL.md references/qc-and-failures.md references/task-state.md references/feishu-base.md scripts/skill_contract_test.py
git commit -m "feat: automate and parallelize outfit-swap records"
```

## Task 12: Install and validate the complete skill

**Files:**

- Verify: all files above
- Update only if a regression is found: the responsible runtime/test/documentation file

- [ ] Confirm the branch is clean except for intended commits and review the complete diff from the pre-implementation base.

```bash
git status --short
git log --oneline --decorate -12
git diff 38e8353..HEAD --stat
```

- [ ] Run the full verification suite again from a clean process.

```bash
python3 -m unittest discover -s scripts -p '*_test.py'
python3 -m compileall -q scripts
git diff --check
```

- [ ] Use the active `skill-installer` workflow at execution time to reinstall this local skill. Do not invent an installation path or overwrite an installed copy outside that workflow.
- [ ] Verify installed source parity for `SKILL.md`, references, runtime scripts, and tests using content hashes.
- [ ] Run a no-cost dry run against fixtures: global preflight, target planning, reference selection, prompt creation, state migration, fake QC, fake finalization, and default concurrency two.
- [ ] With explicit invocation authorization and valid `ARK_API_KEY`/`ARK_VISION_MODEL`, first pass the historical shadow gates, then run a one- or two-record automatic-QC canary against the exact requested Base view. Stop on any scope/readback mismatch.
- [ ] Compare `events.ndjson` metrics against the serial/manual baseline. Acceptance target: at least 35% lower wall time on a representative multi-record batch, with zero extra paid attempts and no critical QC false accept.
- [ ] If validation exposes a defect, first add a regression test, implement the smallest fix, rerun the full suite, and create a focused fix commit. Do not create an empty validation commit.

## Final implementation verification checklist

- [ ] Every requirement in the approved design has a corresponding runtime behavior and test.
- [ ] Every paid-call boundary checks durable state and the global stop signal.
- [ ] Attempts one/two support early acceptance; attempt three always terminates by best complete candidate or existing terminal external-call behavior.
- [ ] Ark parsing is strict, confidence handling is resumable, and remote failures do not spend another generation.
- [ ] Record concurrency defaults to two and all five resource limits are enforced.
- [ ] The finalizer resumes safely at every durable checkpoint and never duplicates an upload.
- [ ] Feishu remains `lark-cli` only, with safe `./basename` file arguments and exact readback.
- [ ] Logs and Base detail contain no credentials, tokens, Base64, or unsanitized remote payloads.
- [ ] Full tests, compileall, contract checks, and diff checks pass.
