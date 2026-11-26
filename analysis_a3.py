#!/usr/bin/env python3
"""
Module A3: wallet forensics and a simple detection rule
=======================================================

Which wallets positioned themselves for the outcome before the news, and do
they look like suspected informed traders? Then: does a simple, transparent
rule catch the publicly named wallets without flagging many wallets in the
control events?

    python analysis_a3.py   # data/analysis/a3_wallets.csv      (one row per wallet x event)
                            # data/analysis/a3_summary.csv      (one row per event)
                            # charts/a3_flags.png

Window: the WINDOW_DAYS before each event's news time (from MARKETS).
Both trading roles count (taker and maker), from the clean YES-normalised
fills. "Towards the outcome" means buying what eventually happened (for the
unresolved 2026 Ballon d'Or, the current favourite).

Per wallet (candidates: net cost towards the outcome >= MIN_NET_USD):
  net_usd        net $ paid for outcome exposure in the window
  one_sided      |net shares| / gross shares traded in the window (0-1)
  entry_prob     average price paid for the outcome = market's probability
  pnl_usd        profit of the window's trades at resolution (mark-to-market
                 for the open 2026 market)
  max_sweep      most resting orders hit in one aggressive order
  age_days       news time minus the wallet's first-ever Polymarket activity
  n_events_pre   distinct events in its last <=500 activities before news
                 (a few = single-topic wallet)

Rules, all conditions must hold:
  flag_profile  age_days <= 14, n_events_pre <= 5, one_sided >= 0.8,
                net_usd >= 1,000, entry_prob <= 0.75   (who the wallet is)
  flag          flag_profile AND dollar-weighted average time of its buys
                <= 24h before news (when most of the money went in)
The profile rule alone also flags many fresh single-topic wallets that join a
hot public topic days earlier (e.g. 54 in the Iran 2025 control); adding the
timing condition removes most of them. All thresholds were set after seeing
the publicly named cases, so recall on them is in-sample; the informative
number is how few control-event wallets each rule flags.

Wallet histories come from the public Data API /activity endpoint and are
cached in data/raw/wallet_profiles.parquet.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import analysis_a1 as a1  # noqa: E402  (GROUP)
import polymarket_feasibility as pf  # noqa: E402

OUT = ROOT / "data/analysis"
CACHE = pf.RAW / "wallet_profiles.parquet"
WINDOW_DAYS = 7
MIN_NET_USD = 1_000
RULE = dict(max_age_days=14, max_events=5, min_one_sided=0.8, min_net_usd=1_000,
            max_entry_prob=0.75, max_buy_hours_before_news=24)

# Publicly named (press / DOJ) vs flagged by our own earlier analysis.
NAMED = {
    "nobel_machado": {"0x234cc49e43dff8b3207bbd3a8a2579f339cb9867": "dirtycup (press)",
                      "0xa430506774f9efaf39903ee7e0db1351f66891ca": "6741 (press)",
                      "0x551e72eda42a5ab39d6d78239a1d9bbb5db6b0e0": "GayPride (press)"},
    "maduro_out_jan31": {"0x31a56e9e690c621ed21de08cb559e9524cdb8ed9": "0x31a56e9e (press, DOJ facts)"},
    "award_nfl_mvp_2025": {"0xa5d2078aeef37590971c5a08b63089a2820a822a": "boobyface (ours)"},
    "iran_strike_2026_02_28": {w: f"{w[:10]} (ours)" for w in pf.REPORTED["iran_strike_2026_02_28"]["wallets"]},
    "bdo_2026_kane": {"0x3ed2c452f141c291adecb97ddfc72b1650acff3d": "0x3ed2c452 (ours)"},
}


def wallet_fills(tr):
    """One row per (wallet, fill) for both roles, YES-direction signed shares."""
    sgn = np.where(tr["side"].eq("BUY"), 1.0, -1.0)
    legs = tr.groupby("tx_hash")["tx_hash"].transform("size")
    taker = pd.DataFrame({"wallet": tr["wallet"], "ts": tr["timestamp_utc"], "q": sgn * tr["size_shares"],
                          "p": tr["yes_price"], "legs": legs, "taker": True})
    maker = pd.DataFrame({"wallet": tr["maker_wallet"], "ts": tr["timestamp_utc"], "q": -sgn * tr["size_shares"],
                          "p": tr["yes_price"], "legs": 0, "taker": False})
    return pd.concat([taker, maker], ignore_index=True)


def wallet_features(tr, news, d, payoff):
    f = wallet_fills(tr)
    f = f[(f["ts"] >= news - pd.Timedelta(days=WINDOW_DAYS)) & (f["ts"] < news)].copy()
    if f.empty:
        return pd.DataFrame()
    f["s"] = d * f["q"]                                    # shares towards the outcome
    f["po"] = np.where(d == 1, f["p"], 1 - f["p"])          # price of the outcome
    f["cost"] = f["s"] * f["po"]
    f["pnl"] = f["s"] * (payoff - f["po"])
    f["buy_s"] = f["s"].clip(lower=0)
    f["buy_usd"] = f["buy_s"] * f["po"]
    f["buy_usd_h"] = f["buy_usd"] * (news - f["ts"]) / pd.Timedelta(hours=1)
    g = f.groupby("wallet").agg(
        net_shares=("s", "sum"), gross_shares=("s", lambda x: x.abs().sum()),
        net_usd=("cost", "sum"), pnl_usd=("pnl", "sum"),
        buy_shares=("buy_s", "sum"), buy_usd=("buy_usd", "sum"), buy_usd_h=("buy_usd_h", "sum"),
        n_fills=("s", "size"),
        taker_share=("taker", "mean"), max_sweep=("legs", "max"),
        first_fill=("ts", "min"), last_fill=("ts", "max"))
    buys = f[f["s"] > 0].groupby("wallet").apply(
        lambda x: (x["s"] * x["po"]).sum() / x["s"].sum(), include_groups=False)
    g["entry_prob"] = buys
    g["one_sided"] = g["net_shares"].abs() / g["gross_shares"]
    g["buy_hours_before_news"] = g["buy_usd_h"] / g["buy_usd"]   # $-weighted
    g = g.drop(columns=["buy_usd_h"])
    return g[g["net_usd"] >= MIN_NET_USD].reset_index()


def get_json(url, params):
    for i in range(5):
        try:
            r = pf.session.get(url, params=params, timeout=40)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1 + 2 * i)
    return None


def profile(wallet, news):
    """First-ever activity time and distinct events in the last <=500 activities before news."""
    end = int(news.timestamp())
    pre = get_json(f"{pf.DATA}/activity", {"user": wallet, "limit": 500, "end": end,
                                           "sortBy": "TIMESTAMP", "sortDirection": "DESC"}) or []
    if len(pre) < 500 and pre:
        first = min(x["timestamp"] for x in pre)
    else:
        a = get_json(f"{pf.DATA}/activity", {"user": wallet, "limit": 1,
                                             "sortBy": "TIMESTAMP", "sortDirection": "ASC"}) or []
        first = a[0]["timestamp"] if a else np.nan
    return dict(wallet=wallet, news_utc=news.isoformat(),
                first_activity=pd.to_datetime(first, unit="s", utc=True) if first == first else pd.NaT,
                n_activities_pre=len(pre),
                n_events_pre=len({x.get("eventSlug") for x in pre}),
                top_event_pre=pd.Series([x.get("eventSlug") for x in pre]).mode().iat[0] if pre else None)


def profiles(pairs):
    cache = pd.read_parquet(CACHE) if CACHE.exists() else pd.DataFrame(columns=["wallet", "news_utc"])
    cache = cache.drop_duplicates(["wallet", "news_utc"], keep="last")
    have = set(zip(cache["wallet"], cache["news_utc"]))
    todo = list(dict.fromkeys((w, n) for w, n in pairs if (w, n.isoformat()) not in have))
    print(f"wallet profiles: {len(pairs) - len(todo)} cached, {len(todo)} to fetch")
    if todo:
        with ThreadPoolExecutor(max_workers=6) as ex:
            new = list(ex.map(lambda x: profile(*x), todo))
        cache = pd.concat([cache, pd.DataFrame(new)], ignore_index=True)
    cache = cache.drop_duplicates(["wallet", "news_utc"], keep="last")
    cache.to_parquet(CACHE, index=False)
    return cache


def main():
    mk = pd.read_csv(ROOT / "markets.csv").set_index("key")
    a1res = pd.read_csv(OUT / "a1_event_study.csv").set_index("key")
    frames = []
    for cfg in pf.MARKETS:
        key, news = cfg["key"], cfg["news_utc"]
        tr = pd.read_parquet(pf.CLEAN / f"trades_{key}.parquet")
        prices = json.loads(mk.loc[key, "final_outcome_prices"])
        d = int(a1res.loc[key, "direction"])
        resolved = prices in (["1", "0"], ["0", "1"])
        last = tr["yes_price"].iloc[-1]
        payoff = 1.0 if resolved else (last if d == 1 else 1 - last)
        g = wallet_features(tr, news, d, payoff)
        if g.empty:
            continue
        g.insert(0, "key", key)
        g.insert(1, "group", a1.GROUP.get(key, "award_panel"))
        g["news_utc"] = news
        frames.append(g)
    w = pd.concat(frames, ignore_index=True)

    prof = profiles(list(zip(w["wallet"], w["news_utc"])))
    prof["news_utc"] = prof["news_utc"].astype(str)
    w["news_key"] = w["news_utc"].map(lambda t: t.isoformat())
    w = w.merge(prof.rename(columns={"news_utc": "news_key"}), on=["wallet", "news_key"], how="left")
    w["age_days"] = (w["news_utc"] - pd.to_datetime(w["first_activity"], utc=True)) / pd.Timedelta(days=1)
    w["flag_profile"] = ((w["age_days"] <= RULE["max_age_days"]) & (w["n_events_pre"] <= RULE["max_events"])
                         & (w["one_sided"] >= RULE["min_one_sided"]) & (w["net_usd"] >= RULE["min_net_usd"])
                         & (w["entry_prob"] <= RULE["max_entry_prob"]))
    w["hours_before_first_fill"] = (w["news_utc"] - w["first_fill"]) / pd.Timedelta(hours=1)
    w["flag"] = w["flag_profile"] & (w["buy_hours_before_news"] <= RULE["max_buy_hours_before_news"])
    w["named"] = [NAMED.get(k, {}).get(x) for k, x in zip(w["key"], w["wallet"])]
    w = w.drop(columns=["news_key"]).sort_values(["key", "net_usd"], ascending=[True, False])
    w.to_csv(OUT / "a3_wallets.csv", index=False)

    s = w.groupby(["key", "group"]).agg(
        candidates=("wallet", "size"), flagged_profile=("flag_profile", "sum"), flagged=("flag", "sum"),
        flagged_net_usd=("net_usd", lambda x: x[w.loc[x.index, "flag"]].sum()),
        flagged_pnl_usd=("pnl_usd", lambda x: x[w.loc[x.index, "flag"]].sum()),
        all_cand_pnl_usd=("pnl_usd", "sum")).reset_index()
    named = [(k, x, lbl) for k, m in NAMED.items() for x, lbl in m.items()]
    hits = {(k, x): bool(w[(w.key == k) & (w.wallet == x)]["flag"].any()) for k, x, _ in named}
    s["named_total"] = s["key"].map(lambda k: len(NAMED.get(k, {})))
    s["named_flagged"] = s["key"].map(lambda k: sum(hits[(k, x)] for x in NAMED.get(k, {})))
    s.to_csv(OUT / "a3_summary.csv", index=False)
    chart(s)
    print(s.to_string(index=False))
    print("\nNamed wallets:")
    for k, x, lbl in named:
        r = w[(w.key == k) & (w.wallet == x)]
        if r.empty:
            print(f"  {k:24s} {lbl:32s} not a candidate (net < ${MIN_NET_USD:,} in window)")
        else:
            r = r.iloc[0]
            print(f"  {k:24s} {lbl:32s} flag={r.flag!s:5s} profile={r.flag_profile!s:5s} "
                  f"buy_t={r.buy_hours_before_news:6.1f}h age={r.age_days:6.1f}d events={r.n_events_pre:3.0f} "
                  f"one_sided={r.one_sided:.2f} net=${r.net_usd:,.0f} entry={r.entry_prob:.2f} pnl=${r.pnl_usd:,.0f}")
    return w, s


def chart(s):
    order = {"treatment": 0, "open": 1, "public_news": 2, "award_panel": 3, "control": 4}
    r = s.assign(o=s.group.map(order)).sort_values(["o", "key"], ascending=[False, False])
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 6.6), dpi=150, sharey=True,
                                   gridspec_kw={"width_ratios": [1, 1.2]})
    y = np.arange(len(r))
    ax1.barh(y, r["flagged_profile"], color="#c9c8c3", height=0.6, label="Profile only (fresh, single-topic, one-sided)")
    ax1.barh(y, r["flagged"], color="#2a78d6", height=0.6, label="+ most $ bought ≤ 24h before news")
    for yi, (f, fp, c) in enumerate(zip(r["flagged"], r["flagged_profile"], r["candidates"])):
        ax1.text(fp + 0.4, yi, f"{f} / {fp} of {c}", va="center", fontsize=8, color="#52514e")
    ax1.set_title("Wallets flagged\n(of candidates with ≥ $1k net towards outcome, 7d before news)",
                  fontsize=9, loc="left")
    ax1.legend(fontsize=7.5, frameon=False, loc="center right")  # empty award-panel rows
    ax2.barh(y, r["flagged_pnl_usd"] / 1e3, color="#2a78d6", height=0.6)
    ax2.axvline(0, color="#52514e", lw=0.8)
    ax2.set_title("Profit of flagged wallets' pre-news trades ($k, timing rule)", fontsize=9, loc="left")
    ax1.set_yticks(y, [f"{k}  [{g}]" for k, g in zip(r.key, r.group)], fontsize=8)
    for ax in (ax1, ax2):
        ax.grid(axis="x", alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
        for b in np.flatnonzero(np.diff(r.o.to_numpy()) != 0):
            ax.axhline(b + 0.5, color="#c9c8c3", lw=0.8)
    fig.suptitle("A3: fresh, single-topic, one-sided wallets buying the outcome before news",
                 fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(ROOT / "charts/a3_flags.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
