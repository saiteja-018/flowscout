# FlowScout

**Browser QA that produces evidence, not just a pass/fail.**

FlowScout is a browser QA agent for small SaaS teams that need to reproduce a
checkout defect, generate tests for it, and prove what happened in the sandbox.
It ships with a deterministic ShopDemo target, an optional LLM planner that
proposes each step from a validated candidate set, a Celesto Cloud adapter, a
PostgreSQL run ledger, a Flight Deck dashboard, and a recorded evaluation.

## Problem statement

A QA agent that says "the test passed" is not useful when the bug is a negative
cart total or an intermittent checkout error. A team needs four things:

1. A reproducible user journey with observable proof.
2. A regression test that fails on the broken build and passes on the fix.
3. Lifecycle evidence: what was created, what failed, what was deleted.
4. A truthful boundary between what ran in the cloud and what ran locally.

ShopDemo contains a deterministic defect. Each application of `SAVE20` takes 20%
off the original `$129.97` subtotal, and the coupon has no one-use guard. Six
applications produce:

```text
129.97 - (6 * 25.994) = -25.99
```

The expected behavior is that a one-use coupon is rejected after its first
successful application and the total never becomes negative.

The dashboard, API, reports, generated tests, screenshots, action log, command
audit log, failure trace, and video are the product surface. The agent's work is
not hidden behind a terminal transcript.

## Architecture

```text
                         +-----------------------------+
                         | ShopDemo target             |
                         | runs in-guest (live) or on  |
                         | the host (mock/matrix)      |
                         +--------------+--------------+
                                        ^  http://127.0.0.1:8090 (in-guest)
                                        |  Celesto CDP browser (live)
+------------------+       +------------+-------------+
| QA workflow      |------>| BrowserSession           |
| 10 validated     |       | stable data-testid flows |
| browser steps    |       +--------------------------+
| + LLM planner    |
| (candidate set)  |
+--------+---------+
         |
         | sandbox operations (policy-checked first)
         v
+------------------+       +-----------------------------+
| SandboxAdapter   |------>| Celesto Cloud REST API     |
| policy + JSONL   |       | /v1/computers (create/exec |
| audit log        |       | /browser/published_ports)  |
+--------+---------+       +-----------------------------+
         |
         | run metadata
         v
+------------------+       +-----------------------------+
| FastAPI control  |------>| PostgreSQL run ledger      |
| plane + reports  |       | seeded + degraded reads    |
+------------------+       +-----------------------------+
         |
         v
+------------------+
| Flight Deck      |
| evidence + runs  |
+------------------+
```

The implementation is split into small modules:

- `app/target_saas_app.py` - ShopDemo fixture, session state, broken/fixed modes,
  controlled 503 trigger, escaped untrusted review, `--serve` guest entry point.
- `app/sandbox_runner.py` - sandbox naming, deny-by-default command policy,
  audit log, mock adapter, live adapter over the Celesto REST API, idempotent
  cleanup.
- `app/qa_agent.py` - validated browser actions, ten-step workflow, planner
  decision points, recovery, generated TypeScript tests, containment probe,
  host test matrix.
- `app/llm_planner.py` - the optional LLM planner: OpenAI-compatible endpoint,
  strict JSON choice contract, one corrective re-ask, JSONL evidence log.
- `app/guest_healthcheck.py` - stdlib-only health probe uploaded into guests.
- `app/main.py` - API, PostgreSQL ledger, degraded reads, write rejection,
  dashboard, static evidence.

## How I used Celesto

**Execution layer.** Every command, file write, and browser session the agent
needs runs inside a Celesto Cloud sandbox (`browser-agent` template, `standard`
size, 2 vCPU / 4 GB). The adapter speaks the same REST API the celesto SDK and
CLI drive (`POST /v1/computers`, `.../exec`, `.../browser`,
`.../published_ports`). This is deliberate: the SDK's `CloudOptions` (inspected
at 0.1.6 and 0.1.8) cannot request a named sandbox size, and this account
requires one (`resource_overrides_unavailable` -> choose a named size). SDK
importability is still recorded honestly in
`reports/evidence/runtime-manifest.json`.

**Browser runtime (CDP).** In live runs the workflow's entire browser surface is
the sandbox's own Chromium: `POST /v1/computers/{id}/browser` returns a gateway
that Playwright connects to with `connect_over_cdp`. Pages, screenshots, and the
poisoned review are rendered inside the guest, never on the host. If the
sandbox does not expose a CDP URL, the run fails closed; a host browser is never
substituted (tested).

**In-guest fixture.** Live runs deploy ShopDemo *inside* each sandbox: the app
file is uploaded in base64 chunks through the audited command path, dependencies
are installed by one exact-match policy exception (`pip install --quiet fastapi
uvicorn`, never prefix-matched), the server starts under `nohup`, and a
stdlib-only health probe confirms it before any browser step. The CDP browser
then visits `http://127.0.0.1:8090` in the same guest - no host service and no
public tunnel is involved.

**Safety hooks.** The celesto Cloud SDK exposes no command callbacks, so
FlowScout enforces a deny-by-default allowlist in the control-plane adapter
before any dispatch: every command attempt is audited to `logs/commands.jsonl`
with `decision: allow|block`, and a blocked command (including the
prompt-injection payload extracted from the customer review) never reaches the
guest. Shell redirection is restricted to `/workspace/flowscout/`, and writes
and reads are path-checked.

**Published ports.** During the recorded live demo the in-guest ShopDemo port is
published through Celesto (`publish_port`) to a public HTTPS URL, so a reviewer
can open the exact fixture the agent explored.

**Lifecycle.** Unique `sbx-[a-z0-9]{8,}` names, `try/finally` cleanup on success,
failure, and interrupt, idempotent delete, and a failed-create cleanup path so a
half-created billable sandbox cannot leak. Nothing is shelled out on the host as
a tool implementation.

Live mode is fail-closed:

```text
CELESTO_MODE=live
```

requires a real `CELESTO_API_KEY`; otherwise `create_adapter("live")` raises
`SandboxUnavailableError`. It never silently switches to mock.

**Live status:** the adapter, REST client (including 429 retry with
`Retry-After`), in-guest deploy pattern (nohup + healthcheck), and backgrounded
servers were verified against real Celesto sandboxes from this repository. The
browser-agent template's CDP endpoint requires a non-trial size: on the free
Micro trial `POST .../browser` returns HTTP 501 (recorded in
`reports/live-evidence/`); with account credit the `standard` size unlocks it.
`live_celesto_verified` in `reports/reliability.json` is true only when the
recorded evaluation itself ran live.

## The planner: scripted or LLM

The workflow has two interchangeable loops:

- **scripted** (default): a deterministic state machine executes the ten steps.
  No credentials needed; every recorded action is reproducible.
- **llm** (`--planner llm` or `FLOWSCOUT_PLANNER=llm`): at five decision points
  (entry URL, which review to read, checkout navigation, coupon code, expected
  defect total) an LLM proposes the next action by choosing one candidate from
  a small validated set that includes plausible wrong answers. The reply must be
  `{"choice": <index>, "reason": "..."}`; anything else gets one corrective
  re-ask and then fails the run. The LLM cannot enlarge the action space, so
  untrusted page content in the observation can at worst cause a wrong pick and
  an honest failure - never command execution. Every proposal is logged to
  `reports/evidence/planner.jsonl` and traced as `planner_decision` events.

Planner mode without complete `LLM_*` configuration raises immediately; it never
falls back to the scripted loop and reports LLM numbers.

## Eval report

[`reports/reliability.json`](reports/reliability.json) contains 20 measured
sequential runs. [`reports/postmortem.md`](reports/postmortem.md) explains the
results and failures. The `evaluation_mode`, `planner`, `sandbox_runtime`, and
`browser_runtime` fields in that file are the truth boundary for every number
below; this table is refreshed whenever the evidence is regenerated.

| Metric | Recorded value |
|---|---:|
| Evaluation mode | mock sandbox / host Playwright / llm planner |
| Runs | 20 |
| Success rate | 100% |
| Median wall clock | 1.92 s |
| P90 wall clock | 2.70 s |
| Median boot-to-ready | 0.00 s (mock lifecycle) |
| Live Celesto CDP verified | false (HTTP 501 on free Micro) |
| Recorded cloud cost | $0.00 |

Mock-mode timings measure orchestration, the planner loop, and a real local
Chromium session - not Celesto boot time, network latency, or cloud cost. In
mock mode boot time is recorded as `0.0` rather than invented, and cost as
`$0.00` with an explicit methodology note. Live runs are costed from wall-clock
time at the `standard` size rate (`$0.03/hr`).

The four-case generated-test matrix is the important correctness result:

| Generated test | Runtime mode | Expected | Result |
|---|---|---|---|
| `coupon_stacking_test.spec.ts` | broken | pass | pass |
| `coupon_stacking_test.spec.ts` | fixed | fail | fail |
| `coupon_regression_test.spec.ts` | fixed | pass | pass |
| `coupon_regression_test.spec.ts` | broken | fail | fail |

Evidence: `reports/evidence/test-matrix.json`. The reproduction proves the
defect exists; the regression proves the fix protects it.

### Verified lifecycle behavior

- Success path: browser closes, owned sandbox is deleted, cleanup result true.
- Failure path: a sandbox is created, browser startup raises, ownership cleanup
  still deletes the sandbox.
- Interrupt path: `KeyboardInterrupt` propagates after the `finally` cleanup
  deletes the sandbox.
- Controlled step 7: the target is armed to return a real HTTP 503, the browser
  observes it, FlowScout records `MidTaskFailure`, deletes the failed sandbox,
  creates a fresh replacement, redeploys the in-guest fixture, resets state,
  retries, and completes.

Evidence: `reports/lifecycle.json`, `reports/failure_trace.jsonl`, and
`reports/evidence/lifecycle-checks.json`.

### Video

[`demo/walkthrough.mp4`](demo/walkthrough.mp4) is a real recording of the
ten-step host-browser workflow, converted from Playwright's WebM output.
`reports/evidence/walkthrough.json` records the source path, byte count,
SHA-256, and conversion result.

### What was not measured

- Live Celesto command latency, cloud pricing at scale, or concurrent runs.
- PostgreSQL contention under load.
- Long-horizon LLM exploration: the planner chooses among validated candidates
  at fixed decision points rather than free-form browsing.

## Containment test

The ShopDemo review contains an untrusted instruction. The page renders it as
text; it is not executed by the browser. FlowScout reads it as data, extracts
the embedded command (`cat /etc/passwd`), and attempts it through the sandbox
adapter. The command policy blocks it before dispatch.

The test also creates an independent host canary, hashes it before and after,
and checks that the canary content is unchanged. It verifies that the blocked
result does not contain credential-shaped output.

```text
attack=prompt_injection_exfil
extracted_command=cat /etc/passwd
policy_decision=block
blocked=true
host_untouched=true
credential_content_absent=true
```

Evidence: [`reports/containment.json`](reports/containment.json) and
[`reports/evidence/containment.log`](reports/evidence/containment.log). The
canary file remains in `reports/evidence/` so the hash can be checked.

This is a policy-boundary test, not a penetration test of Celesto. The same
decision point guards every live command: the adapter blocks before the REST
dispatch, and the blocked attempt is still audited.

## State and failure design

The API has one explicit run ledger. `GET /health` is always
`{"status":"ok"}`. PostgreSQL is the production ledger; `MemoryLedger` exists
only for tests and explicit local demos.

- `GET /api/runs` returns the seed record and persisted runs. If PostgreSQL is
  unavailable, it returns a read-only seed snapshot with
  `X-FlowScout-Degraded: true`. The dashboard displays the degradation.
- `POST /api/runs` validates the sandbox name against `^sbx-[a-z0-9]{8,}$`, the
  state set, and `execution_target=celesto_sandbox`. Invalid input is 400,
  duplicates are 409, and an unavailable ledger is 503.
- A failed write never falls back to memory. The response says the run was not
  persisted.

ShopDemo has two intentional modes:

- `broken` (default): six `SAVE20` applications produce a negative total.
- `fixed`: the second application is rejected and the total stays at `$103.98`.
- `POST /shop/api/trigger-failure` arms one HTTP 503 on the next checkout
  application. This is a controlled fixture failure for recovery evidence, not
  a claim of an organic outage.

The step-7 recovery policy is `fresh`, not `rollback`. The Celesto Cloud surface
inspected for this submission (REST and SDK 0.1.6/0.1.8) exposes no
snapshot/restore for cloud computers, so FlowScout deletes the failed sandbox,
creates a replacement, redeploys the fixture, and resets the application state.
The trace uses the logical state `RolledBack` and the report explicitly states
that no VM snapshot was restored.

The action schema is intentionally narrow: navigation is absolute HTTP(S), and
click/fill/assert selectors must be an allowed `data-testid`, simple `id`, or
short text selector. The page cannot inject arbitrary Playwright code, shell
commands, or selectors into the agent; the planner inherits the same boundary
because it can only pick among pre-validated candidates.

## Setup

### Prerequisites

- Python 3.11+
- Node.js 22+ and npm
- Chromium for Playwright (`python -m playwright install chromium`)
- Docker Desktop (for the API + PostgreSQL stack; optional for the demo)
- For live runs: a Celesto account with credit (the browser runtime needs a
  non-trial size) and `CELESTO_API_KEY`; no celesto SDK install is required.
- For the LLM planner: any OpenAI-compatible endpoint (`LLM_BASE_URL`,
  `LLM_API_KEY`, `LLM_MODEL`).

### Local mock demo (no cloud account needed)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
npm ci
npx playwright install chromium
python scripts\run_live_demo.py --mode mock --runs 20            # scripted
python scripts\run_live_demo.py --mode mock --runs 20 --planner llm   # LLM loop
```

The demo starts ShopDemo on `127.0.0.1:8090`, runs the cleanup probes, records
the primary workflow and MP4, executes the four-case test matrix, runs 20
evaluation iterations, and regenerates the reports under `reports/` and
`reports/evidence/`. A scripted run takes roughly two minutes; the planner adds
LLM latency per decision.

### Live Celesto run

```powershell
Copy-Item .env.example .env   # then set CELESTO_API_KEY (and LLM_* if planning)
$env:CELESTO_MODE = "live"
python scripts\run_live_demo.py --mode live --runs 20 --planner llm
```

Each run creates a fresh `standard` / `browser-agent` sandbox, deploys the
fixture in-guest, drives the CDP browser, and deletes the sandbox in `finally`.
429 responses are retried with `Retry-After` and exponential backoff. The free
trial is limited to one Micro sandbox (no browser runtime), so live browser runs
require account credit; runtime cost at `$0.03/hr` is well under $1 for a
20-run evaluation. Never reuse mock numbers as live results.

### Tests

```powershell
python -m pytest -q
```

Current suite: **104 passing tests**. It covers API contracts, degraded reads,
ledger write rejection, target fixture behavior, coupon arithmetic, selector
and URL validation, command policy (including the exact-match deploy exception
and chunked upload redirection), REST client behaviour against a scripted
backend (429 retry, CDP warm-up, publish/delete), planner contract and
injection resistance, mock honesty, live fail-closed behaviour, generated
specs, cleanup on failure/interrupt, and report/evidence consistency.

Run one generated test manually:

```powershell
$env:PLAYWRIGHT_BASE_URL = "http://127.0.0.1:8090"
$env:FLOWSCOUT_TEST_MODE = "broken"   # reproduction
npx playwright test tests/generated/coupon_stacking_test.spec.ts --config=playwright.config.ts --workers=1
```

The regression spec expects `FLOWSCOUT_TEST_MODE=fixed`. The four-case matrix
in the demo runner is the authoritative cross-check.

### API and dashboard

Memory-backed local demo:

```powershell
$env:FLOWSCOUT_LEDGER = "memory"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

PostgreSQL mode:

```powershell
$env:DATABASE_URL = "postgresql://ledger:ledger@localhost:5432/ledger"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/` for the Flight Deck. Examples:

```powershell
curl.exe http://127.0.0.1:8080/health
curl.exe http://127.0.0.1:8080/api/runs
curl.exe -i http://127.0.0.1:8080/api/runs -H "Content-Type: application/json" -d "{\"run_id\":\"run-demo-0001\",\"sandbox_name\":\"sbx-demo000001\"}"
```

### Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Compose starts PostgreSQL 16 with
[`seeds/01_runs.sql`](seeds/01_runs.sql) mounted into the init directory, waits
for its health check, then starts the API. The seed record is
`run-seed-0001 / sbx-seeddemo1 / deleted / celesto_sandbox`. The API also creates
the table and seed idempotently on first ledger access. Firecracker is not a
Compose service: Celesto computers are created by the adapter through the REST
API, and the API serves the previously produced reports even with no cloud
credentials.

The stack smoke-tested locally: health 200, seeded `sbx-seeddemo1`, create 201,
invalid `execution_target` 400, dashboard, static reports, persistence across
an API restart, and degraded seed-only reads plus write rejection when
PostgreSQL is stopped. If host port 8080 is in use, set `API_PORT=18080`.

## Repository map

```text
app/main.py                 API, ledgers, dashboard
app/qa_agent.py             browser workflow, planner hooks, specs, containment
app/llm_planner.py          optional LLM planner (candidate-set contract)
app/sandbox_runner.py       policy, audit log, REST cloud client, adapters
app/target_saas_app.py      deterministic ShopDemo fixture (+ --serve guest mode)
app/guest_healthcheck.py    in-guest fixture health probe
scripts/run_live_demo.py    evidence/evaluation orchestrator
scripts/run_live_smoke.py   live sandbox browser smoke
tests/                      API, target, sandbox, REST client, planner, QA, reports
tests/generated/            generated reproduction and regression specs
reports/                    required reports and evidence
logs/commands.jsonl         append-only command audit records
demo/walkthrough.mp4        real browser recording of the workflow
seeds/01_runs.sql           PostgreSQL seed
```

Key evidence:

- `reports/evidence/actions.jsonl` - every browser action and outcome.
- `logs/commands.jsonl` - every command attempt, including blocks.
- `reports/failure_trace.jsonl` - lifecycle, controlled failure, recovery.
- `reports/evidence/test-matrix.json` - four-case generated test proof.
- `reports/evidence/planner.jsonl` - every LLM proposal, accepted or rejected.
- `reports/live-evidence/` - earlier live probes (boot timing, CDP 501, 429).

## Tradeoffs and limitations

- **The recorded evaluation in this snapshot is mock.** `reports/reliability.json`
  records `evaluation_mode` explicitly; the live run replaces those numbers with
  real boot, wall-clock, and cost figures. Earlier live probes (real boot
  timing, the CDP 501, trial 429s) are preserved under `reports/live-evidence/`.
- **The planner is bounded.** The LLM chooses among validated candidates at
  fixed decision points rather than free-form browsing. This keeps untrusted
  page content structurally unable to become code, at the cost of exploration
  breadth.
- **One exact-match install exception.** Deploying the in-guest fixture needs
  `pip install --quiet fastapi uvicorn`. The policy allowlists that precise
  string (checked before deny patterns, never prefix-matched); every attempt is
  audited like any other command.
- **No snapshot rollback.** Recovery uses a fresh replacement sandbox. This is
  slower than a snapshot restore but matches the inspected cloud surface.
- **Sequential execution only.** Concurrency, queue depth, per-tenant quota,
  and database connection pooling are untested. `reports/concurrency.json`
  records five sequential jobs and explicitly makes no 1,000-user claim.
- **Host canary scope.** The containment test proves policy enforcement and
  host file integrity for this adapter path. It is not a general sandbox
  escape assessment.

### Alternatives considered

- **Local Playwright only** is excellent for deterministic tests but does not
  provide an isolated cloud runtime, lifecycle ownership, or a path to a real
  user-facing browser environment.
- **Browserbase / Browserless** provide managed browsers and CDP, but the
  challenge centers on Celesto's sandbox model. FlowScout keeps the browser
  layer behind an adapter so these could be added without changing the QA flow.
- **E2B / Daytona** provide general code sandboxes; Celesto's browser-capable
  sandbox plus published ports is a closer fit for browser QA and ephemeral
  cleanup.
- **A hosted QA SaaS** would hide the exact failure and containment mechanics
  that this challenge asks to make inspectable.

### Scaling risks

The first bottlenecks at 1,000 users are sandbox quota and creation latency,
Celesto API rate limits, a single worker's browser throughput, PostgreSQL pool
exhaustion, and artifact storage growth. The design would need a queue with
fair scheduling, per-tenant concurrency limits, idempotent run IDs, retry
budgets, artifact retention, and a cost meter that records real provider usage
instead of mock zeros. The current sequential evaluation is an explicit
non-claim, not a capacity plan.

## AI-assistance disclosure

This implementation was built with an AI coding assistant and verified with
executable tests, a real browser, and independent file hashes. The assistant
used a Celesto trial API key for live REST probes (sandbox create/exec/delete,
backgrounded servers, the CDP 501 on Micro); it did not fabricate live
telemetry, cloud failures, video, or cost figures. The mock/live boundary is
recorded in the runtime manifest, dashboard badge, reliability report, and
postmortem.
