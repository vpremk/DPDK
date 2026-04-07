# Post-Trade — Surveillance

Real-time and end-of-day monitoring for market abuse, regulatory compliance,
and operational anomalies. Every order and fill is logged and analysed.

## Responsibilities

| Function | Description |
|---|---|
| Audit trail | Immutable, timestamped record of every order event |
| Market abuse detection | Layering, spoofing, wash trading, front-running |
| Regulatory reporting | MiFID II RTS 27/28, SEC Rule 613 (CAT), FINRA OATS |
| Best execution reporting | Post-trade TCA (Transaction Cost Analysis) |
| Anomaly detection | Statistical outlier detection on order patterns |
| Alert management | Route alerts to compliance team via Slack / email |

## Audit log

`fix_audit.log` — NDJSON, one line per order event written by `ems/dpdk_pcap`.

Fields per line:
```json
{
  "ts_ns": 1743980412837461200,
  "seq": 42,
  "clordid": "ORD000042",
  "sender": "CLIENT",
  "symbol": "AAPL",
  "side": "Buy",
  "qty": 500,
  "price": 189.50,
  "msg_type": "D",
  "decision": "ACCEPTED",
  "risk_us": 187
}
```

## Market abuse patterns

| Pattern | Detection method |
|---|---|
| Layering / spoofing | High cancel-to-trade ratio on same symbol within 500 ms |
| Wash trading | Buy and sell same symbol from same account within window |
| Front-running | Order placed within 10 ms of large client order on same symbol |
| Momentum ignition | Rapid sequence of small orders to move price then reverse |
| Marking the close | Unusual order activity in last 5 minutes of session |

## Files

| File | Description |
|---|---|
| `fix_audit.log` | Live audit log from `ems/dpdk_pcap` (NDJSON, append-only) |

## Files to implement

- `surveillance_engine.py` — Stream `fix_audit.log`, apply detection rules
- `tca_report.py` — Transaction cost analysis: VWAP, IS, slippage vs arrival
- `regulatory_reporter.py` — Format and submit MiFID II / CAT reports

## Position in Trade Lifecycle

```
EMS (every order event)  ──►  Surveillance
OMS (state transitions)  ──►  Surveillance
```
