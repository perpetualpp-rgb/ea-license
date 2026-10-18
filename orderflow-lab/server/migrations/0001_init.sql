-- Flow Lab schema v1 (plain PostgreSQL 16; TimescaleDB optional, see the end of the file)
--
-- Applied by ``server/migrate.py``, which records it in flowlab.schema_migrations and
-- never applies it twice. Run it as the database owner (not as the API role).
--
-- Conventions
--   * every timestamp is timestamptz and stored in UTC
--   * prices, quantities and money are NUMERIC (no float columns anywhere)
--   * market tables are written by the ingestion role and only read by the API role
--   * per-user tables carry owner_id and Row Level Security keyed on
--     current_setting('app.user_id'), which the API sets per transaction (SET LOCAL);
--     without it a query returns no rows
--   * immutable tables (raw_events, market_snapshots) reject UPDATE/DELETE/TRUNCATE by
--     trigger, and the API role is not granted those privileges either

CREATE SCHEMA IF NOT EXISTS flowlab;
SET LOCAL search_path = flowlab;

-- ---------------------------------------------------------------------------------
-- roles (cluster-wide; created once). Login roles are created by the operator, e.g.
--   CREATE ROLE flowlab_api LOGIN PASSWORD '...' IN ROLE flowlab_app;
--   CREATE ROLE flowlab_ingestor LOGIN PASSWORD '...' IN ROLE flowlab_ingest;
-- ---------------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flowlab_app') THEN
    CREATE ROLE flowlab_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flowlab_ingest') THEN
    CREATE ROLE flowlab_ingest NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;

REVOKE ALL ON SCHEMA flowlab FROM PUBLIC;
GRANT USAGE ON SCHEMA flowlab TO flowlab_app, flowlab_ingest;

-- ---------------------------------------------------------------------------------
-- helper functions
-- ---------------------------------------------------------------------------------

-- The user the current transaction acts for. NULL when app.user_id is unset (or was
-- reset to '' at the end of an earlier transaction on a pooled connection), so every
-- owner_id = app_user_id() comparison is false and RLS returns nothing.
CREATE FUNCTION flowlab.app_user_id() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT nullif(current_setting('app.user_id', true), '')::uuid
$$;
REVOKE ALL ON FUNCTION flowlab.app_user_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION flowlab.app_user_id() TO flowlab_app;

-- Rejects UPDATE/DELETE/TRUNCATE on immutable tables. A retention purge (account
-- deletion, data-licence expiry) may DELETE only when the session sets
-- flowlab.retention_purge = 'on' AND the current role is (a member of) the table owner,
-- which the API role never is.
CREATE FUNCTION flowlab.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE'
     AND current_setting('flowlab.retention_purge', true) = 'on'
     AND pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'MEMBER') THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'flowlab.% is immutable: % rejected', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'restrict_violation';
END $$;

-- Append-only tables that may still lose rows through ON DELETE CASCADE from their
-- parent (personal data deletion) but never change in place.
CREATE FUNCTION flowlab.reject_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'flowlab.% is append-only: UPDATE rejected (add a correction row instead)', TG_TABLE_NAME
    USING ERRCODE = 'restrict_violation';
END $$;

-- ---------------------------------------------------------------------------------
-- market data (shared, no owner)
-- ---------------------------------------------------------------------------------

CREATE TABLE instruments (
  instrument_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  venue               text NOT NULL,                         -- data source, e.g. 'binance'
  symbol              text NOT NULL,                         -- venue symbol, e.g. 'XAUUSDT'
  market_type         text NOT NULL CHECK (market_type IN ('spot', 'perpetual', 'future', 'cfd', 'index')),
  contract_type       text NOT NULL CHECK (contract_type IN ('spot', 'linear', 'inverse')),
  base_asset          text NOT NULL,
  quote_asset         text NOT NULL,
  settlement_asset    text NOT NULL,
  contract_multiplier numeric NOT NULL CHECK (contract_multiplier > 0),
  multiplier_unit     text NOT NULL DEFAULT 'base' CHECK (multiplier_unit IN ('base', 'quote')),
  tick_size           numeric CHECK (tick_size > 0),         -- NULL = not verified yet, never guessed
  qty_step            numeric CHECK (qty_step > 0),
  session_timezone    text NOT NULL DEFAULT 'UTC',           -- IANA name
  session_rules       jsonb NOT NULL DEFAULT '{}'::jsonb,
  spec_status         text NOT NULL DEFAULT 'unverified' CHECK (spec_status IN ('unverified', 'verified')),
  spec_source         text,
  metadata_version    integer NOT NULL DEFAULT 1,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (venue, market_type, symbol),
  UNIQUE (instrument_id, venue, market_type, symbol)
);

-- any change to the contract specification bumps metadata_version
CREATE FUNCTION flowlab.instruments_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.contract_type, NEW.base_asset, NEW.quote_asset, NEW.settlement_asset, NEW.contract_multiplier,
      NEW.multiplier_unit, NEW.tick_size, NEW.qty_step, NEW.session_timezone, NEW.session_rules, NEW.spec_status)
     IS DISTINCT FROM
     (OLD.contract_type, OLD.base_asset, OLD.quote_asset, OLD.settlement_asset, OLD.contract_multiplier,
      OLD.multiplier_unit, OLD.tick_size, OLD.qty_step, OLD.session_timezone, OLD.session_rules, OLD.spec_status) THEN
    NEW.metadata_version := OLD.metadata_version + 1;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER instruments_version BEFORE UPDATE ON instruments
  FOR EACH ROW EXECUTE FUNCTION flowlab.instruments_version();

CREATE TABLE ingestion_runs (
  run_id       uuid PRIMARY KEY,
  tool         text NOT NULL,
  tool_version text NOT NULL,
  source_root  text NOT NULL,
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
  stats        jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- manifest of collector files already loaded (path relative to the data root + checksum)
CREATE TABLE ingested_files (
  relpath       text PRIMARY KEY,
  size_bytes    bigint NOT NULL,
  sha256        text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  rows_read     bigint NOT NULL,
  rows_rejected bigint NOT NULL DEFAULT 0,
  last_run_id   uuid NOT NULL REFERENCES ingestion_runs,
  ingested_at   timestamptz NOT NULL DEFAULT now()
);

-- append-only raw records that have no normalized table yet (meta, mark, oi, liq).
-- event_id is a deterministic UUIDv5 of the source line, so a replay inserts nothing new.
CREATE TABLE raw_events (
  event_id          uuid PRIMARY KEY,
  instrument_id     bigint NOT NULL REFERENCES instruments,
  stream            text NOT NULL,
  exchange_event_ts timestamptz,             -- time stamped by the exchange (NULL for local meta rows)
  received_ts       timestamptz,             -- time stamped by our collector
  source_ids        jsonb NOT NULL DEFAULT '{}'::jsonb,
  schema_version    integer NOT NULL,
  payload           jsonb NOT NULL,          -- the collector row as written (numbers kept exact by jsonb)
  ingestion_run_id  uuid NOT NULL REFERENCES ingestion_runs,
  source_file       text,
  ingested_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX raw_events_stream_received ON raw_events (instrument_id, stream, received_ts);
CREATE INDEX raw_events_stream_exchange ON raw_events (instrument_id, stream, exchange_event_ts);
CREATE TRIGGER raw_events_immutable BEFORE UPDATE OR DELETE ON raw_events
  FOR EACH ROW EXECUTE FUNCTION flowlab.reject_mutation();
CREATE TRIGGER raw_events_no_truncate BEFORE TRUNCATE ON raw_events
  FOR EACH STATEMENT EXECUTE FUNCTION flowlab.reject_mutation();

-- business key: venue + market + symbol + aggregate trade id
CREATE TABLE trades (
  venue            text NOT NULL,
  market_type      text NOT NULL,
  symbol           text NOT NULL,
  agg_trade_id     bigint NOT NULL,
  instrument_id    bigint NOT NULL,
  event_ts         timestamptz NOT NULL,      -- exchange trade time
  price            numeric NOT NULL CHECK (price > 0),
  qty              numeric NOT NULL CHECK (qty > 0),
  taker_side       text NOT NULL CHECK (taker_side IN ('buy', 'sell', 'unknown')),
  aggregation      text NOT NULL DEFAULT 'binance_aggTrade',   -- what one row represents
  quality          text NOT NULL DEFAULT 'ok' CHECK (quality IN ('ok', 'late_arrival', 'suspect')),
  ingestion_run_id uuid REFERENCES ingestion_runs,
  PRIMARY KEY (venue, market_type, symbol, agg_trade_id),
  FOREIGN KEY (instrument_id, venue, market_type, symbol)
    REFERENCES instruments (instrument_id, venue, market_type, symbol)
);
CREATE INDEX trades_instrument_ts ON trades (instrument_id, event_ts);
CREATE INDEX trades_instrument_id ON trades (instrument_id, agg_trade_id);

-- exchange candles (the collector stores closed 1-minute klines)
CREATE TABLE klines (
  instrument_id    bigint NOT NULL REFERENCES instruments,
  interval_s       integer NOT NULL CHECK (interval_s > 0),
  open_time        timestamptz NOT NULL,
  open             numeric NOT NULL,
  high             numeric NOT NULL,
  low              numeric NOT NULL,
  close            numeric NOT NULL,
  volume           numeric NOT NULL CHECK (volume >= 0),     -- base asset
  trade_count      integer,
  taker_buy_volume numeric CHECK (taker_buy_volume >= 0),    -- base asset
  source           text NOT NULL DEFAULT 'exchange_kline',
  ingestion_run_id uuid REFERENCES ingestion_runs,
  PRIMARY KEY (instrument_id, interval_s, open_time)
);

-- order book: per-second bucket summaries from the collector (kind = 'bucket_summary').
-- 'raw_levels' is reserved for raw snapshots/diffs once the collector stores them.
CREATE TABLE book_snapshots (
  instrument_id    bigint NOT NULL REFERENCES instruments,
  kind             text NOT NULL DEFAULT 'bucket_summary' CHECK (kind IN ('bucket_summary', 'raw_levels')),
  captured_ts      timestamptz NOT NULL,      -- collector sample time
  last_update_id   bigint,                    -- exchange book update id applied last
  prev_update_id   bigint,                    -- previous update id (raw diffs only)
  mid              numeric,
  bids             jsonb NOT NULL,            -- [["price","qty"], ...] best first, strings keep decimals exact
  asks             jsonb NOT NULL,
  bucket_size      numeric,
  window_pct       numeric,
  depth_limit      integer,
  synced           boolean NOT NULL,
  ingestion_run_id uuid REFERENCES ingestion_runs,
  PRIMARY KEY (instrument_id, kind, captured_ts)
);

-- stream gaps; a gap keeps its label until the data is actually present
CREATE TABLE data_quality (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instrument_id    bigint NOT NULL REFERENCES instruments,
  stream           text NOT NULL CHECK (stream IN ('trades', 'book', 'kline', 'mark', 'oi', 'liq', 'connection')),
  source_key       text NOT NULL,             -- deterministic key of what produced the row (idempotent upsert)
  reason           text NOT NULL,
  status           text NOT NULL CHECK (status IN ('open', 'recovered', 'partial', 'unrecoverable')),
  gap_start        timestamptz,
  gap_end          timestamptz,               -- NULL while the gap is still open
  first_missing_id bigint,                    -- known ids (trades)
  last_missing_id  bigint,
  expected_count   bigint,                    -- expected window: ids/seconds/minutes that should exist
  present_count    bigint,
  detected_at      timestamptz NOT NULL DEFAULT now(),
  repaired_at      timestamptz,
  details          jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (instrument_id, stream, source_key),
  CHECK (gap_end IS NULL OR gap_start IS NULL OR gap_end >= gap_start),
  CHECK (last_missing_id IS NULL OR first_missing_id IS NULL OR last_missing_id >= first_missing_id)
);
CREATE INDEX data_quality_window ON data_quality (instrument_id, gap_start);

-- versioned derived metrics/events; corrections are new revisions, never overwrites
CREATE TABLE metric_events (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instrument_id    bigint NOT NULL REFERENCES instruments,
  metric_type      text NOT NULL,
  window_start     timestamptz NOT NULL,
  window_end       timestamptz NOT NULL,
  source_watermark timestamptz NOT NULL,      -- newest input the calculation saw
  session_anchor   timestamptz,               -- e.g. CVD accumulation start
  unit             text NOT NULL,
  value            numeric,
  parameters       jsonb NOT NULL DEFAULT '{}'::jsonb,
  parameters_hash  text NOT NULL,
  engine_version   text NOT NULL,
  approximate      boolean NOT NULL DEFAULT false,
  partial          boolean NOT NULL DEFAULT false,
  evidence_ref     jsonb NOT NULL DEFAULT '{}'::jsonb,
  revision         integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
  supersedes_id    bigint REFERENCES metric_events,
  detected_at      timestamptz NOT NULL DEFAULT now(),   -- when the system knew it (no future leakage)
  CHECK (window_end >= window_start),
  UNIQUE (instrument_id, metric_type, window_start, window_end, parameters_hash, engine_version, revision)
);
CREATE INDEX metric_events_window ON metric_events (instrument_id, window_end);

-- ---------------------------------------------------------------------------------
-- members and per-user data (RLS)
-- ---------------------------------------------------------------------------------

CREATE TABLE users (
  id            uuid PRIMARY KEY,             -- UUIDv5(issuer|subject), see app/auth.py
  issuer        text NOT NULL,
  subject       text NOT NULL,
  email         text,
  display_name  text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz,
  UNIQUE (issuer, subject)
);

-- written by billing/admin only; the API can read the caller's rows
CREATE TABLE entitlements (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id    uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  product     text NOT NULL CHECK (product IN ('market_data')),
  level       text NOT NULL CHECK (level IN ('delayed', 'realtime')),
  valid_from  timestamptz NOT NULL DEFAULT now(),
  valid_until timestamptz,
  source      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (valid_until IS NULL OR valid_until > valid_from)
);
CREATE INDEX entitlements_owner ON entitlements (owner_id, product);

CREATE TABLE idempotency_records (
  owner_id     uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  scope        text NOT NULL,
  idem_key     text NOT NULL CHECK (char_length(idem_key) BETWEEN 8 AND 200),
  request_hash text NOT NULL,
  resource_id  uuid,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, scope, idem_key)
);

-- immutable "what the user saw": values are frozen even if the engine changes later
CREATE TABLE market_snapshots (
  id                uuid PRIMARY KEY,
  owner_id          uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  instrument_id     bigint NOT NULL REFERENCES instruments,
  captured_at       timestamptz NOT NULL DEFAULT now(),
  source_event_time timestamptz,              -- newest market event included
  source_watermark  timestamptz,              -- data cut-off the calculation used
  data_access       text NOT NULL CHECK (data_access IN ('delayed', 'realtime')),
  metric_values     jsonb NOT NULL,
  parameters        jsonb NOT NULL DEFAULT '{}'::jsonb,
  engine_version    text NOT NULL,
  schema_version    integer NOT NULL,
  quality_flags     jsonb NOT NULL,
  chart_viewport    jsonb NOT NULL,
  chart_layers      jsonb NOT NULL DEFAULT '[]'::jsonb,
  image_object_key  text,                     -- private object storage key (upload not implemented yet)
  image_sha256      text CHECK (image_sha256 ~ '^[0-9a-f]{64}$'),
  UNIQUE (owner_id, id),
  CHECK ((image_object_key IS NULL) = (image_sha256 IS NULL))
);
CREATE INDEX market_snapshots_owner ON market_snapshots (owner_id, captured_at);
CREATE TRIGGER market_snapshots_immutable BEFORE UPDATE OR DELETE ON market_snapshots
  FOR EACH ROW EXECUTE FUNCTION flowlab.reject_mutation();
CREATE TRIGGER market_snapshots_no_truncate BEFORE TRUNCATE ON market_snapshots
  FOR EACH STATEMENT EXECUTE FUNCTION flowlab.reject_mutation();

-- trades the member entered or imported (never generated from signals or simulations)
CREATE TABLE personal_trades (
  id                    uuid PRIMARY KEY,
  owner_id              uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  instrument_id         bigint REFERENCES instruments,
  venue_label           text NOT NULL,
  symbol                text NOT NULL,
  market_type           text NOT NULL CHECK (market_type IN ('spot', 'perpetual', 'future', 'cfd', 'other')),
  contract_type         text NOT NULL CHECK (contract_type IN ('linear', 'inverse')),
  contract_multiplier   numeric NOT NULL CHECK (contract_multiplier > 0),
  quote_currency        text NOT NULL,
  settlement_currency   text NOT NULL,
  side                  text NOT NULL CHECK (side IN ('long', 'short')),
  initial_risk_amount   numeric CHECK (initial_risk_amount > 0),
  initial_risk_currency text,
  initial_risk_fx_rate  numeric CHECK (initial_risk_fx_rate > 0),   -- risk currency -> settlement currency
  risk_locked_at        timestamptz,          -- set by the first fill; risk cannot change afterwards
  source                text NOT NULL CHECK (source IN ('manual', 'import')),
  source_reference      text,
  status                text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'open', 'partially_closed', 'closed', 'cancelled')),
  funding_status        text NOT NULL CHECK (funding_status IN ('not_applicable', 'recorded', 'unknown')),
  opened_at             timestamptz,
  closed_at             timestamptz,
  notes                 text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_id, id),
  CHECK ((initial_risk_amount IS NULL) = (initial_risk_currency IS NULL))
);
CREATE INDEX personal_trades_owner_closed ON personal_trades (owner_id, closed_at);
CREATE INDEX personal_trades_owner_created ON personal_trades (owner_id, created_at);

-- fills are append-only; a wrong fill is fixed by a correction row (corrects_fill_id)
CREATE TABLE fills (
  id                uuid PRIMARY KEY,
  owner_id          uuid NOT NULL,
  trade_id          uuid NOT NULL,
  ts                timestamptz NOT NULL,
  action            text NOT NULL CHECK (action IN ('entry', 'exit')),
  quantity          numeric NOT NULL CHECK (quantity > 0),     -- contracts/lots/base units
  price             numeric NOT NULL CHECK (price > 0),
  fee               numeric,                                    -- paid (+) or rebate (-); NULL = unknown
  fee_currency      text,
  fee_fx_rate       numeric CHECK (fee_fx_rate > 0),            -- fee currency -> settlement currency
  fx_reference      text,
  import_key        text,
  external_id       text,
  corrects_fill_id  uuid,
  correction_reason text,
  seq               bigint GENERATED ALWAYS AS IDENTITY,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (trade_id, id),
  UNIQUE (owner_id, import_key),
  UNIQUE (corrects_fill_id),
  FOREIGN KEY (owner_id, trade_id) REFERENCES personal_trades (owner_id, id) ON DELETE CASCADE,
  FOREIGN KEY (trade_id, corrects_fill_id) REFERENCES fills (trade_id, id) ON DELETE CASCADE,
  CHECK (fee IS NULL OR fee_currency IS NOT NULL),
  CHECK ((corrects_fill_id IS NULL) = (correction_reason IS NULL))
);
CREATE INDEX fills_trade ON fills (trade_id, ts, seq);
CREATE TRIGGER fills_append_only BEFORE UPDATE ON fills
  FOR EACH ROW EXECUTE FUNCTION flowlab.reject_update();

CREATE TABLE cashflows (
  id           uuid PRIMARY KEY,
  owner_id     uuid NOT NULL,
  trade_id     uuid NOT NULL,
  ts           timestamptz NOT NULL,
  kind         text NOT NULL CHECK (kind IN ('funding', 'rebate', 'fee', 'adjustment')),
  amount       numeric NOT NULL,              -- signed: + credited to the member, - debited
  currency     text NOT NULL,
  fx_rate      numeric CHECK (fx_rate > 0),   -- currency -> settlement currency
  fx_reference text,
  import_key   text,
  note         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_id, import_key),
  FOREIGN KEY (owner_id, trade_id) REFERENCES personal_trades (owner_id, id) ON DELETE CASCADE
);
CREATE INDEX cashflows_trade ON cashflows (trade_id, ts);
CREATE TRIGGER cashflows_append_only BEFORE UPDATE ON cashflows
  FOR EACH ROW EXECUTE FUNCTION flowlab.reject_update();

CREATE TABLE journal_entries (
  id                  uuid PRIMARY KEY,
  owner_id            uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  entry_type          text NOT NULL CHECK (entry_type IN ('observation', 'trade')),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  market_observed_at  timestamptz,
  instrument_id       bigint REFERENCES instruments,
  title               text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
  facts               text,                   -- what was observed
  hypothesis          text,                   -- the member's own hypothesis
  reason_before_entry text,                   -- written before a trade; edits need a reason
  post_review         text,                   -- written after the exit
  lessons             text,
  tags                text[] NOT NULL DEFAULT '{}',
  snapshot_id         uuid,
  personal_trade_id   uuid,
  review_at           timestamptz,
  revision            integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
  client_draft_id     text,                   -- id of the offline draft this came from
  UNIQUE (owner_id, id),
  FOREIGN KEY (owner_id, snapshot_id) REFERENCES market_snapshots (owner_id, id),
  FOREIGN KEY (owner_id, personal_trade_id) REFERENCES personal_trades (owner_id, id)
    ON DELETE SET NULL (personal_trade_id)
);
CREATE INDEX journal_entries_owner_created ON journal_entries (owner_id, created_at DESC, id DESC);

CREATE TABLE journal_revisions (
  entry_id       uuid NOT NULL,
  version        integer NOT NULL CHECK (version >= 1),
  owner_id       uuid NOT NULL,
  author_id      uuid NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('create', 'edit', 'pre_entry_amendment')),
  changed_fields text[] NOT NULL,
  previous       jsonb NOT NULL DEFAULT '{}'::jsonb,
  current        jsonb NOT NULL,
  change_reason  text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entry_id, version),
  FOREIGN KEY (owner_id, entry_id) REFERENCES journal_entries (owner_id, id) ON DELETE CASCADE
);
CREATE TRIGGER journal_revisions_append_only BEFORE UPDATE ON journal_revisions
  FOR EACH ROW EXECUTE FUNCTION flowlab.reject_update();

-- ids of deleted personal records (no content) so other devices drop their cached copy
CREATE TABLE deletion_tombstones (
  owner_id   uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  entity     text NOT NULL,
  entity_id  uuid NOT NULL,
  deleted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, entity, entity_id)
);

CREATE TABLE alert_rules (
  id               uuid PRIMARY KEY,
  owner_id         uuid NOT NULL REFERENCES users ON DELETE CASCADE,
  instrument_id    bigint NOT NULL REFERENCES instruments,  -- asset + venue + market
  name             text CHECK (char_length(name) <= 120),
  metric           text NOT NULL,
  comparator       text NOT NULL CHECK (comparator IN ('gt', 'gte', 'lt', 'lte', 'abs_gt')),
  threshold        numeric NOT NULL,
  threshold_unit   text NOT NULL,
  window_seconds   integer NOT NULL CHECK (window_seconds BETWEEN 60 AND 86400),
  cooldown_seconds integer NOT NULL CHECK (cooldown_seconds BETWEEN 0 AND 604800),
  channel          text NOT NULL CHECK (channel IN ('in_app', 'telegram')),
  enabled          boolean NOT NULL DEFAULT true,
  required_quality text NOT NULL CHECK (required_quality IN ('complete', 'allow_partial')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_id, id)
);

CREATE TABLE alert_events (
  event_id         uuid PRIMARY KEY,
  owner_id         uuid NOT NULL,
  rule_id          uuid NOT NULL,
  dedupe_key       text NOT NULL,
  instrument_id    bigint NOT NULL REFERENCES instruments,
  metric           text NOT NULL,
  comparator       text NOT NULL,
  observed_value   numeric NOT NULL,
  threshold        numeric NOT NULL,          -- copied from the rule at event time
  unit             text NOT NULL,
  window_start     timestamptz NOT NULL,
  window_end       timestamptz NOT NULL,
  observed_at      timestamptz NOT NULL,      -- market time of the observation
  detected_at      timestamptz NOT NULL DEFAULT now(),   -- when the system produced it
  source_watermark timestamptz,
  engine_version   text NOT NULL,
  data_quality     text NOT NULL CHECK (data_quality IN ('complete', 'partial', 'approximate')),
  is_backfill      boolean NOT NULL,
  delivery_policy  text NOT NULL CHECK (delivery_policy IN
                     ('live', 'not_delivered_backfill', 'suppressed_cooldown', 'suppressed_entitlement')),
  message          text NOT NULL,
  acknowledged_at  timestamptz,
  UNIQUE (rule_id, dedupe_key),
  UNIQUE (owner_id, event_id),
  FOREIGN KEY (owner_id, rule_id) REFERENCES alert_rules (owner_id, id) ON DELETE CASCADE
);
CREATE INDEX alert_events_owner ON alert_events (owner_id, observed_at DESC, event_id DESC);

CREATE TABLE alert_deliveries (
  id              uuid PRIMARY KEY,
  owner_id        uuid NOT NULL,
  event_id        uuid NOT NULL,
  channel         text NOT NULL,
  state           text NOT NULL CHECK (state IN ('pending', 'sent', 'failed', 'suppressed')),
  state_reason    text,
  attempts        integer NOT NULL DEFAULT 0,
  last_error      text,
  next_attempt_at timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  delivered_at    timestamptz,
  UNIQUE (event_id, channel),
  FOREIGN KEY (owner_id, event_id) REFERENCES alert_events (owner_id, event_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------------
-- Row Level Security on every per-user table
-- ---------------------------------------------------------------------------------
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_self ON users TO flowlab_app
  USING (id = flowlab.app_user_id()) WITH CHECK (id = flowlab.app_user_id());

ALTER TABLE entitlements ENABLE ROW LEVEL SECURITY;
CREATE POLICY entitlements_read_own ON entitlements FOR SELECT TO flowlab_app
  USING (owner_id = flowlab.app_user_id());

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['idempotency_records', 'market_snapshots', 'personal_trades', 'fills', 'cashflows',
                           'journal_entries', 'journal_revisions', 'deletion_tombstones',
                           'alert_rules', 'alert_events', 'alert_deliveries'] LOOP
    EXECUTE format('ALTER TABLE flowlab.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON flowlab.%I TO flowlab_app '
                   'USING (owner_id = flowlab.app_user_id()) WITH CHECK (owner_id = flowlab.app_user_id())',
                   t || '_owner', t);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------------
-- privileges
-- ---------------------------------------------------------------------------------
-- API role: read market data, own per-user data (RLS narrows every statement)
GRANT SELECT ON instruments, trades, klines, book_snapshots, data_quality, metric_events TO flowlab_app;
GRANT SELECT, INSERT ON users TO flowlab_app;
GRANT UPDATE (email, display_name, last_seen_at) ON users TO flowlab_app;
GRANT SELECT ON entitlements TO flowlab_app;
GRANT SELECT, INSERT ON idempotency_records TO flowlab_app;
GRANT UPDATE (resource_id) ON idempotency_records TO flowlab_app;
GRANT SELECT, INSERT ON market_snapshots TO flowlab_app;            -- immutable: no UPDATE/DELETE
GRANT SELECT, INSERT, UPDATE, DELETE ON personal_trades TO flowlab_app;
GRANT SELECT, INSERT ON fills, cashflows TO flowlab_app;            -- append-only
GRANT SELECT, INSERT, UPDATE, DELETE ON journal_entries TO flowlab_app;
GRANT SELECT, INSERT ON journal_revisions TO flowlab_app;           -- append-only
GRANT SELECT, INSERT ON deletion_tombstones TO flowlab_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON alert_rules TO flowlab_app;
GRANT SELECT, INSERT ON alert_events TO flowlab_app;
GRANT UPDATE (acknowledged_at) ON alert_events TO flowlab_app;
GRANT SELECT, INSERT ON alert_deliveries TO flowlab_app;

-- ingestion role: market tables only, never per-user tables
GRANT SELECT, INSERT, UPDATE ON instruments, ingestion_runs, ingested_files, data_quality TO flowlab_ingest;
GRANT SELECT, INSERT ON raw_events, trades, klines, book_snapshots, metric_events TO flowlab_ingest;

-- ---------------------------------------------------------------------------------
-- TimescaleDB (optional, untested in CI): only when the extension is installed.
-- trades stays a plain table: a hypertable needs event_ts inside every unique key, and
-- the business key (venue, market, symbol, agg_trade_id) deliberately does not have it.
-- ---------------------------------------------------------------------------------
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
    BEGIN
      CREATE EXTENSION IF NOT EXISTS timescaledb;
      PERFORM create_hypertable('flowlab.klines', 'open_time',
                                chunk_time_interval => interval '7 days', if_not_exists => true, migrate_data => true);
      PERFORM create_hypertable('flowlab.book_snapshots', 'captured_ts',
                                chunk_time_interval => interval '1 day', if_not_exists => true, migrate_data => true);
      RAISE NOTICE 'flowlab: TimescaleDB hypertables created for klines and book_snapshots';
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'flowlab: TimescaleDB is available but hypertables were not created: %', SQLERRM;
    END;
  ELSE
    RAISE NOTICE 'flowlab: TimescaleDB not installed; using plain PostgreSQL tables';
  END IF;
END $$;
