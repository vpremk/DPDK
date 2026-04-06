# OMS — Order Management System

Receives inbound orders from the Client (FIX 4.2 / REST / WebSocket), assigns
internal order IDs, persists state, and routes to Pre-Trade Risk.

## Responsibilities

| Function | Description |
|---|---|
| Order ingestion | Accept NewOrderSingle (D), CancelRequest (F), CancelReplace (G) |
| Order ID allocation | Assign monotonic internal order ID; map to client ClOrdID |
| State machine | PENDING_NEW → ACCEPTED / REJECTED → PARTIALLY_FILLED → FILLED / CANCELLED |
| Persistence | Write order state to Aurora (primary) + DynamoDB (audit copy) |
| Routing | Forward accepted orders to Pre-Trade Risk; relay decisions back to client |
| FIX session | Manage FIX sequence numbers, heartbeats, resend requests |

## Files to implement

- `oms_gateway.py` — FIX session acceptor (QuickFIX/N or raw socket)
- `order_store.py` — Aurora / DynamoDB persistence layer
- `state_machine.py` — Order lifecycle state transitions
- `order_router.py` — Route to pre_trade_risk, SOR, EMS

## Position in Trade Lifecycle

```
Client  ──FIX──►  OMS  ──►  Pre-Trade Risk  ──►  SOR  ──►  EMS
```
