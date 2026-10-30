CREATE TABLE IF NOT EXISTS homelab_image_costs (
    client_key_hash text NOT NULL CHECK (client_key_hash = '' OR client_key_hash ~ '^[0-9a-f]{64}$'),
    month date NOT NULL CHECK (EXTRACT(DAY FROM month) = 1),
    total_usd numeric NOT NULL DEFAULT 0 CHECK (total_usd >= 0),
    charged_calls bigint NOT NULL DEFAULT 0 CHECK (charged_calls >= 0),
    sent_80 boolean NOT NULL DEFAULT false,
    sent_100 boolean NOT NULL DEFAULT false,
    PRIMARY KEY (client_key_hash, month)
);
