# FlowScout Postmortem

Generated: 2026-10-24T16:50:38.525683+00:00
Evaluation mode: mock
Browser runtime: host_playwright
Live Celesto verified: false

## Measured summary

- Runs: 20
- Success rate: 100.0%
- Median wall clock: 1.92s
- P90 wall clock: 2.7s
- Median boot-to-ready: 0.0s
- Cost: $0.0

## Failures

No unplanned evaluation failures occurred in the measured runs.

## Controlled step-7 failure

The primary demonstration armed a deterministic target failure immediately before the second coupon application. The browser received HTTP 503. FlowScout recorded MidTaskFailure, deleted the failed sandbox, created a replacement, reset the fixture, retried, and completed six successful applications. This is a recovery demonstration, not a fabricated system failure.

## Test matrix interpretation

- `coupon_stacking_test.spec.ts` in `broken` mode: passed=True, expected=True. reproduction passes on the broken checkout
- `coupon_stacking_test.spec.ts` in `fixed` mode: passed=False, expected=False. reproduction fails once the defect is fixed
- `coupon_regression_test.spec.ts` in `fixed` mode: passed=True, expected=True. regression passes on the fixed checkout
- `coupon_regression_test.spec.ts` in `broken` mode: passed=False, expected=False. regression catches the broken checkout

## Cleanup findings

- Success cleanup: True
- Failure cleanup: True
- Interrupt cleanup: True

## Root causes and limitations

1. The observed product defect is repeated application of a one-use coupon. The fixed path rejects the second application and preserves a non-negative total.
2. Mock sandbox metrics measure orchestration and real browser work, not Celesto boot time, networking, or cloud cost.
3. The current host has no Celesto API key. Live cloud execution is therefore not claimed. The live adapter fails closed when credentials or the SDK are unavailable.
4. Cloud snapshot rollback is not used because the inspected SDK does not expose it on CloudComputer. Recovery uses replacement sandboxes and fixture reset.
5. Evaluation was sequential; concurrency and 1,000-user behavior are untested.

## Corrective actions

1. Run the same workflow on Linux with Celesto credentials and a target URL reachable from the sandbox.
2. Add authenticated target support so private previews can be tested.
3. Add a queue and per-tenant sandbox quota before scaling beyond a single worker.
4. Keep Playwright report artifacts and compare P90 browser latency after live runs.
