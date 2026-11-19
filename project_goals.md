# Evernorth Audit Dashboard: Engineering Spec (v2, rescoped to the export)

17 Nov 2026 · Alejandro Licona

v2 (updated 18 Nov 2026) rescopes v1 to the CSVs actually in `./data`. Anything v1 asked for that the export cannot support is listed under **Deferred** and **Open questions**, not approximated.

## Purpose and scope

Build a read-only Streamlit dashboard that lets Evernorth audit our real track record in three areas: Uniswap LP fee yield, spot execution quality (0x TWAP orders), and the OTC options we have sold. Every number on screen must be computed from CSV files that Evernorth can download and recompute themselves.

- **Users:** Evernorth's investment and risk team (external), and our desk for review before sending.
- **Data:** Alejandro exports the CSVs from our SQL database (queries in `data/dev_queries.md`) and drops them in `./data`. The app never connects to the database.
- **Out of scope:** live data, logins inside the app, editing data, any strategy other than the three above.

### Deferred (not in the export, not shown)

| v1 feature | Why deferred | Needed to bring it back |
|---|---|---|
| LP + Hyperliquid hedge series | No hedge data exported | Daily hedge P&L per position or per day |
| Burn tx links | Only mints are logged | Burn/close tx hash per position |
| TWAP USD weighting, notional, completion %, gas bps, interval TWAP, fill tx links | No qty, target qty, notional, mid-at-fill, gas or tx hash per slice | Those columns per slice |
| Options settlement prices, strike % of spot | No settlement price or spot at trade (outcome is desk-confirmed instead, see Options) | Settlement price per leg; spot at trade |
| Wallet book (`dashboard_uniswap_snapshots.csv`) | Dropped 18 Nov 2026: separate system, no deposit figure, not needed for the LP record | — |

The Overview tab says plainly: "LP figures are the unhedged LP leg; hedge P&L is not included."

## Input data

All files live in `./data`. Timestamps are UTC; USD values are as of the row's day or timestamp. The literal string `null` means missing. If a required column is missing, the app shows an error naming the file and column and stops. It must not guess.

| File | Grain | Required columns |
|---|---|---|
| `uniswap_mm_platform_position_rollover.csv` | position × day (21 May – 12 Nov 2026) | `lp_day, strategy_output_id, asset, token0, token1, tick_lower, tick_upper, price_lower, price_upper, in_range, lp_notional_usd, anchor_notional_usd, fees_accrued_usd, fee_source` |
| `uniswap_mm_platform_daily_notional_and_fees.csv` | day | `lp_day, positions, positions_in_range, lp_notional_usd, fees_accrued_usd` (used as a reconciliation check only) |
| `uniswap_mm_platform_mint_audit_log.csv` | mint | `strategy_output_id, minted_at, chain_id, pool_id, position_id, tx_hash` |
| `twap_tca_1.csv` | slice (fill) | `trade_date, completed_at, symbol, order_id, slice_index, execution_price, baseline_price, deviation_bps` |
| `wintermute_otc_historical_records.csv` | option leg | `id, fund, counterparty, underlying, position, size, strike, trade_date, expiry_at, premium_usd, iv_pct, settlement_price, status` |
| `option_outcomes.csv` | option leg | `id, outcome, confirmed_on, source`. Desk-confirmed outcomes kept apart from the export; applied only where the export has no settlement price, and shown in an `outcome_source` column. Optional. |
| `manifest.csv` | file | Output of `sha256sum uniswap_mm_platform_position_rollover.csv uniswap_mm_platform_daily_notional_and_fees.csv uniswap_mm_platform_mint_audit_log.csv twap_tca_1.csv wintermute_otc_historical_records.csv dev_queries.md` run in `./data` at export time (`<hash>  <filename>` per line), so `sha256sum -c manifest.csv` works as-is |

### Derived fields (deterministic, from columns above)

- **Chain of a rollover position:** second segment of `strategy_output_id` (`20260911_1_5` → chain 1 = Ethereum; `42161` = Arbitrum).
- **TWAP order fields:** `order_id` = `YYYY_MM_DD_<chainId>_<fromToken>_<toToken>_<HHMMSS>_<seq>`. Chain, from-token and to-token come from segments 4–6. Token prefixes and `symbol` addresses map to tickers with a fixed table in the app: `0xeeee…` ETH, `0xaf88…` USDC (Arb), `0xa0b8…` USDC (Eth), `0x2f2a…` WBTC (Arb), `0x2260…` WBTC (Eth), `0xFd08…` USD₮0 (Arb), `0xdAC1…` USDT (Eth). An unmapped token is shown as its raw string and listed in the Data tab; it is never dropped.
- **TWAP side:** "buy" when the from-token is a stablecoin, "sell" when the to-token is a stablecoin, "stable swap" when both are.
- **Explorer links:** chain 1 → `https://etherscan.io/tx/<hash>`, chain 42161 → `https://arbiscan.io/tx/<hash>`. Mint rows with no `chain_id` show the hash unlinked.

### Fee accrual in the export (fixed 18 Nov 2026, needs re-export)

The rollover and daily files' `fees_accrued_usd` used to be the sum of positive changes in `feesUsd`, the USD value of the unclaimed fee balance. That balance is partly ETH/WBTC, so price ticks between snapshots counted as income and downticks were dropped. Measured against the fee-token balances in the same file, this overstated fees by 21% (Aug 10%, Sep 30%, Oct 9%, Nov 0%), lifting the headline fee APR from about 53% to 64.3%. On the six November positions that the on-chain fee-growth export covers end to end, the token method agrees within 2%.

Queries 1 and 2 in `dev_queries.md` now accrue fees in token units between consecutive snapshots (a fall in a balance means collection or re-mint, so the new balance counts) and value token0 at `COALESCE(current_price, pool_price)`. Query 1 also outputs `fees_accrued_token0` and `fees_accrued_token1` so the USD figure can be recomputed. The output columns the app reads are unchanged. Until a corrected export is dropped in, the app accrues fees itself from the end-of-day `fee_token0`/`fee_token1` balances (same rules, daily granularity): headline fee APR 52.95%. When the export carries `fees_accrued_token0`, the app uses its `fees_accrued_usd` as-is. Both queries were run against a test table built from the current export (one snapshot per day): they execute, keep the schema, and reconcile with each other to the cent. The production result will differ because it uses every intraday snapshot. Expect a headline fee APR in the low 50s; well above that means the fix did not take.

### Known data issues (shown in the Data tab, never auto-corrected)

- `gross_il_usd` = −$7,982,645 on `20260911_1_5` (notional $208). `gross_il_usd` is not used in any metric.
- 2 rollover rows with `fee_source = error`: their `fees_accrued_usd` counts as 0 and the rows are flagged.
- 23 rollover rows with blank `lp_notional_usd`: flagged. `lp_notional_usd` is not used for returns, only for the positions table's last notional.
- 6 mint rows with no chain or ticks; 1 with no `strategy_output_id`.
- 5 positions are re-ranged mid-life: on one day the ticks, liquidity and `anchor_notional_usd` all change together under the same `strategy_output_id` (3 pilot positions on 31 Jul; 2 positions on 31 Aug, where the anchor triples from ~$19.9k to ~$59.2k, i.e. capital added). The mint log keeps only the first mint, so the re-mint tx is not exported. Flagged "re-ranged, anchor reset"; the day's return uses the new anchor as denominator.
- Option legs whose `status` is `EXPIRED WORTHLESS` with no `settlement_price` are listed as informational: the outcome is desk-confirmed, the settlement price is not recorded.

## Layout

A single wide page (`st.set_page_config(layout="wide")`): a header and integrity banner, then five tabs. Each tab reads top to bottom: headline numbers first, the chart second, the auditable rows last. Charts use Plotly; tables use `st.dataframe`. Palette: navy `#1F3A5F` for our main series, orange `#E07A1F` for comparisons, grey `#9AA5B1` for benchmarks. Navy lines, bars and points carry a light grey halo/outline so they stay legible on Streamlit's dark theme.

**P&L is shown as a percentage, never in USD or notional.** Returns, P&L and fees are expressed as % of capital (anchor, position value or option notional). USD sizes (anchor, notional, position value, premium, option size, token amounts) are not displayed; they stay in the downloadable CSVs so every percentage can be recomputed.

### Header (above the tabs, always visible)

- Title "White Star Capital: track record audit pack" and a one-line caption naming the three areas.
- Integrity banner: green "Integrity verified: N files match manifest.csv" or amber "Integrity check failed" with a pointer to the Data tab.

### 1. Overview

- Heading "Three strategies, one audit trail" and a one-line pointer to the Data tab.
- **Three bordered cards, one per strategy**, each with two metrics, a small chart and a coverage line:
  - Uniswap LP: fee APR and time in range; cumulative fee yield line (navy).
  - Spot Execution: median cost per fill and share of fills at or better than the quote; per-fill cost histogram with tails clipped at ±40 bps for the mini view (orange).
  - OTC Options: realized yield (% of notional at strike) and annualized premium yield; premium per leg as bars (teal), plus counterparty and outcome counts.
- **Desk activity timeline:** one row per strategy. LP days as squares sized by open positions (pilot in grey), spot execution trading days as dots sized by fills, option legs as lines from trade date to expiry.
- The scope caption: % of capital only, LP is fee income, spot execution is equal-weighted.

### 2. Uniswap LP

The rollover file is the LP record. **LP performance is shown as fee yield on deployed capital only** (desk decision, 18 Nov 2026). Changes in LP position value are not displayed; they remain in the CSVs.

- **Three cards:** fee APR, cumulative fee yield, time in range. A caption states how many position-days have no snapshot.
- **Chart:** cumulative fee yield (navy line) with daily fee yield as bars (orange), both in %.
- **Monthly table:** months as columns; rows are fee yield in the month, its APR, and days with capital deployed.
- **Open positions chart** (grey bars): number of positions open per LP day, with the pilot period shaded.
- **Positions table:** one row per `strategy_output_id`: chain, pair, ticks, first and last day, days open, days recorded, fees as % of anchor, position fee APR, % days in range, mint tx as an explorer link (joined from the mint log). Rows with a known data issue get a flag column.

### 3. Spot Execution

The tab is titled "Spot Execution" (renamed from "0x TWAP execution", 18 Nov 2026). It covers our 0x TWAP orders from `twap_tca_1.csv`.

- **Four cards:** median and mean cost per fill vs baseline (bps), share of fills at or better than baseline, mean cost per order (bps). Sub-label: order and fill counts.
- Caption: "Positive bps = cost to us. Equal-weighted: the export has no trade size, so figures are not USD-weighted."
- **Histogram:** per-fill cost (bps), all fills, not clipped.
- **Scatter:** order cost (y, bps) against the number of slices (fills) in the order (x, log scale with ticks at 1, 2, 3, 5, 10, 20, 30), coloured by pair.
- **Orders table:** date, chain, side, pair, slices, first and last fill time, baseline price, mean cost bps, best and worst fill bps.
- **Expander "Every fill":** the raw rows of `twap_tca_1.csv` with derived chain, side, pair and cost bps.

### 4. Options

- **Four cards:** average premium % of notional at strike, average annualized yield, realized P&L as % of notional ("—" while a leg is unsettled), legs.
- Caption: legs with status EXPIRED WORTHLESS settle with zero intrinsic value, so realized P&L equals the full premium.
- **Bar chart:** premium as % of notional per leg, grouped by underlying.
- **Trades table:** all columns except size and USD amounts, plus recomputed days, premium % of notional, annualized yield and realized P&L % of notional. Realized fields show "—" while a leg has neither a settlement price nor a confirmed outcome.

### 5. Data & verification

- **Integrity table:** file, rows, manifest hash, recomputed hash, match (yes/no).
- **How to audit:** three steps. Run `sha256sum -c manifest.csv` in the data folder, spot-check mint tx hashes on the explorer, then recompute the metrics with the formulas below.
- A download button for each CSV, `dev_queries.md` and `manifest.csv`.
- **Expanders:** the data dictionary (the Input data section of this spec), the token mapping table, the known data issues with the affected rows, and a reconciliation of the rollover file against the daily file (positions and fees per day; differences listed, not hidden).

## Metric definitions

LP figures are fee yield on deployed capital, gross of fund fees.

### Uniswap LP

Per position i and calendar day t, with `A` = `anchor_notional_usd` and `F` = `fees_accrued_usd` (0 if blank or `fee_source = error`). A position is open on every day from its first to its last recorded day. Days with no snapshot carry A forward with F = 0.

```
y(t)        = Σ_i F[i,t] / Σ_{i open at t} A[i]          (daily fee yield)
Fee APR     = Σ_t Σ_i F[i,t] / Σ_t Σ_{i open at t} A[i] × 365
Cumulative  = Π_t (1 + y(t)) − 1
```

- **Why gap-fill (audit, 18 Nov 2026).** 13 positions have days with no snapshot: 53 position-days against 239 recorded in the headline period, 35 of them on 1–7 Oct, the rest on scattered days in late Aug, mid-Oct and early Nov. The export computes `fees_accrued_usd` by differencing `fees_usd` across snapshots, so a gap's fees land on the first recorded day after it. Those 17 post-gap rows carry 26.6% of all fees. The earlier formula counted only recorded days, putting a whole gap's fees on one day of capital, which gave a fee APR of 84.2% (and a compounded APY of 128.3%). Counting every day a position was open gives 64.3%, consistent with the 13.66% cumulative fee yield over 73 days with capital deployed (simple annualization ≈ 68%). The APY figure is withdrawn.
- **Headline period** starts on the first day Σ A ≥ $10,000 (the May–July pilot ran on ~$50 positions). Pilot days stay in the tables and are shaded on the positions chart but are excluded from headline figures.
- Days with no open position (19 of the 92 calendar days in the headline period) are not in the series; the figures are on deployed capital.
- Monthly fee yield = y compounded within the calendar month; monthly APR = that month's Σ F / Σ capital-days × 365.
- Position fee APR (positions table) = (Σ F / anchor) × 365 / days open.
- Time in range = share of recorded position-days with `in_range` true.
- **Scope of the LP figures (desk decision, 18 Nov 2026).** The dashboard reports fee income only. Changes in LP position value (`lp_pnl_usd`, `lp_notional_usd`) are not part of any displayed figure, and every LP caption says so. For the record: `lp_pnl_usd` is the mark-to-market change in LP value since entry, in USD, excluding fees. `net_lp_pnl_usd` = `lp_pnl_usd` + `unclaimed_fees_usd_eod` on every row, and `lp_notional_usd − lp_pnl_usd` is nearly constant within each position (median variation 0.07%). Over the headline period it amounts to −17.7% of deployed capital. Net of that, the return including fees is −6.3%.

### Spot execution (0x TWAP orders; positive = cost)

`execution_price` and `baseline_price` are quoted as output tokens per input token in raw units, so a higher price is always better for us, for buys and sells alike. `baseline_price` is constant within an order.

**What `baseline_price` is (investigated 18 Nov 2026).** It is quoted the same way as the fills (output per input, raw units), so it is a 0x quote, not a market mid. The first fill executes at exactly the baseline in 70 of 288 orders, which fits an executable quote taken at order start. Cost vs baseline therefore measures slippage against our own arrival quote, which already includes 0x routing and fees. It is not a shortfall against an independent mid. 142 of 288 orders are a single slice, so their cost is first fill vs that quote.

```
cost_fill (bps) = −(execution_price / baseline_price − 1) × 10^4   (= −deviation_bps, checked to ±0.001 bps)
cost_order      = mean of cost_fill over the order's slices
```

Headline figures are equal-weighted across fills or orders. Costs within 1e-6 bps of zero are set to 0, so a fill at exactly the quote counts as "at or better" whatever float parsing the pandas version does.

### Options

- Days = expiry date − trade_date. Notional at strike = size × strike.
- Premium % of notional = premium_usd / notional. Annualized yield = that × 365 / days.
- Intrinsic = size × max(settlement − strike, 0) for calls (max(strike − settlement, 0) for puts) when `settlement_price` is present; 0 when `status` = `EXPIRED WORTHLESS` and no price is recorded. Realized P&L % = (premium − intrinsic) / notional at strike for short legs.
- **Outcome of the three Wintermute legs (desk-confirmed 18 Nov 2026):** all expired worthless; the full premium was kept. Query 5 in `dev_queries.md` carries this as `confirmed_worthless`. Our own pool prices agree: ETH traded no higher than $2,244 on 6 Nov (strike $2,660), ETH's high on 13 Nov was $2,016, and BTC traded no higher than $68,171 on 14 Nov (strike $85,900).

## Open questions for the data owner

None open. Resolved 18 Nov 2026: what `lp_pnl_usd` measures, LP headline scope, option outcomes, what `baseline_price` is, and why the anchor changes (see the sections above).

## Integrity, hosting and acceptance

**Running it.** `make start` builds and runs the dashboard in Docker on http://localhost:8501 and, only if `data/manifest.csv` is missing, seals the pack into it. `make stop` removes every Docker resource of the project. After dropping in a new export, delete `data/manifest.csv` before `make start`; otherwise the banner correctly reports the changed files.

**Integrity.** On every load, the app recomputes the SHA-256 of each file listed in `manifest.csv` and compares. A mismatch, missing file or missing manifest switches the banner to amber and marks the row in the Data tab. Nothing is hidden or auto-corrected.

**Hosting.** Streamlit Community Cloud with viewer authentication, or behind our existing Netlify password. Evernorth can also run it locally with `streamlit run app.py`. Ship a `requirements.txt` with streamlit, pandas, numpy and plotly only, pinned to exact versions (the ones the Docker image is tested with).

### Acceptance criteria

- [ ] Loads all five CSVs and renders every tab with no errors on the real export.
- [ ] Headline metrics match an independent spreadsheet recompute to within 0.1% (fee yield) and 0.1 bps (spot execution).
- [ ] Editing any byte of a CSV after export triggers the amber banner and a "no" in the integrity table.
- [ ] A missing required column produces an error naming the file and column.
- [ ] Every mint tx hash opens the right explorer page for its chain.
- [ ] Every known data issue appears in the Data tab with its rows.
- [ ] Pilot-period days and unsettled option legs are in the tables but excluded from headline figures.
- [ ] Every deferred metric is absent, not estimated.
- [ ] Works at 1280 px wide and reads cleanly in Streamlit's light and dark themes.
