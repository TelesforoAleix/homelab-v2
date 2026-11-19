CREATE TABLE IF NOT EXISTS homelab_decision_calls (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    called_at timestamptz NOT NULL,
    application text NOT NULL,
    provider text NOT NULL CHECK (provider = 'typesafe'),
    model text,
    question_count integer NOT NULL CHECK (question_count > 0),
    repeats integer NOT NULL CHECK (repeats BETWEEN 1 AND 5),
    input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
    output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
    cost_usd numeric NOT NULL CHECK (cost_usd >= 0),
    status text NOT NULL
);
CREATE INDEX IF NOT EXISTS homelab_decision_calls_called_at
    ON homelab_decision_calls (called_at);
