-- Store accounts, sign-in, saved masters and run history.
-- Safe to run on every start: every statement is "IF NOT EXISTS".

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- A store (dealership) is the account. Everything a store saves hangs off it.
CREATE TABLE IF NOT EXISTS stores (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    dealer_code     TEXT NOT NULL DEFAULT '',
    threshold_days  INTEGER NOT NULL DEFAULT 60,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      BIGINT
);

-- A person. Email is stored lowercased. is_owner = runs the service and can
-- open every store.
CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    is_owner        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ,
    -- the store this person worked in last (where the next sign-in lands)
    last_store_id   BIGINT
);

-- Which stores a person belongs to, and as what.
CREATE TABLE IF NOT EXISTS memberships (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store_id    BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by    BIGINT,
    added_email TEXT NOT NULL DEFAULT '',
    -- when this person last opened THIS store (never their activity elsewhere)
    last_used_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, store_id)
);
CREATE INDEX IF NOT EXISTS memberships_store ON memberships (store_id);

-- One-time sign-in links. Only the SHA-256 of the token is kept. An
-- invitation link remembers the store it was sent for.
CREATE TABLE IF NOT EXISTS login_tokens (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    store_id    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    -- which browser used it (hash of its page token), so the second half
    -- of a double click is recognised
    used_by     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS login_tokens_user ON login_tokens (user_id, created_at);

-- One row per signed-in browser. The cookie holds a random id; only its
-- SHA-256 is kept here. Deleting the row signs that browser out for good.
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);

-- Every sign-in request (known email or not), for rate limiting.
CREATE TABLE IF NOT EXISTS login_attempts (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    ip          TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_email ON login_attempts (email, created_at);
CREATE INDEX IF NOT EXISTS login_attempts_ip ON login_attempts (ip, created_at);

-- The store's Core Returns master. Every run saves a new version; exactly
-- one version per store is current.
CREATE TABLE IF NOT EXISTS masters (
    id              BIGSERIAL PRIMARY KEY,
    store_id        BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    content         BYTEA NOT NULL,
    row_count       INTEGER NOT NULL DEFAULT 0,
    unpaid_count    INTEGER,
    unpaid_amount   NUMERIC(14, 2),
    source          TEXT NOT NULL,          -- run | upload | restore
    -- why a version from a run was kept but not made current ('' = normal)
    held_reason     TEXT NOT NULL DEFAULT '',
    run_id          TEXT,
    created_by      BIGINT,
    created_email   TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_current      BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS masters_store ON masters (store_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS masters_one_current
    ON masters (store_id) WHERE is_current;

-- One row per run: who ran it, when, what came out.
CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,   -- the job id
    store_id            BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    user_id             BIGINT REFERENCES users(id) ON DELETE SET NULL,
    user_email          TEXT NOT NULL DEFAULT '',
    kind                TEXT NOT NULL DEFAULT 'reconcile',  -- reconcile | quick
    status              TEXT NOT NULL,      -- running | done | error
    asof                DATE,
    options             JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary             JSONB NOT NULL DEFAULT '{}'::jsonb,
    result              JSONB,
    error               TEXT,
    master_in           BIGINT,
    master_out          BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS runs_store ON runs (store_id, created_at DESC);

-- The result files of a run (workbooks and PDFs).
CREATE TABLE IF NOT EXISTS run_files (
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    size        BIGINT NOT NULL DEFAULT 0,
    content     BYTEA NOT NULL,
    PRIMARY KEY (run_id, name)
);

-- A result file's bytes, in pieces (one statement per piece keeps the
-- database's memory use small whatever the size of the file).
CREATE TABLE IF NOT EXISTS run_file_parts (
    run_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    part        INTEGER NOT NULL,
    content     BYTEA NOT NULL,
    PRIMARY KEY (run_id, name, part),
    FOREIGN KEY (run_id, name) REFERENCES run_files (run_id, name)
        ON DELETE CASCADE
);

-- Who changed what on an account (users added/removed, masters restored...).
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    store_id    BIGINT,
    user_id     BIGINT,
    user_email  TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_store ON audit_log (store_id, created_at DESC);

-- Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS
-- leaves an existing table alone, so each later column is also listed
-- here. New columns go in both places.
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_store_id BIGINT;
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS added_email TEXT NOT NULL DEFAULT '';
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS store_id BIGINT;
ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS used_by TEXT NOT NULL DEFAULT '';
ALTER TABLE masters ADD COLUMN IF NOT EXISTS held_reason TEXT NOT NULL DEFAULT '';
