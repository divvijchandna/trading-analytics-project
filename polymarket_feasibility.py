#!/usr/bin/env python3
"""
Polymarket information-leakage feasibility pipeline
===================================================

Re-runnable end-to-end script for the FN6908 market-microstructure project.

    python polymarket_feasibility.py            # use cached raw pulls if present
    python polymarket_feasibility.py --refresh  # re-download everything

Steps (mirrors the project brief):
  1. IDENTIFY   Gamma API -> slug, conditionId, YES/NO token ids, dates, volume  -> markets.csv
  2. TRADES     Polymarket Data API /trades (public, no key)                     -> data/raw/
  3. CLEAN      one row per economic fill, normalised to the YES token           -> data/clean/
  4. VALIDATE   volume, price path (vs CLOB /prices-history), on-chain receipts,
                publicly reported whale trades                                    -> data/validation/
  5. CHART      YES price + whale fills, 48h before news                         -> charts/
  (6. feasibility.md is written by hand from the validation outputs.)

Data-source notes (see feasibility.md for the full log):
  * Data API /trades returns one row per *wallet leg* of a fill.
      takerOnly=true  -> only the taker leg (one per matched tx, size = sum of maker legs)
      takerOnly=false -> taker leg + every maker leg
    Rows carry proxyWallet, side, outcome, price, size, timestamp, tx hash, username.
    Hard limits: limit <= 1000, offset <= 10000. start/end (unix s, both inclusive)
    work, so we recursively split time windows until each window has <= 11,000 rows.
  * Each maker leg == one economic trade between that maker and the tx's taker. We
    therefore build the clean table from maker legs only (the taker leg is the sum
    of them and would double count). This is the same de-duplication you would do
    on-chain, where the exchange emits one OrderFilled per maker order plus one
    aggregate OrderFilled for the taker order whose counterparty is the exchange.
  * Validation pulls receipts for a random sample of txs from a public Polygon RPC
    and decodes the OrderFilled events to check the Data API legs one-for-one.
"""

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

# --------------------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------------------
WHALE_USD = 5_000          # a "whale" fill = taker notional above this (USDC)
CHART_HOURS_BEFORE = 48    # chart window before news
CHART_HOURS_AFTER = 4      # a little post-news context so the jump is visible
PRE_NEWS_HOURS = 24        # window used for the "enough trades" check
MIN_PRE_NEWS_TRADES = 100  # threshold for PASS on that check
ONCHAIN_SAMPLE_TX = 10     # receipts to spot-check per market
SEED = 6908

ROOT = Path(__file__).resolve().parent
RAW, CLEAN, VAL, CHARTS = (ROOT / "data/raw", ROOT / "data/clean",
                           ROOT / "data/validation", ROOT / "charts")
for p in (RAW, CLEAN, VAL, CHARTS):
    p.mkdir(parents=True, exist_ok=True)

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
POLYGON_RPCS = [  # public, keyless endpoints that serve historical receipts
    "https://polygon.drpc.org",
    "https://polygon.gateway.tenderly.co",
    "https://1rpc.io/matic",
]

# Polymarket exchange / operator contracts on Polygon. Any fill with one of these as a
# party is not an economic trade between two users. Extended at run time with any
# exchange contract seen emitting OrderFilled in the on-chain spot check.
OPERATOR_CONTRACTS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",  # CTF Exchange (v1)
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",  # NegRisk CTF Exchange (v1)
    "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296",  # NegRisk Adapter
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",  # Conditional Tokens (CTF)
    "0xe2222d279d744050d28e00520010520000310f59",  # CTF Exchange V2 (2026 migration)
}

def utc(s):
    return pd.Timestamp(s, tz="UTC")

# Markets. `news_utc` is the public-news timestamp; `alt_news_utc` (optional) is a
# second reference line drawn on the chart.
MARKETS = [
    dict(key="nobel_machado",
         group="treatment",
         event_slug="nobel-peace-prize-winner-2025",
         match="María Corina Machado",
         news_utc=utc("2025-10-10 09:00"),
         news_label="Nobel announcement 11:00 Oslo (09:00 UTC)",
         news_source="Norwegian Nobel Committee schedule (brief): 10 Oct 2025 11:00 CEST"),
    dict(key="maduro_out_jan31",
         group="treatment",
         event_slug="maduro-out-in-2025",
         match="Maduro out by January 31, 2026",
         # First public signal: US helicopters land at Maduro's compound 2:01am Caracas
         # (06:01 UTC) amid explosions reported live on social media. Trump's Truth
         # Social post confirming capture came "a little after 09:00 GMT".
         news_utc=utc("2026-01-03 06:01"),
         news_label="Raid begins / explosions in Caracas (06:01 UTC)",
         alt_news_utc=utc("2026-01-03 09:00"),
         alt_news_label="Trump Truth Social post (~09:00 UTC)",
         news_source="Al Jazeera, 'Trump bombs Venezuela, US abducts Maduro: All we know' "
                     "(3 Jan 2026, upd. 4 Jan): helicopters land 2:01am (06:01 GMT); Trump "
                     "post 'a little after 09:00 GMT'"),
    dict(key="fomc_2026_07_nochange",
         group="control",
         event_slug="fed-decision-in-july-181",
         match="no change in Fed interest rates after the July 2026",
         news_utc=utc("2026-07-29 18:00"),
         news_label="FOMC statement 2:00pm ET (18:00 UTC)",
         news_source="FOMC statement release convention 2:00pm ET (EDT = UTC-4)"),
    dict(key="fomc_2026_09_hike25",
         group="control",
         event_slug="fed-decision-in-september-762",
         match="increase interest rates by 25 bps after the September 2026",
         # Speed: only pull 7 days pre-news (full history is ~130k legs). Volume
         # validation against Gamma is therefore not possible for this market.
         pull_days_before_news=7,
         news_utc=utc("2026-09-16 18:00"),
         news_label="FOMC statement 2:00pm ET (18:00 UTC)",
         news_source="FOMC statement release convention 2:00pm ET (EDT = UTC-4)"),
    # ---- Ballon d'Or: votes are cast weeks before the ceremony, so the winner is
    # fixed but secret in between -> any informed pre-ceremony flow is a leak.
    dict(key="bdo_2024_rodri",
         group="treatment",
         event_slug="ballon-dor-winner",
         match="Will Rodri win the Ballon",
         # Public news: Real Madrid boycott reported 8:22 AM Paris = 07:22 UTC (team check);
         # a possible earlier leak at 7:50 AM Paris = 06:50 UTC. The market only flipped at
         # 13:55 UTC (~9:55am New York), ~6.5h AFTER the public report: slow incorporation
         # of public news, not trading ahead of it.
         news_utc=utc("2024-10-28 07:22"),
         news_label="Real Madrid boycott reported 8:22 Paris (07:22 UTC)",
         alt_news_utc=utc("2024-10-28 13:55"),
         alt_news_label="Market flips to Rodri, 13:55 UTC (6.5h after the report)",
         chart_hours_after=8,
         news_source="Team check: Real Madrid boycott reported 8:22 AM Paris time 28 Oct 2024 "
                     "(possible earlier leak 7:50 AM). Paris was on CET (UTC+1) that day, DST "
                     "ended 27 Oct, so 07:22 UTC (06:22 UTC if the source's CEST label is literal). "
                     "Real Madrid cancelled its 3pm flight to Paris"),
    dict(key="bdo_2024_vinicius",
         group="treatment",
         event_slug="ballon-dor-winner",
         match="Will Vinicius Jr win the Ballon",
         # Public news: Real Madrid boycott reported 8:22 AM Paris = 07:22 UTC (team check);
         # a possible earlier leak at 7:50 AM Paris = 06:50 UTC. The market only flipped at
         # 13:55 UTC (~9:55am New York), ~6.5h AFTER the public report: slow incorporation
         # of public news, not trading ahead of it.
         news_utc=utc("2024-10-28 07:22"),
         news_label="Real Madrid boycott reported 8:22 Paris (07:22 UTC)",
         alt_news_utc=utc("2024-10-28 13:55"),
         alt_news_label="Market flips to Rodri, 13:55 UTC (6.5h after the report)",
         chart_hours_after=8,
         news_source="Team check: Real Madrid boycott reported 8:22 AM Paris time 28 Oct 2024 "
                     "(possible earlier leak 7:50 AM). Paris was on CET (UTC+1) that day, DST "
                     "ended 27 Oct, so 07:22 UTC (06:22 UTC if the source's CEST label is literal). "
                     "Real Madrid cancelled its 3pm flight to Paris"),
    dict(key="bdo_2025_dembele",
         group="treatment",
         event_slug="ballon-dor-winner-2025",
         match="Will Ousmane Dembele win the 2025",
         news_utc=utc("2025-09-22 19:00"),
         news_label="Ceremony starts 21:00 Paris (19:00 UTC)",
         news_source="Africanews / UK outlets: ceremony 22 Sep 2025 at 9pm Paris (1900 GMT)"),
    dict(key="bdo_2026_kane",
         group="treatment",
         event_slug="ballon-dor-winner-2026",
         match="Will Harry Kane win the 2026",
         # Unresolved (ceremony 26 Oct 2026). Votes closed 28 Sep 2026. Anchor = the
         # 9 Oct move where Kane overtook Yamal for good; no news trigger found yet.
         news_utc=utc("2026-10-09 17:40"),
         news_label="Kane overtakes Yamal, 17:40 UTC (no news trigger found)",
         news_source="Event time from our own trade data; votes closed 28 Sep 2026, ceremony "
                     "26 Oct 2026 (London Palladium) per press"),
    # ---- Award-vote panel: votes cast before a scheduled announcement, so the winner
    # is fixed but secret in between. Winner market only (YES resolved). All anchor
    # times are PROVISIONAL until official announcement / first insider report times
    # are confirmed (see feasibility.md section 10).
    dict(key="award_nba_mvp_2026",
         group="treatment_award",
         event_slug="nba-mvp-694",
         match="Shai Gilgeous-Alexander win the 2025",
         news_utc=utc("2026-05-17 13:52"),  # 2025-26 MVP
         news_label="Price settles >=99.5% (provisional)",
         news_source="PROVISIONAL, to confirm: first minute the price reaches 99.5% and holds (CLOB prices-history)"),
    dict(key="award_nba_mvp_2025",
         group="treatment_award",
         event_slug="nba-mvp-2024-25",
         match="Shai Gilgeous-Alexander win the 2024-25",
         news_utc=utc("2025-05-20 19:20"),  # 2024-25 MVP
         news_label="Price settles >=99.5% (provisional)",
         news_source="PROVISIONAL, to confirm: first minute the price reaches 99.5% and holds (CLOB prices-history)"),
    dict(key="award_nfl_mvp_2025",
         group="treatment_award",
         event_slug="nfl-mvp-1",
         match="Josh Allen win NFL MVP",
         # NFL Honors main show start, 9pm ET (confirmed by team; red carpet began 8pm
         # ET = 01:00 UTC). Awards are announced in the main show, so the pre-news window
         # cannot contain the announcement. (Price settled >=99.5% at 03:40 UTC.)
         news_utc=utc("2025-02-07 02:00"),
         news_label="NFL Honors main show starts 9pm ET (02:00 UTC)",
         # From our data: Allen ~53% until 23:10 UTC on 6 Feb, then 56% -> 96% by ~23:55,
         # hours before the NFL Honors show. Cause not yet sourced.
         alt_news_utc=utc("2025-02-06 23:20"),
         alt_news_label="Allen jumps ~53% -> 96% from 23:20 UTC (data; cause unsourced)",
         news_source="Team check: NFL Honors 6 Feb 2025, red carpet 8pm ET (01:00 UTC), main show 9pm ET (02:00 UTC)"),
    dict(key="award_nfl_oroy_2025",
         group="treatment_award",
         event_slug="nfl-offensive-rookie-of-the-year",
         match="Jayden Daniels win NFL Offensive",
         # Price was already >=99.5% from 2 Feb, so the settle rule is no use here:
         # anchor on the NFL Honors main show instead (9pm ET, 6 Feb 2025; confirmed).
         news_utc=utc("2025-02-07 02:00"),
         news_label="NFL Honors main show starts 9pm ET (02:00 UTC)",
         news_source="Team check: NFL Honors 6 Feb 2025, red carpet 8pm ET, main show 9pm ET"),
    dict(key="award_heisman_2024",
         group="treatment_award",
         event_slug="heisman-winner",
         match="Travis Hunter win the Heisman",
         news_utc=utc("2024-12-15 01:55"),  # Heisman ceremony evening of 14 Dec 2024 ET
         news_label="Price settles >=99.5% (provisional)",
         news_source="PROVISIONAL, to confirm: first minute the price reaches 99.5% and holds (CLOB prices-history)"),
    dict(key="award_nba_dpoy_2026",
         group="treatment_award",
         event_slug="nba-2025-26-defensive-player-of-the-year",
         match="Victor Wembanyama win the 2025",
         news_utc=utc("2026-04-20 14:57"),  # 2025-26 DPOY
         news_label="Price settles >=99.5% (provisional)",
         news_source="PROVISIONAL, to confirm: first minute the price reaches 99.5% and holds (CLOB prices-history)"),
    dict(key="award_nba_coy_2026",
         group="treatment_award",
         event_slug="nba-2025-26-coach-of-the-year",
         match="Joe Mazzulla win the 2025",
         news_utc=utc("2026-05-26 23:38"),  # 2025-26 COY
         news_label="Price settles >=99.5% (provisional)",
         news_source="PROVISIONAL, to confirm: first minute the price reaches 99.5% and holds (CLOB prices-history)"),
    # ---- Military strikes on Iran. Bubblemaps/Bloomberg (Mar 2026) flagged fresh
    # wallets buying the 28 Feb 2026 date "hours before explosions were first reported
    # in Tehran". Anchor times are PROVISIONAL (from price data) until the first
    # public report / official announcement times are confirmed.
    dict(key="iran_strike_2026_02_28",
         group="treatment",
         event_slug="us-next-strikes-iran-on-843",
         match="February 28, 2026 (ET)",
         # First public reports 8:10 Israel time (IST = UTC+2 in Feb) = 06:10 UTC (team
         # check). The price had already climbed 11% -> 26% from ~05:00 UTC before them.
         news_utc=utc("2026-02-28 06:10"),
         news_label="First reports 8:10 Israel time (06:10 UTC)",
         alt_news_utc=utc("2026-02-27 10:46"),  # early move 4% -> 14%, cause unknown
         alt_news_label="Early move 4% -> 14%, 10:46 UTC 27 Feb (cause unsourced)",
         news_source="Team check: first reports 8:10 Israel time (06:10 UTC). Bloomberg/Cointelegraph "
                     "(1 Mar 2026): six wallets created in February made ~$1M, buying hours before "
                     "explosions were first reported"),
    dict(key="iran_strike_2025_06_control",
         group="control",
         event_slug="us-military-action-against-iran-before-july",
         match="US military action against Iran before July",
         news_utc=utc("2025-06-21 23:47"),  # jump starts 23:47:21 (public announcement)
         news_label="Price jumps 58% -> 98% from 23:47 UTC (public announcement)",
         # Team timestamp 22:40 (UTC assumed; meaning, e.g. strike time, to confirm).
         # Price was flat at ~57% from 22:30 to 23:40 UTC.
         alt_news_utc=utc("2025-06-21 22:40"),
         alt_news_label="Team timestamp 22:40 UTC (to confirm)",
         news_source="PROVISIONAL, to confirm: exact time of the public announcement of "
                     "US strikes on Iran, 21 Jun 2025 (evening ET)"),
]

# Publicly reported wallets used in Step 4.
REPORTED = {
    "nobel_machado": dict(usernames=["dirtycup", "6741", "GayPride"]),  # named in press reports
    "maduro_out_jan31": dict(wallets=["0x31a56e9e690c621ed21de08cb559e9524cdb8ed9"]),
    # Not press-reported: flagged by our own 9 Oct 2026 trade analysis
    # (fresh single-topic wallet; largest two-sided trader in the move).
    # Flagged by our analysis: first-ever activity 22:21 UTC 6 Feb 2025, bought $23k of
    # Allen at 22:47 UTC, ~30 min before the jump.
    # Flagged by our analysis: wallets whose first-ever Polymarket activity was 20-26 Feb
    # 2026 and that bought the 28 Feb date before the price passed 50%.
    "iran_strike_2026_02_28": dict(wallets=["0x1d81ff4717323ba033e8480f0e3801cfd7e35c1e",
                                            "0x918628150136edd43be507c1b18c122d0960fad1",
                                            "0xa64bde82ed7cc2afe62fc30813294c55bc21e68c",
                                            "0xa60bed1766a004ab93245e31481f0bc531f34b20"]),
    "award_nfl_mvp_2025": dict(wallets=["0xa5d2078aeef37590971c5a08b63089a2820a822a"]),
    "bdo_2026_kane": dict(wallets=["0x3ed2c452f141c291adecb97ddfc72b1650acff3d",
                                   "0x032eb1bc893940263ad0b01889f262fc232f2a9e"]),
}

session = requests.Session()
session.headers["User-Agent"] = "fn6908-feasibility/1.0 (university research)"


def get_json(url, params=None, tries=6):
    """GET with polite retry/back-off on 429/5xx."""
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** i)
                continue
            raise RuntimeError(f"{r.status_code} {r.text[:200]} :: {url} {params}")
        except requests.RequestException:
            time.sleep(2 ** i)
    raise RuntimeError(f"giving up on {url} {params}")


# --------------------------------------------------------------------------------------
# Step 1: identify markets
# --------------------------------------------------------------------------------------
def identify_markets():
    rows = []
    for m in MARKETS:
        ev = get_json(f"{GAMMA}/events", {"slug": m["event_slug"]})
        (RAW / f"gamma_event_{m['key']}.json").write_text(json.dumps(ev, indent=1))
        hits = [x for x in ev[0]["markets"] if m["match"] in x["question"]]
        assert len(hits) == 1, (m["key"], [x["question"] for x in hits])
        x = hits[0]
        outcomes = json.loads(x["outcomes"])
        tokens = json.loads(x["clobTokenIds"])
        assert outcomes == ["Yes", "No"], outcomes
        rows.append(dict(
            key=m["key"], group=m["group"], question=x["question"], slug=x["slug"],
            event_slug=m["event_slug"], condition_id=x["conditionId"],
            yes_token=tokens[0], no_token=tokens[1], neg_risk=x.get("negRisk"),
            start_date=x.get("startDate"), end_date=x.get("endDate"),
            closed_time=x.get("closedTime"),
            final_outcome_prices=x.get("outcomePrices"),
            gamma_volume_usd=float(x.get("volume") or 0),
            gamma_volume_clob_usd=float(x.get("volumeClob") or 0),
            news_utc=m["news_utc"].isoformat(), news_source=m["news_source"],
        ))
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "markets.csv", index=False)
    print(df[["key", "slug", "gamma_volume_usd", "start_date", "closed_time"]])
    return df


# --------------------------------------------------------------------------------------
# Step 2: trades from Data API with adaptive time-window splitting
# --------------------------------------------------------------------------------------
PAGE, MAX_OFFSET = 1000, 10_000
_calls = {"n": 0}


def _page(cond, taker_only, start, end, offset):
    _calls["n"] += 1
    time.sleep(0.12)  # stay well under the public rate limit
    return get_json(f"{DATA}/trades", {
        "market": cond, "takerOnly": str(taker_only).lower(), "limit": PAGE,
        "offset": offset, "start": start, "end": end})


def fetch_window(cond, taker_only, start, end, out):
    """Fetch every row with start <= timestamp <= end (inclusive), splitting the
    window whenever it holds more rows than offset<=10000 can reach."""
    probe = _page(cond, taker_only, start, end, MAX_OFFSET)
    if len(probe) == PAGE:               # >11,000 rows -> cannot page through; split
        if end - start < 1:
            raise RuntimeError(f"> {MAX_OFFSET + PAGE} rows in one second at {start}")
        mid = (start + end) // 2
        fetch_window(cond, taker_only, start, mid, out)
        fetch_window(cond, taker_only, mid + 1, end, out)
        return
    rows = []
    for off in range(0, MAX_OFFSET, PAGE):
        page = _page(cond, taker_only, start, end, off)
        rows += page
        if len(page) < PAGE:
            break
    else:
        rows += probe
    out.extend(rows)


def pull_trades(mk, cfg, refresh=False):
    """Raw Data API pulls for one market (taker legs and all legs)."""
    res = {}
    t0 = int(pd.Timestamp(mk.start_date).timestamp()) - 86400
    end = mk.closed_time if pd.notna(mk.closed_time) else mk.end_date  # open market: no closedTime
    t1 = int(pd.Timestamp(end).timestamp()) + 86400
    t1 = min(t1, int(time.time()))  # open markets: no point probing the future
    if cfg.get("pull_days_before_news"):  # partial pull (see MARKETS)
        t0 = int((cfg["news_utc"] - pd.Timedelta(days=cfg["pull_days_before_news"])).timestamp())
    for taker_only in (True, False):
        tag = "taker" if taker_only else "all"
        f = RAW / f"dataapi_trades_{mk.key}_{tag}.parquet"
        if f.exists() and not refresh:
            res[tag] = pd.read_parquet(f)
            continue
        n0 = _calls["n"]
        # pre-split into non-overlapping 7-day chunks (busy markets don't waste probes)
        # and fetch 4 chunks at a time; results are concatenated in time order.
        chunks = [(s, min(s + 7 * 86400 - 1, t1)) for s in range(t0, t1 + 1, 7 * 86400)]

        def _chunk(c):
            buf = []
            fetch_window(mk.condition_id, taker_only, c[0], c[1], buf)
            return buf

        with ThreadPoolExecutor(max_workers=4) as pool:
            out = [row for buf in pool.map(_chunk, chunks) for row in buf]
        df = pd.DataFrame(out)
        df.to_parquet(f, index=False)
        print(f"  {mk.key} {tag}: {len(df):,} rows in {_calls['n'] - n0} requests")
        res[tag] = df
    return res


# --------------------------------------------------------------------------------------
# Step 3: clean
# --------------------------------------------------------------------------------------
LEG_KEY = ["transactionHash", "proxyWallet", "asset", "side", "size", "price", "timestamp"]


def clean_market(mk, raw):
    tk, al = raw["taker"].copy(), raw["all"].copy()
    for d in (tk, al):
        d["proxyWallet"] = d["proxyWallet"].str.lower()
    stats = dict(key=mk.key, rows_taker=len(tk), rows_all=len(al))

    # Maker legs = all legs minus taker legs (multiset difference: identical legs can
    # legitimately repeat, so count occurrences rather than dropping duplicates).
    tk["_n"] = tk.groupby(LEG_KEY).cumcount()
    al["_n"] = al.groupby(LEG_KEY).cumcount()
    m = al.merge(tk[LEG_KEY + ["_n"]].assign(_is_taker=True), on=LEG_KEY + ["_n"], how="left")
    stats["taker_legs_missing_from_all"] = int(len(tk) - m["_is_taker"].sum())
    makers = m[m["_is_taker"].isna()].drop(columns=["_is_taker", "_n"])

    # One taker leg per tx is expected (one matchOrders call per tx).
    tk_per_tx = tk.groupby("transactionHash").size()
    stats["tx_total"] = int(len(tk_per_tx))
    stats["tx_with_multiple_taker_legs"] = int((tk_per_tx > 1).sum())
    single = tk[tk["transactionHash"].map(tk_per_tx) == 1]
    tmap = single.set_index("transactionHash")[["proxyWallet", "outcome", "side", "size", "name"]]
    tmap.columns = ["taker_wallet", "taker_outcome", "taker_raw_side", "taker_size", "taker_name"]
    makers = makers.join(tmap, on="transactionHash")
    stats["maker_legs"] = int(len(makers))
    stats["maker_legs_unpaired"] = int(makers["taker_wallet"].isna().sum())

    # Per-tx completeness check: maker leg sizes must sum to the taker leg size.
    g = makers.groupby("transactionHash").agg(msum=("size", "sum"), tsize=("taker_size", "first"))
    ok = np.isclose(g["msum"], g["tsize"], rtol=1e-4, atol=0.02)
    stats["tx_size_reconciled_pct"] = round(100 * ok.mean(), 3) if len(g) else None
    stats["tx_without_maker_legs"] = int(len(set(tk_per_tx.index) - set(g.index)))

    # Normalise to YES. Maker leg: outcome o, side s, price p, size q.
    is_yes = makers["outcome"].eq("Yes")
    yes_price = np.where(is_yes, makers["price"], 1 - makers["price"])
    maker_yes_side = np.where(is_yes, makers["side"],
                              np.where(makers["side"].eq("BUY"), "SELL", "BUY"))
    taker_side = np.where(maker_yes_side == "BUY", "SELL", "BUY")
    # USDC the taker actually paid/received for this leg (depends on token traded)
    taker_px = np.where(makers["taker_outcome"].eq("Yes"), yes_price, 1 - yes_price)

    out = pd.DataFrame({
        "timestamp_utc": pd.to_datetime(makers["timestamp"], unit="s", utc=True),
        "tx_hash": makers["transactionHash"],
        "wallet": makers["taker_wallet"],          # taker
        "maker_wallet": makers["proxyWallet"],
        "side": taker_side,                        # taker's side in YES terms
        "yes_price": yes_price.astype(float),
        "size_shares": makers["size"].astype(float),
        "notional_usd": (makers["size"] * taker_px).astype(float),
        "yes_notional_usd": (makers["size"] * yes_price).astype(float),
        "taker_name": makers["taker_name"],
        "maker_name": makers["name"],
        "taker_outcome": makers["taker_outcome"],
        "maker_outcome": makers["outcome"],
    })
    out = out[out["wallet"].notna()]
    excl = out["wallet"].isin(OPERATOR_CONTRACTS) | out["maker_wallet"].isin(OPERATOR_CONTRACTS)
    stats["excluded_operator_fills"] = int(excl.sum())
    out = out[~excl]
    out["self_trade"] = out["wallet"].eq(out["maker_wallet"])
    out = out.sort_values(["timestamp_utc", "tx_hash"]).reset_index(drop=True)
    out.to_parquet(CLEAN / f"trades_{mk.key}.parquet", index=False)
    stats["clean_trades"] = len(out)
    return out, stats


# --------------------------------------------------------------------------------------
# Step 4: validation helpers
# --------------------------------------------------------------------------------------
def price_history(token, t0, t1):
    """Polymarket's own displayed price series (minute fidelity)."""
    out = []
    for s in range(t0, t1, 86400 * 7):  # chunked; long ranges get downsampled
        h = get_json(f"{CLOB}/prices-history",
                     {"market": token, "startTs": s, "endTs": min(s + 86400 * 7, t1),
                      "fidelity": 1})
        out += h.get("history", [])
    df = pd.DataFrame(out).drop_duplicates("t")
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["t"], unit="s", utc=True)
    return df.sort_values("ts")


def trade_vwap_at(tr, ts, minutes=10):
    w = tr[(tr.timestamp_utc > ts - pd.Timedelta(minutes=minutes)) & (tr.timestamp_utc <= ts)]
    if w.empty:
        w = tr[tr.timestamp_utc <= ts].tail(5)
    return float((w.yes_price * w.size_shares).sum() / w.size_shares.sum()) if len(w) else None


def last_price_before(tr, ts):
    w = tr[tr.timestamp_utc <= ts]
    return float(w.yes_price.iloc[-1]) if len(w) else None


_OF_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
# = keccak256("OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
# CTF Exchange V2 (live from 2026) emits a different OrderFilled. Observed layout:
# topics = [sig, orderHash, maker, taker]; data = [side (0 BUY / 1 SELL), tokenId,
# makerAmountFilled, takerAmountFilled, fee, ...]. Shares = makerAmt if SELL else takerAmt.
_OF_TOPIC_V2 = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"


def rpc_receipt(tx):
    """Receipt from the first public RPC that has it. 1rpc.io returns `null` for some
    older txs (partial index), so fall through to the others before giving up."""
    for i in range(3):
        for rpc in POLYGON_RPCS:
            try:
                r = session.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                 "method": "eth_getTransactionReceipt", "params": [tx]},
                                 timeout=30).json()
                if r.get("result"):
                    return r["result"]
            except (requests.RequestException, ValueError):
                pass
        time.sleep(1 + 2 * i)
    return None


def onchain_spotcheck(mk, tr):
    """Decode OrderFilled logs for a random sample of txs and compare each
    (maker, taker, shares) leg with the clean table."""
    rng = random.Random(SEED)
    txs = list(tr["tx_hash"].unique())
    sample = rng.sample(txs, min(ONCHAIN_SAMPLE_TX, len(txs)))
    res, exchanges = [], set()
    for tx in sample:
        rec = rpc_receipt(tx)
        if rec is None:
            res.append(dict(tx=tx, status="rpc_failed"))
            continue
        legs = []
        for lg in rec["logs"]:
            if lg["topics"][0] not in (_OF_TOPIC, _OF_TOPIC_V2):
                continue
            exchanges.add(lg["address"].lower())
            maker = "0x" + lg["topics"][2][-40:]
            taker = "0x" + lg["topics"][3][-40:]
            d = lg["data"][2:]
            w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
            if lg["topics"][0] == _OF_TOPIC_V2:
                side, tok, m_amt, t_amt = w[0], w[1], w[2] / 1e6, w[3] / 1e6
                m_asset, t_asset = (tok, 0) if side == 1 else (0, tok)
            else:
                m_asset, t_asset, m_amt, t_amt = w[0], w[1], w[2] / 1e6, w[3] / 1e6
            if taker.lower() == lg["address"].lower():
                continue  # aggregate taker leg (counterparty = exchange) -> skip
            if str(m_asset) not in (mk.yes_token, mk.no_token) and \
               str(t_asset) not in (mk.yes_token, mk.no_token):
                continue  # other market in a batched tx
            shares = t_amt if m_asset == 0 else m_amt
            legs.append((maker.lower(), taker.lower(), round(shares, 2)))
        ours = tr[tr.tx_hash == tx]
        ours_legs = sorted(zip(ours.maker_wallet, ours.wallet, ours.size_shares.round(2)))
        res.append(dict(tx=tx, status="match" if sorted(legs) == ours_legs else "mismatch",
                        onchain_legs=len(legs), our_legs=len(ours)))
        time.sleep(0.2)
    return pd.DataFrame(res), exchanges


def wallet_pnl(tr, payoff):
    """PnL from YES-normalised fills, counting both taker and maker roles, valuing YES
    at `payoff` (1/0 at resolution, or the last trade price = mark-to-market if the
    market is still open). Buying YES at y earns (payoff - y) per share; selling = mirror."""
    a = pd.DataFrame({"wallet": tr.wallet,
                      "q": np.where(tr.side == "BUY", tr.size_shares, -tr.size_shares),
                      "y": tr.yes_price, "usd": tr.notional_usd})
    b = pd.DataFrame({"wallet": tr.maker_wallet,
                      "q": np.where(tr.side == "BUY", -tr.size_shares, tr.size_shares),
                      "y": tr.yes_price, "usd": tr.size_shares * 0})
    x = pd.concat([a, b])
    x["pnl"] = x.q * (payoff - x.y)
    x["cost"] = x.q * x.y
    return x.groupby("wallet").agg(net_yes_shares=("q", "sum"), net_cost_usd=("cost", "sum"),
                                   pnl_usd=("pnl", "sum"))


# --------------------------------------------------------------------------------------
# Step 5: chart
# --------------------------------------------------------------------------------------
def chart(mk, cfg, tr, ph, highlight=()):
    news = cfg["news_utc"]
    after = cfg.get("chart_hours_after", CHART_HOURS_AFTER)
    lo, hi = news - pd.Timedelta(hours=CHART_HOURS_BEFORE), news + pd.Timedelta(hours=after)
    w = tr[(tr.timestamp_utc >= lo) & (tr.timestamp_utc <= hi)]
    whales = w[w.notional_usd > WHALE_USD]
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=150)
    if ph is not None and len(ph):
        p = ph[(ph.ts >= lo) & (ph.ts <= hi)]
        ax.plot(p.ts, p.p, color="#c9c8c3", lw=2, label="Polymarket displayed price (CLOB prices-history)")
    ax.plot(w.timestamp_utc, w.yes_price, color="#52514e", lw=1.0, drawstyle="steps-post",
            label="YES trade price (our clean fills)")
    for side, col in (("BUY", "#2a78d6"), ("SELL", "#eb6834")):
        s = whales[whales.side == side]
        ax.scatter(s.timestamp_utc, s.yes_price, s=np.clip(s.notional_usd / 60, 15, 900),
                   color=col, alpha=0.55, edgecolor="white", lw=0.5, zorder=3,
                   label=f"Whale taker {side} YES (> ${WHALE_USD:,.0f}, n={len(s)})")
    # Publicly reported wallets: every fill they were party to (taker or maker), so
    # order-splitting below the whale threshold is still visible.
    for wlt, mkr in zip(highlight, ["x", "+", "1", "2"]):
        h = w[(w.wallet == wlt) | (w.maker_wallet == wlt)]
        if len(h):
            name = h.taker_name[h.wallet == wlt].dropna().tolist() + h.maker_name[h.maker_wallet == wlt].dropna().tolist()
            lbl = name[0] if name and name[0] and not name[0].startswith("0x") else wlt[:10] + "…"
            ax.scatter(h.timestamp_utc, h.yes_price, marker=mkr, s=26, lw=1.1, color="#0b0b0b",
                       zorder=4, label=f"Named/flagged wallet {lbl}: all fills (n={len(h)})")
    ax.axvline(news, color="black", lw=1.4, ls="--", label=cfg["news_label"])
    if cfg.get("alt_news_utc") is not None:
        ax.axvline(cfg["alt_news_utc"], color="#7a4fb5", lw=1.2, ls=":", label=cfg["alt_news_label"])
    ax.set_xlim(lo, hi)
    ax.set_ylim(-0.02, 1.02)
    ax.set_ylabel("YES price")
    ax.set_xlabel("UTC")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
    ax.grid(alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title(f"{mk.question}\n{CHART_HOURS_BEFORE}h before news; dot area ∝ taker notional",
                 fontsize=10, loc="left")
    # keep the legend off the price path: bottom if the market trades high, else top
    ax.legend(fontsize=7.5, frameon=False,
              loc="lower left" if w.yes_price.median() > 0.5 else "upper left")
    fig.tight_layout()
    fig.savefig(CHARTS / f"{mk.key}.png")
    plt.close(fig)


# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download raw pulls")
    ap.add_argument("--only", help="run steps 2-5 for these market keys only (comma-separated)")
    args = ap.parse_args()

    print("STEP 1: identify markets")
    mdf = identify_markets()

    all_stats, val = [], []
    for mk, cfg in zip(mdf.itertuples(), MARKETS):
        if args.only and mk.key not in args.only.split(","):
            continue
        print(f"\nSTEP 2-5: {mk.key}")
        raw = pull_trades(mk, cfg, args.refresh)
        tr, st = clean_market(mk, raw)
        news = cfg["news_utc"]
        prices = json.loads(mk.final_outcome_prices)
        resolved = prices in (["1", "0"], ["0", "1"])
        resolved_yes = prices[0] == "1" if resolved else None
        payoff = (1.0 if resolved_yes else 0.0) if resolved else float(tr.yes_price.iloc[-1])

        # ---- volume / price validation
        v = dict(key=mk.key, gamma_volume_usd=mk.gamma_volume_usd,
                 our_taker_notional_usd=tr.notional_usd.sum(),
                 our_yes_notional_usd=tr.yes_notional_usd.sum(),
                 our_shares=tr.size_shares.sum(),
                 first_trade=tr.timestamp_utc.min(), last_trade=tr.timestamp_utc.max(),
                 last_trade_yes_price=float(tr.yes_price.iloc[-1]),
                 resolved=resolved, resolved_yes=resolved_yes)
        t0 = int((news - pd.Timedelta(hours=CHART_HOURS_BEFORE + 24)).timestamp())
        t1 = int((news + pd.Timedelta(hours=cfg.get("chart_hours_after", CHART_HOURS_AFTER) + 1)).timestamp())
        ph = price_history(mk.yes_token, t0, t1)
        ph.to_parquet(RAW / f"clob_prices_history_{mk.key}.parquet", index=False)
        if len(ph):  # compare our last-trade price with displayed price minute by minute
            ours = pd.merge_asof(ph[["ts", "p"]], tr[["timestamp_utc", "yes_price"]]
                                 .rename(columns={"timestamp_utc": "ts"}), on="ts")
            ours = ours.dropna()
            v["price_path_mae_vs_clob"] = float((ours.p - ours.yes_price).abs().mean())
            v["price_path_within_5c_pct"] = float(100 * ((ours.p - ours.yes_price).abs() <= 0.05).mean())
            pre = ph[ph.ts <= news]
            v["clob_price_just_before_news"] = float(pre.p.iloc[-1]) if len(pre) else None
        v["our_price_just_before_news"] = last_price_before(tr, news)
        pre24 = tr[(tr.timestamp_utc > news - pd.Timedelta(hours=PRE_NEWS_HOURS)) & (tr.timestamp_utc <= news)]
        v["trades_24h_pre_news"] = len(pre24)
        v["taker_wallets_24h_pre_news"] = pre24.wallet.nunique()
        v["notional_24h_pre_news_usd"] = pre24.notional_usd.sum()
        v["whale_fills_24h_pre_news"] = int((pre24.notional_usd > WHALE_USD).sum())

        # ---- on-chain spot check
        oc, ex = onchain_spotcheck(mk, tr)
        oc.to_csv(VAL / f"onchain_spotcheck_{mk.key}.csv", index=False)
        v["onchain_sample"] = len(oc)
        v["onchain_match"] = int((oc.status == "match").sum())
        v["exchange_contracts_seen"] = ",".join(sorted(ex))
        unknown_ex = ex - OPERATOR_CONTRACTS
        if unknown_ex:  # new (e.g. V2) exchange contract -> make sure it's excluded too
            v["new_exchange_contracts"] = ",".join(sorted(unknown_ex))
            st["fills_with_new_exchange_contract"] = int(
                (tr.wallet.isin(unknown_ex) | tr.maker_wallet.isin(unknown_ex)).sum())

        # ---- wallet PnL & publicly reported wallets
        pnl = wallet_pnl(tr, payoff)  # mark-to-market if unresolved
        names = pd.concat([tr[["wallet", "taker_name"]].set_axis(["wallet", "name"], axis=1),
                           tr[["maker_wallet", "maker_name"]].set_axis(["wallet", "name"], axis=1)]
                          ).dropna().drop_duplicates("wallet").set_index("wallet")["name"]
        pnl["name"] = names
        pre_buy = tr[(tr.timestamp_utc <= news) & (tr.side == "BUY")]
        pnl["pre_news_taker_buy_usd"] = pre_buy.groupby("wallet").notional_usd.sum()
        pnl["pre_news_taker_buy_shares"] = pre_buy.groupby("wallet").size_shares.sum()
        pnl = pnl.sort_values("pnl_usd", ascending=False)
        pnl.to_csv(VAL / f"wallet_pnl_{mk.key}.csv")

        rep = REPORTED.get(mk.key, {})
        wallets = set(rep.get("wallets", [])) | set(names[names.isin(rep.get("usernames", []))].index)
        for wlt in wallets:
            t = tr[(tr.wallet == wlt) | (tr.maker_wallet == wlt)].copy()
            t["role"] = np.where(t.wallet == wlt, "taker", "maker")
            t.to_csv(VAL / f"reported_wallet_{mk.key}_{wlt[:10]}.csv", index=False)
            print(f"  reported wallet {wlt} ({names.get(wlt)}): {len(t)} fills")

        # Nobel: reported odds at 00:40 and 02:00 Oslo (CEST = UTC+2)
        if mk.key == "nobel_machado":
            for lbl, ts in (("0040_oslo", utc("2025-10-09 22:40")), ("0200_oslo", utc("2025-10-10 00:00"))):
                v[f"our_vwap10m_{lbl}"] = trade_vwap_at(tr, ts)
                v[f"our_last_{lbl}"] = last_price_before(tr, ts)
                p = ph[ph.ts <= ts]
                v[f"clob_{lbl}"] = float(p.p.iloc[-1]) if len(p) else None

        all_stats.append(st)
        val.append(v)
        chart(mk, cfg, tr, ph, sorted(wallets))
        print(json.dumps(st, default=str))
        print(json.dumps(v, default=str, indent=1))

    for rows, name in ((all_stats, "clean_stats.csv"), (val, "validation_summary.csv")):
        new = pd.DataFrame(rows)
        if args.only and (VAL / name).exists():  # keep the other markets' rows
            old = pd.read_csv(VAL / name)
            new = pd.concat([old[~old.key.isin(args.only.split(","))], new], ignore_index=True)
        new.to_csv(VAL / name, index=False)
    print(f"\nDone. Data API requests this run: {_calls['n']}")


if __name__ == "__main__":
    sys.exit(main())
