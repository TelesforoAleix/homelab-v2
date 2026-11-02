CREATE TABLE IF NOT EXISTS homelab_video_jobs (
    id uuid PRIMARY KEY,
    client_key_hash text NOT NULL CHECK (client_key_hash ~ '^[0-9a-f]{64}$'),
    operation text NOT NULL,
    model text NOT NULL,
    reference jsonb,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    cost_usd numeric CHECK (cost_usd >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
