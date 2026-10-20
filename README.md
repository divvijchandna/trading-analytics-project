# Wallet Behaviour and Information Flow (FN6908)

Group mini-project for FN6908 Trading Analytics & Processes (NTU MFE), Track A.

**Research question.** Is the trading of large traders followed by other traders? How do market prices respond to their trades? Do the followers get worse execution prices?

The setting is Polymarket. Cases where large traders are suspected of trading on private information (Nobel 2025, Maduro, NFL MVP 2024-25, Iran strike Feb 2026) are compared with control events (FOMC, Ballon d'Or 2025, Iran strikes Jun 2025).

## Files

| File | Purpose |
|---|---|
| `polymarket_feasibility.py` | Data pipeline: market identification (Gamma API), trade download with wallets (Data API), cleaning, validation (volume, price, on-chain receipts), per-market charts |
| `analysis_a1.py` | Module A1: event study of the window before news, with placebo inference |
| `analysis_a2.py` | Module A2: microstructure metrics (VPIN, Kyle's lambda) before news vs normal periods |
| `analysis_a3.py` | Module A3: wallet forensics and a detection rule (fresh, single-topic, one-sided, late buying) |
| `analysis_a4.py` | Module A4: are large / flagged traders followed, how does price respond, and what do followers pay and earn |
| `markets.csv` | Market identifiers, news timestamps and sources |
| `feasibility.md` | Data feasibility report and source log |
| `data/raw/` | Cached API pulls (parquet) |
| `data/clean/` | One row per economic fill, normalised to YES (parquet) |
| `data/validation/` | Validation outputs, wallet PnL, on-chain spot checks |
| `data/analysis/` | Module results |
| `charts/` | Figures |

## Reproduce

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python polymarket_feasibility.py   # uses cached data/raw; --refresh re-downloads
.venv/bin/python analysis_a1.py
.venv/bin/python analysis_a2.py
.venv/bin/python analysis_a3.py
.venv/bin/python analysis_a4.py
```

All data come from public, keyless Polymarket APIs.

## Conventions

- Wallets are pseudonymous. Refer to them by address only. Say "suspected informed trading", never insider trading as fact.
- The Maduro DOJ case is "charged, not convicted". Its link to the wallet rests on matching public facts.
- News timestamps marked PROVISIONAL in `polymarket_feasibility.py` still need confirming.
