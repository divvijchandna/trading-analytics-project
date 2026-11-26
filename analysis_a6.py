#!/usr/bin/env python3
"""
Module A6: back-test of a real-time early-warning monitor (mitigation)
=====================================================================

A3 flags wallets with hindsight (it knows the outcome and the news time). A
platform, market maker or limit-order trader would need the same signal in
real time. This module replays each market's last 7 days before news and
raises an alert the moment any wallet satisfies, using only fills up to then:

  * net exposure bought on one side (YES or NO) in the trailing 24h >= $1,000
  * trailing-24h one-sidedness >= 0.8
  * average price paid for that side <= 0.75 (buying the less likely side)
  * wallet age at that moment <= 14 days
  * <= 5 distinct events in its Polymarket history before news (from the
    profile cache; a small look-ahead, since activity after the alert time
    but before news is counted)

    python analysis_a6.py   # data/analysis/a6_alerts.csv   (one row per alert)
                            # data/analysis/a6_monitor.csv  (one row per market)
                            # charts/a6_monitor.png

Per market it reports: alerts and their direction (towards the eventual
outcome or not), hours of warning before news from the first correct alert,
the alert rate in false directions / control markets, and the share of the
A5 transfer to flagged wallets that happened after the first correct alert
(the part a resting-order owner could have avoided by cancelling then).

Two refinements are also reported:
  * cluster alerts: >= 3 alerting wallets on the same side within 12h;
  * scheduled-announcement mode: for news at a known time (Nobel, NFL Honors,
    FOMC, award ceremonies) the A3 timing condition can be checked live, so only
    alerts in the final 24h before the announcement count
    (data/analysis/a6_scheduled.csv). Unscheduled news (Maduro, Iran strikes)
    has no such window.
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
import analysis_a1 as a1  # noqa: E402
import analysis_a3 as a3  # noqa: E402  (profile fetcher + cache)
import analysis_a5 as a5  # noqa: E402  (legs, EVENT)
import polymarket_feasibility as pf  # noqa: E402

OUT = ROOT / "data/analysis"
WINDOW_DAYS = 7
CLUSTER_N, CLUSTER_WIN = 3, "12h"
# markets whose news came at a time known in advance, and their role
SCHEDULED = {
    "nobel_machado": "leak", "nobel_trump": "leak", "award_nfl_mvp_2025": "leak",
    "nfl_mvp_lamar": "leak", "nfl_mvp_burrow": "leak", "nfl_mvp_goff": "leak",
    "fomc_2026_07_nochange": "control", "fomc_2026_09_hike25": "control", "bdo_2025_dembele": "control",
    "award_nba_mvp_2026": "award_panel", "award_nba_mvp_2025": "award_panel",
    "award_nfl_oroy_2025": "award_panel", "award_heisman_2024": "award_panel",
    "award_nba_dpoy_2026": "award_panel", "award_nba_coy_2026": "award_panel",
}
RULE = dict(min_net_usd=1_000, min_one_sided=0.8, max_price=0.75, max_age_days=14, max_events=5)


def candidate_alerts(L, news):
    """Per wallet and side, the first fill time at which the dollar, one-sidedness
    and price conditions hold on the trailing 24h (profile checks come later)."""
    w = L[(L["ts"] >= news - pd.Timedelta(days=WINDOW_DAYS)) & (L["ts"] < news)]
    out = []
    for D in (1, -1):
        x = w.assign(sD=D * w["q"], pD=w["p"] if D == 1 else 1 - w["p"])
        x = x.assign(cost=x["sD"] * x["pD"], abs_s=x["sD"].abs(),
                     buy_s=x["sD"].clip(lower=0), buy_c=(x["sD"].clip(lower=0) * x["pD"]))
        x = x.sort_values("ts")
        for wal, g in x.groupby("wallet", sort=False):
            if g["cost"].sum() < RULE["min_net_usd"] and g["cost"].cumsum().max() < RULE["min_net_usd"]:
                continue
            r = g.set_index("ts")[["cost", "sD", "abs_s", "buy_s", "buy_c"]].rolling("24h").sum()
            ok = ((r["cost"] >= RULE["min_net_usd"])
                  & (r["sD"].abs() / r["abs_s"] >= RULE["min_one_sided"])
                  & (r["buy_c"] / r["buy_s"].replace(0, np.nan) <= RULE["max_price"]))
            if ok.any():
                t = ok.index[ok.to_numpy().argmax()]
                out.append(dict(wallet=wal, side=D, alert_ts=t,
                                net_usd_at_alert=float(r.loc[ok].iloc[0]["cost"])))
    return pd.DataFrame(out)


def main():
    a1res = pd.read_csv(OUT / "a1_event_study.csv").set_index("key")
    cps = pd.read_csv(OUT / "a5_counterparties.csv")
    all_alerts, pairs = [], []
    per_market = {}
    for cfg in pf.MARKETS:
        key, news = cfg["key"], cfg["news_utc"]
        tr = pd.read_parquet(pf.CLEAN / f"trades_{key}.parquet")
        L = a5.legs(tr)
        c = candidate_alerts(L, news)
        per_market[key] = (cfg, c, L)
        if len(c):
            pairs += [(w, news) for w in c["wallet"].unique()]
    prof = a3.profiles(pairs)
    prof = prof.assign(news_utc=prof["news_utc"].astype(str)).set_index(["wallet", "news_utc"])

    rows = []
    for key, (cfg, c, L) in per_market.items():
        news = cfg["news_utc"]
        d = int(a1res.loc[key, "direction"])
        grp = a1.GROUP.get(key, "award_panel")
        if len(c):
            idx = list(zip(c["wallet"], [news.isoformat()] * len(c)))
            p = prof.reindex(idx)
            first = pd.to_datetime(p["first_activity"].to_numpy(), utc=True)
            c = c.assign(age_days=(c["alert_ts"] - first).dt.total_seconds().to_numpy() / 86400,
                         n_events_pre=p["n_events_pre"].to_numpy())
            c = c[(c["age_days"] <= RULE["max_age_days"]) & (c["n_events_pre"] <= RULE["max_events"])].copy()
            c["towards_outcome"] = c["side"] == d
            c["hours_before_news"] = (news - c["alert_ts"]).dt.total_seconds() / 3600
            c.insert(0, "key", key)
            all_alerts.append(c)
        good = c[c["towards_outcome"]] if len(c) else c
        bad = c[~c["towards_outcome"]] if len(c) else c
        first_good = good["alert_ts"].min() if len(good) else pd.NaT
        # share of the A5 transfer that happened after the first correct alert
        tf = cps[cps["key"] == key]
        avoid = np.nan
        if len(tf) and pd.notna(first_good):
            a3w = pd.read_csv(OUT / "a3_wallets.csv")
            flagged = set(a3w[(a3w["key"] == key) & a3w["flag"]]["wallet"])
            Y = float(json.loads(pd.read_csv(ROOT / "markets.csv").set_index("key").loc[key, "final_outcome_prices"])[0])
            f = L[L["wallet"].isin(flagged) & (L["ts"] >= news - pd.Timedelta(days=WINDOW_DAYS)) & (L["ts"] < news)]
            pnl = f["q"] * (Y - f["p"])
            avoid = pnl[f["ts"] >= first_good].sum() / pnl.sum() if pnl.sum() else np.nan
        rows.append(dict(key=key, group=grp, alerts=len(c), alerts_towards_outcome=len(good),
                         alerts_against_outcome=len(bad),
                         first_correct_alert=first_good,
                         warning_hours=(news - first_good).total_seconds() / 3600 if pd.notna(first_good) else np.nan,
                         share_of_transfer_after_first_alert=avoid,
                         transfer_usd=tf["transfer_usd"].sum() if len(tf) else np.nan))
    alerts = pd.concat(all_alerts, ignore_index=True) if all_alerts else pd.DataFrame()
    alerts.to_csv(OUT / "a6_alerts.csv", index=False)
    mon = pd.DataFrame(rows)
    # cluster alerts: first time >= CLUSTER_N alerting wallets on one side within CLUSTER_WIN
    cl = {}
    for k, g in (alerts.groupby("key") if len(alerts) else []):
        best = None
        for side, s in g.groupby("side"):
            s = s.sort_values("alert_ts")
            cnt = pd.Series(1, index=s["alert_ts"]).rolling(CLUSTER_WIN).sum()
            hit = cnt[cnt >= CLUSTER_N]
            if len(hit) and (best is None or hit.index[0] < best[0]):
                best = (hit.index[0], bool(s["towards_outcome"].iloc[0]))
        cl[k] = best
    news_of = {c["key"]: c["news_utc"] for c in pf.MARKETS}
    mon["cluster_alert_fires"] = mon["key"].map(lambda k: cl.get(k) is not None)
    mon["cluster_alert_towards_outcome"] = mon["key"].map(lambda k: cl[k][1] if cl.get(k) else np.nan)
    mon["cluster_alert_hours_before_news"] = mon["key"].map(
        lambda k: (news_of[k] - cl[k][0]).total_seconds() / 3600 if cl.get(k) else np.nan)
    mon.to_csv(OUT / "a6_monitor.csv", index=False)
    # scheduled-announcement mode: alerts in the final 24h only
    s24 = alerts[alerts["key"].isin(SCHEDULED) & (alerts["hours_before_news"] <= 24)] if len(alerts) else alerts
    sched = pd.DataFrame({"role": pd.Series(SCHEDULED)}).join(
        s24.groupby("key").agg(alerts_24h=("wallet", "size"), towards_outcome=("towards_outcome", "sum"))
        if len(s24) else pd.DataFrame()).fillna(0).rename_axis("key").reset_index()
    sched.to_csv(OUT / "a6_scheduled.csv", index=False)
    print(sched.groupby("role")[["alerts_24h", "towards_outcome"]].sum())
    chart(mon, sched)
    pd.set_option("display.width", 220)
    print(mon.round(2).to_string(index=False))
    return alerts, mon


def chart(mon, sched):
    order = {"treatment": 0, "open": 1, "public_news": 2, "award_panel": 3, "control": 4}
    r = mon.assign(o=mon.group.map(order)).sort_values(["o", "key"], ascending=[False, False])
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 7.4), dpi=150, gridspec_kw={"width_ratios": [1.15, 1]})
    y = np.arange(len(r))
    ax1.barh(y - 0.18, r["alerts_towards_outcome"], 0.34, color="#2a78d6", label="Alert in the outcome's direction")
    ax1.barh(y + 0.18, r["alerts_against_outcome"], 0.34, color="#eb6834", label="Alert against the outcome")
    ax1.set_title("All markets: real-time alerts in the 7 days before news", fontsize=9.5, loc="left")
    ax1.legend(fontsize=7.5, frameon=False, loc="center right")
    ax1.set_yticks(y, [f"{k}  [{g}]" for k, g in zip(r.key, r.group)], fontsize=7.5)
    for b in np.flatnonzero(np.diff(r.o.to_numpy()) != 0):
        ax1.axhline(b + 0.5, color="#c9c8c3", lw=0.8)
    # scheduled announcements: alerts in the final 24h only
    rord = {"leak": 0, "award_panel": 1, "control": 2}
    s = sched.assign(o=sched.role.map(rord)).sort_values(["o", "key"], ascending=[False, False])
    y2 = np.arange(len(s))
    good = s["towards_outcome"].to_numpy()
    bad = (s["alerts_24h"] - s["towards_outcome"]).to_numpy()
    ax2.barh(y2, good, 0.55, color="#2a78d6")
    ax2.barh(y2, bad, 0.55, left=good, color="#eb6834")
    for yi, n in zip(y2, s["alerts_24h"]):
        ax2.text(n + 0.4, yi, f"{int(n)}", va="center", fontsize=7.5, color="#52514e")
    ax2.set_yticks(y2, [f"{k}  [{g}]" for k, g in zip(s.key, s.role)], fontsize=7.5)
    for b in np.flatnonzero(np.diff(s.o.to_numpy()) != 0):
        ax2.axhline(b + 0.5, color="#c9c8c3", lw=0.8)
    tot = s.groupby("role")["alerts_24h"].sum()
    ax2.set_title("Scheduled news only: alerts in the final 24h\n"
                  f"(leak markets {int(tot.get('leak', 0))}, controls {int(tot.get('control', 0))}, "
                  f"award favourites {int(tot.get('award_panel', 0))})", fontsize=9.5, loc="left")
    for ax in (ax1, ax2):
        ax.grid(axis="x", alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("A6: would a real-time version of the A3 rule have warned?", fontsize=10.5, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(ROOT / "charts/a6_monitor.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
