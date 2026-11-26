# Mitigation and market design (module A6)

This report covers the brief's fifth requirement: how strategies and market design can be improved. Every recommendation is tied to a result from modules A1–A6. Scripts: `analysis_a1.py`–`analysis_a6.py`; results in `data/analysis/`.

## 1. What the evidence says

| # | Finding | Module | Numbers |
|---|---|---|---|
| 1 | Prices drift towards the outcome before the news in leak events, but not in controls | A1 | Leak markets p ≤ 0.02. Nobel +68pp, NFL MVP +50pp (Lamar +50pp towards NO), Iran 2026 +19.5pp, Iran "by Feb 28" +16pp, Maduro +5pp. Controls p ≥ 0.06 |
| 2 | Standard toxicity and liquidity measures don't separate leaks from anticipation | A2 | VPIN is high for Nobel and Maduro, but also for 2 of 4 controls and for near-certain favourites. Kyle's λ is never higher before news, and lower in 11 of 16 markets |
| 3 | Suspected informed wallets are fresh, single-topic, one-sided and buy late | A3 | Flags 15–25% of pre-news buyers in leak events, vs ≤ 0.2% in FOMC, 0% in Ballon d'Or 2025 and 4.6% in Iran 2025. 31 wallets, $942k profit |
| 4 | Large traders are followed; suspected informed wallets are not | A4 | Large traders draw +1.35 extra same-direction orders in 5 min (1,405 matched pairs, p < 0.001) and a +0.2pp persistent price move; followers pay +0.13pp. Followers lose about 10¢ per $ in control markets. Flagged wallets draw no extra followers and no price move (30 pairs) |
| 5 | Ordinary resting orders pay for the leaks, not professional market makers | A5 | Market makers paid 3–31% of the $942k. One-sided limit-order bettors and active traders paid most. The top 10 counterparties paid 61–87% per event. Flagged wallets mostly took liquidity |
| 6 | A real-time wallet monitor works for scheduled news but is noisy for unscheduled news | A6 | In the final 24h before scheduled announcements: 49 alerts in leak markets vs 6 in controls and 0 for award favourites (~8×), but only 23 of 49 point the right way. Over 7 days it also fires in hot public markets (Iran 2025: 151 alerts; FOMC Sep: 38). Cluster rules don't fix this |

## 2. Recommendations

### Liquidity providers and limit-order traders (who paid)

1. **Cancel or re-price resting orders before scheduled announcements.** Resting orders were the counterparty in 59–100% of the flagged wallets' fills (A5). An order left in the book overnight before a known announcement is a free option for anyone who knows the result.
2. **Don't leave stale one-sided orders on long shots.** The biggest single loser in the Maduro market (−$130k) was a resting order betting against the long shot, filled at 7–10¢. Use orders with an expiry time, and shrink size as the resolution date approaches.
3. **Use wallet-level signals, not just VPIN or λ.** Neither measure separated leaks from anticipation (A2). Fresh, single-topic wallets building ≥ $1k on one side in the 24h before a known announcement do (A6, about 8× more alerts in leak markets).
4. **Treat a wallet alert as a reason to widen or pull, not as a direction signal.** Only about half of the alerts pointed the right way (A6). The safe response is to reduce exposure on both sides.

### Large and institutional traders (execution)

5. **Split and randomise large orders.** Visible large orders attract followers and move price about 0.2pp against later child orders (A4). Use random child sizes and timing, limit orders, and participation caps.
6. **Learn from the informed wallets.** They traded in episodes of about $770, similar to ordinary orders, and drew no followers or price impact (A4). Small, spread-out orders hide intent, as Kyle (1985) predicts.
7. **Monitor your own follower flow.** A rise in same-direction orders right after your trades means your orders are leaking information. Slow down or switch to passive orders when it rises.

### Platform and market design

8. **Run a "scheduled-news surveillance mode."** For markets that resolve at a known time (awards, FOMC, ceremonies), flag fresh, single-topic wallets accumulating one-sided positions in the final 24h (A6). This is cheap: one activity-history lookup per qualifying wallet.
9. **Close or limit trading once the outcome is fixed but secret.** Award votes are cast weeks before the ceremony (e.g. the Ballon d'Or closed votes on 28 Sep 2026 for a 26 Oct ceremony). Halting new positions, or capping their size, after voting closes removes the window that insiders exploit.
10. **Protect resting orders near known announcement times.** Options: default order expiry before scheduled events, a short speed bump on aggressive orders in the final hour, or automatic cancellation of stale resting orders when a surveillance alert fires.
11. **Apply stricter controls to markets on government or military action.** The two largest cases (Maduro, Iran 2026) concerned operations known to a small circle. Real-time alerts there are too noisy: Iran 2025 produced 151 (A6). Practical tools are identity checks and position limits for new accounts, and post-trade wallet forensics with referral to authorities.

### Regulators

12. **Keep the on-chain trail public.** Wallet-level data is what identified every case here, including the one matching the DOJ indictment (A3, A5). Price and volume data alone would have missed the quiet leaks (A1, A2, A4).
13. **Prioritise after-the-fact forensics over real-time policing for unscheduled events.** The A3 rule plus counterparty tracing (A5) reconstructs who profited and who paid, to the dollar.

## 3. Limits

- **The thresholds were set after seeing the named cases**, so recall is in-sample. The evidence that holds up is the near-zero flags in FOMC, Ballon d'Or 2025 and award favourites.
- **The sample is small:** 4 leak events (13 markets), 30 informed pairs in A4, and imperfect controls (Iran 2025 looks suspicious too).
- **Some news times are still provisional:** Maduro's Trump post, the NBA awards and the Heisman.
- **The proposals are untested as policy.** A6 back-tests detection only. Effects on market quality, such as thinner books if makers pull quotes more often, are not measured.
