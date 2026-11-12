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
