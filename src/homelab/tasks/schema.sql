-- The durable record of coding tasks. Applied at every worker start, so every statement is
-- idempotent and nothing here may touch a row that already exists.

CREATE TABLE IF NOT EXISTS homelab_roles (
    role text PRIMARY KEY,
    routes jsonb NOT NULL CHECK (jsonb_typeof(routes) = 'array'),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Roles are edited by hand, so the database keeps updated_at honest.
CREATE OR REPLACE FUNCTION homelab_roles_touch() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER homelab_roles_touch BEFORE UPDATE ON homelab_roles
    FOR EACH ROW EXECUTE FUNCTION homelab_roles_touch();

INSERT INTO homelab_roles (role, routes) VALUES
    ('worker', '[{"kind": "litellm", "purpose": "task-worker"}]'),
    ('ticket-reviewer', '[{"kind": "litellm", "purpose": "task-worker"}]'),
    ('side-job', '[{"kind": "litellm", "purpose": "task-worker"}]')
ON CONFLICT (role) DO NOTHING;

CREATE TABLE IF NOT EXISTS homelab_tasks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slice text NOT NULL,
    repository text NOT NULL,
    branch text NOT NULL,
    base_commit text NOT NULL,
    position integer NOT NULL CHECK (position >= 1),
    depends_on bigint[] NOT NULL DEFAULT '{}',
    title text NOT NULL,
    goal text NOT NULL,
    allowed_files text[] NOT NULL,
    check_command text NOT NULL,
    pinned_paths text[] NOT NULL DEFAULT '{}',
    priority integer NOT NULL DEFAULT 0,
    severity text NOT NULL DEFAULT 'normal',
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'escalated')),
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (slice, position)
);

-- role and route are copies, not references: a run stays comparable after its role is edited.
CREATE TABLE IF NOT EXISTS homelab_task_attempts (
    task_id bigint NOT NULL REFERENCES homelab_tasks (id),
    number integer NOT NULL CHECK (number >= 1),
    role text NOT NULL,
    route jsonb NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    reason text,
    run_id text,
    check_exit_code integer,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    PRIMARY KEY (task_id, number)
);

-- attempt is empty for a job that belongs to the task but to no single attempt.
CREATE TABLE IF NOT EXISTS homelab_task_traces (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id bigint NOT NULL REFERENCES homelab_tasks (id),
    attempt integer,
    role text NOT NULL,
    route jsonb NOT NULL,
    kind text NOT NULL CHECK (kind IN ('lead', 'review', 'side')),
    steps integer NOT NULL DEFAULT 0 CHECK (steps >= 0),
    fresh_tokens bigint NOT NULL DEFAULT 0 CHECK (fresh_tokens >= 0),
    cached_tokens bigint NOT NULL DEFAULT 0 CHECK (cached_tokens >= 0),
    output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_usd numeric CHECK (cost_usd >= 0),
    seconds numeric NOT NULL DEFAULT 0 CHECK (seconds >= 0),
    result text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id, attempt) REFERENCES homelab_task_attempts (task_id, number)
);

-- Public observations only. A failed first read has no offer or successful seen_at.
CREATE TABLE IF NOT EXISTS homelab_providers (
    provider text PRIMARY KEY,
    offer jsonb,
    seen_at timestamptz,
    state text NOT NULL CHECK (state IN ('fresh', 'away'))
);

-- Expand only: older submitters and runners may omit these fields.
ALTER TABLE homelab_tasks ADD COLUMN IF NOT EXISTS eval_set text;
ALTER TABLE homelab_tasks ADD COLUMN IF NOT EXISTS eval_case text;
ALTER TABLE homelab_tasks ADD COLUMN IF NOT EXISTS eval_configuration text;
ALTER TABLE homelab_task_attempts ADD COLUMN IF NOT EXISTS configuration_hash text;
CREATE INDEX IF NOT EXISTS homelab_tasks_eval_set_idx
    ON homelab_tasks (eval_set, eval_case) WHERE eval_set IS NOT NULL;

-- Runs own their content. The task bridge stores an empty goal, resolved only at claim.
CREATE SEQUENCE IF NOT EXISTS homelab_run_revision;
CREATE TABLE IF NOT EXISTS homelab_runs (
    id uuid PRIMARY KEY,
    application text NOT NULL,
    request_id text NOT NULL,
    request_hash text NOT NULL,
    goal text NOT NULL,
    request jsonb NOT NULL,
    executor jsonb NOT NULL,
    configuration_hash text NOT NULL,
    task_id bigint UNIQUE REFERENCES homelab_tasks(id),
    predecessors uuid[] NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued', 'running', 'done', 'question', 'waiting', 'escalated', 'failed', 'cancelled')),
    status_reason text CHECK (status_reason IN
        ('usage_limit', 'client_step', 'executor_gave_up', 'check_failed', 'review_rejected',
         'limit_requests', 'limit_minutes', 'limit_usd', 'no_progress', 'provider_lost',
         'outside_allowed_files', 'sandbox_error', 'push_refused', 'interrupted',
         'no_executor_available', 'predecessor', 'cancelled_by_client', 'role_not_available')),
    resume_at timestamptz,
    final_message text,
    commits text[] NOT NULL DEFAULT '{}',
    revision bigint NOT NULL DEFAULT nextval('homelab_run_revision'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (application, request_id)
);
CREATE INDEX IF NOT EXISTS homelab_runs_changes ON homelab_runs(application, revision);
CREATE INDEX IF NOT EXISTS homelab_runs_predecessors ON homelab_runs USING gin(predecessors);

-- Expand only; the host task bridge still handles tickets and evals unchanged.
ALTER TABLE homelab_runs ADD COLUMN IF NOT EXISTS resumable boolean NOT NULL DEFAULT false;
ALTER TABLE homelab_runs ADD COLUMN IF NOT EXISTS session_id text;
ALTER TABLE homelab_runs ADD COLUMN IF NOT EXISTS work_id text;
ALTER TABLE homelab_runs ADD COLUMN IF NOT EXISTS copy_ready boolean NOT NULL DEFAULT false;
ALTER TABLE homelab_runs ADD COLUMN IF NOT EXISTS harness_started boolean NOT NULL DEFAULT false;
ALTER TABLE homelab_runs ADD COLUMN IF NOT EXISTS interruptions integer NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS homelab_run_messages (
    run_id uuid NOT NULL REFERENCES homelab_runs(id),
    message_id text NOT NULL,
    text text NOT NULL,
    consumed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, message_id)
);

-- All bridge writers take this transaction lock before locking task or run rows. Revisions
-- therefore become visible in order; a changed-after cursor cannot skip a later commit.
CREATE OR REPLACE FUNCTION homelab_run_touch() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(157441, 1);
    NEW.revision = nextval('homelab_run_revision');
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER homelab_run_touch BEFORE UPDATE ON homelab_runs
    FOR EACH ROW EXECUTE FUNCTION homelab_run_touch();

-- Synchronize without changing the host runner's protocol. Legacy tasks have no run row.
CREATE OR REPLACE FUNCTION homelab_run_task_changed() RETURNS trigger AS $$
DECLARE
    run_id uuid;
    run_status text;
    run_reason text;
BEGIN
    SELECT id, status INTO run_id, run_status FROM homelab_runs WHERE task_id = NEW.id;
    IF run_id IS NULL OR run_status = 'cancelled' THEN RETURN NEW; END IF;
    -- Whole-run conversations own their state, including waiting and resumable escalation.
    IF EXISTS (SELECT 1 FROM homelab_runs WHERE id = run_id
               AND request->>'role' IN ('slice', 'cut', 'review')
               AND request->>'eval' IS NULL) THEN RETURN NEW; END IF;
    run_reason = CASE
        WHEN NEW.reason IN ('check_failed', 'review_rejected', 'outside_allowed_files', 'interrupted')
            THEN NEW.reason
        WHEN NEW.reason IN ('no_usable_route', 'no_usable_review_route')
            THEN 'no_executor_available'
        WHEN NEW.reason LIKE 'push%' THEN 'push_refused'
        WHEN NEW.reason LIKE 'check%' THEN 'check_failed'
        WHEN NEW.reason LIKE 'review%' THEN 'review_rejected'
        WHEN NEW.reason = 'runner_died' THEN 'interrupted'
        WHEN NEW.reason IN ('request_limit', 'limit_requests') THEN 'limit_requests'
        WHEN NEW.reason IN ('timeout', 'limit_minutes') THEN 'limit_minutes'
        WHEN NEW.status IN ('failed', 'escalated') THEN 'executor_gave_up'
        ELSE NULL END;
    UPDATE homelab_runs SET status = NEW.status, status_reason = run_reason,
        started_at = NEW.started_at, finished_at = NEW.finished_at
        WHERE id = run_id;
    IF NEW.status IN ('failed', 'escalated') THEN
        -- Cancel the entire descendant set, including children not yet given a queue job.
        WITH RECURSIVE descendants AS (
            SELECT id FROM homelab_runs WHERE run_id = ANY(predecessors)
            UNION SELECT r.id FROM homelab_runs r JOIN descendants d
                ON d.id = ANY(r.predecessors)
        )
        UPDATE homelab_runs SET status = 'cancelled', status_reason = 'predecessor',
            finished_at = clock_timestamp()
            WHERE id IN (SELECT id FROM descendants) AND status = 'queued';
        UPDATE homelab_tasks SET status = 'failed', reason = 'predecessor',
            finished_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE id IN (SELECT task_id FROM homelab_runs
                         WHERE status = 'cancelled' AND status_reason = 'predecessor')
                AND status = 'queued';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER homelab_run_task_changed AFTER UPDATE ON homelab_tasks
    FOR EACH ROW EXECUTE FUNCTION homelab_run_task_changed();

CREATE OR REPLACE FUNCTION homelab_run_attempt_changed() RETURNS trigger AS $$
BEGIN
    UPDATE homelab_runs SET updated_at = clock_timestamp() WHERE task_id = NEW.task_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER homelab_run_attempt_changed
    AFTER INSERT OR UPDATE ON homelab_task_attempts
    FOR EACH ROW EXECUTE FUNCTION homelab_run_attempt_changed();
CREATE OR REPLACE TRIGGER homelab_run_trace_changed
    AFTER INSERT OR UPDATE ON homelab_task_traces
    FOR EACH ROW EXECUTE FUNCTION homelab_run_attempt_changed();
