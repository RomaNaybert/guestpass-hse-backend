CREATE TABLE IF NOT EXISTS events (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS guests (
    id uuid PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES events(id),
    telegram_id bigint NOT NULL CHECK (telegram_id > 0),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    category text NOT NULL DEFAULT 'Гость',
    pass_nonce uuid NOT NULL,
    pass_expires bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, telegram_id),
    UNIQUE (event_id, id)
);
CREATE TABLE IF NOT EXISTS point_entries (
    id uuid PRIMARY KEY,
    event_id uuid NOT NULL,
    guest_id uuid NOT NULL,
    actor_telegram_id bigint NOT NULL CHECK (actor_telegram_id > 0),
    amount integer NOT NULL CHECK (amount > 0 AND amount <= 10000),
    kind text NOT NULL CHECK (kind IN ('check_in')),
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (event_id, guest_id) REFERENCES guests(event_id, id),
    UNIQUE (event_id, guest_id, kind)
);
CREATE INDEX IF NOT EXISTS point_entries_guest_history ON point_entries (guest_id, created_at);
CREATE TABLE IF NOT EXISTS review_spaces (
    actor_telegram_id bigint PRIMARY KEY CHECK (actor_telegram_id > 0),
    event_id uuid NOT NULL REFERENCES events(id),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Upgrade the early private-sandbox schema to one shared demonstration event.
ALTER TABLE review_spaces DROP CONSTRAINT IF EXISTS review_spaces_event_id_key;
