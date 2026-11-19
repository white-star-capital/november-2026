## 1. One row per position per day we actually LP'd

```sql
WITH live AS (
  SELECT
    timezone('UTC', timestamp)::date AS lp_day,
    timestamp,
    strategy_output_id,
    in_range,
    lp_notional_usd,
    anchor_notional_usd,
    pool_price,
    current_price,
    NULLIF(snapshot->'uni'->>'liquidity', '')::numeric AS liquidity,
    NULLIF(snapshot->'uni'->>'feesUsd', '')::numeric AS fees_usd,
    NULLIF(snapshot->'uni'->>'feeToken0Human', '')::numeric AS fee_token0,
    NULLIF(snapshot->'uni'->>'feeToken1Human', '')::numeric AS fee_token1,
    NULLIF(snapshot->'uni'->>'pnlUsd', '')::numeric AS lp_pnl_usd,
    NULLIF(snapshot->'uni'->>'netLpPnlUsd', '')::numeric AS net_lp_pnl_usd,
    NULLIF(snapshot->'uni'->>'grossIlUsd', '')::numeric AS gross_il_usd,
    NULLIF(snapshot->'uni'->>'tickLower', '')::int AS tick_lower,
    NULLIF(snapshot->'uni'->>'tickUpper', '')::int AS tick_upper,
    NULLIF(snapshot->'uni'->>'priceLower', '')::numeric AS price_lower,
    NULLIF(snapshot->'uni'->>'priceUpper', '')::numeric AS price_upper,
    snapshot->'uni'->>'token0Symbol' AS token0,
    snapshot->'uni'->>'token1Symbol' AS token1,
    NULLIF(snapshot->'uni'->>'token0Amount', '')::numeric AS token0_amount,
    NULLIF(snapshot->'uni'->>'token1Amount', '')::numeric AS token1_amount,
    snapshot->'uni'->>'asset' AS asset,
    snapshot->'uni'->>'feeSource' AS fee_source
  FROM historical_dnclmm_position_snapshot
  WHERE COALESCE(lp_notional_usd, 0) > 0
     OR COALESCE(NULLIF(snapshot->'uni'->>'liquidity', '')::numeric, 0) > 0
),
fee_steps AS (
  SELECT
    lp_day,
    strategy_output_id,
    token1,
    COALESCE(current_price, pool_price) AS px,
    fee_token0,
    fee_token1,
    LAG(fee_token0) OVER w AS prev_fee0,
    LAG(fee_token1) OVER w AS prev_fee1
  FROM live
  WHERE fee_token0 IS NOT NULL AND fee_token1 IS NOT NULL
  WINDOW w AS (PARTITION BY strategy_output_id ORDER BY timestamp)
),
fee_accrual AS (
  SELECT
    *,
    CASE WHEN prev_fee0 IS NULL THEN NULL
         WHEN fee_token0 >= prev_fee0 THEN fee_token0 - prev_fee0
         ELSE fee_token0 END AS acc0,   -- balance fell: collected or re-minted
    CASE WHEN prev_fee1 IS NULL THEN NULL
         WHEN fee_token1 >= prev_fee1 THEN fee_token1 - prev_fee1
         ELSE fee_token1 END AS acc1
  FROM fee_steps
),
accrued AS (
  SELECT
    lp_day,
    strategy_output_id,
    SUM(acc0) AS fees_accrued_token0,
    SUM(acc1) AS fees_accrued_token1,
    SUM(CASE WHEN acc0 IS NULL AND acc1 IS NULL THEN NULL
             WHEN token1 IN ('USDC', 'USDT', 'USD₮0', 'USDC.e', 'DAI')
             THEN COALESCE(acc0, 0) * px + COALESCE(acc1, 0) END) AS fees_accrued_usd
  FROM fee_accrual
  GROUP BY lp_day, strategy_output_id
),
eod AS (
  SELECT DISTINCT ON (strategy_output_id, lp_day)
    *
  FROM live
  ORDER BY strategy_output_id, lp_day, timestamp DESC
)
SELECT
  e.lp_day,
  e.strategy_output_id,
  e.asset,
  e.token0,
  e.token1,
  e.tick_lower,
  e.tick_upper,
  e.price_lower,
  e.price_upper,
  e.in_range,
  e.pool_price,
  e.current_price,
  e.liquidity,
  e.token0_amount,
  e.token1_amount,
  e.lp_notional_usd,
  e.anchor_notional_usd,
  e.lp_pnl_usd,
  e.gross_il_usd,
  e.net_lp_pnl_usd,
  e.fees_usd AS unclaimed_fees_usd_eod,
  a.fees_accrued_usd,
  e.fee_token0,
  e.fee_token1,
  e.fee_source,
  e.timestamp AS snapshot_at,
  a.fees_accrued_token0,
  a.fees_accrued_token1
FROM eod e
LEFT JOIN accrued a
  ON a.strategy_output_id = e.strategy_output_id
 AND a.lp_day = e.lp_day
ORDER BY e.lp_day, e.strategy_output_id;
```

Change `'UTC'` to `'America/New_York'` if the charts should follow the New York day.

**Fee accrual (changed 18 Nov 2026).** Fees are accrued in token units, then valued in USD at the snapshot's price.
The previous version differenced `feesUsd` (the USD value of the unclaimed balance) and kept only positive changes.
Because part of that balance is ETH or WBTC, its USD value moves with the price, so every uptick counted as income and
every downtick was dropped. On the 18 Nov export that overstated fees by about 21% (30% in September).
Now, per position and per consecutive pair of snapshots that both have fee balances:

- if a token's unclaimed balance rose, the increase is the accrual for that token;
- if it fell, fees were collected (or the position was re-minted), and the new balance is what accrued since;
- the token0 accrual is valued at `COALESCE(current_price, pool_price)` (the price `feesUsd` itself uses) and token1
  at 1, since token1 is a stablecoin in every pool here; other pairs get a NULL USD value instead of a guess.

Snapshots with no fee balances (`feeSource = error`) are skipped, so the next good snapshot is differenced against the
last good one. Fees accrued and collected between two snapshots are not seen; with ~15-minute snapshots this is small.

## 2. One row per day for the charts

Same filter. A day appears only when at least one position had liquidity or notional that day.

```sql
WITH live AS (
  SELECT
    timezone('UTC', timestamp)::date AS lp_day,
    timestamp,
    strategy_output_id,
    in_range,
    lp_notional_usd,
    NULLIF(snapshot->'uni'->>'liquidity', '')::numeric AS liquidity,
    pool_price,
    current_price,
    NULLIF(snapshot->'uni'->>'feesUsd', '')::numeric AS fees_usd,
    NULLIF(snapshot->'uni'->>'feeToken0Human', '')::numeric AS fee_token0,
    NULLIF(snapshot->'uni'->>'feeToken1Human', '')::numeric AS fee_token1,
    snapshot->'uni'->>'token1Symbol' AS token1,
    NULLIF(snapshot->'uni'->>'pnlUsd', '')::numeric AS lp_pnl_usd,
    NULLIF(snapshot->'uni'->>'grossIlUsd', '')::numeric AS gross_il_usd,
    NULLIF(snapshot->'uni'->>'netLpPnlUsd', '')::numeric AS net_lp_pnl_usd
  FROM historical_dnclmm_position_snapshot
  WHERE COALESCE(lp_notional_usd, 0) > 0
     OR COALESCE(NULLIF(snapshot->'uni'->>'liquidity', '')::numeric, 0) > 0
),
fee_accrual AS (
  SELECT
    lp_day,
    token1,
    COALESCE(current_price, pool_price) AS px,
    CASE WHEN prev_fee0 IS NULL THEN NULL
         WHEN fee_token0 >= prev_fee0 THEN fee_token0 - prev_fee0
         ELSE fee_token0 END AS acc0,
    CASE WHEN prev_fee1 IS NULL THEN NULL
         WHEN fee_token1 >= prev_fee1 THEN fee_token1 - prev_fee1
         ELSE fee_token1 END AS acc1
  FROM (
    SELECT
      *,
      LAG(fee_token0) OVER w AS prev_fee0,
      LAG(fee_token1) OVER w AS prev_fee1
    FROM live
    WHERE fee_token0 IS NOT NULL AND fee_token1 IS NOT NULL
    WINDOW w AS (PARTITION BY strategy_output_id ORDER BY timestamp)
  ) d
),
accrued AS (
  SELECT
    lp_day,
    SUM(CASE WHEN acc0 IS NULL AND acc1 IS NULL THEN NULL
             WHEN token1 IN ('USDC', 'USDT', 'USD₮0', 'USDC.e', 'DAI')
             THEN COALESCE(acc0, 0) * px + COALESCE(acc1, 0) END) AS fees_accrued_usd
  FROM fee_accrual
  GROUP BY lp_day
),
eod AS (
  SELECT DISTINCT ON (strategy_output_id, lp_day)
    *
  FROM live
  ORDER BY strategy_output_id, lp_day, timestamp DESC
)
SELECT
  e.lp_day,
  COUNT(*) AS positions,
  COUNT(*) FILTER (WHERE e.in_range) AS positions_in_range,
  SUM(e.lp_notional_usd) AS lp_notional_usd,
  SUM(e.lp_pnl_usd) AS lp_pnl_usd,
  SUM(e.gross_il_usd) AS gross_il_usd,
  SUM(e.net_lp_pnl_usd) AS net_lp_pnl_usd,
  SUM(e.fees_usd) AS unclaimed_fees_usd_eod,
  a.fees_accrued_usd
FROM eod e
LEFT JOIN accrued a ON a.lp_day = e.lp_day
GROUP BY e.lp_day, a.fees_accrued_usd
ORDER BY e.lp_day;
```

`fees_accrued_usd` is the series to chart for fee income. It uses the same token-unit accrual as query 1 (see the note under query 1), so the two files reconcile. `unclaimed_fees_usd_eod` is the balance still sitting in the positions at the last snapshot of that day.

## 3. Position identity (pool, NFT, mint)

Snapshot rows do not store the NFT id. This is the append-only mint record. Successful mints only.

```sql
SELECT DISTINCT ON (strategy_output_id)
  m->>'strategy_output_id' AS strategy_output_id,
  s.created_at AS minted_at,
  (m->'mint'->>'chainId')::int AS chain_id,
  m->'mint'->>'poolId' AS pool_id,
  m->'mint'->'poolKey'->'currency0'->>'symbol' AS token0,
  m->'mint'->'poolKey'->'currency1'->>'symbol' AS token1,
  (m->'mint'->'ticks'->>'lower')::int AS tick_lower,
  (m->'mint'->'ticks'->>'upper')::int AS tick_upper,
  m->'result'->>'positionId' AS position_id,
  m->'result'->>'transactionHash' AS tx_hash,
  m->'result'->>'liquidity' AS liquidity_at_mint,
  m->'result'->>'amount0' AS amount0,
  m->'result'->>'amount1' AS amount1
FROM historical_dnclmm_strategy_execution_summary s
CROSS JOIN LATERAL jsonb_array_elements(
  COALESCE(s.results->'mint_results'->'output'->'results', '[]'::jsonb)
) AS m
WHERE m->>'error' IS NULL
  AND NULLIF(m->'result'->>'positionId', '') IS NOT NULL
  AND lower(m->'result'->>'positionId') <> 'unknown'
ORDER BY strategy_output_id, s.created_at;
```

## 4. Dashboard Uniswap Snapshots
```sql
WITH s AS (
  SELECT h.*,
         lag(snapshot_at)             OVER w AS prev_at,
         lag(liquidity)               OVER w AS prev_liq,
         lag(fee_growth_inside0_x128) OVER w AS prev_g0,
         lag(fee_growth_inside1_x128) OVER w AS prev_g1,
         lag(uncollected0)            OVER w AS prev_u0,
         lag(uncollected1)            OVER w AS prev_u1,
         token1_symbol IN ('USDC','USDT','DAI','USDC.e','USD₮0') AS t1_stable,
         token0_symbol IN ('USDC','USDT','DAI','USDC.e','USD₮0') AS t0_stable
  FROM dnclmm_historical_positions_summary h
  WHERE liquidity > 0
  WINDOW w AS (PARTITION BY chain_id, token_id ORDER BY snapshot_at)
),
f AS (
  SELECT *,
         CASE WHEN prev_at IS NULL THEN NULL ELSE COALESCE(
           CASE WHEN fee_growth_inside0_x128 >= prev_g0
                THEN trunc((fee_growth_inside0_x128 - prev_g0) * prev_liq / 340282366920938463463374607431768211456) / power(10::numeric, token0_decimals) END,
           CASE WHEN uncollected0 IS NULL OR prev_u0 IS NULL THEN NULL
                WHEN uncollected0 >= prev_u0 THEN uncollected0 - prev_u0 ELSE uncollected0 END) END AS fees0,
         CASE WHEN prev_at IS NULL THEN NULL ELSE COALESCE(
           CASE WHEN fee_growth_inside1_x128 >= prev_g1
                THEN trunc((fee_growth_inside1_x128 - prev_g1) * prev_liq / 340282366920938463463374607431768211456) / power(10::numeric, token1_decimals) END,
           CASE WHEN uncollected1 IS NULL OR prev_u1 IS NULL THEN NULL
                WHEN uncollected1 >= prev_u1 THEN uncollected1 - prev_u1 ELSE uncollected1 END) END AS fees1,
         CASE WHEN t1_stable THEN amount0 * price_market + amount1
              WHEN t0_stable THEN amount0 + amount1 / NULLIF(price_market, 0) END AS value_usd,
         snapshot_at - prev_at > interval '2 hours' AS after_gap
  FROM s
),
p AS (
  SELECT *,
         CASE WHEN t1_stable THEN fees0 * price_market + fees1
              WHEN t0_stable THEN fees0 + fees1 / NULLIF(price_market, 0) END AS fees_usd
  FROM f
)
SELECT
  (snapshot_at AT TIME ZONE 'UTC')::date                         AS day,
  chain_name,
  token_id                                                       AS position_id,
  wallet_label,
  token0_symbol || '/' || token1_symbol                          AS pair,
  fee_label                                                      AS fee_tier,
  display_base || '/' || display_quote                           AS price_quote,
  min(snapshot_at)                                               AS first_snapshot,
  max(snapshot_at)                                               AS last_snapshot,
  count(*)                                                       AS snapshots,
  round(100.0 * avg(CASE WHEN in_range THEN 1 ELSE 0 END), 1)    AS pct_time_in_range,
  (array_agg(display_low    ORDER BY snapshot_at DESC))[1]       AS range_low,
  (array_agg(display_high   ORDER BY snapshot_at DESC))[1]       AS range_high,
  (array_agg(tick_lower     ORDER BY snapshot_at DESC))[1]       AS tick_lower,
  (array_agg(tick_upper     ORDER BY snapshot_at DESC))[1]       AS tick_upper,
  min(display_market)                                            AS price_low,
  max(display_market)                                            AS price_high,
  (array_agg(display_market ORDER BY snapshot_at DESC))[1]       AS price_close,
  (array_agg(range_status   ORDER BY snapshot_at DESC))[1]       AS range_status_close,
  (array_agg(amount0        ORDER BY snapshot_at DESC))[1]       AS amount0_close,
  token0_symbol,
  (array_agg(amount1        ORDER BY snapshot_at DESC))[1]       AS amount1_close,
  token1_symbol,
  round((array_agg(value_usd ORDER BY snapshot_at DESC))[1], 2)  AS position_value_usd_close,
  (array_agg(fees_usdc      ORDER BY snapshot_at DESC))[1]       AS uncollected_fees_usd_close,
  sum(fees0)                                                     AS fees_earned_token0,
  sum(fees1)                                                     AS fees_earned_token1,
  round(sum(fees_usd), 2)                                        AS fees_earned_usd,
  count(*) FILTER (WHERE fees_usd IS NULL AND prev_at IS NOT NULL) AS unpriced_intervals,
  coalesce(bool_or(after_gap), false)                            AS includes_outage
FROM p
GROUP BY day, chain_name, token_id, wallet_label, pair, fee_label, price_quote, token0_symbol, token1_symbol
ORDER BY day, chain_name, token_id;
```

## 5. Historical OTC Options
```sql
-- confirmed_worthless: desk confirmation (18 Nov 2026) that the leg expired out of the money and the full premium was kept.
-- Used only while settlement_price is NULL; a recorded settlement price always takes precedence.
WITH o (id, fund, underlying, option_type, side, strike, expiry_at, size, premium_usd, trade_date, counterparty, iv, settlement_price, confirmed_worthless) AS (
  VALUES
    ('WM-20261008-ETH-C2660',       NULL,          'ETH', 'call', 'short', 2660,  timestamptz '2026-11-06 08:00Z', 100, 6040.00,  date '2026-10-08', 'Wintermute', 0.52, NULL::numeric, true),
    ('WM-20261014-ETH-C2660-1114',  'Liquid SP I', 'ETH', 'call', 'short', 2660,  timestamptz '2026-11-14 08:00Z', 139, 7368.39,  date '2026-10-14', 'Wintermute', 0.55, NULL::numeric, true),
    ('WM-20261014-BTC-C85900-1114', 'Liquid SP I', 'BTC', 'call', 'short', 85900, timestamptz '2026-11-14 08:00Z', 6,   10815.00, date '2026-10-14', 'Wintermute', 0.42, NULL::numeric, true)
),
x AS (
  SELECT *,
         CASE WHEN settlement_price IS NULL AND confirmed_worthless THEN 0
              WHEN settlement_price IS NULL THEN NULL
              WHEN option_type = 'call' THEN size * greatest(settlement_price - strike, 0)
              ELSE size * greatest(strike - settlement_price, 0) END AS intrinsic_usd
  FROM o
)
SELECT id, fund, counterparty, underlying,
       side || ' ' || option_type                                     AS position,
       size, strike, trade_date, expiry_at,
       expiry_at::date - trade_date                                   AS days,
       premium_usd,
       round(premium_usd / size, 2)                                   AS premium_per_coin,
       size * strike                                                  AS notional_at_strike,
       round(100 * premium_usd / (size * strike), 3)                  AS premium_pct_of_notional,
       round(100 * premium_usd / (size * strike) * 365 / (expiry_at::date - trade_date), 2) AS annualized_yield_pct,
       round(100 * iv, 1)                                             AS iv_pct,
       settlement_price,
       intrinsic_usd                                                  AS intrinsic_at_expiry_usd,
       CASE WHEN intrinsic_usd IS NULL THEN NULL
            WHEN side = 'short' THEN premium_usd - intrinsic_usd
            ELSE intrinsic_usd - premium_usd END                      AS net_pnl_usd,
       CASE WHEN settlement_price IS NULL AND confirmed_worthless THEN 'EXPIRED WORTHLESS'
            WHEN settlement_price IS NULL THEN CASE WHEN expiry_at < now() THEN 'AWAITING SETTLEMENT PRICE' ELSE 'OPEN' END
            WHEN intrinsic_usd > 0 THEN 'EXERCISED'
            ELSE 'EXPIRED WORTHLESS' END                              AS status
FROM x
ORDER BY trade_date, id;
```
