-- FlowScout Run Ledger Schema
-- Idempotent initialization for PostgreSQL

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    sandbox_name    TEXT NOT NULL,
    problem_id      TEXT,
    state           TEXT NOT NULL DEFAULT 'created'
                    CHECK (state IN ('created','ready','running','succeeded','failed','deleted')),
    execution_target TEXT NOT NULL DEFAULT 'celesto_sandbox'
                    CHECK (execution_target = 'celesto_sandbox'),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed record required by challenge specification
INSERT INTO runs (run_id, sandbox_name, state, execution_target)
VALUES ('run-seed-0001', 'sbx-seeddemo1', 'deleted', 'celesto_sandbox')
ON CONFLICT DO NOTHING;
