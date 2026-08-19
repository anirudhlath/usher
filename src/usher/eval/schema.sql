-- The quality-eval harness's own schema.
--
-- **Deliberately not an alembic migration.** This is dev tooling: production
-- must never carry it, `alembic heads` must stay at one head, and a migration
-- would create these tables in every deployment for a harness those
-- deployments cannot run. Applied idempotently by `ledger.ensure_schema`.
-- ADR-0039.
--
-- Every statement is `IF NOT EXISTS` or `OR REPLACE`, because this runs at the
-- start of every eval run rather than once. **`DROP ... CASCADE` is the other
-- way to make a file re-appliable and is not available here**: it runs twice
-- as cleanly and takes every previous run's rows with it, which for a trend
-- table is the whole artefact. `tests/unit/test_eval_contract.py` holds the
-- spelling and `tests/integration/test_eval_ledger_postgres.py` holds the
-- rows, because a second apply that raises nothing is exactly what the
-- destructive spelling also produces.

CREATE SCHEMA IF NOT EXISTS eval;

CREATE TABLE IF NOT EXISTS eval.runs (
    id             uuid PRIMARY KEY,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    surface        text        NOT NULL,
    mode           text        NOT NULL,
    verdict        text        NOT NULL,
    reason         text,
    -- The digest is over `inputs` alone. Compared. See fingerprint.py for why
    -- the git sha is in `provenance` instead: digested, every commit would be
    -- incomparable with the last.
    inputs_digest  text        NOT NULL,
    inputs         jsonb       NOT NULL,
    provenance     jsonb       NOT NULL,
    -- Which bars this run actually faced. A bar edited after seeing a number
    -- is a hash change here rather than a git blame nobody reads.
    bars_sha256    text        NOT NULL,
    case_count     integer     NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_eval_runs_surface_started
    ON eval.runs (surface, started_at DESC);

-- One row per metric per stratum. Strata are never averaged together by the
-- harness: ADR-0031 ships two tiers with very different latency profiles and a
-- mean over them describes neither.
CREATE TABLE IF NOT EXISTS eval.scores (
    run_id       uuid    NOT NULL REFERENCES eval.runs(id) ON DELETE CASCADE,
    surface      text    NOT NULL,
    tier         text    NOT NULL,
    metric       text    NOT NULL,
    stratum      text    NOT NULL,
    value        double precision NOT NULL,
    observations integer NOT NULL,
    -- NULL where the bar is `pending` or absent. **Not false**: "no bar to
    -- fail" and "failed a bar" are different facts and a boolean cannot hold
    -- both. `judgement` is the other half of that argument and is NOT NULL --
    -- `Judgement` has four members, of which `pending` and `unbarred` are
    -- judgements rather than absences, so an unbarred metric is stored with
    -- three NULL bar columns and a judgement that says so.
    bar_kind     text,
    bar_low      double precision,
    bar_high     double precision,
    judgement    text    NOT NULL,
    PRIMARY KEY (run_id, surface, tier, metric, stratum)
);

-- What a trend panel reads. `--full` only: a quick run enforces no bar and is
-- a sample, so plotting it beside a full run compares two populations.
--
-- `surface` is the *run's*, not the score's. Both tables carry the column and
-- they agree on every row this harness writes, so the choice is invisible
-- until it is not -- the case that pins it is the one fixture in the suite
-- where the two disagree.
CREATE OR REPLACE VIEW eval.v_trend AS
SELECT r.started_at,
       r.surface,
       s.tier,
       s.metric,
       s.stratum,
       s.value,
       s.judgement,
       r.verdict,
       r.inputs_digest,
       r.bars_sha256
FROM eval.scores s
JOIN eval.runs r ON r.id = s.run_id
WHERE r.mode = 'full';
