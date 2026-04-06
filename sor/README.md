# SOR — Smart Order Router

Receives risk-approved orders from OMS and decides how to split and route
them across venues to minimise market impact and achieve best execution.

## Responsibilities

| Function | Description |
|---|---|
| Venue selection | Rank lit exchanges, dark pools, internalisation by fill probability and cost |
| Order splitting | Slice large orders across venues (parent → child orders) |
| Best execution | MiFID II / Reg NMS best-price obligation |
| Dark pool sweep | Check internal crossing engine before sending to lit market |
| Algo selection | TWAP / VWAP / IS Optimal (Almgren-Chriss) based on urgency |
| Child order routing | Send child orders to EMS for execution |

## Algo strategies (see `pre_trade_risk/montecarlo_pricing.py`)

| Strategy | When | Logic |
|---|---|---|
| TWAP | Low urgency, large size | Equal slices over horizon T |
| IS Optimal | Medium urgency | Front-loaded sinh schedule, minimise E[IS] + λ·Var[IS] |
| Aggressive FOK/IOC | High urgency | Sweep book immediately |

## Files to implement

- `sor_engine.py` — Main routing logic
- `venue_ranker.py` — Score venues by spread, fill rate, rebates
- `algo_selector.py` — Map order urgency to execution strategy
- `child_order_manager.py` — Track child orders back to parent

## Position in Trade Lifecycle

```
OMS  ──►  SOR  ──child orders──►  EMS
               ──►  Dark Pool
               ──►  Internalise
```
