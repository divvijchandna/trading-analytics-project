#!/usr/bin/env python3
"""
Module A1: pre-news event study with placebo inference
======================================================

For every market in polymarket_feasibility.MARKETS, ask: was the window just
before the news unusual compared with the same market's normal behaviour?

    python analysis_a1.py            # writes data/analysis/a1_event_study.csv
                                     # and charts/a1_placebo_summary.png

Inputs (already produced by polymarket_feasibility.py):
  * data/raw/dataapi_trades_<key>_taker.parquet  one row per matched tx (the taker
    order), so each trade is counted once. For fomc_2026_09_hike25 the
    full-history taker file is used, because the clean table covers only 7 days.
  * markets.csv                                  final outcome of each market.

Everything is normalised to the YES token (NO trade at p = YES trade at 1 - p,
opposite side) and then to the direction of the eventual outcome d (+1 if YES
won, -1 if NO won). So "positive" always means "towards what actually happened".
For the unresolved 2026 Ballon d'Or market, d is taken from the current price
(Kane, the favourite) and its results are labelled provisional.

Statistics on a window [news - h, news), h = 24h (main) and 6h:
  drift_pp     price move towards the outcome, in percentage points. Prices are
               the median YES price of the last 5 trades before each end point,
               which damps bid/ask bounce.
  imbalance    d * (taker buy $ - taker sell $) / (taker buy $ + taker sell $),
               in [-1, 1]: one-sidedness of aggressive flow towards the outcome.
  net_usd      d * (taker buy $ - taker sell $).
  volume_usd   taker notional in the window (abnormal volume vs placebo median).
  share_priced fraction of the eventual move already priced before news:
               (q_pre - q_start) / (1 - q_start), q = probability of the actual
               outcome, start = 48h before news. Resolved markets only.

Placebo inference: the same statistics on N_PLACEBO fake news times drawn from
the market's own recent history (the LOOKBACK_DAYS before news), ending at
least GAP_DAYS before the real news (so no fake window overlaps the event). One-sided p-value for "real window is more
extreme towards the outcome than normal": (1 + #fake >= real) / (1 + N).
For volume the test is "more volume than normal".
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
import polymarket_feasibility as pf  # noqa: E402  (MARKETS, paths)

OUT = ROOT / "data/analysis"
OUT.mkdir(parents=True, exist_ok=True)

HORIZONS_H = [24, 6]       # window lengths before news (hours); 24h is the main one
N_PLACEBO = 200
GAP_DAYS = 3               # fake windows must end >= this long before the real news
LOOKBACK_DAYS = 30         # ...and start no earlier than this before it: compares the
                           # event with the market's recent normal, not its quiet infancy
MIN_GAP_DAYS = 1           # fallback for short-lived markets
SEED = 6908

# Presentation grouping (the pipeline's `group` field is coarser).
GROUP = {
    "nobel_machado": "treatment", "maduro_out_jan31": "treatment",
    "award_nfl_mvp_2025": "treatment", "iran_strike_2026_02_28": "treatment",
    # Market moved ~6.5h AFTER the public report (Real Madrid boycott, 07:22 UTC):
    # slow reaction to public news, not trading ahead of it.
    "bdo_2024_rodri": "public_news", "bdo_2024_vinicius": "public_news",
    "fomc_2026_07_nochange": "control", "fomc_2026_09_hike25": "control",
    "bdo_2025_dembele": "control", "iran_strike_2025_06_control": "control",
    "bdo_2026_kane": "open",
}  # everything else = award panel (provisional anchors, heavy favourites)


def load_taker(key):
    """Taker legs normalised to YES: time, yes_price, signed YES-direction $ and |$|."""
    f = pf.RAW / f"dataapi_trades_{key}_taker_fullhistory.parquet"
    if not f.exists():
        f = pf.RAW / f"dataapi_trades_{key}_taker.parquet"
    t = pd.read_parquet(f)
    is_yes = t["outcomeIndex"].astype(int).eq(0)
    buy = t["side"].eq("BUY")
    yes_dir = np.where(is_yes == buy, 1, -1)          # BUY YES / SELL NO = +1
    usd = (t["size"] * t["price"]).to_numpy()          # USDC the taker paid/received
    d = pd.DataFrame({
        "ts": pd.to_datetime(t["timestamp"], unit="s", utc=True),
        "yes_price": np.where(is_yes, t["price"], 1 - t["price"]),
        "signed": yes_dir * usd,
        "usd": usd,
    }).sort_values("ts").reset_index(drop=True)
    return d, f.name


class Series:
    """Fast window statistics on sorted arrays."""

    def __init__(self, d):
        self.t = d["ts"].to_numpy("datetime64[ns]")
        self.p = d["yes_price"].to_numpy(float)
        self.s = d["signed"].to_numpy(float)
        self.u = d["usd"].to_numpy(float)
        self.cs = np.concatenate([[0], np.cumsum(self.s)])
        self.cu = np.concatenate([[0], np.cumsum(self.u)])

    def price_before(self, t):
        i = np.searchsorted(self.t, np.datetime64(t.tz_convert(None)), "left")
        if i == 0:
            return np.nan
        return float(np.median(self.p[max(0, i - 5):i]))

    def window(self, t0, t1, d):
        a = np.searchsorted(self.t, np.datetime64(t0.tz_convert(None)), "left")
        b = np.searchsorted(self.t, np.datetime64(t1.tz_convert(None)), "left")
        net, vol = self.cs[b] - self.cs[a], self.cu[b] - self.cu[a]
        p0, p1 = self.price_before(t0), self.price_before(t1)
        return {
            "drift_pp": d * (p1 - p0) * 100 if np.isfinite(p0) and np.isfinite(p1) else np.nan,
            "imbalance": d * net / vol if vol > 0 else np.nan,
            "net_usd": d * net,
            "volume_usd": vol,
            "n_trades": int(b - a),
        }


def placebo_times(first, news, h, rng):
    for gap in (GAP_DAYS, MIN_GAP_DAYS):
        lo = max(first, news - pd.Timedelta(days=LOOKBACK_DAYS)) + pd.Timedelta(hours=h)
        hi = news - pd.Timedelta(days=gap)
        if hi > lo:
            u = rng.uniform(lo.value, hi.value, N_PLACEBO)
            return pd.to_datetime(np.sort(u).astype("int64"), utc=True), gap
    return None, None


def pval(real, fake, greater=True):
    fake = fake[np.isfinite(fake)]
    if not np.isfinite(real) or len(fake) < 20:
        return np.nan
    k = (fake >= real).sum() if greater else (fake <= real).sum()
    return (1 + k) / (1 + len(fake))


def main():
    mk = pd.read_csv(ROOT / "markets.csv").set_index("key")
    rng = np.random.default_rng(SEED)
    rows = []
    for cfg in pf.MARKETS:
        key, news = cfg["key"], cfg["news_utc"]
        prices = json.loads(mk.loc[key, "final_outcome_prices"])
        resolved = prices in (["1", "0"], ["0", "1"])
        d_raw, src = load_taker(key)
        S = Series(d_raw)
        # direction of the actual (or, if open, currently favoured) outcome
        if resolved:
            d = 1 if prices[0] == "1" else -1
        else:
            d = 1 if S.price_before(news) >= 0.5 else -1
        first = d_raw["ts"].iloc[0]

        q = lambda p: p if d == 1 else 1 - p  # prob. of the actual outcome
        p48, p_pre = S.price_before(news - pd.Timedelta(hours=48)), S.price_before(news)
        share = ((q(p_pre) - q(p48)) / (1 - q(p48))
                 if resolved and np.isfinite(p48) and q(p48) < 0.999 else np.nan)

        row = dict(key=key, group=GROUP.get(key, "award_panel"), resolved=resolved,
                   direction=d, news_utc=news, source_file=src,
                   q_48h_before=round(q(p48), 4) if np.isfinite(p48) else np.nan,
                   q_just_before=round(q(p_pre), 4) if np.isfinite(p_pre) else np.nan,
                   share_priced_48h=share)
        for h in HORIZONS_H:
            real = S.window(news - pd.Timedelta(hours=h), news, d)
            ft, gap = placebo_times(first, news, h, rng)
            if ft is None:
                fake = {k: np.array([np.nan]) for k in real}
            else:
                ws = [S.window(t - pd.Timedelta(hours=h), t, d) for t in ft]
                fake = {k: np.array([w[k] for w in ws], float) for k in real}
            for k in ("drift_pp", "imbalance", "net_usd", "volume_usd", "n_trades"):
                row[f"{k}_{h}h"] = real[k]
            for k in ("drift_pp", "imbalance", "net_usd", "volume_usd"):
                row[f"p_{k}_{h}h"] = pval(real[k], fake[k])
            med = np.nanmedian(fake["volume_usd"]) if np.isfinite(fake["volume_usd"]).any() else np.nan
            row[f"abn_volume_x_{h}h"] = real["volume_usd"] / med if med and med > 0 else np.nan
            row[f"placebo_gap_days_{h}h"] = gap
            row[f"placebo_n_{h}h"] = int(np.isfinite(fake["drift_pp"]).sum())
        rows.append(row)
        print(f"{key:30s} drift24 {row['drift_pp_24h']:+7.1f}pp (p={row['p_drift_pp_24h']:.3f})  "
              f"imb24 {row['imbalance_24h']:+.2f} (p={row['p_imbalance_24h']:.3f})  "
              f"vol x{row['abn_volume_x_24h']:.1f} (p={row['p_volume_usd_24h']:.3f})")

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "a1_event_study.csv", index=False)
    chart(res)
    return res


def chart(res):
    """Placebo p-values per event for the three 24h statistics (one chart)."""
    order = {"treatment": 0, "open": 1, "public_news": 2, "award_panel": 3, "control": 4}
    r = res.assign(o=res.group.map(order)).sort_values(["o", "key"], ascending=[False, False])
    stats_ = [("p_drift_pp_24h", "Price drift towards outcome", "o", "#2a78d6"),
              ("p_imbalance_24h", "Order-flow imbalance", "s", "#eb6834"),
              ("p_volume_usd_24h", "Abnormal volume", "^", "#1baf7a")]
    fig, ax = plt.subplots(figsize=(9.5, 7.2), dpi=150)
    y = np.arange(len(r))
    for j, (col, lbl, m, c) in enumerate(stats_):
        ax.scatter(r[col].clip(lower=1 / (N_PLACEBO + 1)), y + (j - 1) * 0.22, marker=m, s=40,
                   color=c, label=lbl, zorder=3, edgecolor="white", lw=0.6)
    ax.axvline(0.05, color="#52514e", ls="--", lw=1)
    ax.text(0.05, len(r) - 0.3, " p = 0.05", fontsize=8, color="#52514e", va="bottom")
    ax.set_xscale("log")
    ax.set_xlim(1 / (N_PLACEBO + 1) / 1.5, 1.2)
    ax.set_yticks(y, [f"{k}  [{g}]" for k, g in zip(r.key, r.group)], fontsize=8)
    ax.set_xlabel("Placebo p-value, 24h before news (log scale; left = more unusual)")
    ax.grid(axis="x", alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    # separators between groups
    for b in np.flatnonzero(np.diff(r.o.to_numpy()) != 0):
        ax.axhline(b + 0.5, color="#c9c8c3", lw=0.8)
    ax.set_title("A1: is the 24h before news unusual vs the market's own history?\n"
                 f"{N_PLACEBO} placebo windows from each market's last {LOOKBACK_DAYS} days; "
                 f"p = {1 / (N_PLACEBO + 1):.3f} = beyond all placebos",
                 fontsize=10, loc="left", pad=22)
    ax.legend(fontsize=8, frameon=False, ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0))
    fig.tight_layout()
    fig.savefig(ROOT / "charts/a1_placebo_summary.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
