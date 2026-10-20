#!/usr/bin/env python3
"""
Module A4: do other traders follow large / informed traders, and at what price?
==============================================================================

The core question of the approved title. Adapted from the teammate's
wallet-behaviour design, with these fixes: trades normalised to YES (a NO buy is
a YES sell), no minimum trade size, controls matched on recent activity as well
as size and price, and inference that resamples markets, not individual pairs.

    python analysis_a4.py   # data/analysis/a4_events.csv   (events + matched controls)
                            # data/analysis/a4_summary.csv  (group comparisons)
                            # charts/a4_followers.png

Unit: aggressive (taker) orders, one row per matched transaction, in YES terms.
A wallet's same-direction orders less than EPISODE_GAP apart are merged into one
"episode" (order splitting); the episode starts at its first order, at that
order's price, and its size is the episode total.

Event groups, all in the 7 days before news (and ending >= 15 min before it, so
no follow window contains the news):
  informed  episodes by A3-flagged wallets, in the direction of the outcome
  large     episodes by large-trader wallets: top 20% of wallets by taker volume
            in the market's formation period (30d to 7d before news, >= 3 orders),
            with size >= the formation period's 90th-percentile order (and >= $100)
  control   for each informed/large episode, the nearest ordinary order (not by a
            flagged or large-trader wallet) in the same market and direction:
            distance = |d log size| + 2 |d price| + 0.5 |d log(1 + orders in the
            previous 5 min)| + 0.03 x days apart; within 1 day; not within 15 min
            of any event; without replacement. Calipers: within 2x in size, 5pp
            in price and 2x in (1 + orders in the previous 5 min); events without
            a match inside the calipers are dropped (reported as n_unmatched).

Metrics per episode (h = 60s, 300s, 900s after the first order; strictly later):
  follow_n_h        other wallets' same-direction taker orders in (t, t+h]
  follow_excess_h   that minus the same count in [t-h, t)
  impact_pp_h       price move in the episode's direction, percentage points
  penalty_pp        followers' $-weighted price disadvantage vs the episode price
                    within 300s (positive = followers paid more / got less)
  follower_return   followers' (300s) $-weighted return at resolution per $
  own_return        the episode trader's return at resolution per $

Inference: mean paired difference (event - control) with a 95% interval from
resampling markets (then pairs within them) 2,000 times, and a sign-flip
permutation p-value at the pair level.
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
import polymarket_feasibility as pf  # noqa: E402

OUT = ROOT / "data/analysis"
WINDOW_DAYS = 7
FORMATION_DAYS = 30
EPISODE_GAP = pd.Timedelta(minutes=5)
HORIZONS = [60, 300, 900]
MAIN_H = 300
EXCLUSION = pd.Timedelta(minutes=15)
CAL_LOG_SIZE, CAL_PRICE, CAL_LOG_ACT = np.log(2), 0.05, np.log(2)   # matching calipers
N_BOOT, SEED = 2000, 6908


def load_orders(key):
    """Taker orders (one per tx) in YES terms, with wallets."""
    f = pf.RAW / f"dataapi_trades_{key}_taker_fullhistory.parquet"
    if not f.exists():
        f = pf.RAW / f"dataapi_trades_{key}_taker.parquet"
    t = pd.read_parquet(f)
    is_yes = t["outcomeIndex"].astype(int).eq(0)
    buy = t["side"].eq("BUY")
    o = pd.DataFrame({
        "ts": pd.to_datetime(t["timestamp"], unit="s", utc=True),
        "wallet": t["proxyWallet"].str.lower(),
        "dir": np.where(is_yes == buy, 1, -1),            # +1 = buys YES exposure
        "p": np.where(is_yes, t["price"], 1 - t["price"]),  # YES price
        "usd": (t["size"] * t["price"]).to_numpy(),
    })
    return o.sort_values("ts", kind="stable").reset_index(drop=True)


def episodes(o):
    """Merge a wallet's same-direction orders < EPISODE_GAP apart."""
    o = o.sort_values(["wallet", "dir", "ts"], kind="stable").copy()
    new = (o["wallet"].ne(o["wallet"].shift()) | o["dir"].ne(o["dir"].shift())
           | (o["ts"] - o["ts"].shift() > EPISODE_GAP))
    o["ep"] = new.cumsum()
    e = o.groupby("ep").agg(ts=("ts", "first"), wallet=("wallet", "first"), dir=("dir", "first"),
                            p=("p", "first"), usd=("usd", "sum"), n_orders=("usd", "size"))
    return e.reset_index(drop=True)


class Book:
    """Fast window queries on one market's taker orders."""

    def __init__(self, o):
        self.t = o["ts"].to_numpy("datetime64[ns]")
        self.w = o["wallet"].to_numpy()
        self.d = o["dir"].to_numpy()
        self.p = o["p"].to_numpy(float)
        self.u = o["usd"].to_numpy(float)

    def idx(self, t, side="right"):
        return np.searchsorted(self.t, np.datetime64(t.tz_convert(None)), side)

    def count_before(self, t, h):
        a, b = self.idx(t - pd.Timedelta(seconds=h), "left"), self.idx(t, "left")
        return b - a

    def metrics(self, t, wallet, d, p0, Y):
        out = {}
        a = self.idx(t, "right")                      # strictly after t
        lo = self.idx(t - pd.Timedelta(seconds=max(HORIZONS)), "left")
        pre_w, pre_d, pre_t = self.w[lo:a], self.d[lo:a], self.t[lo:a]
        for h in HORIZONS:
            b = self.idx(t + pd.Timedelta(seconds=h), "right")
            m = (self.w[a:b] != wallet) & (self.d[a:b] == d)
            out[f"follow_n_{h}"] = int(m.sum())
            out[f"follow_usd_{h}"] = float(self.u[a:b][m].sum())
            tcut = np.datetime64((t - pd.Timedelta(seconds=h)).tz_convert(None))
            pm = (pre_t >= tcut) & (pre_t < np.datetime64(t.tz_convert(None))) & (pre_w != wallet) & (pre_d == d)
            out[f"follow_excess_{h}"] = out[f"follow_n_{h}"] - int(pm.sum())
            out[f"impact_pp_{h}"] = (d * (np.median(self.p[max(a, b - 3):b]) - p0) * 100
                                     if b > a else np.nan)
            if h == MAIN_H:
                fp, fu = self.p[a:b][m], self.u[a:b][m]
                if len(fp):
                    out["penalty_pp"] = float(np.average(d * (fp - p0) * 100, weights=fu))
                    cost = np.where(d == 1, fp, 1 - fp)
                    ret = d * (Y - fp) / np.clip(cost, 1e-3, None)
                    out["follower_return"] = float(np.average(ret, weights=fu))
                else:
                    out["penalty_pp"] = out["follower_return"] = np.nan
        cost0 = p0 if d == 1 else 1 - p0
        out["own_return"] = d * (Y - p0) / max(cost0, 1e-3)
        return out


def match_controls(ev, pool, used):
    """Nearest ordinary order for each event (greedy, without replacement)."""
    rows = []
    for i, e in ev.iterrows():
        c = pool[(pool["dir"] == e["dir"]) & ~pool.index.isin(used)
                 & ((pool["ts"] - e["ts"]).abs() <= pd.Timedelta(days=1))]
        ds = (np.log1p(c["usd"]) - np.log1p(e["usd"])).abs()
        dp = (c["p"] - e["p"]).abs()
        da = (np.log1p(c["pre_n"]) - np.log1p(e["pre_n"])).abs()
        c = c[(ds <= CAL_LOG_SIZE) & (dp <= CAL_PRICE) & (da <= CAL_LOG_ACT)]
        if c.empty:
            continue
        dist = (ds[c.index] + 2 * dp[c.index] + 0.5 * da[c.index]
                + 0.03 * (c["ts"] - e["ts"]).abs() / pd.Timedelta(days=1))
        j = dist.idxmin()
        used.add(j)
        rows.append((i, j))
    return rows


def main():
    mk = pd.read_csv(ROOT / "markets.csv").set_index("key")
    a1res = pd.read_csv(OUT / "a1_event_study.csv").set_index("key")
    a3 = pd.read_csv(OUT / "a3_wallets.csv")
    flagged = a3[a3["flag"]].groupby("key")["wallet"].apply(set).to_dict()
    rows, unmatched = [], []
    for cfg in pf.MARKETS:
        key, news = cfg["key"], cfg["news_utc"]
        o = load_orders(key)
        if o.empty:
            continue
        prices = json.loads(mk.loc[key, "final_outcome_prices"])
        resolved = prices in (["1", "0"], ["0", "1"])
        Y = float(prices[0]) if resolved else float(o["p"].iloc[-1])  # YES payoff
        d_out = int(a1res.loc[key, "direction"])
        book = Book(o)
        w0, w1 = news - pd.Timedelta(days=WINDOW_DAYS), news - EXCLUSION
        f0 = news - pd.Timedelta(days=FORMATION_DAYS)

        # large-trader wallets from the formation period
        form = o[(o["ts"] >= f0) & (o["ts"] < w0)]
        ws = form.groupby("wallet")["usd"].agg(["sum", "size"])
        ws = ws[ws["size"] >= 3]
        large_w = set(ws[ws["sum"] >= ws["sum"].quantile(0.8)].index) if len(ws) >= 5 else set()
        size_cut = max(100.0, form["usd"].quantile(0.9)) if len(form) else np.inf
        flag_w = flagged.get(key, set())

        win = o[(o["ts"] >= w0) & (o["ts"] < w1)].copy()
        if win.empty:
            continue
        ep = episodes(win)
        ep["pre_n"] = [book.count_before(t, 300) for t in ep["ts"]]
        inf = ep[ep["wallet"].isin(flag_w) & (ep["dir"] == d_out)].assign(group="informed")
        lrg = ep[ep["wallet"].isin(large_w) & ~ep["wallet"].isin(flag_w) & (ep["usd"] >= size_cut)].assign(group="large")
        events = pd.concat([inf, lrg])
        if events.empty:
            continue
        # ordinary single orders as the control pool, away from any event
        pool = win[~win["wallet"].isin(flag_w | large_w)].copy()
        pool["pre_n"] = [book.count_before(t, 300) for t in pool["ts"]]
        et = events["ts"].to_numpy("datetime64[ns]")
        pt = pool["ts"].to_numpy("datetime64[ns]")
        k = np.searchsorted(np.sort(et), pt)
        se = np.sort(et)
        near = np.zeros(len(pt), bool)
        for off in (0, -1):
            kk = np.clip(k + off, 0, len(se) - 1)
            near |= np.abs((pt - se[kk]).astype("timedelta64[s]").astype(float)) <= EXCLUSION.total_seconds()
        pool = pool[~near]
        used = set()
        for grp in ("informed", "large"):
            ev = events[events["group"] == grp]
            for n_pair, (i, j) in enumerate(match_controls(ev, pool, used)):
                e, c = ev.loc[i], pool.loc[j]
                pid = f"{key}:{grp}:{n_pair}"
                for role, r in (("event", e), ("control", c)):
                    m = book.metrics(r["ts"], r["wallet"], int(r["dir"]), float(r["p"]), Y)
                    rows.append(dict(key=key, mgroup=a1.GROUP.get(key, "award_panel"), group=grp,
                                     role=role, pair=pid, ts=r["ts"], wallet=r["wallet"],
                                     dir=int(r["dir"]), price=float(r["p"]), usd=float(r["usd"]),
                                     pre_n=int(r["pre_n"]), **m))
        n_pairs = {g: sum(1 for r in rows if r["key"] == key and r["group"] == g and r["role"] == "event")
                   for g in ("informed", "large")}
        unmatched.append(dict(key=key, informed=len(inf), informed_matched=n_pairs["informed"],
                              large=len(lrg), large_matched=n_pairs["large"]))
        print(f"{key:30s} informed {n_pairs['informed']:3d}/{len(inf):3d}  large {n_pairs['large']:4d}/{len(lrg):4d} matched")
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "a4_events.csv", index=False)
    pd.DataFrame(unmatched).to_csv(OUT / "a4_matching.csv", index=False)
    summ = summarise(df)
    summ.to_csv(OUT / "a4_summary.csv", index=False)
    print(summ.to_string(index=False))
    chart(df, summ)
    return df, summ


METRICS = (["follow_n_300", "follow_excess_300", "follow_usd_300"]
           + [f"impact_pp_{h}" for h in HORIZONS] + ["penalty_pp", "follower_return", "own_return"])


def summarise(df):
    rng = np.random.default_rng(SEED)
    out = []
    subsets = {"informed": df["group"].eq("informed"),
               "informed_leak_events": df["group"].eq("informed") & df["mgroup"].eq("treatment"),
               "large": df["group"].eq("large"),
               "large_leak_events": df["group"].eq("large") & df["mgroup"].eq("treatment"),
               "large_controls": df["group"].eq("large") & df["mgroup"].eq("control")}
    for grp, sel in subsets.items():
        g = df[sel]
        ev = g[g["role"] == "event"].set_index("pair")
        co = g[g["role"] == "control"].set_index("pair")
        for m in METRICS:
            d = (ev[m] - co[m]).dropna()
            mk = ev.loc[d.index, "key"]
            if len(d) < 3:
                continue
            # cluster bootstrap over markets
            groups = [d[mk == k].to_numpy() for k in mk.unique()]
            boots = []
            for _ in range(N_BOOT):
                pick = rng.integers(0, len(groups), len(groups))
                vals = np.concatenate([rng.choice(groups[i], len(groups[i])) for i in pick])
                boots.append(vals.mean())
            # sign-flip permutation at pair level
            flips = rng.choice([-1, 1], size=(N_BOOT, len(d)))
            perm = np.abs((flips * d.to_numpy()).mean(axis=1))
            out.append(dict(group=grp, metric=m, n_pairs=len(d), n_markets=len(groups),
                            event_mean=ev.loc[d.index, m].mean(), control_mean=co.loc[d.index, m].mean(),
                            diff=d.mean(), ci_low=np.quantile(boots, 0.025), ci_high=np.quantile(boots, 0.975),
                            p_signflip=(1 + (perm >= abs(d.mean())).sum()) / (1 + N_BOOT)))
    return pd.DataFrame(out)


def chart(df, summ):
    s = summ.set_index(["group", "metric"])
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.8), dpi=150)
    groups = [("informed_leak_events", "Flagged wallets,\nleak events"), ("large_leak_events", "Large traders,\nleak events"),
              ("large_controls", "Large traders,\ncontrol events")]
    cols = {"event": "#2a78d6", "control": "#c9c8c3"}
    x = np.arange(len(groups))

    def bars(ax, metric, title, ylabel):
        for k, role in enumerate(("event", "control")):
            vals = [s.loc[(g, metric), f"{role}_mean"] if (g, metric) in s.index else np.nan for g, _ in groups]
            ax.bar(x + (k - 0.5) * 0.38, vals, 0.36, color=cols[role],
                   label="Event" if role == "event" else "Matched ordinary trade")
        for xi, (g, _) in enumerate(groups):
            if (g, metric) in s.index:
                r = s.loc[(g, metric)]
                p = r["p_signflip"]
                ptxt = "p<0.001" if p <= 1 / N_BOOT else f"p={p:.3f}"
                ax.annotate(f"diff {r['diff']:+.2f}\n{ptxt}\nn={int(r['n_pairs'])}", (xi, 1.0),
                            xycoords=("data", "axes fraction"), ha="center", va="top", fontsize=7, color="#52514e")
        lo, hi = ax.get_ylim()
        ax.set_ylim(lo, hi + (hi - lo) * 0.45)  # headroom for the labels
        ax.set_xticks(x, [lbl for _, lbl in groups], fontsize=8)
        ax.set_title(title, fontsize=9.5, loc="left")
        ax.set_ylabel(ylabel, fontsize=8.5)
        ax.axhline(0, color="#52514e", lw=0.8)
        ax.grid(axis="y", alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)

    bars(axes[0], "follow_excess_300", "Following: extra same-direction orders\nby others in the next 5 min (minus prior 5 min)", "orders")
    bars(axes[1], "impact_pp_900", "Price response after 15 min\n(in the trade's direction)", "percentage points")
    bars(axes[2], "follower_return", "Followers' return at resolution\n(per $, followers within 5 min)", "return per $")
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h[:2], l[:2], fontsize=8, frameon=False, ncol=2, loc="upper right")
    fig.suptitle("A4: do others follow large / suspected informed traders, and does it pay?",
                 fontsize=10.5, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(ROOT / "charts/a4_followers.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
