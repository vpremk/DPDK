# Post-Trade — Clearance

Clearance is the process of reconciling buy and sell orders and confirming
the details of the transaction between counterparties before settlement.

## Responsibilities

| Function | Description |
|---|---|
| Trade matching | Match execution reports from EMS against counterparty confirms |
| Novation | CCP (e.g. DTCC / LCH) interposes as buyer to every seller and vice versa |
| Margin calculation | Initial margin + variation margin (SPAN / TIMS model) |
| Netting | Net offsetting positions across trades to reduce settlement obligations |
| Trade affirmation | Institutional allocations affirmed via DTC / Omgeo CTM |

## Key message types

- FIX `ExecutionReport` (MsgType=8) — fill confirms from EMS
- FIX `AllocationInstruction` (MsgType=J) — block trade allocations
- FIX `Confirmation` (MsgType=AK) — post-trade confirmation

## CCPs by asset class

| Asset class | CCP |
|---|---|
| US equities | DTCC / NSCC |
| US options | OCC |
| US Treasuries | FICC |
| EU equities/derivatives | LCH, Eurex Clearing |

## Files to implement

- `clearance_engine.py` — Match and confirm trades with CCP
- `margin_calculator.py` — SPAN margin simulation
- `netting_engine.py` — Position netting across trades

## Position in Trade Lifecycle

```
EMS (fills)  ──►  Clearance  ──►  Settlement
```
