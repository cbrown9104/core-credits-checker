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
    created_by      BIGINT,
    -- A store that signed itself up waits here until the owner of the
    -- service approves it. Only the owner can open a waiting store.
    waiting         BOOLEAN NOT NULL DEFAULT FALSE
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
    used_by     TEXT NOT NULL DEFAULT '',
    -- A link from the sign-up page carries the store it sets up. The store
    -- is made when the link is used, not before.
    signup_name   TEXT,
    signup_dealer TEXT NOT NULL DEFAULT ''
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
    source          TEXT NOT NULL,          -- run | upload | restore | edit
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
ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS signup_name TEXT;
ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS signup_dealer TEXT NOT NULL DEFAULT '';
ALTER TABLE masters ADD COLUMN IF NOT EXISTS held_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE stores ADD COLUMN IF NOT EXISTS waiting BOOLEAN NOT NULL DEFAULT FALSE;

-- ── Shortage Check ──────────────────────────────────────────────────────
-- Mopar parts invoices against the receipts in the store's DMS.

-- A store's settings for the tool. The row is made with the first invoice.
CREATE TABLE IF NOT EXISTS sc_settings (
    store_id        BIGINT PRIMARY KEY REFERENCES stores(id) ON DELETE CASCADE,
    -- days to file a claim, counted from the invoice date
    default_days    INTEGER NOT NULL DEFAULT 14,
    -- a different number of days for one PDC: {"03181": 7}
    pdc_days        JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- part numbers that are never counted as short
    ignored_parts   JSONB NOT NULL DEFAULT '[]'::jsonb
);

-- One row per invoice number per store.
CREATE TABLE IF NOT EXISTS sc_invoices (
    id              BIGSERIAL PRIMARY KEY,
    store_id        BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    number          TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    invoice_date    DATE,
    facing_pdc      TEXT NOT NULL DEFAULT '',
    -- NET INVOICE AMOUNT as printed (NULL: the copy had no SUMMARY page)
    net_total       NUMERIC(14, 2),
    -- the part lines after discount, plus the charges
    lines_total     NUMERIC(14, 2) NOT NULL DEFAULT 0,
    -- the SUMMARY page was read and the lines and charges add up to it
    complete        BOOLEAN NOT NULL DEFAULT FALSE,
    -- charges that are not parts: {"ARC45012": ["TRANSPORTATION CHARGE", 602.51]}
    charges         JSONB NOT NULL DEFAULT '{}'::jsonb,
    pages           INTEGER NOT NULL DEFAULT 0,
    filename        TEXT NOT NULL DEFAULT '',
    done            BOOLEAN NOT NULL DEFAULT FALSE,
    created_by      BIGINT,
    created_email   TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_id, number)
);

-- One row per part line of an invoice. received, how, hint, state and
-- deadline are worked out again after every upload. status, claim_no
-- and note are what the store typed.
CREATE TABLE IF NOT EXISTS sc_lines (
    id              BIGSERIAL PRIMARY KEY,
    invoice_id      BIGINT NOT NULL REFERENCES sc_invoices(id) ON DELETE CASCADE,
    store_id        BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    page            INTEGER NOT NULL DEFAULT 0,
    pdc             TEXT NOT NULL DEFAULT '',
    shipper         TEXT NOT NULL DEFAULT '',
    order_no        TEXT NOT NULL DEFAULT '',
    order_type      TEXT NOT NULL DEFAULT '',
    order_date      DATE,
    part            TEXT NOT NULL,
    -- the number that was ordered, when the invoice says it was superseded
    alt_part        TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    qty             INTEGER NOT NULL DEFAULT 0,
    unit_price      NUMERIC(12, 2) NOT NULL DEFAULT 0,
    net             NUMERIC(14, 2) NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL DEFAULT 'part',   -- part | core | free
    detail          TEXT NOT NULL DEFAULT '',
    received        INTEGER NOT NULL DEFAULT 0,
    how             TEXT NOT NULL DEFAULT '',
    hint            TEXT NOT NULL DEFAULT '',
    -- short | partial | received | core | ignored | none
    state           TEXT NOT NULL DEFAULT 'short',
    deadline        DATE,
    -- '' | review | filed | paid | denied | found
    status          TEXT NOT NULL DEFAULT '',
    claim_no        TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    changed_email   TEXT NOT NULL DEFAULT '',
    changed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sc_lines_invoice ON sc_lines (invoice_id, seq);
CREATE INDEX IF NOT EXISTS sc_lines_store ON sc_lines (store_id, state);

-- One row per receipt row of the DMS file. rec_key is the DMS record id,
-- so loading the same file again adds nothing.
CREATE TABLE IF NOT EXISTS sc_receipts (
    store_id        BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    rec_key         TEXT NOT NULL,
    day             DATE NOT NULL,
    part            TEXT NOT NULL,
    part_raw        TEXT NOT NULL DEFAULT '',
    order_no        TEXT NOT NULL DEFAULT '',
    shipper         TEXT NOT NULL DEFAULT '',
    qty             INTEGER NOT NULL,
    PRIMARY KEY (store_id, rec_key)
);

-- How many rows of the DMS file each day has (the coverage calendar).
CREATE TABLE IF NOT EXISTS sc_days (
    store_id        BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    day             DATE NOT NULL,
    row_count       INTEGER NOT NULL DEFAULT 0,
    receipts        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (store_id, day)
);

-- Every file sent to the tool and what became of it.
CREATE TABLE IF NOT EXISTS sc_uploads (
    id              BIGSERIAL PRIMARY KEY,
    store_id        BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT '',   -- invoice | receipts | ''
    result          TEXT NOT NULL,              -- loaded | already | cut_off | refused
    message         TEXT NOT NULL DEFAULT '',
    user_email      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sc_uploads_store ON sc_uploads (store_id, id DESC);

-- ── Unpaid Cores ────────────────────────────────────────────────────────
-- What a store typed on a control ticket of its master: a status and a
-- note. The ticket's own facts (shipment, part, amount, the date credit
-- was requested) stay in the master workbook.
CREATE TABLE IF NOT EXISTS core_notes (
    store_id        BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    -- the control ticket as the master has it, in capitals
    ticket          TEXT NOT NULL,
    -- '' | review | denied | closed
    status          TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    changed_email   TEXT NOT NULL DEFAULT '',
    changed_at      TIMESTAMPTZ,
    PRIMARY KEY (store_id, ticket)
);
