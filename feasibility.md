# Feasibility report: Polymarket trade-level data with wallets, pre-news leakage

Prepared 16 Oct 2026. Everything below comes from `polymarket_feasibility.py`. The numbers are in `data/validation/`.

## Verdict

**Feasible.** Polymarket's public **Data API** (`/trades`) gives complete fill-level history for all four markets. It needs no API key and no sign-up. Each fill carries the taker wallet, the maker wallet, the tx hash, and the Polymarket username.

The data validates well:

- Volume matches to within 0.002%.
- On-chain receipts match leg-for-leg in 40 of 40 sampled transactions.
- Every publicly reported whale is identifiable, and the reported dollar figures are reproduced.

The caveats are listed at the end. The main ones are:

- The Data API's undocumented pagination caps.
- Our maker/taker pairing is derived, not delivered by the API, although it is validated.
- The Fed-Sep clean table currently covers only 7 days.
- The Maduro news timestamp is uncertain by about 3 hours.

**Recommended source:** use these together.

- Data API `/trades`, pulled twice (`takerOnly=true` and `takerOnly=false`), sliced into time windows.
- CLOB `/prices-history` as the price benchmark.
- Polygon tx receipts as the audit trail.

## 1. Markets (`markets.csv`)

| key | group | market slug | conditionId | open → closed | Gamma volume | news timestamp (UTC) |
|---|---|---|---|---|---|---|
| nobel_machado | treatment | will-mara-corina-machado-win-the-nobel-peace-prize-in-2025 | 0x14a3…5a1d | 8 Jul 2025 → 10 Oct 2025 | 2,243,006 | 10 Oct 2025 09:00 (11:00 Oslo) |
| maduro_out_jan31 | treatment | maduro-out-by-january-31-2026-318 | 0x580a…f993 | 12 Dec 2025 → 3 Jan 2026 | 10,966,929 | 3 Jan 2026 06:01 (alt. ~09:00) |
| fomc_2026_07_nochange | control | will-there-be-no-change-in-fed-interest-rates-after-the-july-2026-meeting | 0x8bf1…ef86 | 20 Mar 2026 → 29 Jul 2026 | 40,497,086 | 29 Jul 2026 18:00 |
| fomc_2026_09_hike25 | control | will-the-fed-increase-interest-rates-by-25-bps-after-the-september-2026-meeting-649 | 0x8765…5d0d | 13 May 2026 → 16 Sep 2026 | 42,158,279 | 16 Sep 2026 18:00 |

Full conditionIds and YES/NO token IDs are in `markets.csv`. All four markets resolved YES.

For the FOMC markets we used the outcome that actually happened: July "no change" and September "+25 bp". These are the two most recent resolved meetings. Both were well anticipated: YES traded at about 0.79–0.81 and 0.88 just before the statement.

**Maduro news timestamp.** We use **06:01 UTC, 3 Jan 2026**. Al Jazeera's timeline ("Trump bombs Venezuela, US abducts Maduro: All we know", 3 Jan 2026, updated 4 Jan) says US helicopters landed at Maduro's compound at 2:01am Caracas time (06:01 GMT), amid explosions reported live in Caracas.

Trump's Truth Social post confirming the capture came "a little after 09:00 GMT", per the same source. We could not confirm the exact minute; one smaller outlet says ~4:30am ET (09:30 UTC). Both times are drawn on the chart. For a "pre-news" test, 06:01 is the conservative (earlier) cut-off.

## 2. Source log

| Source | Result | Detail |
|---|---|---|
| **(a) Data API `/trades`** | **WORKS: used** | Rows are per wallet-leg: `proxyWallet`, side, outcome, price, size, timestamp, `transactionHash`, username. `limit` ≤ 1000. `offset` ≤ 10,000 (beyond that: HTTP 400 `max historical trades offset of 10000 exceeded`). `start`/`end` (unix seconds, both inclusive) work, so the script splits time windows recursively until each window holds ≤ 11k rows. There is no explicit maker/taker flag: `takerOnly=true` returns only the taker leg, and `takerOnly=false` returns taker plus all maker legs. Speed is ~1–3 s per request. A full-history pull of a $40M Fed market is ~190 requests (~10 min); Nobel and Maduro take ~1 min each. Requests are sequential with a 0.12 s pause (4 parallel chunks); no HTTP 429s were seen. |
| (a) CLOB `/prices-history` | Works: used for validation | Minute-level displayed price per token. |
| (a) CLOB `/trades` | Not used | Needs L2 API-key auth and returns only the caller's own trades. |
| **(b) Goldsky orderbook subgraph** | **FAILED** | Returns `ENDPOINT_DEPRECATED`: "paused and deprecated following Polymarket's migration to V2 — the data is stale and incorrect". It points to Goldsky Edge Data API, which we did not try because it may need an account. |
| (b) On-chain `OrderFilled` via public Polygon RPC | Works for receipts; used for audit only | Most public RPCs need keys or are dead: publicnode (archive needs token), polygon-rpc.com (403 "tenant disabled"), Ankr (key), Blast (discontinued), Nodies (paid). drpc.org and Tenderly gateway serve historical receipts without a key. 1rpc.io returns `null` for some txs. Scanning full history via `eth_getLogs` is impractical on free RPCs, because the asset ID is not an indexed topic, so you would have to pull *every* exchange fill. **Polymarket migrated to CTF Exchange V2 (`0xe2222d27…`) during the July-2026 market's life.** V2 emits a different `OrderFilled` (topic `0xd543adfd…`, layout side/tokenId/amounts). The decoder handles both. The Data API covers both transparently. |
| (c) Telonex / HuggingFace `SII-WANGZJ/Polymarket_data` | Not tried | Per the brief we stopped at (a). The HF set is pre-2026, so it would not cover Maduro or 2026 FOMC. |

## 3. Cleaning (`data/clean/trades_<key>.parquet`)

**One row per maker leg.** In each matched tx the taker leg's size equals the sum of the maker legs. So the maker legs are the economic trades, and the taker leg is dropped to avoid double counting. This mirrors the on-chain structure: one `OrderFilled` per maker order, plus one aggregate taker `OrderFilled` whose counterparty is the exchange. The pairing checks:

| check | Nobel | Maduro | Fed Jul | Fed Sep (7d) |
|---|---|---|---|---|
| taker legs (= txs) | 6,143 | 9,367 | 53,049 | 14,502 |
| maker legs = clean trades | 9,455 | 17,037 | 75,022 | 20,063 |
| txs with >1 taker leg | 0 | 0 | 0 | 0 |
| tx sum(maker sizes) = taker size | 100% | 100% | 100% | 100% |
| on-chain receipt match (random txs) | 10/10 | 10/10 | 10/10 (V1+V2) | 10/10 (V2) |

On the on-chain check: an earlier run also matched 25/25 on Nobel. All Maduro receipts that loaded matched; the rest failed to load from the RPC.

**Columns.**

- `timestamp_utc`, `tx_hash`.
- `wallet` (taker), `maker_wallet`.
- `side`: the taker's side in YES terms.
- `yes_price`, `size_shares`.
- `notional_usd`: USDC the taker actually paid or received on that leg.
- Extras: `yes_notional_usd`, usernames, the outcome each side traded, and `self_trade`.

**Normalisation.** A NO fill at price p becomes a YES fill at 1−p with the opposite side. Selling NO at 1−y has the same payoff as buying YES at y, so wallet PnL on the normalised table is exact.

**Operator exclusion.** We filter V1 CTF Exchange, NegRisk Exchange, NegRisk Adapter, the CTF contract and V2 Exchange. No user-to-user fill had one of these as a party (0 excluded), because the Data API already omits exchange legs.

## 4. Validation

**Volume.** Gamma's `volume` field is matched **shares**, not USDC. Our share totals match it almost exactly:

| | Gamma volume | our shares | diff |
|---|---|---|---|
| Nobel | 2,243,005.86 | 2,242,960.44 | −0.002% |
| Maduro | 10,966,928.53 | 10,966,928.53 | 0.000% |
| Fed Jul | 40,497,085.55 | 40,497,086.86 | +0.000% |
| Fed Sep | 42,158,279.48 | 42,157,874 (full-history taker legs) | −0.001% |

USDC notional actually paid by takers is much lower: Nobel $1.10M, Maduro $4.54M, Fed Jul $24.8M.

**Price.**

- The last trade is 0.999 in all four markets, consistent with resolution to 1.
- Against Polymarket's displayed minute price (CLOB `prices-history`), mean absolute error is 0.1–0.5¢.
- 99.4–99.98% of minutes are within 5¢.

**Publicly reported figures.**

Odds and timing:

| Report | Our data | Status |
|---|---|---|
| Nobel odds ~3.75% at 00:40 Oslo | last trade 3.1%, 10-min VWAP 3.3%; Polymarket displayed 3.6% | ✔ (within 0.7pp) |
| Nobel odds ~72.8% at 02:00 Oslo | last trade 74.5%; displayed 72.6% | ✔ |
| Nobel price jump | first trade >10¢ at 22:47:27 UTC 9 Oct (00:47 Oslo), ~10h before the announcement | consistent |

Nobel wallets:

| Report | Our data | Status |
|---|---|---|
| "dirtycup" bet ~$68–70k, profit ~$30k | wallet `0x234cc49e…`: $68,374 bought at 0.65–0.705, 03:29–03:41 UTC 10 Oct; PnL to resolution $30,634 | ✔ |
| "6741" (reported ~$53.5k / >$50k) | `0xa4305067…`: first buy 22:42:57 UTC 9 Oct at 3.9¢ (one of the first buyers in the jump); PnL $57,342 | ✔ (≈7% above the report) |
| "GayPride" (reported >$85k) | `0x551e72ed…`: PnL $85,778 | ✔ |
| "Three accounts ~$90k combined" | Composition differs across outlets. 6741 + dirtycup = $87,976; GayPride alone = $85,778 | ⚠ which three is ambiguous in the press |

Maduro wallet:

| Report | Our data | Status |
|---|---|---|
| New wallet put ~$32–34k into YES | `0x31a56e9e…`: $32,538 in this market (reported $33.9k across 4 markets) | ✔ |
| Bought at ~5–8¢ | 124 taker buys at 6–10¢, VWAP 7.45¢ | ✔ (range slightly higher than reported) |
| Biggest buys on the night of 2 Jan (US time) | $20.2k of $32.5k bought 01:00–03:00 UTC 3 Jan (8–10pm ET, 2 Jan) | ✔ |
| ~$400k profit | 436,760 shares (press: redeemed $436,759.61); PnL $404,221 | ✔ |

**DOJ case (added 17 Oct 2026).** On 23 April 2026 federal prosecutors unsealed an indictment against an active-duty Army Special Forces master sergeant. It alleges he used classified information about the raid to bet on Polymarket. He is charged, not convicted.

The indictment does not publish a wallet address. Its public facts nonetheless match this wallet on four points:

| Indictment (as reported) | This wallet (our data / Data API) |
|---|---|
| Account created 26 Dec 2025 | Profile ID suffix `1766730765984` decodes to 26 Dec 2025 06:32 UTC |
| Bets from 27 Dec 2025, including >600 shares of "US forces in Venezuela by end of January" | First-ever trade 27 Dec 2025 05:05 UTC: $96 in "US forces in Venezuela by January 31, 2026?" |
| About $33,000 bet across Maduro/Venezuela markets | $32,538 in this market; $33.9k across 4 markets (GIGAZINE) |
| Netted more than $409,000 | PnL $404,221 in this market; final balance $409,882 (GIGAZINE) |

Present this as "the indictment's public facts match this wallet", not as a confirmed identification. Refer to the wallet by address only. Sources: [Task & Purpose](https://taskandpurpose.com/news/special-forces-polymarket-maduro-venezuela/), [Boston.com/AP](https://www.boston.com/news/national-news/2026/04/24/u-s-soldier-charged-with-using-classified-intel-to-win-400k-polymarket-bet-on-maduro-raid/).

## 5. Charts (`charts/<key>.png`)

Each chart shows:

- Our YES trade price, with Polymarket's displayed price underneath.
- Taker fills above `WHALE_USD = 5000` (a parameter) as dots: blue = buy YES, orange = sell YES, area ∝ notional.
- Every fill by a reported wallet, as black markers.
- The news line.

The window is the 48h before news plus 4h after.

Note: the Maduro wallet split its buying into 124 fills, nearly all below $5k. A per-fill whale threshold therefore misses it. The real analysis should also aggregate by wallet × time bucket.

## 6. PASS/FAIL

| Criterion | Nobel | Maduro | Fed Jul | Fed Sep |
|---|---|---|---|---|
| Complete trade history obtained | PASS | PASS | PASS | **PARTIAL**¹ |
| Wallet addresses present (taker and maker) | PASS | PASS | PASS | PASS |
| Volume/price validate within ~5% | PASS | PASS | PASS | PASS² |
| Publicly reported whale trades identifiable | PASS³ | PASS | n/a (control) | n/a (control) |
| Enough trades in 24h pre-news | PASS (4,632 fills, 1,169 takers) | PASS (1,073 fills, 255 takers) | PASS (8,166 / 2,236) | PASS (4,311 / 1,709) |

¹ To save time, the clean, maker-paired Fed-Sep table covers only 9–16 Sep 2026 (`pull_days_before_news=7` in `MARKETS`). Full-history taker legs were pulled (38,863 txs), so the data exists. Delete that line and re-run (~10 min) to get the full table.

² Validated from the full-history taker legs, plus the price path inside the 7-day window.

³ The "three accounts ~$90k" composition is ambiguous in the press. Each named account is found individually.

## 7. Main risks

1. **API fragility.** The Data API is public but its limits are undocumented (offset cap 10k, limit 1k), and it could change. Raw pulls are snapshotted in `data/raw/` so results are reproducible.
2. **Derived maker/taker pairing.** The Data API does not label maker vs taker; we infer it from `takerOnly=true` minus `takerOnly=false`. It is validated (100% size reconciliation, 40/40 on-chain matches) but should be re-checked for any new market.
3. **Wallet ≠ person.** These are proxy wallets. One actor can split across many fresh wallets. Linking them needs on-chain funding-flow tracing (USDC transfers into proxies), which this check does not do. The only case with public legal action is the Maduro DOJ indictment (section 4), and even there the wallet link rests on matching public facts, not on a published address.
4. **News timestamps.**
   - Maduro: 06:01 (first raid reports) vs ~09:00 (Trump post) changes which trades count as "pre-news".
   - Nobel: the announcement time is fixed, but the leak's onset (~22:40 UTC 9 Oct) is ~10h earlier.
5. **PnL approximations.** PnL is held-to-resolution and uses this market's trades only. It ignores splits/merges, NegRisk conversions, and tokens moved between wallets. This explains small gaps vs press figures (e.g. 6741: $57.3k vs ~$53.5k).
6. **Control comparability.** The Fed markets are 4–20× more active and nearly certain going in. Leakage metrics need normalising (e.g. by baseline volume, or by price-implied surprise) before comparing treatment and control.
7. **Exchange V2 migration (2026).** Any independent on-chain pipeline must decode both event layouts and both collateral tokens.

## 8. Added treatment markets: Ballon d'Or 2024, 2025, 2026

Ballon d'Or votes are cast weeks before the ceremony. Between the vote closing and the ceremony, the winner is fixed but secret. Any informed trading in that window can only come from a leak, which makes the Ballon d'Or a clean treatment case. Same pipeline, same checks.

| key | market | Gamma volume | our shares | on-chain | tx reconciled | news / anchor time (UTC) |
|---|---|---|---|---|---|---|
| bdo_2024_rodri | Will Rodri win the Ballon d'Or? (YES) | 807,072 | 807,072 | 10/10 | 100% | 28 Oct 2024 07:22, Real Madrid boycott reported (team check) |
| bdo_2024_vinicius | Will Vinicius Jr win the Ballon d'Or? (NO) | 1,405,149 | 1,405,149 | 10/10 | 100% | 28 Oct 2024 07:22 |
| bdo_2025_dembele | Will Ousmane Dembele win the 2025 Ballon d'Or? (YES) | 3,043,257 | 3,043,117 (−0.005%) | 10/10 | 99.996%¹ | 22 Sep 2025 19:00, ceremony start |
| bdo_2026_kane | Will Harry Kane win the 2026 Ballon d'Or? (open) | 5,732,694 | 5,734,255 (+0.03%)² | 10/10 | 100% | 9 Oct 2026 17:40, Kane overtakes Yamal³ |

¹ One tx has two taker legs, so its 2 maker legs are left unpaired and dropped.

² The market is still open. Gamma's figure and our pull were taken a few minutes apart.

³ This is not a public news time. It comes from our own trade data, and we found no news trigger for it. Votes closed 28 Sep 2026; the ceremony is 26 Oct 2026. PnL for this market is marked to the last trade price (0.64), because it has not resolved.

**Findings**

- **2024: the market reacted ~6.5 hours *after* the public report (updated 20 Oct 2026).**
  - Rodri traded at 5–16% through 27 Oct and the morning of 28 Oct.
  - The first trade above 50% was at **13:55:47 UTC on 28 Oct** (wallet "BluntedH"). Within the 14:00 UTC hour Rodri reached 93%, and Vinícius fell from 74% to 6%.
  - The ceremony started at 19:45 UTC.
  - **News time (team check):** the Real Madrid boycott was reported at 8:22 AM Paris time. Paris was on CET (UTC+1) that day, because summer time ended 27 Oct, so this is **07:22 UTC**. A possible earlier leak came at 7:50 AM Paris (06:50 UTC). Real Madrid cancelled its 3pm flight to Paris.
  - **So the flip came ~6.5h after the public report**, at about 9:55am in New York. This is slow incorporation of public news, not trading ahead of it. We regroup it as "public news" in module A1.
  - **Before the report**, the market drifted only +6–7pp (Vinícius 94% → 88%; A1 placebo p ≤ 0.01). There was also a brief Rodri spike to 46% around 23:30 UTC on 27 Oct. These small pre-report moves are worth a wallet check.
- **2025: no pre-ceremony flip.** Dembélé, the favourite, drifted from 90% to 76% on the evening of 21 Sep, recovered to ~84%, and resolved YES. A brief drop to 41% came right at the ceremony start. This is a "no leak" comparison within the same event type.
- **2026:** see the 9 Oct analysis. The move began 17:40 UTC with many wallets trading at once, and was driven mostly by the active trader purplegatto, who bought Kane and sold Yamal. A brand-new single-topic wallet (`0x3ed2c452…`) bought ~$30k of Kane from 18:09 UTC. The 26 Oct result will show whether that was informed.

**Scale caveat:** these markets are thin. In the 24h before the anchor time, takers traded $44k (2026 Kane) to $445k (2025 Dembélé). A few wallets can move the price, so price impact and informed flow are hard to separate.

## 9. Extension checked: cricket (match fixing)

Polymarket covers many cricket leagues. Examples, from search results capped at 50 events per query:

| League | Typical match volume |
|---|---|
| IPL | median ~$2.3M |
| The Hundred | median ~$0.2M |
| PSL | median ~$0.1M |
| Lanka Premier League | median ~$28k |
| SA20 | median ~$27k |
| ILT20 | median ~$11k |
| Associate internationals (e.g. Nepal v Oman) | ~$15k |

Each match is essentially **one match-winner market**, plus negligible "toss" and "completed match" markets. For example:

- LPL Jaffna v Kandy: $253k, 725 taker trades.
- ILT20 Abu Dhabi v Desert Vipers: $51k, 374 taker trades.

There are no spot-fixing style markets (runs in an over, no-balls).

**Feasibility: medium-low.** The data mechanics work, since it is the same Data API. But fixing has no public "news time" to test against. A study would need either documented fixing cases inside Polymarket's coverage period (2025–2026), or a screen across many small-league matches. Such a screen would flag fresh wallets taking large one-sided positions on underdogs that then won. It flags anomalies; it does not prove fixing.

## 10. Award-vote panel (NBA, NFL, Heisman)

Awards are voted on before a scheduled announcement, so the winner is fixed but secret in between. That is the same design as the Ballon d'Or. We use each winner's market (YES resolved).

**Anchor times are provisional.** Each one is the first minute the price reaches 99.5% and holds, taken from Polymarket's own price history. For OROY the anchor is the NFL Honors show instead, because the price had already passed 99.5% days earlier. Official announcement times and first insider-report times still need confirming. All panel validation checks pass.

| key | winner market | Gamma volume | volume diff | on-chain | price 2h before anchor | trades in 24h before anchor | provisional anchor (UTC) |
|---|---|---|---|---|---|---|---|
| award_nba_mvp_2026 | Gilgeous-Alexander, 2025-26 MVP | 4,481,676 | 0.000% | 10/10 | 98% | 3,329 | 17 May 2026 13:52 |
| award_nba_mvp_2025 | Gilgeous-Alexander, 2024-25 MVP | 816,938 | 0.000% | 10/10 | 99% | 104 | 20 May 2025 19:20 |
| award_nfl_mvp_2025 | Josh Allen, 2024-25 MVP | 3,331,122 | +0.007% | 10/10 | 93% | 1,320 | 7 Feb 2025 03:40 |
| award_nfl_oroy_2025 | Jayden Daniels, OROY | 569,402 | 0.000% | 10/10 | 99.5% | 85 | 7 Feb 2025 02:00 (Honors start, assumed) |
| award_heisman_2024 | Travis Hunter, Heisman | 221,193 | 0.000% | 10/10 | 94% | 153 | 15 Dec 2024 01:55 |
| award_nba_dpoy_2026 | Wembanyama, 2025-26 DPOY | 1,484,831 | 0.000% | 10/10 | 99.5% | 34 | 20 Apr 2026 14:57 |
| award_nba_coy_2026 | Mazzulla, 2025-26 COY | 577,582 | +0.13% | 10/10 | 96% | 344 | 26 May 2026 23:38 |

Every market reconciles 100% of transactions.

**Findings**

- **NFL MVP 2024-25 is the standout case.**
  - Allen traded at 42–55% for days. He was ~53% until 23:10 UTC on 6 Feb 2025, then went from 56% to 96% by ~23:55 UTC. The price settled at 99.5% at 03:40 UTC on 7 Feb, during the NFL Honors.
  - A brand-new wallet, **"boobyface" (`0xa5d2078a…`)**, made its first-ever Polymarket activity at 22:21 UTC in this market. It bought $23.2k at ~59.5% at 22:47:50 UTC, about 30 minutes before the jump. It later sold out for **+$15.2k**.
  - From 23:20 UTC, a wave of $5–12k buyers followed: Cashmaster222, DLEK, Arcani12, jt7.
  - What triggered the jump is not yet sourced.
- **Most other winners were 94–99.5% favourites before the anchor.** The winner market leaves little room for a leak signal there. For those events, the losing contenders' markets are where a leak would show.
- **Thin windows:** NBA DPOY (34 trades), NFL OROY (85) and NBA MVP 2024-25 (104) are below the 100-trade threshold in the 24h before the anchor.

## 11. Military strikes on Iran

Polymarket lists no terrorist-attack markets of any size: two exist, both under $0.3M. State military-strike markets are large, and the 2026 US–Iran strikes are where analytics firms have documented suspicious wallets.

| key | market | group | Gamma volume | volume diff | on-chain | tx reconciled | provisional anchor (UTC) |
|---|---|---|---|---|---|---|---|
| iran_strike_2026_02_28 | Will the US next strike Iran on February 28, 2026 (ET)? | treatment | 5,837,357 | 0.000% | 10/10 | 100% | 28 Feb 2026 06:10, first reports 8:10 Israel time (team check) |
| iran_strike_2025_06_control | US military action against Iran before July? | control | 29,917,021 | 0.000% | 10/10 | 100% | 21 Jun 2025 23:47, price jump (public announcement); team timestamp 22:40 UTC, meaning TBC |

**Anchor times (updated 20 Oct 2026).**
- **28 Feb 2026:** first reports at 8:10 Israel time (UTC+2 in February) = 06:10 UTC, per team check.
- **June 2025:** the public-news anchor is still the price jump at 23:47 UTC. The team gave a timestamp of 22:40 (UTC assumed), possibly the strike time. Between 22:30 and 23:40 UTC the price was flat at ~57%, so nobody traded on it in that window.

**28 Feb 2026 strike (treatment)**

- **Price path.** The 28 Feb date contract traded at 4–5% until 10:46 UTC on 27 Feb. It then jumped to ~14% and hovered at 11–20% overnight. It climbed from 11% to 26% between ~05:00 and 06:10 UTC on 28 Feb, *before* the first reports. It then jumped to 58% in the 06:10 bar and reached 99.8% by ~07:30. In A1, the 24h before the reports shows +19.5pp (placebo p = 0.005), about 20% of the eventual move.
- **The early move (27 Feb, 10:00–11:30 UTC) was not a buying wave.** Takers traded only $22k in that window and were net *sellers* (−$9.9k). The cause is unsourced.
- **Four fresh wallets bought before the price passed 50%:**

| wallet | first-ever Polymarket activity | bought (before anchor) | price range | PnL in this market |
|---|---|---|---|---|
| `0x1d81ff47…` | 25 Feb 2026 | $4.9k, from 01:33 UTC 28 Feb | 15–20¢ | +$22.5k |
| `0xa64bde82…` | 20 Feb 2026 | $1.2k, 27 Feb | 12–19¢ | +$6.5k |
| `0x91862815…` | 23 Feb 2026 | $2.9k, from 11:17 UTC 27 Feb | 9–70¢ | +$5.3k |
| `0xa60bed17…` | 26 Feb 2026 | $1.0k, 27 Feb | 16–28¢ | +$3.5k |

- **Comparison with press reports.** These wallets match the Bubblemaps/Bloomberg profile: wallets created in February, buying hours before the first Tehran reports. They are smaller than the reported ~$1M across six wallets. We cover only this one date contract, and we have not matched our wallets to the Bubblemaps list.
- **Biggest pre-anchor buyer.** The largest net buyer before the anchor, "balthazar" (an account since Feb 2025), made 392 trades both ways and *lost* $42k in this market. So large pre-news flow does not equal informed flow.

**21 June 2025 strikes (control)**

- The price was 40% 24h before the anchor, 46% at −6h, 57% at −1h and 58% at −5min. The jump started at 23:47:21 UTC.
- The drift from 40% to 58% across 21 June is not "flat". It may reflect public signals that day, but no news check has been done.
- This is a weaker control than FOMC. Use it as "same event type, jump at the announcement".

**Not covered:** the "nine accounts, $2.4M" cluster and the "$87k bet 71 minutes before" case span other strike markets we have not pulled. We also have not tested any link between our flagged wallets and the Bubblemaps lists. Sources: [Bloomberg](https://www.bloomberg.com/graphics/2026-polymarket-bets-iran-war-insider-trading/), [Cointelegraph](https://cointelegraph.com/news/polymarket-traders-1m-us-iran-strike-insider-trading-concerns), [Snopes](https://www.snopes.com/news/2026/03/03/polymarket-bets-iran-strikes/), [Quartz](https://qz.com/polymarket-insider-trading-iran-war-bets-bubblemaps-051926), [CNN](https://www.cnn.com/2026/08/21/politics/polymarket-doj-military-prediction-market).

## Reproduce

```bash
python3 -m venv .venv && .venv/bin/pip install pandas pyarrow matplotlib requests
.venv/bin/python polymarket_feasibility.py            # uses cached data/raw if present
.venv/bin/python polymarket_feasibility.py --refresh  # re-download everything
.venv/bin/python polymarket_feasibility.py --only fomc_2026_09_hike25
.venv/bin/python polymarket_feasibility.py --only bdo_2024_rodri,bdo_2024_vinicius,bdo_2025_dembele,bdo_2026_kane
.venv/bin/python polymarket_feasibility.py --only award_nba_mvp_2026,award_nba_mvp_2025,award_nfl_mvp_2025,award_nfl_oroy_2025,award_heisman_2024,award_nba_dpoy_2026,award_nba_coy_2026
.venv/bin/python polymarket_feasibility.py --only iran_strike_2026_02_28,iran_strike_2025_06_control
```
