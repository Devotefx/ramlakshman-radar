-- ═══════════════════════════════════════════════════════════════
-- DHAN ALPHA — Database Schema
-- PostgreSQL 15+ (TimescaleDB extension recommended for tick data)
-- ═══════════════════════════════════════════════════════════════

-- Enable TimescaleDB if available
-- CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─────────────────────────────────────────────────────────────
-- INSTRUMENTS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE instruments (
    id              SERIAL PRIMARY KEY,
    security_id     VARCHAR(20)  NOT NULL UNIQUE,
    symbol          VARCHAR(50)  NOT NULL,
    name            VARCHAR(100),
    exchange        VARCHAR(20)  NOT NULL,  -- NSE_FNO, NSE_EQ
    instrument_type VARCHAR(20)  NOT NULL,  -- OPTIDX, OPTSTK, FUTIDX, EQ
    lot_size        INTEGER      DEFAULT 1,
    tick_size       DECIMAL(10,2) DEFAULT 0.05,
    expiry_date     DATE,
    strike_price    DECIMAL(12,2),
    option_type     CHAR(2),                -- CE, PE
    underlying_id   VARCHAR(20),
    is_active       BOOLEAN      DEFAULT TRUE,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX idx_instruments_symbol    ON instruments(symbol);
CREATE INDEX idx_instruments_underlying ON instruments(underlying_id);
CREATE INDEX idx_instruments_expiry    ON instruments(expiry_date);


-- ─────────────────────────────────────────────────────────────
-- OHLCV CANDLES (1-min)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE ohlcv_1min (
    time          TIMESTAMPTZ  NOT NULL,
    security_id   VARCHAR(20)  NOT NULL,
    open          DECIMAL(12,2) NOT NULL,
    high          DECIMAL(12,2) NOT NULL,
    low           DECIMAL(12,2) NOT NULL,
    close         DECIMAL(12,2) NOT NULL,
    volume        BIGINT        DEFAULT 0,
    oi            BIGINT        DEFAULT 0,
    vwap          DECIMAL(12,2),
    FOREIGN KEY (security_id) REFERENCES instruments(security_id)
);

-- Convert to TimescaleDB hypertable (if TimescaleDB installed)
-- SELECT create_hypertable('ohlcv_1min', 'time');

CREATE INDEX idx_ohlcv_1min_secid_time ON ohlcv_1min(security_id, time DESC);


-- ─────────────────────────────────────────────────────────────
-- OPTION CHAIN SNAPSHOTS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE option_chain_snapshots (
    id           BIGSERIAL    PRIMARY KEY,
    snapshot_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    underlying   VARCHAR(20)  NOT NULL,
    expiry       DATE         NOT NULL,
    strike       DECIMAL(12,2) NOT NULL,

    ce_ltp       DECIMAL(12,2),
    ce_oi        BIGINT,
    ce_oi_change BIGINT,
    ce_volume    BIGINT,
    ce_iv        DECIMAL(8,4),
    ce_delta     DECIMAL(8,4),
    ce_gamma     DECIMAL(10,6),
    ce_theta     DECIMAL(10,4),
    ce_vega      DECIMAL(10,4),

    pe_ltp       DECIMAL(12,2),
    pe_oi        BIGINT,
    pe_oi_change BIGINT,
    pe_volume    BIGINT,
    pe_iv        DECIMAL(8,4),
    pe_delta     DECIMAL(8,4),
    pe_gamma     DECIMAL(10,6),
    pe_theta     DECIMAL(10,4),
    pe_vega      DECIMAL(10,4)
);

CREATE INDEX idx_oc_snap_underlying_time ON option_chain_snapshots(underlying, snapshot_at DESC);
CREATE INDEX idx_oc_snap_strike         ON option_chain_snapshots(underlying, expiry, strike);

-- Partition by month for performance
-- SELECT create_hypertable('option_chain_snapshots', 'snapshot_at');


-- ─────────────────────────────────────────────────────────────
-- SIGNALS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE signals (
    id            BIGSERIAL    PRIMARY KEY,
    signal_id     VARCHAR(100) NOT NULL UNIQUE,  -- deterministic ID
    generated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    security_id   VARCHAR(20)  NOT NULL,
    underlying    VARCHAR(20)  NOT NULL,
    strategy      VARCHAR(50)  NOT NULL,
    signal_type   VARCHAR(20)  NOT NULL,  -- BUY_CE, BUY_PE
    ltp_at_signal DECIMAL(12,2),
    sl            DECIMAL(12,2),
    target        DECIMAL(12,2),
    rr_ratio      DECIMAL(6,2),
    confidence    INTEGER      CHECK(confidence BETWEEN 0 AND 100),
    triggers      JSONB,
    reason        TEXT,
    status        VARCHAR(20)  DEFAULT 'ACTIVE',  -- ACTIVE, HIT_TARGET, HIT_SL, EXPIRED, CANCELLED
    result_pct    DECIMAL(8,2),
    closed_at     TIMESTAMPTZ
);

CREATE INDEX idx_signals_generated   ON signals(generated_at DESC);
CREATE INDEX idx_signals_underlying  ON signals(underlying, generated_at DESC);
CREATE INDEX idx_signals_status      ON signals(status);
CREATE INDEX idx_signals_strategy    ON signals(strategy);


-- ─────────────────────────────────────────────────────────────
-- SMART MONEY LOG
-- ─────────────────────────────────────────────────────────────
CREATE TABLE smart_money_log (
    id           BIGSERIAL    PRIMARY KEY,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    security_id  VARCHAR(20)  NOT NULL,
    pattern      VARCHAR(30)  NOT NULL,  -- LONG_BUILDUP, SHORT_BUILDUP, etc.
    ltp          DECIMAL(12,2),
    oi           BIGINT,
    oi_change    DECIMAL(8,2),
    price_change DECIMAL(8,4),
    signal_type  VARCHAR(20),
    strength     VARCHAR(10)  -- STRONG, WEAK
);

CREATE INDEX idx_sm_log_detected ON smart_money_log(detected_at DESC);
CREATE INDEX idx_sm_log_security ON smart_money_log(security_id, detected_at DESC);


-- ─────────────────────────────────────────────────────────────
-- NEWS EVENTS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE news_events (
    id           BIGSERIAL    PRIMARY KEY,
    published_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    stock        VARCHAR(20),
    headline     TEXT         NOT NULL,
    category     VARCHAR(30),  -- RBI, SEBI, RESULT, MERGER, POLICY
    bias         VARCHAR(10),  -- BULLISH, BEARISH, NEUTRAL
    keywords     JSONB,
    signal_type  VARCHAR(20),
    confidence   INTEGER,
    vol_spike    DECIMAL(6,2),  -- actual volume spike when event detected
    source       VARCHAR(100)
);

CREATE INDEX idx_news_published  ON news_events(published_at DESC);
CREATE INDEX idx_news_stock      ON news_events(stock, published_at DESC);


-- ─────────────────────────────────────────────────────────────
-- ORDERS (paper/live tracking)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE orders (
    id               BIGSERIAL    PRIMARY KEY,
    dhan_order_id    VARCHAR(50)  UNIQUE,
    placed_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    signal_id        VARCHAR(100) REFERENCES signals(signal_id),
    security_id      VARCHAR(20)  NOT NULL,
    transaction_type VARCHAR(5)   NOT NULL,  -- BUY, SELL
    order_type       VARCHAR(10)  NOT NULL,  -- MARKET, LIMIT, SL
    quantity         INTEGER      NOT NULL,
    price            DECIMAL(12,2),
    trigger_price    DECIMAL(12,2),
    avg_price        DECIMAL(12,2),
    status           VARCHAR(20)  DEFAULT 'PENDING',
    product_type     VARCHAR(15)  DEFAULT 'INTRADAY',
    notes            TEXT
);

CREATE INDEX idx_orders_placed   ON orders(placed_at DESC);
CREATE INDEX idx_orders_signal   ON orders(signal_id);
CREATE INDEX idx_orders_status   ON orders(status);


-- ─────────────────────────────────────────────────────────────
-- DAILY P&L SUMMARY
-- ─────────────────────────────────────────────────────────────
CREATE TABLE daily_pnl (
    id              SERIAL       PRIMARY KEY,
    trade_date      DATE         NOT NULL UNIQUE,
    realized_pnl    DECIMAL(14,2) DEFAULT 0,
    unrealized_pnl  DECIMAL(14,2) DEFAULT 0,
    total_signals   INTEGER      DEFAULT 0,
    successful      INTEGER      DEFAULT 0,
    failed          INTEGER      DEFAULT 0,
    win_rate        DECIMAL(5,2),
    max_drawdown    DECIMAL(14,2),
    peak_capital    DECIMAL(14,2),
    closing_capital DECIMAL(14,2),
    notes           TEXT
);

CREATE INDEX idx_daily_pnl_date ON daily_pnl(trade_date DESC);


-- ─────────────────────────────────────────────────────────────
-- EXPIRY ANALYSIS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE expiry_analysis (
    id            BIGSERIAL    PRIMARY KEY,
    analyzed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    underlying    VARCHAR(20)  NOT NULL,
    expiry_date   DATE         NOT NULL,
    spot          DECIMAL(12,2),
    max_pain      DECIMAL(12,2),
    pcr           DECIMAL(6,3),
    gamma_zones   JSONB,
    pinning_strikes JSONB,
    dealer_gex    DECIMAL(16,2),
    squeeze_type  VARCHAR(30),
    squeeze_signal VARCHAR(20)
);

CREATE INDEX idx_expiry_analyzed ON expiry_analysis(analyzed_at DESC);


-- ─────────────────────────────────────────────────────────────
-- USEFUL VIEWS
-- ─────────────────────────────────────────────────────────────

-- Today's signal performance
CREATE VIEW v_today_signals AS
SELECT
    s.strategy,
    s.signal_type,
    COUNT(*)                              AS total,
    COUNT(*) FILTER (WHERE s.status = 'HIT_TARGET') AS targets_hit,
    COUNT(*) FILTER (WHERE s.status = 'HIT_SL')     AS sl_hit,
    ROUND(AVG(s.confidence), 1)           AS avg_confidence,
    ROUND(AVG(s.result_pct), 2)           AS avg_result_pct
FROM signals s
WHERE DATE(s.generated_at AT TIME ZONE 'Asia/Kolkata') = CURRENT_DATE
GROUP BY s.strategy, s.signal_type
ORDER BY total DESC;


-- Smart money summary
CREATE VIEW v_smart_money_today AS
SELECT
    security_id,
    pattern,
    COUNT(*) AS count,
    MAX(detected_at) AS last_seen,
    ROUND(AVG(oi_change), 2) AS avg_oi_change
FROM smart_money_log
WHERE DATE(detected_at AT TIME ZONE 'Asia/Kolkata') = CURRENT_DATE
GROUP BY security_id, pattern
ORDER BY count DESC;


-- ─────────────────────────────────────────────────────────────
-- REDIS SCHEMA REFERENCE
-- ─────────────────────────────────────────────────────────────
/*
KEY PATTERNS & TTLs:

tick:{security_id}              → JSON tick data                  TTL: 7200s
prev_ltp:{security_id}          → float, last LTP                 TTL: 300s
prev_oi:{security_id}           → int, last OI                    TTL: 3600s
avg_vol:{security_id}           → float, rolling avg volume       TTL: 86400s
orb_sig:{security_id}           → "1" (debounce)                  TTL: 900s
oi_spike_sig:{security_id}      → "1" (debounce)                  TTL: 300s
momentum_sig:{security_id}      → "1" (debounce)                  TTL: 600s
ivr_sig:nifty                   → "1" (debounce)                  TTL: 1800s

chain:{underlying}:latest        → JSON option chain               TTL: 30s
iv:nifty_atm                     → JSON IV data                    TTL: 60s
iv:summary                       → JSON IV summary                 TTL: 60s

signals:feed                     → LIST of JSON signals (LPUSH)    Max: 200 items
smart_money:latest               → JSON SM signals                 TTL: 60s
news:feed                        → LIST of news keys               Max: 100 items
news:{key}                       → JSON news item                  TTL: 3600s
news_sig:{headline_hash}         → "1" dedup                       TTL: 3600s

sm_prev_ltp:{security_id}        → float                          TTL: 3600s
sm_prev_oi:{security_id}         → float                          TTL: 3600s
sm_signal:{security_id}          → JSON SM signal                 TTL: 300s

market:summary                   → JSON market summary             TTL: 30s
*/
