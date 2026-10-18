-- Flow Lab schema v2: normalized futures market state (mark/index/funding, open interest,
-- liquidations) loaded by ingest.py from the collector's mark / oi / liq rows, and the
-- origin (WebSocket or REST) of each 1m kline.
--
-- Same conventions as 0001: timestamptz in UTC, NUMERIC for every price/size/rate,
-- market tables are written by flowlab_ingest and only read by flowlab_app, no RLS
-- (shared data without owner_id), business keys + ON CONFLICT DO NOTHING make a replay
-- insert nothing. The raw lines stay in raw_events as before; these tables are a typed
-- copy. Like trades/klines they have no retention job yet (0001 has none for market data).

SET LOCAL search_path = flowlab;

-- collector row: mark [E, mark, index, funding, nextFunding]   (futures markPrice@1s, sampled
-- every mark_every_s). The collector writes 0 for a field the stream did not send, so ingest
-- stores index 0 as NULL and the funding rate as NULL when nextFunding is 0 (no schedule).
CREATE TABLE mark_prices (
  instrument_id    bigint NOT NULL REFERENCES instruments,
  event_ts         timestamptz NOT NULL,          -- E: exchange event time
  mark_price       numeric NOT NULL CHECK (mark_price > 0),
  index_price      numeric CHECK (index_price > 0),
  funding_rate     numeric,                       -- predicted rate of the next settlement, as a fraction
                                                  -- (0.0001 = 0.01 %); an estimate, not a settled payment
  next_funding_ts  timestamptz,
  ingestion_run_id uuid REFERENCES ingestion_runs,
  PRIMARY KEY (instrument_id, event_ts)
);

-- collector row: oi [time, openInterest]   (REST /fapi/v1/openInterest every oi_every_s)
CREATE TABLE open_interest (
  instrument_id    bigint NOT NULL REFERENCES instruments,
  event_ts         timestamptz NOT NULL,          -- 'time' of the exchange answer
  open_interest    numeric NOT NULL CHECK (open_interest >= 0),   -- contracts (base asset for USDT-M)
  ingestion_run_id uuid REFERENCES ingestion_runs,
  PRIMARY KEY (instrument_id, event_ts)
);

-- collector row: liq [T, side, price, avgPrice, qty, status]   (forceOrder stream)
-- side is the liquidation ORDER side: 'sell' closes a liquidated long, 'buy' a liquidated short.
-- The stream has no order id, so the key is the order's own fields.
CREATE TABLE liquidations (
  instrument_id    bigint NOT NULL REFERENCES instruments,
  event_ts         timestamptz NOT NULL,          -- T: order trade time
  side             text NOT NULL CHECK (side IN ('buy', 'sell', 'unknown')),
  price            numeric NOT NULL CHECK (price > 0),
  avg_price        numeric CHECK (avg_price > 0), -- NULL when the exchange sent 0 (nothing filled yet)
  qty              numeric NOT NULL CHECK (qty > 0),
  status           text NOT NULL,                 -- exchange order status, e.g. FILLED
  ingestion_run_id uuid REFERENCES ingestion_runs,
  PRIMARY KEY (instrument_id, event_ts, side, price, qty)
);

-- the primary keys (instrument_id, event_ts ...) serve "newest row before t" and range reads;
-- the API also looks up 1m klines by close time, which the klines primary key already covers.

-- privileges: API reads, ingestion writes (no UPDATE/DELETE for either)
GRANT SELECT ON mark_prices, open_interest, liquidations TO flowlab_app;
GRANT SELECT, INSERT ON mark_prices, open_interest, liquidations TO flowlab_ingest;

-- klines.source: the collector now also writes 1m candles fetched over REST (kline_history at
-- start, kline_backfill after a reconnect; same row format). ingest.py tags those minutes from the
-- meta rows so a REST candle is never taken as proof that the WebSocket was up. Values:
--   exchange_kline  candle received on the WebSocket (0001 default)
--   rest_backfill   candle fetched over REST by the collector (history or reconnect backfill)
ALTER TABLE klines ADD CONSTRAINT klines_source_known CHECK (source IN ('exchange_kline', 'rest_backfill'));
GRANT UPDATE (source) ON klines TO flowlab_ingest;

-- TimescaleDB (optional, untested): hypertables keyed on event_ts, as 0001 does for klines/book.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    BEGIN
      PERFORM create_hypertable('flowlab.mark_prices', 'event_ts',
                                chunk_time_interval => interval '7 days', if_not_exists => true, migrate_data => true);
      PERFORM create_hypertable('flowlab.open_interest', 'event_ts',
                                chunk_time_interval => interval '30 days', if_not_exists => true, migrate_data => true);
      PERFORM create_hypertable('flowlab.liquidations', 'event_ts',
                                chunk_time_interval => interval '30 days', if_not_exists => true, migrate_data => true);
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'flowlab: hypertables for market state were not created: %', SQLERRM;
    END;
  END IF;
END $$;
