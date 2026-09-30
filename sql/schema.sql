-- Royalty settlement service - schema
-- All monetary amounts are integers in the smallest currency unit (e.g. cents).
-- Agreement validity intervals are half-open: [valid_from, valid_to).

-- btree_gist ships with the official postgres Docker image (postgresql-contrib).
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Right holders ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rightsholders (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Repertoire / works ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS works (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Royalty split agreements ----------------------------------------------------
CREATE TABLE IF NOT EXISTS agreements (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    work_id     BIGINT NOT NULL REFERENCES works(id) ON DELETE RESTRICT,
    valid_from  TIMESTAMPTZ NOT NULL,
    valid_to    TIMESTAMPTZ,                    -- NULL = open-ended
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT agreements_to_after_from CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- For a given work, validity intervals must never overlap.
-- Half-open ranges [valid_from, COALESCE(valid_to,'infinity')) guarantee that
-- an agreement ending exactly when the next starts is allowed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agreements_work_interval
    ON agreements USING gist (
        work_id,
        tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')
    );

-- Right holder shares inside one agreement ------------------------------------
CREATE TABLE IF NOT EXISTS agreement_shares (
    agreement_id     BIGINT NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    rightsholder_id  BIGINT NOT NULL REFERENCES rightsholders(id) ON DELETE RESTRICT,
    ratio            NUMERIC(12, 6) NOT NULL,
    PRIMARY KEY (agreement_id, rightsholder_id),
    CONSTRAINT shares_ratio_positive CHECK (ratio > 0)
);

CREATE OR REPLACE FUNCTION assert_agreement_ratio_total()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    total NUMERIC(15, 6);
BEGIN
    SELECT SUM(ratio) INTO total
      FROM agreement_shares
     WHERE agreement_id = COALESCE(NEW.agreement_id, OLD.agreement_id);

    IF total IS DISTINCT FROM 100 THEN
        RAISE EXCEPTION 'agreement % ratio total must equal 100, got %',
            COALESCE(NEW.agreement_id, OLD.agreement_id), COALESCE(total, 0)
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_shares_total ON agreement_shares;
CREATE CONSTRAINT TRIGGER trg_shares_total
    AFTER INSERT OR UPDATE OR DELETE ON agreement_shares
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION assert_agreement_ratio_total();

-- Plays -----------------------------------------------------------------------
-- A correction chain: corrects_id points to the record being corrected, and is
-- itself unique, so a record can never be corrected by more than one new record
-- (no forking). New records only point at existing rows, so cycles cannot be
-- created either. Only the chain tail has no child and participates in billing.
CREATE TABLE IF NOT EXISTS play_records (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    work_id       BIGINT NOT NULL REFERENCES works(id) ON DELETE RESTRICT,
    occurred_at   TIMESTAMPTZ NOT NULL,
    amount        BIGINT NOT NULL CONSTRAINT play_amount_nonneg CHECK (amount >= 0),
    content_hash  TEXT NOT NULL,
    corrects_id   BIGINT UNIQUE REFERENCES play_records(id) ON DELETE RESTRICT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_play_work_time ON play_records (work_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_play_corrects ON play_records (corrects_id);
CREATE INDEX IF NOT EXISTS ix_agreements_work ON agreements (work_id, valid_from, valid_to);

-- Settlement periods / statements ---------------------------------------------
CREATE TABLE IF NOT EXISTS settlement_periods (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_from TIMESTAMPTZ NOT NULL,
    period_to   TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT settlement_period_half_open CHECK (period_to > period_from),
    CONSTRAINT settlement_period_unique EXCLUDE USING btree (
        period_from WITH =,
        period_to   WITH =
    )
);

CREATE TABLE IF NOT EXISTS statements (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_id    BIGINT NOT NULL REFERENCES settlement_periods(id) ON DELETE RESTRICT,
    seq          INTEGER NOT NULL,          -- 0 = initial, 1.. =追补
    kind         TEXT NOT NULL,            -- 'initial' | 'adjustment'
    doc_no       TEXT NOT NULL UNIQUE,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT statement_seq_unique UNIQUE (period_id, seq)
);

CREATE TABLE IF NOT EXISTS statement_items (
    statement_id     BIGINT NOT NULL REFERENCES statements(id) ON DELETE RESTRICT,
    rightsholder_id  BIGINT NOT NULL REFERENCES rightsholders(id) ON DELETE RESTRICT,
    rightsholder_code TEXT NOT NULL,      -- snapshot for stable documents
    -- Initial statement: the payable amount. Adjustment: signed delta.
    amount           BIGINT NOT NULL,
    PRIMARY KEY (statement_id, rightsholder_id)
);

-- Snapshot of plays that had no matching agreement at issuance time.
CREATE TABLE IF NOT EXISTS statement_pending (
    statement_id  BIGINT NOT NULL REFERENCES statements(id) ON DELETE RESTRICT,
    play_id       BIGINT NOT NULL REFERENCES play_records(id) ON DELETE RESTRICT,
    code          TEXT NOT NULL,
    work_code     TEXT NOT NULL,
    work_title    TEXT NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL,
    amount        BIGINT NOT NULL,
    PRIMARY KEY (statement_id, play_id)
);
