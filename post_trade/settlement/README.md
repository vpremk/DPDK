# Post-Trade — Settlement

Settlement is the actual exchange of securities and cash between counterparties,
completing the trade obligation. US equities settle T+1 (since May 2024).

## Responsibilities

| Function | Description |
|---|---|
| Delivery vs Payment (DvP) | Simultaneous exchange of securities and cash |
| Settlement instructions | Generate SSIs (Standard Settlement Instructions) |
| Fail management | Identify, report, and manage settlement failures |
| Corporate actions | Handle dividends, splits, mergers on open positions |
| Reconciliation | Reconcile custodian positions vs internal books end-of-day |

## Settlement cycles

| Market | Cycle | Effective |
|---|---|---|
| US equities | T+1 | May 2024 |
| US Treasuries | T+1 | — |
| EU equities | T+2 | — |
| FX spot | T+2 | — |
| Options (US) | T+1 | — |

## Key infrastructure

- **DTCC DTC** — US equity settlement depository
- **Fedwire** — US cash leg (Fed funds)
- **SWIFT MT54x** — Cross-border settlement instructions
- **TARGET2-Securities (T2S)** — EU central securities depository

## Files to implement

- `settlement_engine.py` — Generate and track settlement instructions
- `dvp_monitor.py` — Monitor DvP completion; flag fails
- `reconciliation.py` — End-of-day position reconciliation vs custodian

## Position in Trade Lifecycle

```
Clearance  ──►  Settlement  ──►  Position update (books & records)
```
