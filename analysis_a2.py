#!/usr/bin/env python3
"""
Module A2: microstructure metrics before news (VPIN and Kyle's lambda)
=====================================================================

Two course measures of informed trading, computed in the 24h before news and
compared with the same market's normal behaviour (placebo windows, as in A1):

    python analysis_a2.py   # writes data/analysis/a2_microstructure.csv,
                            # charts/a2_summary.png and charts/a2_vpin_paths.png

VPIN (Easley, Lopez de Prado & O'Hara 2012) - order-flow toxicity
  * Trades are grouped into equal-dollar "volume buckets". Bucket size = average
    daily taker volume over the market's recent normal period (30d to 3d before
    news) / buckets per day, where buckets per day = min(50, normal trades per
    day / 20). So a normal bucket holds ~20 trades. A trade that crosses a bucket
    boundary is split pro rata between the two buckets.
  * Per bucket, imbalance = |buy $ - sell $| / bucket $, in YES terms. We know
    the true aggressor side of every trade, so no bulk classification is needed.
  * VPIN at time t = mean imbalance over the last 50 completed buckets.
  * Statistics: VPIN just before news, and the maximum VPIN over the 24h before.

Kyle's lambda (Kyle 1985) - price impact per dollar of net order flow
  * 15-minute bins; regress the change in logit(YES price) on net taker buying
    (YES-direction $, in $1k): d logit(p) = a + lambda * Q + e. Logit keeps the
    0-1 price bounds from distorting the slope; bins with no trades are dropped.
  * Statistic: lambda in the 24h before news, and its ratio to the median
    placebo lambda. A higher lambda means each dollar of flow moves price more,
    which is what Kyle predicts when market makers expect informed traders.

Placebo inference reuses A1: N_PLACEBO fake news times from the market's last
LOOKBACK_DAYS, ending at least GAP_DAYS before the real news. One-sided
p-value for "higher than normal".
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import analysis_a1 as a1  # noqa: E402  (load_taker, GROUP, placebo settings)
import polymarket_feasibility as pf  # noqa: E402

OUT = ROOT / "data/analysis"
H = 24                    # hours before news
MAX_BUCKETS_PER_DAY = 50  # VPIN bucket size: normal daily $ volume / buckets per day,
TRADES_PER_BUCKET = 20    # with buckets per day capped so a normal bucket holds ~20
                          # trades (thin prediction markets are far from futures volume)
VPIN_N = 50               # buckets in the VPIN rolling mean
BIN = "15min"             # Kyle's lambda bins
MIN_BINS = 20             # minimum non-empty bins for a lambda estimate


def vpin_series(d, bucket_usd):
    """VPIN indexed by the completion time of each bucket. Trades crossing a bucket
    boundary are split pro rata, so one large trade cannot fill a bucket alone."""
    usd, sgn = d["usd"].to_numpy(), np.sign(d["signed"].to_numpy())
    ts = d["ts"].to_numpy()
    end = np.cumsum(usd)
    start = end - usd
    b0, b1 = (start // bucket_usd).astype(np.int64), (end // bucket_usd).astype(np.int64)
    pieces_b, pieces_u, pieces_s, pieces_t = [b0], [usd.copy()], [sgn], [ts]
    cross = np.flatnonzero(b1 > b0)
    if len(cross):
        # rebuild crossing trades as one piece per bucket they touch
        keep = np.ones(len(usd), bool)
        keep[cross] = False
        pieces_b, pieces_u, pieces_s, pieces_t = [b0[keep]], [usd[keep]], [sgn[keep]], [ts[keep]]
        for i in cross:
            edges = np.arange(b0[i] + 1, b1[i] + 1) * bucket_usd
            cuts = np.concatenate([[start[i]], edges, [end[i]]])
            amt = np.diff(cuts)
            ok = amt > 0
            pieces_b.append(np.arange(b0[i], b1[i] + 1)[ok])
            pieces_u.append(amt[ok])
            pieces_s.append(np.full(ok.sum(), sgn[i]))
            pieces_t.append(np.full(ok.sum(), ts[i]))
    g = pd.DataFrame({"b": np.concatenate(pieces_b), "u": np.concatenate(pieces_u),
                      "s": np.concatenate(pieces_s), "t": np.concatenate(pieces_t)})
    g["su"] = g["s"] * g["u"]
    g = g.groupby("b").agg(t=("t", "max"), su=("su", "sum"), u=("u", "sum"))
    g = g.iloc[:-1]  # last bucket is still filling
    imb = (g["su"].abs() / g["u"]).rolling(VPIN_N, min_periods=VPIN_N).mean()
    return pd.Series(imb.to_numpy(), index=pd.DatetimeIndex(g["t"])).dropna()


def vpin_stats(v, t_end):
    """VPIN just before t_end, and the max over the H hours before it."""
    w = v[(v.index >= t_end - pd.Timedelta(hours=H)) & (v.index < t_end)]
    before = v[v.index < t_end]
    return (float(before.iloc[-1]) if len(before) else np.nan,
            float(w.max()) if len(w) else np.nan)


def kyle_lambda(d, t0, t1):
    w = d[(d["ts"] >= t0) & (d["ts"] < t1)]
    if len(w) < MIN_BINS:
        return np.nan
    w = w.set_index("ts")
    g = pd.DataFrame({"p": w["yes_price"].resample(BIN).last(),
                      "q": w["signed"].resample(BIN).sum() / 1e3,
                      "n": w["usd"].resample(BIN).count()})
    p = g["p"].ffill().clip(0.001, 0.999)  # wide bounds: long shots trade near 1c
    g["dl"] = np.log(p / (1 - p)).diff()
    g = g[(g["n"] > 0)].dropna()
    if len(g) < MIN_BINS or g["q"].std() == 0:
        return np.nan
    x, y = g["q"].to_numpy(), g["dl"].to_numpy()
    x = x - x.mean()
    return float((x * (y - y.mean())).sum() / (x * x).sum())


def main():
    rng = np.random.default_rng(a1.SEED)
    rows, paths = [], {}
    for cfg in pf.MARKETS:
        key, news = cfg["key"], cfg["news_utc"]
        d, _ = a1.load_taker(key)
        first = d["ts"].iloc[0]
        # normal-period daily volume -> bucket size
        lo = max(first, news - pd.Timedelta(days=a1.LOOKBACK_DAYS))
        hi = news - pd.Timedelta(days=a1.GAP_DAYS)
        base = d[(d["ts"] >= lo) & (d["ts"] < hi)]
        days = max((hi - lo) / pd.Timedelta(days=1), 1)
        per_day = max(1.0, min(MAX_BUCKETS_PER_DAY, len(base) / days / TRADES_PER_BUCKET))
        bucket = base["usd"].sum() / days / per_day
        if not bucket > 0:
            bucket = d["usd"].sum() / 30
        row_bpd = per_day
        v = vpin_series(d, bucket)
        paths[key] = v[(v.index >= news - pd.Timedelta(hours=72)) & (v.index < news)]

        real_last, real_max = vpin_stats(v, news)
        real_lam = kyle_lambda(d, news - pd.Timedelta(hours=H), news)
        ft, _ = a1.placebo_times(first, news, H, rng)
        if ft is None:
            fl = fm = fk = np.array([np.nan])
        else:
            fl, fm = np.array([vpin_stats(v, t) for t in ft]).T
            fk = np.array([kyle_lambda(d, t - pd.Timedelta(hours=H), t) for t in ft])
        med_k = np.nanmedian(fk) if np.isfinite(fk).any() else np.nan
        row = dict(
            key=key, group=a1.GROUP.get(key, "award_panel"), news_utc=news,
            bucket_usd=round(bucket, 2), buckets_per_day=round(row_bpd, 1),
            vpin_at_news=real_last, vpin_placebo_median=np.nanmedian(fl),
            p_vpin_at_news=a1.pval(real_last, fl),
            vpin_max_24h=real_max, p_vpin_max_24h=a1.pval(real_max, fm),
            lambda_24h=real_lam, lambda_placebo_median=med_k,
            # ratio only when normal-period impact is measurably positive
            lambda_ratio=real_lam / med_k if np.isfinite(med_k) and med_k > 1e-6 else np.nan,
            p_lambda_24h=a1.pval(real_lam, fk),
            placebo_lambda_n=int(np.isfinite(fk).sum()),
        )
        rows.append(row)
        print(f"{key:30s} VPIN@news {real_last:.2f} (med {row['vpin_placebo_median']:.2f}, p={row['p_vpin_at_news']:.3f})  "
              f"max24 p={row['p_vpin_max_24h']:.3f}  lambda x{row['lambda_ratio']:.1f} (p={row['p_lambda_24h']:.3f})")
    res = pd.DataFrame(rows)
    res.to_csv(OUT / "a2_microstructure.csv", index=False)
    chart_summary(res)
    chart_paths(res, paths)
    return res


ORDER = {"treatment": 0, "open": 1, "public_news": 2, "award_panel": 3, "control": 4}


def chart_summary(res):
    r = res.assign(o=res.group.map(ORDER)).sort_values(["o", "key"], ascending=[False, False])
    stats_ = [("p_vpin_at_news", "VPIN just before news", "o", "#2a78d6"),
              ("p_vpin_max_24h", "Max VPIN, 24h before", "s", "#eb6834"),
              ("p_lambda_24h", "Kyle's lambda, 24h before", "^", "#1baf7a")]
    floor = 1 / (a1.N_PLACEBO + 1)
    fig, ax = plt.subplots(figsize=(9.5, 7.2), dpi=150)
    y = np.arange(len(r))
    for j, (col, lbl, m, c) in enumerate(stats_):
        ax.scatter(r[col].clip(lower=floor), y + (j - 1) * 0.22, marker=m, s=40, color=c,
                   label=lbl, zorder=3, edgecolor="white", lw=0.6)
    ax.axvline(0.05, color="#52514e", ls="--", lw=1)
    ax.text(0.05, len(r) - 0.3, " p = 0.05", fontsize=8, color="#52514e", va="bottom")
    ax.set_xscale("log")
    ax.set_xlim(floor / 1.5, 1.2)
    ax.set_yticks(y, [f"{k}  [{g}]" for k, g in zip(r.key, r.group)], fontsize=8)
    ax.set_xlabel("Placebo p-value (log scale; left = higher than normal)")
    ax.grid(axis="x", alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    for b in np.flatnonzero(np.diff(r.o.to_numpy()) != 0):
        ax.axhline(b + 0.5, color="#c9c8c3", lw=0.8)
    ax.set_title("A2: is flow toxicity (VPIN) or price impact (Kyle's lambda) higher before news?\n"
                 f"{a1.N_PLACEBO} placebo windows from each market's last {a1.LOOKBACK_DAYS} days; "
                 f"p = {floor:.3f} = beyond all placebos", fontsize=10, loc="left", pad=22)
    ax.legend(fontsize=8, frameon=False, ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0))
    fig.tight_layout()
    fig.savefig(ROOT / "charts/a2_summary.png")
    plt.close(fig)


def chart_paths(res, paths):
    """VPIN over the 72h before news: treatment (top row) vs control (bottom row)."""
    keys = [k for k in ["nobel_machado", "maduro_out_jan31", "award_nfl_mvp_2025",
                        "iran_strike_2026_02_28", "fomc_2026_07_nochange",
                        "fomc_2026_09_hike25", "bdo_2025_dembele",
                        "iran_strike_2025_06_control"] if k in paths]
    med = res.set_index("key")["vpin_placebo_median"]
    fig, axes = plt.subplots(2, 4, figsize=(13, 5.6), dpi=150, sharey=True)
    for ax, k in zip(axes.flat, keys):
        v = paths[k]
        news =next(c["news_utc"] for c in pf.MARKETS if c["key"] == k)
        x = (v.index - news) / pd.Timedelta(hours=1)
        treat = a1.GROUP.get(k) == "treatment"
        ax.plot(x, v.to_numpy(), color="#2a78d6" if treat else "#52514e", lw=1.4)
        ax.axhline(med[k], color="#c9c8c3", lw=1.2, ls="--")
        ax.axvline(0, color="#0b0b0b", lw=0.8)
        ax.set_title(f"{k}\n[{a1.GROUP.get(k, 'award_panel')}]", fontsize=8, loc="left")
        ax.set_xlim(-72, 0)
        ax.set_ylim(0, 1)
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
    for ax in axes[1]:
        ax.set_xlabel("Hours before news")
    for ax in axes[:, 0]:
        ax.set_ylabel("VPIN")
    fig.suptitle("VPIN over the 72h before news (dashed = market's normal median)",
                 fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(ROOT / "charts/a2_vpin_paths.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
