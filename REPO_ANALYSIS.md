# FlowScout — Repository Analysis

Analysis date: 2026-11-07. Scope: full working tree (not just `HEAD`).

## 1. What this repo is

A browser-QA agent built for the Celesto sandbox challenge. It reproduces a
deterministic checkout defect (`SAVE20` stacks without limit → `-25.99` total),
generates a reproduction spec and a regression spec, and records lifecycle,
containment, and evaluation evidence.

Stack: Python 3.11 (FastAPI + Playwright), TypeScript (Playwright Test),
PostgreSQL 16, Docker Compose. ~2,600 lines of Python across 10 app modules,
plus a 47 KB orchestration script.

Module split is clean and each file has one job:

| Module | Responsibility | Size |
|---|---|---|
| `app/qa_agent.py` | 10-step workflow, action schema, planner hooks, spec generation, containment | 54 KB |
| `app/sandbox_runner.py` | policy, audit log, REST client, mock + live adapters | 45 KB |
| `app/main.py` | API, PostgreSQL ledger, degraded reads, dashboard | 26 KB |
| `app/target_saas_app.py` | ShopDemo fixture (broken/fixed, 503 trigger, poisoned review) | 17 KB |
| `app/llm_planner.py` | candidate-set planner, strict JSON contract | 9 KB |
| `scripts/run_live_demo.py` | evidence/evaluation orchestrator | 48 KB |

## 2. What I verified (not just read)

| Claim in README | Check | Result |
|---|---|---|
| 120 passing tests | `pytest -q` on Python 3.11 | **120 passed in 3.06 s** |
| 20 runs, 100%, median 35.585 s, p90 36.56 s | recomputed from `reports/reliability.json` | matches |
| Mock mode, `$0.00`, boot `0.0` | `evaluation_mode: mock`, `cost_methodology` | honest |
| LLM planner was actually used | `reports/evidence/planner.jsonl`: 105 proposals, `openai/gpt-oss-120b`, 105 accepted, median 640 ms | real calls, 5 phases × 21 runs |
| 4-case generated test matrix | `reports/evidence/test-matrix.json` contains real Playwright stdout/stack traces | 4/4 expectations met |
| API contract | live smoke: `/health` 200, seed run listed, POST 201, bad `execution_target` 400, dashboard renders, `/shop/` 200 | works |
| No credential leakage | searched `reports/`, `logs/`, `demo/` for the `.env` key value | not present; `.env` untracked and gitignored |

The README's mock/live boundary is unusually disciplined: `live_celesto_verified`
is `false`, the dashboard badge says "MOCK SANDBOX / HOST BROWSER", and
`test_reliability_mode_is_honest` fails the build if a mock report claims live
verification.

## 3. Architecture assessment

**The strongest design decision is the action schema.** `BrowserAction`
(`qa_agent.py:78`) only accepts `data-testid`, simple `id`, or short `text=`
selectors, absolute http(s) navigation, ≤200-char fill values, and a timeout
cap. The LLM planner can only return an index into a pre-validated candidate
list, so untrusted page text can at worst cause a wrong pick — it cannot become
code. That is a real structural boundary, not a prompt instruction.

**Policy is enforced before dispatch**, in the adapter (`sandbox_runner.py:172`),
not after. Every attempt — allowed or blocked — lands in `logs/commands.jsonl`
with `decision`. The containment test then proves the boundary end to end with a
host canary hash.

**Failure handling is honest.** Step 7 arms a real HTTP 503, the run records
`MidTaskFailure`, deletes the sandbox, creates a replacement, redeploys, resets,
and retries. Cleanup is covered on success, failure, and `KeyboardInterrupt`.

**Report tests recompute the numbers.** `test_reliability_stats_match_runs`
recalculates median/P90/success rate from the run rows, so the summary cannot
drift from the evidence. That is the single best quality signal in the repo.

## 4. Findings

### P1 — The working tree is 73 files ahead of the only commit

`git log` has exactly one commit (`0207b54`). `git status` shows +2,440/−1,043
lines across 73 files, including `app/qa_agent.py` (+221), `scripts/run_live_demo.py`
(+364), regenerated reports, and ~40 deleted evidence screenshots.

If this is submitted or reviewed from `HEAD`, the README numbers, the evidence
PNGs, and the code that produced them will not match. **Commit the tree, or
regenerate evidence from `HEAD` and commit that.** There is also no CI config
(`.github/` absent), so nothing enforces the 120-test suite automatically.

### P1 — `reclaim_orphans()` deletes every computer on the account

`sandbox_runner.py:826` lists all cloud computers and deletes any id that is not
owned by this process instance and not listed in `CELESTO_KEEP_COMPUTER_IDS`.
It fires on a `retained_sandbox_limit` 429 during `create()`.

This is account-wide and destructive by default. A second FlowScout process, a
teammate's sandbox, or a demo box left running will be deleted. Scope it by name
prefix + age, or make reclamation explicit opt-in (`CELESTO_RECLAIM_ORPHANS=1`).

### P2 — The failure taxonomy collapses to two values

`DefectClassification` defines six outcomes, but only `CONFIRMED`
(`qa_agent.py:1129`) and `ENVIRONMENT_FAILURE` (line 819, the initial value) are
ever assigned. `SUSPECTED`, `INVALID_TEST`, `BLOCKED_BY_POLICY`, and `PASSED`
are dead code.

Consequence: a defect that fails to reproduce, a planner wrong-pick
(`WELCOME5` instead of `SAVE20`), or a failed assertion is all reported as
"environment_or_infrastructure_failure". That is the least honest bucket for a
product bug that didn't reproduce, and it is the one label the README leans on
for its truth boundary. Classify by exception site: assertion → `INVALID_TEST`
or `SUSPECTED`, `PlannerError` → `INVALID_TEST`, policy block → `BLOCKED_BY_POLICY`.

### P2 — `cleanup_ok` produces false negatives

`_cleanup()` (`qa_agent.py:1301`) does:

```python
if self.adapter.get_info(self.sandbox_name) is not None:
    deleted = self.adapter.delete(self.sandbox_name)
    ok = ok and deleted.ok
else:
    ok = False
```

But `CelestoSandboxAdapter.delete()` and `MockSandboxAdapter.delete()` both
already return `ok=True` for "already absent or never created". So when
`create()` fails before a sandbox exists, cleanup is reported as failed even
though there is nothing to clean — and `run_evaluation` folds `cleanup_ok` into
`success`, turning an infrastructure failure into a double failure.

Fix: drop the `get_info` pre-check and trust the adapter's idempotent `delete()`.

### P2 — Evidence grows without bound and churns in git

- `reports/evidence/actions.jsonl` is **appended** on every action, never rotated:
  851 lines / 188 KB after 20 runs.
- Each evaluation writes ~40 full-page PNGs; `reports/` is 16 MB, with 43 PNGs +
  36 JPGs committed.
- `planner.jsonl` accumulates every proposal (105 rows per 20-run eval).
- The last run deleted ~40 previously committed screenshots, so every re-run
  produces a noisy binary diff.

Cap the JSONL files (keep last N runs), prune screenshots older than the latest
run, or gitignore regenerated binaries and commit a curated evidence set.

### P3 — Docker image contradicts the "SDK not required" claim

`Dockerfile` runs `pip install celesto==0.1.6`, while `requirements.txt` and the
README say the SDK is not required and only builds on Linux. Harmless (it's a
Linux image) but confusing; either document it as "recorded for the runtime
manifest" or drop it.

### P3 — Each guest browser action is a new process

`GuestBrowserSession.execute()` (`qa_agent.py:479`) spawns
`python3 guest_browser_worker.py` per action, which starts Playwright, connects
over CDP, runs one action, and exits. Correct and isolated, but it adds
process + connect overhead to every step, and the batch API
(`_exec_actions`) is only ever called with one action. Batching the whole step
would cut live latency noticeably.

### P3 — Planner has no latency/retry budget

`LLMPlanner` retries once on malformed JSON (`max_attempts=2`) and otherwise
aborts the run with `PlannerError`. At ~640 ms per decision × 5 decisions, the
planner adds ~3 s per run; the reported 100% success rate measures the
constrained loop, not free-form exploration. The README says this plainly, but
`reliability.json` does not record planner latency, so the cost is invisible in
the eval report.

### P3 — Fixture state is per-process memory

`target_saas_app.py:76` keeps `_STATES` in an in-process `OrderedDict` with a
lock and a `MAX_SESSIONS` prune. Fine for a fixture; it silently breaks with
`uvicorn --workers > 1`, and the dashboard/API container is single-worker only.

### P3 — Policy composition allows write-then-exec inside the guest

The allowlist includes `printf `, `echo `, `python3 /workspace/flowscout/`, and
redirection into `/workspace/flowscout/`. Individually reasonable; composed, a
`printf ... > /workspace/flowscout/x.py` followed by
`python3 /workspace/flowscout/x.py` is arbitrary code execution in the guest.
Page content cannot reach `run_command` today (the only path is the containment
test, and `cat /etc/passwd` is denied), so this is latent rather than live. But
any future feature that pipes model or page output into `run_command` inherits
it. Worth an explicit comment or a "no interpreter on files written this run"
rule.

## 5. What is genuinely good

- Deny-by-default command policy checked before dispatch, with an audit record
  for blocks — the rare case where the security claim and the code agree.
- Fail-closed live mode: `create_adapter("live")` without a key raises, and the
  guest-browser fallback refuses to substitute a host browser (tested).
- Generated-spec matrix is a real correctness proof, not a smoke test: the
  reproduction must fail on the fixed build, the regression must fail on the
  broken build.
- Dashboard escapes every field (`_esc`) and ships a strict CSP
  (`default-src 'none'`), which is more than most internal dashboards do.
- The README documents what was *not* measured (concurrency, live boot latency,
  cloud cost) instead of rounding up. That is the differentiator here.

## 6. Suggested next steps, in order

1. Commit the working tree; add a CI job that runs `pytest` and the 4-case
   Playwright matrix.
2. Fix `_cleanup` to use the adapter's idempotent `delete()`.
3. Wire the unused `DefectClassification` values at the real failure sites.
4. Scope or gate `reclaim_orphans()`.
5. Add retention for `actions.jsonl`, `planner.jsonl`, and screenshots.
6. Batch guest browser actions per step.
