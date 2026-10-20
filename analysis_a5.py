#!/usr/bin/env python3
"""
Module A5: who paid? Counterparties of the suspected informed wallets
====================================================================

Every fill has two sides, so a flagged wallet's profit on a fill is exactly
the loss of the wallet on the other side (Polymarket charged no trading fees
on these markets). This module finds those counterparties and classifies them.

    python analysis_a5.py   # data/analysis/a5_counterparties.csv  (counterparty x market)
                            # data/analysis/a5_summary.csv         (per event / market)
                            # charts/a5_who_paid.png

Scope: A3-flagged wallets in the leak events (and, for comparison, the Iran
2025 control), their fills in the same 7-day pre-news window as A3, valued at
resolution in YES terms: P&L of q YES shares bought at price p = q * (Y - p),
with Y = 1 if YES won, else 0.

Counterparty classes, from each wallet's behaviour in that market over the
30 days before news (our clean fills, both roles):
  flagged        another A3-flagged wallet (an internal transfer)
  market_maker   >= 50 fills, >= 80% of them passive (maker), and two-sided
                 (|net shares| / gross shares <= 0.5)
  limit_bettor   >= 3 fills, >= 80% passive but one-sided (> 0.5): resting
                 limit orders on one side, e.g. a standing bet against the
                 long shot
  active         >= 20 fills, none of the above
  occasional     < 20 fills, none of the above

Caveat: the "loss" is the counterparty's P&L on these fills at resolution. A
counterparty that bought earlier and sold to a flagged wallet (taking profit)
shows a loss relative to holding, not necessarily an overall loss.
"""

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import polymarket_feasibility as pf  # noqa: E402

OUT = ROOT / "data/analysis"
WINDOW_DAYS, LOOKBACK_DAYS = 7, 30
MM_MIN_FILLS, MM_MIN_MAKER, MM_MAX_ONE_SIDED, ACTIVE_MIN = 50, 0.8, 0.5, 20
LB_MIN_FILLS = 3

EVENT = {  # market -> event it belongs to
    "maduro_out_jan31": "Maduro", "maduro_out_mar31": "Maduro", "maduro_out_feb28": "Maduro",
    "maduro_out_dec31_2026": "Maduro", "us_forces_venezuela_jan31": "Maduro",
    "nobel_machado": "Nobel 2025", "nobel_trump": "Nobel 2025",
    "award_nfl_mvp_2025": "NFL MVP 2024-25", "nfl_mvp_lamar": "NFL MVP 2024-25",
    "nfl_mvp_burrow": "NFL MVP 2024-25", "nfl_mvp_goff": "NFL MVP 2024-25",
    "iran_strike_2026_02_28": "Iran strike Feb 2026", "iran_usisrael_by_feb28": "Iran strike Feb 2026",
    "iran_strike_2025_06_control": "Iran strikes Jun 2025 (control)",
}
CLASSES = ["market_maker", "limit_bettor", "active", "occasional", "flagged"]
COLORS = {"market_maker": "#2a78d6", "limit_bettor": "#6250d6", "active": "#eb6834",
          "occasional": "#1baf7a", "flagged": "#c9c8c3"}
LABELS = {"market_maker": "Market makers (two-sided)", "limit_bettor": "One-sided limit-order bettors",
          "active": "Active traders", "occasional": "Occasional traders", "flagged": "Other flagged wallets"}


def legs(tr):
    """Both sides of every fill, YES shares signed per party."""
    q = np.where(tr["side"].eq("BUY"), 1.0, -1.0) * tr["size_shares"]
    base = dict(ts=tr["timestamp_utc"], p=tr["yes_price"], tx=tr["tx_hash"])
    a = pd.DataFrame({**base, "wallet": tr["wallet"], "other": tr["maker_wallet"], "q": q, "maker": False})
    b = pd.DataFrame({**base, "wallet": tr["maker_wallet"], "other": tr["wallet"], "q": -q, "maker": True})
    return pd.concat([a, b], ignore_index=True)


def classify(l30, flagged):
    g = l30.groupby("wallet").agg(n=("q", "size"), maker_share=("maker", "mean"),
                                  net=("q", "sum"), gross=("q", lambda x: x.abs().sum()))
    g["one_sided"] = g["net"].abs() / g["gross"]
    cls = np.where(g.index.isin(flagged), "flagged",
          np.where((g["n"] >= MM_MIN_FILLS) & (g["maker_share"] >= MM_MIN_MAKER)
                   & (g["one_sided"] <= MM_MAX_ONE_SIDED), "market_maker",
          np.where((g["n"] >= LB_MIN_FILLS) & (g["maker_share"] >= MM_MIN_MAKER), "limit_bettor",
          np.where(g["n"] >= ACTIVE_MIN, "active", "occasional"))))
    return g.assign(cls=cls)


def main():
    mk = pd.read_csv(ROOT / "markets.csv").set_index("key")
    a3 = pd.read_csv(OUT / "a3_wallets.csv")
    a2 = pd.read_csv(OUT / "a2_microstructure.csv").set_index("key")
    news_of = {c["key"]: c["news_utc"] for c in pf.MARKETS}
    rows = []
    for key, event in EVENT.items():
        flagged = set(a3[(a3["key"] == key) & a3["flag"]]["wallet"])
        if not flagged:
            continue
        news = news_of[key]
        Y = float(json.loads(mk.loc[key, "final_outcome_prices"])[0])
        tr = pd.read_parquet(pf.CLEAN / f"trades_{key}.parquet")
        L = legs(tr)
        prof = classify(L[(L["ts"] >= news - pd.Timedelta(days=LOOKBACK_DAYS)) & (L["ts"] < news)], flagged)
        f = L[L["wallet"].isin(flagged) & (L["ts"] >= news - pd.Timedelta(days=WINDOW_DAYS)) & (L["ts"] < news)].copy()
        f["pnl_flagged"] = f["q"] * (Y - f["p"])          # = counterparty's loss on this fill
        f["cp_passive"] = ~f["maker"]                     # counterparty was the resting order
        cp = f.groupby("other").agg(transfer_usd=("pnl_flagged", "sum"), fills=("q", "size"),
                                    shares=("q", lambda x: x.abs().sum()),
                                    passive_share=("cp_passive", "mean"),
                                    first=("ts", "min"), last=("ts", "max")).reset_index()
        cp = cp.rename(columns={"other": "counterparty"}).join(
            prof[["cls", "n", "maker_share", "one_sided"]], on="counterparty")
        cp["cls"] = cp["cls"].fillna("occasional")
        cp.insert(0, "key", key)
        cp.insert(1, "event", event)
        rows.append(cp)
    cps = pd.concat(rows, ignore_index=True)
    cps.to_csv(OUT / "a5_counterparties.csv", index=False)

    def summ(g):
        tot = g["transfer_usd"].sum()
        by = g.groupby("cls")["transfer_usd"].sum().reindex(CLASSES, fill_value=0)
        losers = g[g["transfer_usd"] > 0].sort_values("transfer_usd", ascending=False)
        out = {"counterparties": g["counterparty"].nunique(), "transfer_usd": tot,
               **{f"from_{c}": by[c] for c in CLASSES},
               "share_market_makers": by["market_maker"] / tot if tot else np.nan,
               "top10_share": losers["transfer_usd"].head(10).sum() / losers["transfer_usd"].sum()
               if len(losers) else np.nan,
               "passive_share_of_fills": np.average(g["passive_share"], weights=g["fills"])}
        return pd.Series(out)
    by_event = cps.groupby("event").apply(summ, include_groups=False).reset_index()
    by_market = cps.groupby(["event", "key"]).apply(summ, include_groups=False).reset_index()
    by_market["lambda_ratio_a2"] = by_market["key"].map(a2["lambda_ratio"])
    summary = pd.concat([by_event.assign(level="event"), by_market.assign(level="market")], ignore_index=True)
    summary.to_csv(OUT / "a5_summary.csv", index=False)
    pd.set_option("display.width", 220)
    print(by_event.round(2).to_string(index=False))
    print(by_market.round(2).to_string(index=False))
    chart(by_event)
    return cps, summary


def chart(by_event):
    order = ["Maduro", "Nobel 2025", "Iran strike Feb 2026", "NFL MVP 2024-25", "Iran strikes Jun 2025 (control)"]
    e = by_event.set_index("event").reindex([o for o in order if o in set(by_event["event"])])
    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=150)
    y = np.arange(len(e))[::-1]
    left = np.zeros(len(e))
    for c in CLASSES:
        v = e[f"from_{c}"].clip(lower=0).to_numpy() / 1e3
        ax.barh(y, v, left=left, color=COLORS[c], height=0.6, label=LABELS[c], edgecolor="white", lw=1.5)
        left += v
    for yi, (tot, mm) in zip(y, zip(e["transfer_usd"], e["share_market_makers"])):
        ax.text(left[list(y).index(yi)] + 5, yi, f"${tot / 1e3:,.0f}k  ({mm:.0%} from market makers)",
                va="center", fontsize=8, color="#52514e")
    ax.set_yticks(y, e.index, fontsize=9)
    ax.set_xlabel("Paid to flagged wallets at resolution ($k), by counterparty type")
    ax.set_xlim(0, left.max() * 1.45)
    ax.grid(axis="x", alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(fontsize=8, frameon=False, loc="lower right", ncol=2)
    ax.set_title("A5: who paid the suspected informed wallets? (7 days before news)", fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(ROOT / "charts/a5_who_paid.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
