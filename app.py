"""White Star Capital: track record audit pack.

Read-only Streamlit dashboard. Every figure is computed from the CSVs in ./data.
Metric definitions live in project_goals.md (v2); keep the two in sync.

Run locally:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DATA_DIR = Path(__file__).parent / "data"
NA = ["null", ""]

NAVY, ORANGE, GREY = "#1F3A5F", "#E07A1F", "#9AA5B1"
TEAL = "#2A9D8F"

HEADLINE_MIN_CAPITAL = 10_000  # headline LP stats start on the first day deployed capital reaches this
STABLES = {"USDC", "USDT", "USD₮0", "USDC.e", "DAI"}
TOKEN_MAP = {  # lowercase address prefix -> ticker
    "0xeeee": "ETH",
    "0xaf88": "USDC",
    "0xa0b8": "USDC",
    "0x2f2a": "WBTC",
    "0x2260": "WBTC",
    "0xfd08": "USD₮0",
    "0xdac1": "USDT",
}
TOKEN_MAP_ROWS = [
    ("0xeeee…", "ETH", "Native ETH (both chains)"),
    ("0xaf88…", "USDC", "Arbitrum"),
    ("0xa0b8…", "USDC", "Ethereum"),
    ("0x2f2a…", "WBTC", "Arbitrum"),
    ("0x2260…", "WBTC", "Ethereum"),
    ("0xFd08…", "USD₮0", "Arbitrum"),
    ("0xdAC1…", "USDT", "Ethereum"),
]
EXPLORERS = {1: "https://etherscan.io/tx/", 42161: "https://arbiscan.io/tx/"}
CHAIN_NAMES = {1: "Ethereum", 42161: "Arbitrum"}

FILES = {
    "rollover": "uniswap_mm_platform_position_rollover.csv",
    "daily": "uniswap_mm_platform_daily_notional_and_fees.csv",
    "mints": "uniswap_mm_platform_mint_audit_log.csv",
    "twap": "twap_tca_1.csv",
    "options": "wintermute_otc_historical_records.csv",
}
REQUIRED = {
    "rollover": [
        "lp_day", "strategy_output_id", "asset", "token0", "token1", "tick_lower", "tick_upper",
        "price_lower", "price_upper", "in_range", "lp_notional_usd", "anchor_notional_usd",
        "fees_accrued_usd", "fee_source", "fee_token0", "fee_token1", "pool_price", "current_price",
    ],
    "daily": ["lp_day", "positions", "positions_in_range", "lp_notional_usd", "fees_accrued_usd"],
    "mints": ["strategy_output_id", "minted_at", "chain_id", "pool_id", "position_id", "tx_hash"],
    "twap": [
        "trade_date", "completed_at", "symbol", "order_id", "slice_index",
        "execution_price", "baseline_price", "deviation_bps",
    ],
    "options": [
        "id", "fund", "counterparty", "underlying", "position", "size", "strike", "trade_date",
        "expiry_at", "premium_usd", "iv_pct", "settlement_price", "status",
    ],
}
MANIFEST = "manifest.csv"
OUTCOMES = "option_outcomes.csv"  # desk-confirmed option outcomes, kept apart from the export (optional)
OUTCOME_COLUMNS = ["id", "outcome", "confirmed_on", "source"]
EXTRA_DOWNLOADS = ["dev_queries.md", OUTCOMES]
STABLE_QUOTES = {"USDC", "USDT", "USD₮0", "USDC.e", "DAI"}


# ---------------------------------------------------------------- loading


def load_all() -> dict[str, pd.DataFrame]:
    """Load the six CSVs. Stop with a named error if a file or required column is missing."""
    out = {}
    for key, name in FILES.items():
        path = DATA_DIR / name
        if not path.exists():
            st.error(f"Missing file: `{name}` is not in ./data. The dashboard cannot be computed without it.")
            st.stop()
        df = pd.read_csv(path, na_values=NA, keep_default_na=True)
        missing = [c for c in REQUIRED[key] if c not in df.columns]
        if missing:
            cols = ", ".join(f"`{c}`" for c in missing)
            st.error(f"Missing column in `{name}`: {cols}. Re-export the file; the dashboard does not guess.")
            st.stop()
        out[key] = df
    return out


def to_bool(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    return s.astype(str).str.strip().str.lower().isin(["true", "t", "1", "yes"])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def integrity_check() -> tuple[pd.DataFrame, bool, str]:
    """Recompute SHA-256 of every file against manifest.csv (sha256sum format). Never cached."""
    manifest_path = DATA_DIR / MANIFEST
    expected: dict[str, str] = {}
    note = ""
    if manifest_path.exists():
        for line in manifest_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                expected[parts[1].lstrip("*").strip()] = parts[0].lower()
    else:
        note = "manifest.csv is missing"
    names = list(dict.fromkeys(list(expected) + list(FILES.values()) + EXTRA_DOWNLOADS))
    rows = []
    for name in names:
        path = DATA_DIR / name
        exists = path.exists()
        n_rows = None
        if exists and name.endswith(".csv"):
            n_rows = sum(1 for _ in open(path, "rb")) - 1
        got = sha256(path) if exists else None
        exp = expected.get(name)
        rows.append({
            "file": name,
            "rows": n_rows,
            "manifest hash": exp or "— not in manifest",
            "recomputed hash": got or "— file missing",
            "match": "yes" if (exp and got and exp == got) else "no",
        })
    table = pd.DataFrame(rows)
    ok = bool(expected) and (table["match"] == "yes").all()
    return table, ok, note


def explorer_link(tx_hash, chain_id) -> str | None:
    if pd.isna(tx_hash) or pd.isna(chain_id):
        return None
    base = EXPLORERS.get(int(chain_id))
    return f"{base}{tx_hash}" if base else None


# ---------------------------------------------------------------- metrics


def token_fee_accrual(df: pd.DataFrame) -> pd.Series:
    """Daily fee income from the end-of-day unclaimed fee-token balances.

    Older exports built fees_accrued_usd by summing positive changes in the USD value of the unclaimed
    balance, which counts price upticks as income (about +21% on the 18 Nov export). Here each token is
    accrued in its own units: a rise in the balance is the accrual, a fall means fees were collected (or the
    position re-minted) and the new balance is what accrued since. The first observation counts in full
    (fees since mint). Token0 is valued at current_price (pool_price if blank), the price feesUsd itself
    uses; token1 is a stablecoin. Rows without fee balances (fee_source = error) book 0.
    """
    out = pd.Series(0.0, index=df.index)
    valid = df["fee_token0"].notna() & df["fee_token1"].notna() & df["token1"].isin(STABLE_QUOTES)
    sub = df[valid].sort_values(["strategy_output_id", "lp_day"])
    g = sub.groupby("strategy_output_id")
    acc = {}
    for tok in ["fee_token0", "fee_token1"]:
        prev = g[tok].shift()
        acc[tok] = np.where(prev.isna() | (sub[tok] < prev), sub[tok], sub[tok] - prev)
    px = sub["current_price"].fillna(sub["pool_price"])
    out.loc[sub.index] = acc["fee_token0"] * px + acc["fee_token1"]
    return out.fillna(0.0)


def strategy_book(roll: pd.DataFrame) -> dict:
    df = roll.copy()
    df["lp_day"] = pd.to_datetime(df["lp_day"])
    df["in_range"] = to_bool(df["in_range"])
    df = df.sort_values(["strategy_output_id", "lp_day"]).reset_index(drop=True)
    df["fee_error"] = df["fee_source"].astype(str).str.lower().eq("error")
    if "fees_accrued_token0" in df.columns:
        # Export already accrues fees in token units (dev_queries.md, query 1 as of 18 Nov 2026).
        df["F"] = df["fees_accrued_usd"].fillna(0).where(~df["fee_error"], 0.0)
        df.attrs["fee_method"] = "export"
    else:
        df["F"] = token_fee_accrual(df)
        df.attrs["fee_method"] = "token balances"
    # LP performance is reported as fee income on deployed capital only (desk decision, 18 Nov 2026).
    # Changes in LP position value (lp_pnl_usd) are not part of any displayed figure.
    df["chain_id"] = pd.to_numeric(df["strategy_output_id"].astype(str).str.split("_").str[1], errors="coerce")

    # Gap-filled panel: a position is open on every calendar day from its first to its last recorded day.
    # Days with no snapshot carry the anchor forward and book zero fees; the fees earned over a gap are
    # already in fees_accrued_usd on the first recorded day after it (the export differences fees_usd
    # across snapshots). Counting only recorded days would put a whole gap's fees on one day of capital.
    parts = []
    for sid, g in df.groupby("strategy_output_id"):
        g = g.set_index("lp_day")
        idx = pd.date_range(g.index.min(), g.index.max(), freq="D")
        parts.append(pd.DataFrame({
            "strategy_output_id": sid,
            "anchor": g["anchor_notional_usd"].reindex(idx).ffill(),
            "F": g["F"].reindex(idx).fillna(0.0),
            "recorded": pd.Series(True, index=g.index).reindex(idx, fill_value=False),
        }, index=idx))
    panel = pd.concat(parts)
    daily = panel.groupby(level=0).agg(
        F=("F", "sum"), capital=("anchor", "sum"),
        positions=("strategy_output_id", "count"), recorded=("recorded", "sum"),
    )
    daily["fee_yield"] = daily["F"] / daily["capital"]
    above = daily.index[daily["capital"] >= HEADLINE_MIN_CAPITAL]
    start = above.min() if len(above) else daily.index.min()
    head = daily.loc[daily.index >= start]
    head_rows = df[df["lp_day"] >= start]
    n_days = len(head)
    fee_apr = head["F"].sum() / head["capital"].sum() * 365 if n_days else np.nan
    return {
        "rows": df,
        "fee_method": df.attrs.get("fee_method", "export"),
        "daily": daily,
        "head": head,
        "start": start,
        "fee_cum": (1 + head["fee_yield"]).prod() - 1,
        "n_days": n_days,
        "unrecorded_position_days": int((head["positions"] - head["recorded"]).sum()),
        "time_in_range": head_rows["in_range"].mean(),
        "fee_apr": fee_apr,
        "positions_head": head_rows["strategy_output_id"].nunique(),
    }


def strategy_positions(rows: pd.DataFrame, mints: pd.DataFrame, start) -> pd.DataFrame:
    g = rows.groupby("strategy_output_id")
    pos = pd.DataFrame({
        "chain_id": g["chain_id"].first(),
        "pair": g["token0"].first() + "/" + g["token1"].first().fillna("?"),
        "tick_lower": g["tick_lower"].last(),
        "tick_upper": g["tick_upper"].last(),
        "first_day": g["lp_day"].min().dt.date,
        "last_day": g["lp_day"].max().dt.date,
        "days_held": (g["lp_day"].max() - g["lp_day"].min()).dt.days + 1,
        "days_recorded": g.size(),
        "_anchor": g["anchor_notional_usd"].first(),
        "_fees": g["F"].sum(),
        "pct_days_in_range": g["in_range"].mean() * 100,
    }).reset_index()
    pos["fees_pct"] = pos["_fees"] / pos["_anchor"] * 100
    pos["fee_apr_pct"] = pos["fees_pct"] * 365 / pos["days_held"]
    pos["chain"] = pos["chain_id"].map(CHAIN_NAMES)
    m = mints.dropna(subset=["strategy_output_id"]).drop_duplicates("strategy_output_id")
    pos = pos.merge(m[["strategy_output_id", "position_id", "tx_hash", "chain_id"]].rename(
        columns={"chain_id": "mint_chain_id", "tx_hash": "mint_tx"}), on="strategy_output_id", how="left")
    link_chain = pos["mint_chain_id"].fillna(pos["chain_id"])
    pos["mint_explorer"] = [explorer_link(h, c) for h, c in zip(pos["mint_tx"], link_chain)]

    flags = {}
    for sid, grp in rows.groupby("strategy_output_id"):
        f = []
        if grp["fee_error"].any():
            f.append("fee_source error")
        if grp["lp_notional_usd"].isna().any():
            f.append("blank notional")
        if "gross_il_usd" in grp and (grp["gross_il_usd"].abs() > 100 * grp["lp_notional_usd"].abs()).any():
            f.append("gross_il outlier")
        if grp["anchor_notional_usd"].nunique() > 1:
            f.append("re-ranged, anchor reset")
        if grp["lp_day"].max() < start:
            f.append("pilot")
        flags[sid] = ", ".join(f)
    pos["flags"] = pos["strategy_output_id"].map(flags)
    cols = ["strategy_output_id", "chain", "pair", "tick_lower", "tick_upper", "first_day", "last_day",
            "days_held", "days_recorded", "fees_pct", "fee_apr_pct", "pct_days_in_range",
            "position_id", "mint_tx", "mint_explorer", "flags"]
    return pos[cols].sort_values("first_day")


def token_name(tok: str) -> str:
    t = str(tok)
    return TOKEN_MAP.get(t.lower()[:6], t) if t.lower().startswith("0x") else t


def twap_book(tw: pd.DataFrame) -> dict:
    df = tw.copy()
    parts = df["order_id"].astype(str).str.split("_")
    df["chain_id"] = pd.to_numeric(parts.str[3], errors="coerce")
    df["chain"] = df["chain_id"].map(CHAIN_NAMES).fillna(parts.str[3])
    df["from_token"] = parts.str[4].map(token_name)
    df["to_token"] = parts.str[5].map(token_name)
    fs, ts = df["from_token"].isin(STABLES), df["to_token"].isin(STABLES)
    df["side"] = np.select([fs & ts, fs, ts], ["stable swap", "buy", "sell"], default="other")
    df["pair"] = np.select(
        [df["side"].eq("buy"), df["side"].eq("sell")],
        [df["to_token"] + "/" + df["from_token"], df["from_token"] + "/" + df["to_token"]],
        default=df["from_token"] + "→" + df["to_token"],
    )
    cost = -(df["execution_price"] / df["baseline_price"] - 1) * 1e4
    # Fills priced exactly at the quote come out as ±1e-10 bps depending on how the CSV floats are parsed;
    # snap anything under 1e-6 bps to 0 so "at or better than quote" does not depend on the library version.
    df["cost_bps"] = cost.where(cost.abs() >= 1e-6, 0.0)
    df["completed_at"] = pd.to_datetime(df["completed_at"], utc=True, format="mixed")
    check = (df["cost_bps"] + df["deviation_bps"]).abs().max()

    orders = df.sort_values("completed_at").groupby("order_id").agg(
        date=("trade_date", "first"), chain=("chain", "first"), side=("side", "first"), pair=("pair", "first"),
        slices=("slice_index", "count"), first_fill=("completed_at", "min"), last_fill=("completed_at", "max"),
        baseline_price=("baseline_price", "first"), cost_bps=("cost_bps", "mean"),
        best_fill_bps=("cost_bps", "min"), worst_fill_bps=("cost_bps", "max"),
    ).reset_index().sort_values("first_fill")
    known = set(TOKEN_MAP) | {s.lower() for s in STABLES} | {"eth", "wbtc"}
    raw_tokens = pd.concat([parts.str[4], parts.str[5], df["symbol"].astype(str)])
    unmapped = sorted({t for t in raw_tokens if t.lower()[:6] not in known and t.lower() not in known})
    return {"fills": df, "orders": orders, "check": check, "unmapped": unmapped}


def load_outcomes() -> pd.DataFrame | None:
    path = DATA_DIR / OUTCOMES
    if not path.exists():
        return None
    df = pd.read_csv(path, na_values=NA, keep_default_na=True)
    missing = [c for c in OUTCOME_COLUMNS if c not in df.columns]
    if missing:
        st.error(f"Missing column in `{OUTCOMES}`: " + ", ".join(f"`{c}`" for c in missing) + ".")
        st.stop()
    return df


def options_book(opt: pd.DataFrame, outcomes: pd.DataFrame | None = None) -> pd.DataFrame:
    df = opt.copy()
    df["outcome_source"] = np.where(df["settlement_price"].notna(), "settlement price", "export status")
    if outcomes is not None and len(outcomes):
        o = outcomes.drop_duplicates("id", keep="last").set_index("id")
        # A desk confirmation applies only while the export has no settlement price; a price always wins.
        hit = df["id"].isin(o.index) & df["settlement_price"].isna()
        conf = df.loc[hit, "id"].map(o["outcome"]).astype(str).str.upper()
        df.loc[hit, "status"] = conf
        df.loc[hit, "outcome_source"] = ("desk confirmation " + df.loc[hit, "id"].map(o["confirmed_on"]).astype(str)
                                         + " (" + OUTCOMES + ")")
    trade = pd.to_datetime(df["trade_date"])
    expiry = pd.to_datetime(df["expiry_at"], utc=True).dt.tz_localize(None).dt.normalize()
    df["days_calc"] = (expiry - trade).dt.days.clip(lower=1)
    df["notional_at_strike"] = df["size"] * df["strike"]
    df["premium_pct_notional"] = df["premium_usd"] / df["notional_at_strike"] * 100
    df["annualized_yield_pct_calc"] = df["premium_pct_notional"] * 365 / df["days_calc"]
    pos = df["position"].astype(str).str.lower()
    is_call = pos.str.contains("call")
    is_short = pos.str.contains("short")
    s, k = df["settlement_price"], df["strike"]
    intrinsic = np.where(is_call, (s - k).clip(lower=0), (k - s).clip(lower=0)) * df["size"]
    worthless = df["status"].astype(str).str.upper().eq("EXPIRED WORTHLESS")
    settled = s.notna() | worthless  # worthless legs settle with zero intrinsic even without a recorded price
    df["intrinsic_calc"] = np.where(s.notna(), intrinsic, np.where(worthless, 0.0, np.nan))
    df["realized_pnl_calc"] = np.where(
        settled, np.where(is_short, df["premium_usd"] - df["intrinsic_calc"], df["intrinsic_calc"] - df["premium_usd"]),
        np.nan)
    df["realized_pnl_pct_calc"] = df["realized_pnl_calc"] / df["notional_at_strike"] * 100
    df["fund"] = df["fund"].fillna("—")
    return df


def known_issues(d: dict[str, pd.DataFrame], opt: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """opt is the options table after desk-confirmed outcomes are applied (options_book)."""
    roll, mints = d["rollover"], d["mints"]
    issues = []
    if "gross_il_usd" in roll:
        bad = roll[roll["gross_il_usd"].abs() > 100 * roll["lp_notional_usd"].abs()]
        issues.append(("Rollover: gross_il_usd more than 100× the notional (not used in any metric)", bad))
    issues.append(("Rollover: fee_source = error (no fee balances; fees counted as 0)",
                   roll[roll["fee_source"].astype(str).str.lower().eq("error")]))
    issues.append(("Rollover: blank lp_notional_usd (not used for returns; last-notional column only)", roll[roll["lp_notional_usd"].isna()]))
    multi = roll.groupby("strategy_output_id")["anchor_notional_usd"].transform("nunique") > 1
    issues.append(("Rollover: position re-ranged mid-life (ticks, liquidity and anchor reset together; re-mint tx not exported)",
                   roll[multi].sort_values(["strategy_output_id", "lp_day"])))
    issues.append(("Mint log: no chain_id or no strategy_output_id",
                   mints[mints["chain_id"].isna() | mints["strategy_output_id"].isna()]))
    expired = pd.to_datetime(opt["expiry_at"], utc=True) < pd.Timestamp.now(tz="UTC")
    no_outcome = expired & opt["settlement_price"].isna() & ~opt["status"].astype(str).str.upper().eq("EXPIRED WORTHLESS")
    issues.append(("Options: past expiry with no settlement_price or confirmed outcome", opt[no_outcome]))
    issues.append(("Options: expired worthless with no settlement price recorded (informational; see outcome_source)",
                   opt[opt["status"].astype(str).str.upper().eq("EXPIRED WORTHLESS") & opt["settlement_price"].isna()]))
    return issues


def reconciliation(roll: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    r = roll.copy()
    r["in_range"] = to_bool(r["in_range"])
    agg = r.groupby("lp_day").agg(
        positions_rollover=("strategy_output_id", "count"),
        in_range_rollover=("in_range", "sum"),
        fees_rollover=("fees_accrued_usd", "sum"),
    ).reset_index()
    m = daily[["lp_day", "positions", "positions_in_range", "fees_accrued_usd"]].merge(agg, on="lp_day", how="outer")
    m["positions_diff"] = m["positions_rollover"].fillna(0) - m["positions"].fillna(0)
    m["in_range_diff"] = m["in_range_rollover"].fillna(0) - m["positions_in_range"].fillna(0)
    m["fees_diff"] = (m["fees_rollover"].fillna(0) - m["fees_accrued_usd"].fillna(0)).round(2)
    return m


# ---------------------------------------------------------------- formatting


def pct(x, d=2):
    return "—" if x is None or pd.isna(x) else f"{x * 100:,.{d}f}%"


def usd(x, d=0):
    return "—" if x is None or pd.isna(x) else f"${x:,.{d}f}"


def bps(x, d=2):
    return "—" if x is None or pd.isna(x) else f"{x:,.{d}f} bps"


def fig_layout(fig: go.Figure, height=380, **kw) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", yanchor="top", y=-0.22, x=0), **kw)
    return fig


def card(col, label, value, sub=None):
    """Metric card; the sub-label is a plain caption, not a delta arrow."""
    col.metric(label, value)
    if sub:
        col.caption(sub)


HALO = "rgba(154,165,177,0.45)"  # light outline so navy stays visible on Streamlit's dark background


def navy_line(fig, x, y, name, **kw):
    """Navy line drawn over a light halo: reads on light and dark themes without changing the palette."""
    fig.add_trace(go.Scatter(x=x, y=y, mode="lines", line=dict(color=HALO, width=6),
                             hoverinfo="skip", showlegend=False, **kw))
    fig.add_trace(go.Scatter(x=x, y=y, name=name, mode="lines+markers", line=dict(color=NAVY, width=2.5),
                             marker=dict(size=5, color=NAVY, line=dict(color=HALO, width=1)), **kw))


def outlined(color):
    """Marker style for bars/points: fill colour plus the halo outline."""
    return dict(color=color, line=dict(color=HALO, width=1))


def show_fig(fig):
    st.plotly_chart(fig, use_container_width=True, theme="streamlit")


def show_mini(fig, title):
    """Small card chart: no legend, no toolbar, tight margins."""
    fig.update_layout(height=170, margin=dict(l=0, r=0, t=28, b=0), showlegend=False,
                      title=dict(text=title, font=dict(size=12)), bargap=0.15)
    fig.update_xaxes(showgrid=False)
    st.plotly_chart(fig, use_container_width=True, theme="streamlit", config={"displayModeBar": False})


# ---------------------------------------------------------------- page

st.set_page_config(page_title="WSC track record audit pack", layout="wide")

data = load_all()
outcomes = load_outcomes()
integrity, integrity_ok, integrity_note = integrity_check()
sb = strategy_book(data["rollover"])
tw = twap_book(data["twap"])
op = options_book(data["options"], outcomes)

st.title("White Star Capital: track record audit pack")
st.caption("Uniswap LP, spot execution and OTC options. Every figure is computed from the CSVs in the Data & verification tab.")
n_files = int((integrity["match"] == "yes").sum())
if integrity_ok:
    st.success(f"✔ Integrity verified: {n_files} files match manifest.csv", icon=None)
else:
    reason = f" ({integrity_note})" if integrity_note else ""
    st.warning(f"⚠ Integrity check failed{reason}. See the Data & verification tab for the files that do not match.")

t_over, t_lp, t_twap, t_opt, t_data = st.tabs(
    ["Overview", "Uniswap LP", "Spot Execution", "Options", "Data & verification"])

# ---- Overview
with t_over:
    f = tw["fills"]
    head = sb["head"]
    st.markdown("#### Three strategies, one audit trail")
    st.caption("Each card is computed from the CSVs in the Data & verification tab; open the strategy's tab for every row "
               "behind it.")

    col_lp, col_spot, col_opt = st.columns(3)

    with col_lp:
        with st.container(border=True):
            st.markdown("**Uniswap LP** · fee income")
            m1, m2 = st.columns(2)
            m1.metric("Fee APR", pct(sb["fee_apr"]))
            m2.metric("Time in range", pct(sb["time_in_range"], 1))
            fig = go.Figure()
            navy_line(fig, head.index, ((1 + head["fee_yield"]).cumprod() - 1) * 100, "Cumulative fee yield")
            fig.update_yaxes(ticksuffix="%")
            show_mini(fig, "Cumulative fee yield")
            st.caption(f"{sb['start']:%d %b} – {sb['daily'].index.max():%d %b %Y} · {sb['positions_head']} positions · "
                       f"{sb['n_days']} days deployed")

    with col_spot:
        with st.container(border=True):
            st.markdown("**Spot Execution** · 0x TWAP orders")
            m1, m2 = st.columns(2)
            m1.metric("Median cost", bps(f["cost_bps"].median()))
            m2.metric("At or better than quote", pct((f["cost_bps"] <= 0).mean(), 0))
            fig = go.Figure(go.Histogram(x=f["cost_bps"].clip(-40, 40), nbinsx=40, marker=outlined(ORANGE)))
            fig.add_vline(x=0, line_color=GREY, line_dash="dash")
            fig.update_xaxes(ticksuffix=" bps")
            show_mini(fig, "Cost per fill (tails clipped at ±40 bps)")
            st.caption(f"{pd.to_datetime(f['trade_date'].min()):%d %b} – {pd.to_datetime(f['trade_date'].max()):%d %b %Y} · "
                       f"{len(tw['orders'])} orders · {len(f)} fills")

    with col_opt:
        with st.container(border=True):
            st.markdown("**OTC Options** · short-call premium")
            realized = op["realized_pnl_pct_calc"].dropna()
            m1, m2 = st.columns(2)
            m1.metric("Realized yield", f"{realized.mean():.2f}%" if len(realized) else "—")
            m2.metric("Annualized", f"{op['annualized_yield_pct_calc'].mean():.1f}%")
            labels = [f"{u} {k:,.0f}C<br>{pd.to_datetime(e):%d %b}" for u, k, e in
                      zip(op["underlying"], op["strike"], op["expiry_at"])]
            fig = go.Figure(go.Bar(x=labels, y=op["premium_pct_notional"], marker=outlined(TEAL),
                                   text=[f"{v:.2f}%" for v in op["premium_pct_notional"]], textposition="outside"))
            fig.update_yaxes(ticksuffix="%", range=[0, op["premium_pct_notional"].max() * 1.35])
            fig.update_xaxes(tickangle=0)
            show_mini(fig, "Premium per leg, % of notional at strike")
            outcomes_txt = ", ".join(f"{n} {s.lower()}" for s, n in op["status"].value_counts().items())
            st.caption(f"{len(op)} legs with {', '.join(sorted(op['counterparty'].dropna().unique()))} · {outcomes_txt}")

    st.markdown("#### Desk activity")
    fig = go.Figure()
    daily = sb["daily"]
    pilot = daily.index < sb["start"]
    for mask, name, color in [(pilot, "Uniswap LP (pilot)", GREY), (~pilot, "Uniswap LP", NAVY)]:
        d = daily[mask]
        fig.add_trace(go.Scatter(
            x=d.index, y=["Uniswap LP"] * len(d), mode="markers", name=name,
            marker=dict(symbol="square", size=6 + 2 * np.sqrt(d["positions"]), **outlined(color)),
            customdata=d["positions"], hovertemplate="%{x|%d %b %Y}<br>%{customdata} positions open<extra></extra>"))
    by_day = f.groupby("trade_date").agg(fills=("cost_bps", "size"), cost=("cost_bps", "median"))
    fig.add_trace(go.Scatter(
        x=pd.to_datetime(by_day.index), y=["Spot Execution"] * len(by_day), mode="markers", name="Spot Execution",
        marker=dict(size=6 + 2 * np.sqrt(by_day["fills"]), **outlined(ORANGE)),
        customdata=np.stack([by_day["fills"], by_day["cost"]], axis=1),
        hovertemplate="%{x|%d %b %Y}<br>%{customdata[0]} fills · median %{customdata[1]:.2f} bps<extra></extra>"))
    for i, row in op.reset_index(drop=True).iterrows():
        x0, x1 = pd.to_datetime(row["trade_date"]), pd.to_datetime(row["expiry_at"]).tz_localize(None)
        fig.add_trace(go.Scatter(
            x=[x0, x1], y=["OTC Options"] * 2, mode="lines+markers", name="OTC Options", showlegend=i == 0,
            line=dict(color=TEAL, width=4), marker=dict(size=9, **outlined(TEAL)),
            hovertemplate=f"{row['underlying']} {row['strike']:,.0f} call · {row['status'].lower()}"
                          "<br>%{x|%d %b %Y}<extra></extra>"))
    fig.update_yaxes(categoryorder="array", categoryarray=["OTC Options", "Spot Execution", "Uniswap LP"], title=None)
    fig.update_xaxes(showgrid=True)
    show_fig(fig_layout(fig, height=300, title=None))
    st.caption("LP: one square per day with positions open, sized by how many (pilot in grey). Spot execution: one dot per "
               "trading day, sized by fills. Options: each leg from trade date to expiry.")

    st.caption("All figures are shown as % of capital, not USD. LP figures are fee income on deployed capital, gross of fund "
               "fees; changes in LP position value and hedge P&L are not included. "
               "Spot execution figures are equal-weighted: the export has no trade size.")

# ---- Uniswap LP
with t_lp:
    st.subheader("Uniswap LP strategy")
    st.caption(f"Positions file, headline period from {sb['start']:%d %b %Y} (first day with ≥ "
               f"${HEADLINE_MIN_CAPITAL:,} deployed). Fee income on deployed capital; changes in LP position value "
               "are not included.")
    head = sb["head"]
    c1, c2, c3 = st.columns(3)
    card(c1, "Fee APR", pct(sb["fee_apr"]), "fees ÷ capital-days × 365, simple")
    card(c2, "Cumulative fee yield", pct(sb["fee_cum"]), f"{sb['n_days']} days with capital deployed")
    card(c3, "Time in range", pct(sb["time_in_range"], 1), "share of recorded position-days")
    st.caption(f"{sb['unrecorded_position_days']} position-days have no snapshot. Positions count as open from their first "
               "to their last recorded day; fees earned over a gap are booked on the first day after it.")
    if sb["fee_method"] == "token balances":
        st.caption("Fee income is accrued from the end-of-day fee-token balances (`fee_token0`, `fee_token1`) valued at "
                   "`current_price`, not from `fees_accrued_usd`: this export's `fees_accrued_usd` counts price moves "
                   "in the unclaimed balance as income. A re-export with the corrected query 1 is used as-is.")

    fig = go.Figure()
    fig.add_trace(go.Bar(x=head.index, y=head["fee_yield"] * 100, name="Daily fee yield (%)", marker_color=ORANGE,
                         yaxis="y2", opacity=0.8))
    navy_line(fig, head.index, ((1 + head["fee_yield"]).cumprod() - 1) * 100, "Cumulative fee yield (%)")
    fig.update_layout(yaxis=dict(title="Cumulative fee yield", ticksuffix="%"),
                      yaxis2=dict(title="Daily fee yield", ticksuffix="%", overlaying="y", side="right", showgrid=False))
    show_fig(fig_layout(fig, title="Fee yield on deployed capital"))

    st.markdown("**Monthly fee yield**")
    by_month = head["fee_yield"].groupby(head.index.to_period("M"))
    monthly = pd.DataFrame({
        "Fee yield in month": by_month.apply(lambda x: (1 + x).prod() - 1),
        "Annualized (APR)": head["F"].groupby(head.index.to_period("M")).sum()
                            / head["capital"].groupby(head.index.to_period("M")).sum() * 365,
        "Days deployed": by_month.size(),
    }).T
    monthly.columns = [p.strftime("%b %Y") for p in monthly.columns]
    disp = monthly.astype(object)
    for row in ["Fee yield in month", "Annualized (APR)"]:
        disp.loc[row] = monthly.loc[row].map(pct)
    disp.loc["Days deployed"] = monthly.loc["Days deployed"].astype(int).astype(str)
    st.dataframe(disp, use_container_width=True)

    daily = sb["daily"]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=daily.index, y=daily["positions"], name="Open positions", marker_color=GREY))
    fig.add_vrect(x0=daily.index.min() - pd.Timedelta(hours=12), x1=sb["start"] - pd.Timedelta(hours=12),
                  fillcolor=GREY, opacity=0.15, line_width=0, annotation_text="pilot (excluded)",
                  annotation_position="top left")
    fig.update_yaxes(title="Positions")
    show_fig(fig_layout(fig, height=280, title="Open positions per day", showlegend=False))

    st.markdown("**Positions**")
    positions = strategy_positions(sb["rows"], data["mints"], sb["start"])
    st.dataframe(
        positions, use_container_width=True, hide_index=True,
        column_config={
            "mint_explorer": st.column_config.LinkColumn("mint explorer", display_text="explorer ↗"),
            "fees_pct": st.column_config.NumberColumn("fees (% of anchor)", format="%.2f%%"),
            "fee_apr_pct": st.column_config.NumberColumn("fee APR (simple)", format="%.1f%%"),
            "days_held": st.column_config.NumberColumn("days open"),
            "pct_days_in_range": st.column_config.NumberColumn("% days in range", format="%.0f%%"),
            "position_id": st.column_config.NumberColumn("NFT id", format="%d"),
        },
    )


# ---- Spot Execution (0x TWAP orders)
with t_twap:
    fills, orders = tw["fills"], tw["orders"]
    c1, c2, c3, c4 = st.columns(4)
    sub = f"{len(orders)} orders · {len(fills)} fills"
    card(c1, "Median cost per fill", bps(fills["cost_bps"].median()), sub)
    card(c2, "Mean cost per fill", bps(fills["cost_bps"].mean()), sub)
    card(c3, "Fills at or better than baseline", pct((fills["cost_bps"] <= 0).mean(), 1))
    card(c4, "Mean cost per order", bps(orders["cost_bps"].mean()), sub)
    st.caption("Positive bps = cost to us, for buys and sells alike. Cost = −(execution price / baseline price − 1) × 10⁴. "
               "Equal-weighted: the export has no trade size, so figures are not USD-weighted.")

    fig = go.Figure(go.Histogram(x=fills["cost_bps"], nbinsx=80, marker=outlined(NAVY)))
    fig.add_vline(x=0, line_color=GREY, line_dash="dash")
    fig.update_xaxes(title="Cost per fill (bps)")
    fig.update_yaxes(title="Fills")
    show_fig(fig_layout(fig, title="Per-fill cost vs baseline", showlegend=False))

    pairs = sorted(orders["pair"].unique())
    palette = [NAVY, ORANGE, "#2A9D8F", "#7A5195", "#C9A227", GREY, "#D1495B", "#66A3D2"]
    fig = go.Figure()
    for i, p in enumerate(pairs):
        o = orders[orders["pair"] == p]
        fig.add_trace(go.Scatter(x=o["slices"], y=o["cost_bps"], mode="markers", name=p,
                                 marker=dict(size=8, opacity=0.85, **outlined(palette[i % len(palette)])),
                                 customdata=o[["order_id", "date"]],
                                 hovertemplate="%{customdata[0]}<br>%{customdata[1]}<br>%{x} slices · %{y:.2f} bps"))
    fig.add_hline(y=0, line_color=GREY, line_dash="dash")
    fig.update_xaxes(title="Slices (fills) per order, log scale", type="log",
                     tickvals=[1, 2, 3, 5, 10, 20, 30], ticktext=["1", "2", "3", "5", "10", "20", "30"])
    fig.update_yaxes(title="Order cost (bps)")
    fig = fig_layout(fig, height=420, title="Order cost vs number of slices per order")
    fig.update_layout(legend=dict(y=-0.28), margin=dict(b=30))
    show_fig(fig)

    st.markdown("**Orders**")
    st.dataframe(orders, use_container_width=True, hide_index=True, column_config={
        "cost_bps": st.column_config.NumberColumn("mean cost (bps)", format="%.2f"),
        "best_fill_bps": st.column_config.NumberColumn("best fill (bps)", format="%.2f"),
        "worst_fill_bps": st.column_config.NumberColumn("worst fill (bps)", format="%.2f"),
    })
    with st.expander("Every fill"):
        cols = list(data["twap"].columns) + ["chain", "side", "pair", "cost_bps"]
        st.dataframe(fills[cols], use_container_width=True, hide_index=True)

# ---- Options
with t_opt:
    c1, c2, c3, c4 = st.columns(4)
    realized = op["realized_pnl_pct_calc"].dropna()
    card(c1, "Avg premium % of notional at strike", f"{op['premium_pct_notional'].mean():.3f}%")
    card(c2, "Avg annualized yield", f"{op['annualized_yield_pct_calc'].mean():.2f}%")
    card(c3, "Realized P&L (% of notional)", f"{realized.mean():.3f}%" if len(realized) else "—",
         f"{len(realized)} of {len(op)} legs settled")
    card(c4, "Legs", f"{len(op)}")
    st.caption("Notional is at strike (spot at trade is not exported). Legs with status EXPIRED WORTHLESS settle with zero "
               "intrinsic value, so realized P&L equals the full premium. Legs still awaiting settlement show \"—\".")
    fig = go.Figure()
    for i, u in enumerate(sorted(op["underlying"].unique())):
        o = op[op["underlying"] == u]
        fig.add_trace(go.Bar(x=o["id"], y=o["premium_pct_notional"], name=u,
                             marker=outlined([NAVY, ORANGE, GREY][i % 3])))
    fig.update_yaxes(title="Premium (% of notional)", ticksuffix="%")
    show_fig(fig_layout(fig, height=320, title="Premium yield per leg"))
    usd_cols = ["size", "premium_usd", "premium_per_coin", "notional_at_strike", "intrinsic_at_expiry_usd",
                "net_pnl_usd", "intrinsic_calc", "realized_pnl_calc"]
    table = op.drop(columns=[c for c in usd_cols if c in op.columns])
    table["realized_pnl_pct_calc"] = table["realized_pnl_pct_calc"].map(lambda x: "—" if pd.isna(x) else f"{x:.3f}%")
    st.dataframe(table, use_container_width=True, hide_index=True, column_config={
        "realized_pnl_pct_calc": st.column_config.TextColumn("realized P&L % notional (calc)"),
        "premium_pct_notional": st.column_config.NumberColumn("premium % notional (calc)", format="%.3f%%"),
        "annualized_yield_pct_calc": st.column_config.NumberColumn("annualized yield (calc)", format="%.2f%%"),
        "days_calc": st.column_config.NumberColumn("days (calc)"),
    })

# ---- Data & verification
with t_data:
    st.subheader("Integrity")
    st.dataframe(integrity, use_container_width=True, hide_index=True)
    st.markdown(
        "**How to audit**\n"
        "1. Download the files below into one folder and run `sha256sum -c manifest.csv`. Every line should say OK "
        "and match the table above.\n"
        "2. Spot-check mint transactions: each explorer link opens Etherscan or Arbiscan for the position's chain.\n"
        "3. Recompute the metrics from the CSVs with the formulas in the data dictionary below; headline figures "
        "should match to within 0.1% (returns) and 0.1 bps (spot execution)."
    )
    cols = st.columns(4)
    downloads = list(FILES.values()) + EXTRA_DOWNLOADS + [MANIFEST]
    for i, name in enumerate(downloads):
        path = DATA_DIR / name
        if path.exists():
            cols[i % 4].download_button(f"⬇ {name}", path.read_bytes(), file_name=name,
                                        mime="text/csv" if name.endswith(".csv") else "text/markdown",
                                        use_container_width=True)

    with st.expander("Data dictionary and metric definitions"):
        st.markdown(f"""
| File | Grain | Used for |
|---|---|---|
| `{FILES['rollover']}` | position × day | Fee APR, fee yield, time in range, positions table |
| `{FILES['daily']}` | day | Reconciliation only |
| `{FILES['mints']}` | mint | NFT id and mint tx links |
| `{FILES['twap']}` | spot execution slice (fill) of a 0x TWAP order | Execution cost |
| `{FILES['options']}` | option leg | Premium and yield |
| `{OUTCOMES}` | option leg | Desk-confirmed outcomes, applied only where the export has no settlement price |

**Uniswap LP.** F = daily fee income per position. If the export has `fees_accrued_token0` (corrected query 1), F =
`fees_accrued_usd`. Otherwise F is accrued from the end-of-day balances `fee_token0`/`fee_token1`: a rise is the accrual, a fall
means a collection and the new balance counts, the first observation counts in full; token0 is valued at `current_price`
(`pool_price` if blank), token1 at 1. Rows with no fee balances (`fee_source = error`) book 0. A position is open on every calendar day from its
first to its last recorded day; days with no snapshot carry the anchor forward with F = 0 (the export books a gap's fees on the
first recorded day after it). Daily fee yield y = Σ F / Σ anchor of positions open that day. Headline period starts on the first
day Σ anchor ≥ ${HEADLINE_MIN_CAPITAL:,}. Fee APR = Σ F / Σ (anchor × days open) × 365; cumulative fee yield = Π(1 + y) − 1;
monthly fee yield compounds y within the month and its APR uses that month's fees and capital-days. Time in range = share of
recorded position-days with `in_range` true. Position fee APR = (Σ F / anchor) × 365 / days open.
Changes in LP position value (`lp_pnl_usd`, `lp_notional_usd`) are not part of any figure.

**Spot execution** (0x TWAP orders, `twap_tca_1.csv`). Prices are output tokens per input token in raw units, so higher is better for us on either side.
Cost per fill = −(`execution_price` / `baseline_price` − 1) × 10⁴ = −`deviation_bps`. Order cost = mean of its fills.
Chain, from-token and to-token come from `order_id` segments 4–6. Side: buy if from-token is a stablecoin, sell if to-token is.

**Options.** Days = expiry date − trade date (min 1). Notional = size × strike. Premium % = premium / notional;
annualized = that × 365 / days. Realized P&L % = (premium − intrinsic) / notional for short legs, where intrinsic = size × max(settlement − strike, 0)
for calls, or 0 when `status` = EXPIRED WORTHLESS (desk-confirmed; settlement price not recorded).

**Display.** P&L is shown as % of capital only. USD values remain in the CSVs (downloads below) so the percentages can be recomputed.
""")
    with st.expander("Token mapping (spot execution)"):
        st.dataframe(pd.DataFrame(TOKEN_MAP_ROWS, columns=["address prefix", "ticker", "chain"]),
                     hide_index=True, use_container_width=True)
        if tw["unmapped"]:
            st.warning("Unmapped tokens (shown raw): " + ", ".join(tw["unmapped"]))
        else:
            st.caption("All tokens in the export are mapped.")
        st.caption(f"Check: max |cost_bps + deviation_bps| = {tw['check']:.6f} bps.")
    with st.expander("Price source for the spot execution baseline"):
        st.markdown(
            "`baseline_price` comes from the TCA export (`twap_tca_1.csv`). It is one price per order, quoted the same way as "
            "the fills (output per input, raw units), so it is a 0x quote, not a market mid. The first fill executes at exactly "
            "the baseline in 70 of 288 orders, which fits an executable quote taken at order start. Cost vs baseline therefore "
            "measures slippage against our own arrival quote (which already includes 0x routing and fees), not against an "
            "independent mid. Half of all orders (142) are a single slice, so their cost is first fill vs that quote.")
    with st.expander("Known data issues (shown, never auto-corrected)"):
        for title, rows in known_issues(data, op):
            st.markdown(f"**{title}** — {len(rows)} row(s)")
            if len(rows):
                st.dataframe(rows, use_container_width=True, hide_index=True)
    with st.expander("Reconciliation: positions file vs daily file"):
        rec = reconciliation(data["rollover"], data["daily"])
        diff = rec[(rec["positions_diff"] != 0) | (rec["in_range_diff"] != 0) | (rec["fees_diff"].abs() > 0.01)]
        st.markdown(f"{len(rec)} days compared, {len(diff)} with a difference.")
        if len(diff):
            st.dataframe(diff, use_container_width=True, hide_index=True)
