# Pre-Trade — Deep Dive Architecture

## Overview

The pre-trade phase is the most latency-sensitive and data-intensive stage of the trade lifecycle.
Every millisecond of delay here directly impacts execution quality, fill rates, and alpha capture.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          PRE-TRADE SUBSYSTEMS                                   │
│                                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ Market Data  │  │  Reference   │  │  Pricing &   │  │   Analytics &    │   │
│  │Infrastructure│  │    Data      │  │  Valuation   │  │ Decision Support │   │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘   │
│         │                 │                  │                   │             │
│         └─────────────────┴──────────────────┴───────────────────┘             │
│                                     │                                           │
│                          ┌──────────▼──────────┐                               │
│                          │  Order Management   │                               │
│                          │     System (OMS)    │                               │
│                          └──────────┬──────────┘                               │
│                                     │                                           │
│                          ┌──────────▼──────────┐                               │
│                          │   Pre-Trade Risk    │                               │
│                          │   & Compliance      │                               │
│                          └──────────┬──────────┘                               │
│                                     │                                           │
│                          ┌──────────▼──────────┐                               │
│                          │   APPROVED ORDER    │──► Phase 2: Execution          │
│                          └─────────────────────┘                               │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Market Data Infrastructure

### 1.1 Feed Sources & Ingestion Layer

```
External Data Sources
│
├─► Exchange Direct Feeds           (ITCH, FAST, PITCH, XDP)
├─► Consolidated Tape               (SIP — CTA/UTP)
├─► Bloomberg B-PIPE / SAPI
├─► Refinitiv Elektron / RDF
├─► Broker Contributed Data
└─► Alternative Data                (news, sentiment, ESG, satellite)
        │
        ▼
AWS Direct Connect (10 Gbps, dual-provider redundancy)
        │
        ▼
EC2 Nitro (C7gn / C6in) — Feed Handler Cluster
├─ Placement Group (cluster)        ← same physical rack, lowest inter-node latency
├─ Enhanced Networking (ENA)        ← 100 Gbps, low-latency NIC
├─ SR-IOV                           ← direct hardware access, bypass hypervisor
└─ DPDK / EFA (HFT mode)           ← kernel bypass, <5µs packet processing
```

**Feed Handler responsibilities:**
- Raw binary protocol decode (ITCH, FAST, FIX)
- Sequence gap detection and retransmission requests
- Duplicate filtering
- Timestamp normalization (exchange time → arrival time → processing time)
- Feed arbitrage (pick best-priced source across primary + backup feeds)

### 1.2 Market Data Normalization & Distribution

```
Feed Handlers (EC2 Nitro)
        │
        ▼
Normalization Engine (EKS — dedicated node group, CPU-pinned pods)
├─ Unified market data model        (instrument, price, size, side, timestamp)
├─ Currency normalization           (FX conversion to base CCY)
├─ Corporate action adjustment      (splits, dividends)
└─ Data quality checks              (stale price, crossed market, outlier detection)
        │
        ├─► Amazon Kinesis Data Streams     (fan-out, 1ms, durable)
        │       ├─ 1 shard per exchange/asset class
        │       └─ Enhanced fan-out         (dedicated 2 MB/s per consumer)
        │
        └─► UDP Multicast (within VPC)      ← HFT path, <50µs, no persistence
```

### 1.3 Market Data Storage Tiers

```
┌─────────────────────────────────────────────────────────────────────┐
│ TIER 1 — MICROSECOND (Hot)                                          │
│ ElastiCache for Redis (cluster mode, r7g.4xlarge × 6 nodes)        │
│   ├─ NBBO (National Best Bid/Offer) per instrument                  │
│   ├─ Full order book (L2 — 10 levels bid/ask)                       │
│   ├─ Last sale price + volume                                       │
│   ├─ Intraday VWAP / TWAP                                           │
│   └─ TTL: intraday only, evicted at market close                    │
│   Latency: <200µs read | Write pipeline (async)                     │
├─────────────────────────────────────────────────────────────────────┤
│ TIER 2 — MILLISECOND (Warm)                                         │
│ Amazon MemoryDB for Redis (durable, Multi-AZ)                       │
│   ├─ Today's OHLCV bars (1s, 1m, 5m, 15m, 1h)                      │
│   ├─ Rolling 30-day price history                                   │
│   ├─ Intraday statistics (high, low, volume, trade count)           │
│   └─ Durability: replicated transaction log → S3                    │
│   Latency: <1ms read                                                │
├─────────────────────────────────────────────────────────────────────┤
│ TIER 3 — MILLISECOND (Reference)                                    │
│ Amazon DynamoDB (on-demand, DAX accelerator)                        │
│   ├─ Instrument master (ISIN, CUSIP, SEDOL, ticker mappings)        │
│   ├─ Exchange calendars, trading hours, halt status                 │
│   ├─ Corporate actions (dividends, splits, spinoffs)                │
│   └─ Static risk parameters (lot sizes, tick sizes, margin rates)  │
│   Latency: <1ms with DAX | <10ms without                            │
├─────────────────────────────────────────────────────────────────────┤
│ TIER 4 — SECOND/MINUTE (Analytics)                                  │
│ Amazon S3 + Parquet (partitioned by date/asset class/exchange)      │
│   ├─ Full tick history (TAQ — trades and quotes)                    │
│   ├─ EOD price/volume files                                         │
│   ├─ Implied volatility surfaces (historical)                       │
│   └─ Factor data (momentum, value, quality, size)                   │
│   Query engine: Amazon Athena (ad hoc) | Redshift Spectrum (OLAP)   │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.4 Market Data API Layer

```
Internal consumers (OMS, EMS, Risk, Pricing)
        │
        ▼
Market Data Service (EKS — gRPC + WebSocket)
├─ Subscription management          (topic-based, instrument-level)
├─ Snapshot + delta streaming       (initial state + incremental updates)
├─ Conflation                       (drop stale ticks under load)
├─ Entitlement enforcement          (per-user/per-desk data permissions)
└─ Rate limiting                    (prevent subscriber overload)

API patterns:
├─ gRPC streaming   ← algo engines, pricing models (lowest overhead)
├─ WebSocket        ← trader UI, web-based tools
└─ REST (GET)       ← one-shot price lookups, reference data queries
```

---

## 2. Reference Data Management

### 2.1 Instrument Master

```
External Sources:
├─► Bloomberg OpenFIGI API
├─► DTCC / CUSIP Global Services
├─► London Stock Exchange (SEDOL)
└─► Internal instrument creation (OTC, structured products)
        │
        ▼
Reference Data Service (EKS)
├─ Instrument master management     (create, update, deactivate)
├─ Identifier cross-reference       (ISIN ↔ CUSIP ↔ SEDOL ↔ ticker ↔ RIC)
├─ Classification hierarchy         (asset class → sector → sub-sector)
└─ Change event publishing          ── EventBridge ──► downstream consumers
        │
        ├─► DynamoDB (primary store, GSI on all identifier types)
        ├─► ElastiCache (hot cache for active instruments)
        └─► S3 (daily snapshot for DR + bulk loads)
```

### 2.2 Counterparty & Entity Data

```
DynamoDB Tables:
├─ counterparty_master              (LEI, BIC, MPID, credit rating, jurisdiction)
├─ counterparty_limits              (credit limits, exposure by asset class)
├─ account_master                   (accounts, sub-accounts, mandates)
├─ trader_profiles                  (permissions, limits, asset class access)
└─ entity_relationships             (parent-child legal entities, consolidation)

Data sources:
├─► GLEIF (LEI registry API)        ── Lambda daily sync ──► DynamoDB
├─► Internal CRM / Onboarding       ── Step Functions ──► DynamoDB
└─► Rating agencies (S&P, Moody's)  ── S3 feed ──► Lambda ──► DynamoDB
```

### 2.3 Market Calendars & Schedules

```
DynamoDB: trading_calendars
├─ Exchange trading sessions        (pre-market, regular, post-market)
├─ Settlement calendars             (T+1, T+2, T+0 by market)
├─ Public holidays per market
└─ Early close / late open events

EventBridge Scheduler:
├─ Market open trigger              ──► SNS ──► OMS warm-up
├─ Market close trigger             ──► Lambda ──► EOD processing kickoff
└─ T+1 / T+2 settlement reminders  ──► Step Functions ──► settlement workflow
```

---

## 3. Pricing & Valuation Engine

### 3.1 Architecture Overview

```
Market Data (ElastiCache / Kinesis)
Reference Data (DynamoDB)
        │
        ▼
┌────────────────────────────────────────────────────────┐
│              Pricing Engine (EKS)                      │
│                                                        │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │  Equities    │  │    Fixed     │  │ Derivatives │  │
│  │  Pricing     │  │   Income     │  │  Pricing    │  │
│  └──────────────┘  └──────────────┘  └─────────────┘  │
│                                                        │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │    FX        │  │ Commodities  │  │  Structured │  │
│  │  Pricing     │  │  Pricing     │  │  Products   │  │
│  └──────────────┘  └──────────────┘  └─────────────┘  │
└────────────────────────────────────────────────────────┘
        │
        ▼
Pricing Cache (ElastiCache)         ← live mid / bid / ask / theo
        │
        ├─► OMS                     ← order valuation
        ├─► Risk Engine             ← Greeks, exposure
        └─► Trader UI               ← indicative prices
```

### 3.2 Equities Pricing

```
Inputs:
├─ NBBO (best bid/offer)
├─ Exchange-specific quotes
├─ Dark pool indications
└─ Broker quotes (RFQ responses)

Models:
├─ Mid-price                        (simple average bid/ask)
├─ Micro-price                      (size-weighted: bid×ask_size + ask×bid_size / total)
├─ Fair value adjustment            (adjust for short-term mean reversion signal)
└─ Market impact model              ← Almgren-Chriss, predict slippage at target size

AWS: Lambda (stateless, <1ms) for simple models
     EKS pods (stateful, pinned CPU) for continuous micro-price calculation
```

### 3.3 Fixed Income Pricing

```
Inputs:
├─ Bloomberg BVAL / evaluated prices
├─ Broker dealer quotes (via FIX)
├─ CDS spreads (credit proxy)
├─ Benchmark yields (on-the-run Treasuries)
└─ Yield curve (par, zero, forward)

Calculations (EKS — Java/C++ pods):
├─ Yield-to-maturity / yield-to-worst
├─ Modified / Macaulay duration
├─ Convexity
├─ DV01 / PVBP
├─ OAS (Option-Adjusted Spread)
├─ Z-spread
└─ Scenario analysis (parallel shift, twist, butterfly)

Yield Curve Construction:
├─ Bootstrap from on-the-run Treasuries      ── Lambda (triggered on new quote)
├─ Cubic spline interpolation                ── EKS
└─ Multi-curve framework (OIS, LIBOR fallback) ── EKS
Output: ElastiCache (full curve in one key, JSON array)
```

### 3.4 Derivatives Pricing

```
Options Pricing (EKS — C++ Pods, AVX-512 SIMD):
├─ Black-Scholes-Merton             ← vanilla equity options
├─ Black model                      ← interest rate options, swaptions
├─ Heston stochastic vol            ← volatility smile modeling
├─ SABR model                       ← rate/FX options
├─ Binomial / trinomial trees       ← American options, barriers
├─ Finite difference methods        ← PDE-based, complex payoffs
└─ Monte Carlo (GPU-accelerated)    ← exotic payoffs, path-dependent

AWS: EC2 P4d (A100 GPU) for Monte Carlo simulation batches
     EC2 C7g (Graviton3) for Black-Scholes at scale

Greeks (real-time, per contract):
├─ Delta, Gamma, Vega, Theta, Rho
├─ Higher-order: Vanna, Volga, Charm
└─ Scenario P&L (scenario matrix)
Output: ElastiCache (per-contract Greeks, refreshed on vol surface update)
```

### 3.5 Volatility Surface Management

```
Inputs:
├─ Exchange-listed options quotes    (bid/ask IV per strike/expiry)
├─ Broker OTC vol quotes             (FIX / API)
└─ Model-implied vols                (from fitted models)

Surface Construction (EKS):
├─ SVI (Stochastic Volatility Inspired) parametrization
├─ SSVI (Surface SVI)                ← arbitrage-free across expiries
├─ Interpolation: cubic spline (strike axis), linear (time axis)
└─ Arbitrage check: butterfly / calendar spread violations → alert

Storage:
├─ ElastiCache: current surface      (per underlying, updated on each new quote)
├─ S3: EOD surface snapshots         (Parquet, 3-year history)
└─ DynamoDB: surface metadata        (calibration params, fit error stats)

EventBridge Pipe: new option quote → Lambda → surface recalibration → ElastiCache
```

### 3.6 ML-Driven Analytics (SageMaker)

```
Models deployed on SageMaker Real-Time Inference (ml.c6i):
├─ Short-term return prediction      (features: order flow imbalance, spread, vol)
├─ Execution cost prediction         (market impact at target size + urgency)
├─ Volatility regime classifier      (low / medium / high vol regime)
├─ Spread prediction                 (bid-ask spread 5s ahead)
└─ Liquidity score                   (composite: depth, spread, turnover ratio)

Training pipeline:
S3 (tick history) → SageMaker Processing → Feature Store → SageMaker Training
→ Model Registry → Approval gate (backtesting metrics) → Endpoint update

Inference latency: <5ms p99 (SageMaker real-time endpoint)
Batch scoring: SageMaker Batch Transform (nightly, all instruments)
```

---

## 4. Order Management System (OMS)

### 4.1 OMS Architecture

```
                    ┌─────────────────────────────────────┐
                    │         API / Entry Points          │
                    ├──────────┬──────────┬───────────────┤
                    │ FIX 4.2  │ REST API │  WebSocket UI │
                    │ Gateway  │ (traders)│  (blotter)    │
                    └────┬─────┴────┬─────┴───────┬───────┘
                         │          │             │
                         └──────────┼─────────────┘
                                    │
                    ┌───────────────▼─────────────────────┐
                    │       OMS Core (EKS, multi-AZ)      │
                    │                                     │
                    │  ┌───────────┐  ┌────────────────┐  │
                    │  │  Order    │  │   Blotter /    │  │
                    │  │ Lifecycle │  │  Position View │  │
                    │  │ Manager   │  └────────────────┘  │
                    │  └─────┬─────┘                      │
                    │        │  ┌────────────────────────┐ │
                    │        │  │   Allocation Engine    │ │
                    │        │  │  (block → account)     │ │
                    │        │  └────────────────────────┘ │
                    └────────┼────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  Pre-Trade Risk │ ←── synchronous gate
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Route to EMS    │ ──► Phase 2
                    └─────────────────┘
```

### 4.2 Order Lifecycle State Machine

```
NEW ORDER RECEIVED
        │
        ▼
   [PENDING_NEW]      ← assigned internal order ID, persisted to Aurora
        │
        ▼
   [RISK_CHECKING]    ← synchronous pre-trade risk (Lambda, <5ms)
        │
   ┌────┴────┐
PASS       FAIL
   │           └──► [REJECTED] ── FIX reject → client ── SNS → audit
   ▼
[ACCEPTED]           ← confirmed to client (FIX ExecutionReport, OrdStatus=0)
        │
        ▼
[PENDING_ROUTE]      ← SOR selects venue(s), order may split
        │
        ▼
[ROUTED]             ── child orders sent to EMS ──► Phase 2: Execution
        │
        ▼ (on fills streaming back from EMS)
[PARTIALLY_FILLED]
        │
        ▼
[FILLED] / [CANCELLED] / [EXPIRED] / [DONE_FOR_DAY]
        │
        ▼
[CONFIRMED]          ── downstream: post-trade workflow kicks off
```

**State persistence:** DynamoDB (order state, hot path) + Aurora PostgreSQL (full audit, reporting)

### 4.3 Order Types Supported

| Order Type | Description | Key Fields |
|---|---|---|
| Market (MKT) | Execute immediately at best available price | Side, Qty |
| Limit (LMT) | Execute only at limit price or better | Side, Qty, LimitPx |
| Stop (STP) | Trigger market order when stop price touched | Side, Qty, StopPx |
| Stop-Limit | Trigger limit order when stop price touched | Side, Qty, StopPx, LimitPx |
| Market-on-Open | Execute at opening auction price | Side, Qty |
| Market-on-Close | Execute at closing auction price | Side, Qty |
| VWAP | Algo — target VWAP over time window | Side, Qty, StartTime, EndTime |
| TWAP | Algo — time-sliced equal participation | Side, Qty, StartTime, EndTime |
| IS (Impl Shortfall) | Algo — minimize implementation shortfall | Side, Qty, Urgency |
| Iceberg | Show only partial quantity | Side, Qty, DisplayQty |
| Peg | Peg to NBBO mid, bid, or offer | Side, Qty, PegType, Offset |
| RFQ | Request-for-quote to multiple dealers | Side, Qty, Dealers[] |

### 4.4 OMS Data Layer

```
Amazon Aurora PostgreSQL (Multi-AZ, writer + 2 readers):
├─ orders                 (order_id PK, state, timestamps, all FIX fields)
├─ executions             (exec_id PK, order_id FK, fill_qty, fill_px)
├─ allocations            (allocation_id PK, exec_id FK, account, qty, px)
├─ order_history          (full audit trail — every state transition logged)
└─ cash_blotter           (cash entries from executions)

ElastiCache for Redis (live state):
├─ open_orders:{account}  ← set of open order IDs per account (fast blotter)
├─ order:{order_id}       ← current order state hash (all fields)
├─ position:{account}:{symbol} ← current intraday position
└─ pending_fills          ← fills awaiting allocation

DynamoDB (high-throughput lookups):
├─ order_index            (GSI: by account, by symbol, by state, by date)
└─ order_events           (partition: order_id, sort: event_timestamp — full log)
```

### 4.5 FIX Protocol Gateway

```
FIX Clients (algos, buy-side, internal systems)
        │
        ▼
FIX Gateway (EC2 C6in, placement group)
├─ FIX 4.2, 4.4, 5.0 SP2 support
├─ Session management                (logon, heartbeat, sequence reset)
├─ Message parsing / validation      (QuickFIX/J or custom C++ engine)
├─ Rate limiting per session
├─ Duplicate detection               (check BeginSeqNo / MsgSeqNum)
└─ Async dispatch to OMS             via SQS FIFO (exactly-once)

Inbound:  NewOrderSingle (D), OrderCancelRequest (F), OrderCancelReplaceRequest (G)
Outbound: ExecutionReport (8), OrderCancelReject (9), BusinessMessageReject (j)

HA: Two FIX gateway instances per AZ, DNS failover via Route 53 health checks
```

### 4.6 Allocation Engine

```
Block Order (e.g., buy 100,000 AAPL for multiple accounts)
        │
        ▼
Allocation Engine (Lambda — triggered on fill)
        │
        ├─ Pro-rata allocation        ← fills split by account participation
        ├─ Average price calculation  ← weighted avg fill price across partials
        ├─ Lot size rounding          ← round to lot size, residual to largest acct
        ├─ Cash impact calculation    ← fill_qty × avg_px × FX rate
        └─ Settlement instruction creation
        │
        ▼
        ├─► Aurora (allocation records)
        ├─► SNS → Portfolio management system
        └─► EventBridge → Post-trade workflow trigger
```

---

## 5. Pre-Trade Risk Engine

### 5.1 Architecture — Synchronous Gate

```
OMS (order accepted)
        │
        ▼
Pre-Trade Risk Service (Lambda — provisioned concurrency, <5ms P99)
        │
        ├─► [Check 1] Buying Power / Cash Check
        ├─► [Check 2] Position Limits
        ├─► [Check 3] Order Size Limits
        ├─► [Check 4] Concentration Limits
        ├─► [Check 5] Restricted Securities
        ├─► [Check 6] Short-Sell Restrictions
        ├─► [Check 7] Regulatory Checks
        └─► [Check 8] Fat Finger / Anomaly Detection
        │
        ▼
RESULT: PASS (all checks green) / SOFT REJECT (warn, trader override) / HARD REJECT
        │
        ├─ PASS         → OMS routes to EMS
        ├─ SOFT REJECT  → OMS returns warning → trader confirms → proceed / cancel
        └─ HARD REJECT  → OMS rejects order → FIX ExecutionReport (OrdStatus=8)
```

### 5.2 Check 1 — Buying Power & Cash

```
Inputs (ElastiCache, <200µs lookup):
├─ Available cash balance per account
├─ Settled cash
├─ Unsettled cash (T+1, T+2 pending inflows)
├─ Open order reserved cash (pending fills)
├─ Margin available (for margin accounts)
└─ FX converted to base currency

Logic:
  required_cash = order_qty × limit_price × FX_rate × (1 + commission_rate)
  available     = settled_cash + unsettled_cash_haircut - reserved_cash + margin

  if required_cash > available → HARD REJECT "Insufficient buying power"

Margin accounts:
  margin_buying_power = (equity - maintenance_margin) / initial_margin_rate
  Reg T check: max 50% initial margin for equity purchases
```

### 5.3 Check 2 — Position Limits

```
Inputs (ElastiCache + DynamoDB):
├─ Current net position per symbol per account
├─ Position limits (by symbol, account, desk, fund)
├─ Open orders (to include pending fills in position projection)
└─ Limit override table (temporary increases approved by risk)

Logic:
  projected_position = current_position + pending_fills + this_order_qty (net)

  for each limit in [account_limit, desk_limit, fund_limit, firm_limit]:
      if abs(projected_position) > limit.max_abs_position → REJECT
      if projected_position > limit.max_long              → REJECT
      if projected_position < limit.max_short             → REJECT
      if abs(projected_position) > limit.warn_threshold   → SOFT REJECT

DynamoDB schema: position_limits
  PK: entity_id#symbol#limit_type | attrs: max_long, max_short, max_abs, warn_pct
```

### 5.4 Check 3 — Order Size Limits

```
Checks:
├─ Max single order size                (symbol-level, e.g. 1M shares)
├─ Max notional value                   (e.g. $10M per order)
├─ Max order size as % of ADV           (average daily volume — anti-manipulation)
│      ADV lookup: ElastiCache (20-day rolling avg, refreshed daily)
│      limit: typically 5–15% ADV depending on desk/strategy
├─ Max order size vs. current quote size (fat finger: order qty vs. market depth)
│      if order_qty > N × best_quote_size → SOFT REJECT "Order large vs. market"
└─ Min order size                        (avoid sub-lot orders)
```

### 5.5 Check 4 — Concentration Limits

```
Portfolio-level checks (Lambda reads from ElastiCache/DynamoDB):
├─ Single name concentration:
│      projected_market_value(symbol) / total_portfolio_NAV > limit% → REJECT
│
├─ Sector concentration:
│      sum(projected_mv by GICS sector) / total_NAV > sector_limit% → REJECT
│
├─ Country concentration:
│      sum(projected_mv by country) / total_NAV > country_limit% → REJECT
│
├─ Currency concentration:
│      sum(projected_mv by CCY) / total_NAV > ccy_limit% → REJECT
│
└─ Asset class limits:
       sum(projected_mv by asset class) must respect mandate constraints

Mandate rules stored in DynamoDB: account_mandates
  (min/max % by asset class, sector, country, rating, duration band)
```

### 5.6 Check 5 — Restricted Securities

```
Restricted list types:
├─ Firm-wide restricted                 (e.g., M&A advisory clients)
├─ Desk-specific restricted             (e.g., research blackout)
├─ Watch list                           (MNPI — material non-public info)
├─ Short-sell restricted                (SSR — Rule 201 triggered)
├─ Sanctioned entities / securities    (OFAC, EU sanctions)
└─ Regulatory halt                      (exchange-triggered trading halt)

AWS implementation:
  DynamoDB table: restricted_securities
    PK: symbol | SK: list_type | attrs: restriction_type (buy/sell/all), expiry, reason

  ElastiCache SET: restricted:{list_type}
    ← SET of restricted symbols, SISMEMBER check in <1ms

  Lambda: SISMEMBER against all relevant lists for the order's symbol
  Match → HARD REJECT with restriction type code logged to audit trail

  Updates:
    Compliance system → EventBridge → Lambda → update DynamoDB + ElastiCache
    CloudTrail captures all restriction additions/removals (immutable)
```

### 5.7 Check 6 — Short-Sell Restrictions

```
Short-sell validation flow:
        │
        ├─ Is this a sell order?
        │     No → skip
        │     Yes → is it a short sell?
        │
        ├─ Long-sell check:
        │     projected_position after sell ≥ 0 → long sell, OK
        │     projected_position after sell <  0 → short sell, apply rules below
        │
        ├─ SSR (Short Sale Rule / Rule 201):
        │     DynamoDB: ssr_list (SSR-triggered stocks, updated by exchange feed)
        │     If symbol on SSR list → only allow short at bid+1 or above
        │
        ├─ Locate check (for naked short prevention):
        │     DynamoDB: stock_borrow_locates
        │       PK: account#symbol | attrs: locate_qty, locate_source, expiry
        │     projected_short > available_locate → HARD REJECT "Insufficient locate"
        │
        └─ Easy-to-borrow (ETB) vs. hard-to-borrow (HTB):
              ETB: pre-approved locate (auto)
              HTB: require explicit locate from prime broker → Lambda → API call
```

### 5.8 Check 7 — Regulatory Pre-Trade Checks

```
MiFID II best execution:
├─ Venue eligibility check            (is instrument tradable on requested venue?)
├─ Pre-trade transparency             (large-in-scale waiver eligibility?)
└─ SI (Systematic Internaliser) check (eligible for SI execution?)

Wash trade / self-trade prevention:
├─ Check open orders opposite side same symbol same account
├─ If buy vs. sell same symbol → SOFT REJECT "Potential wash trade"
└─ Cross-account wash: check affiliated accounts (entity relationship graph)

PDT (Pattern Day Trader — US retail):
├─ Count day trades in rolling 5 business days    (DynamoDB counter)
├─ If day_trade_count ≥ 4 AND account_equity < $25K → REJECT (PDT rule)

Uptick rule (Rule 10a-1 / 201):
├─ For short sales: last tick must be + or 0 (on SSR-triggered securities)

EMIR trade reporting eligibility:
├─ Classify order (financial counterparty vs. non-financial)
├─ Apply clearing threshold check    (DynamoDB: entity_thresholds)
└─ Flag for mandatory clearing if above threshold
```

### 5.9 Check 8 — Fat Finger & Anomaly Detection

```
Fat Finger Rules:
├─ Price sanity check:
│     limit_price vs. last_trade_price deviation > X% → SOFT REJECT
│     typical thresholds: equities ±5%, bonds ±2%, FX ±1%
│
├─ Order value cap:
│     order_qty × limit_price > firm_max_single_order_notional → HARD REJECT
│
├─ Decimal point check:
│     order_qty is unusually round but notional is enormous → SOFT REJECT
│
├─ Duplicate order detection:
│     Same account + symbol + side + qty + price within 30s → SOFT REJECT "Duplicate?"
│     ElastiCache: SET with 30s TTL per (account, symbol, side, qty, px) hash
│
└─ Velocity check:
      order count per account per minute > threshold → SOFT REJECT "Order rate limit"
      ElastiCache: sliding window counter per account, 1-minute TTL

ML Anomaly (SageMaker):
├─ Online anomaly detection model (Isolation Forest / Autoencoder)
├─ Features: order size, price deviation, time of day, order frequency, symbol
└─ Anomaly score > threshold → SOFT REJECT + alert to compliance desk
```

### 5.10 Risk Parameter Management

```
Risk Desk UI → API Gateway → Lambda → DynamoDB (risk_parameters table)
                                   └─► ElastiCache (hot cache invalidation)
                                   └─► EventBridge → audit log → S3

Parameter types:
├─ Global firm-level parameters         (max order notional, blocked asset classes)
├─ Desk-level parameters                (per-desk position limits, sector limits)
├─ Account-level parameters             (per-account buying power, position limits)
├─ Symbol-level parameters              (custom limits for specific instruments)
└─ Trader-level parameters              (daily loss limits, max position per trader)

Change control:
├─ Dual approval required for risk limit increases (4-eyes principle)
├─ All changes → CloudTrail + DynamoDB Streams → S3 (immutable audit)
└─ Rollback: DynamoDB point-in-time recovery + versioned parameter history
```

---

## 6. Transaction Cost Analysis (TCA) & Best Execution

### 6.1 Pre-Trade TCA

```
Order intent
        │
        ▼
TCA Engine (EKS — Python/Cython, numpy vectorized)

Pre-trade estimates:
├─ Expected market impact:
│     Almgren-Chriss model:
│       market_impact = σ × τ × f(participation_rate)
│       + spread cost + timing risk
│
├─ Implementation shortfall estimate:
│     IS = (decision_price - execution_price) × shares
│     Components: delay cost + market impact + spread cost + timing risk
│
├─ Participation rate recommendation:
│     given urgency level → suggest %ADV to minimize IS
│
├─ Venue selection recommendation:
│     rank venues by: liquidity score, fill rate, latency, cost
│     ElastiCache: venue_stats:{symbol} ← recent fill rates per venue
│
└─ Optimal execution schedule:
      discretize order over time horizon → slice sizes per interval
      output: [(t1, qty1), (t2, qty2), ...] → feeds into TWAP/VWAP algo

Output: TCA report stored in S3, summary in DynamoDB, live view via QuickSight
```

### 6.2 Venue & Broker Selection

```
Smart Order Router (SOR) inputs:
├─ Real-time venue liquidity          (ElastiCache: L2 order book per venue)
├─ Historical fill rates              (DynamoDB: venue_fill_stats, 30-day rolling)
├─ Venue fees / rebates               (DynamoDB: venue_fee_schedule)
├─ Latency to venue                   (DynamoDB: venue_latency_stats, p99 by hour)
├─ Dark pool indications              (ElastiCache: dark_pool_indications)
└─ Broker tiering                     (DynamoDB: broker_tiers, based on research votes)

SOR decision algorithm:
  1. Filter eligible venues (trading hours, instrument eligibility, order type)
  2. Score each venue: fill_probability × (1 - market_impact) - fee
  3. Determine split: primary venue + overflow to secondary
  4. Apply IOC/FOK logic for dark pool sweeps first (minimize information leakage)
  5. Output: [(venue_A, qty_A, order_type_A), (venue_B, qty_B, order_type_B), ...]
```

---

## 7. Analytics & Decision Support

### 7.1 Real-Time Market Analytics

```
Kinesis Data Streams (market data)
        │
        ▼
Kinesis Data Analytics (Apache Flink — streaming SQL + Java)
        │
        ├─ VWAP (rolling intraday)            → ElastiCache
        ├─ TWAP (rolling intraday)            → ElastiCache
        ├─ Volume participation rate          → ElastiCache
        ├─ Bid-ask spread ewma                → ElastiCache
        ├─ Realized volatility (5m rolling)   → ElastiCache
        ├─ Order flow imbalance (OFI)         → ElastiCache + SageMaker feature
        └─ Price momentum (1m, 5m, 15m)       → ElastiCache

Flink operators:
  TumblingWindow(1s)   ← per-second OHLCV bars
  SlidingWindow(5m,1s) ← rolling 5-minute stats, updated every second
  SessionWindow        ← activity-based windows (track auction phases)
```

### 7.2 Trader Dashboard & Blotter

```
WebSocket API (API Gateway)
        │
        ▼
Blotter Service (EKS)
├─ Open orders                        (ElastiCache: open_orders:{account})
├─ Positions (real-time)              (ElastiCache: position:{account}:{symbol})
├─ P&L (intraday)                     (Lambda: mark_to_market vs. avg cost)
├─ Fills stream                       (Kinesis → WebSocket push)
├─ Market data stream                 (Kinesis → WebSocket push, subscribed symbols)
└─ Alerts                             (SNS → WebSocket push)

Latency target: <100ms UI refresh on fill event (end-to-end, fill → screen)
```

### 7.3 Algo Strategy Signals

```
Signal Generation (SageMaker + EKS):
├─ Alpha signals                      (price prediction, factor signals)
├─ Execution signals                  (optimal slice timing, venue timing)
├─ Risk signals                       (vol regime, correlation changes)
└─ Liquidity signals                  (market depth, spread direction)

Signal bus: Amazon MSK (Kafka)
  Topics: signals.alpha, signals.execution, signals.risk, signals.liquidity
  Consumers: OMS (automated order generation), Risk (pre-trade overlay), UI

Feature Store (SageMaker Feature Store):
├─ Online store  → real-time feature retrieval (<5ms) for inference
└─ Offline store → S3 (historical features for model training)
```

---

## 8. Infrastructure & Reliability

### 8.1 Network Topology

```
                    ┌─────────────────────────────────┐
                    │  Exchange Co-location / PoP      │
                    │  (Equinix NY4/NY5, LD4/TY3)     │
                    └──────────────┬──────────────────┘
                                   │ Direct Connect
                                   │ (10G, dual-provider)
                    ┌──────────────▼──────────────────┐
                    │  AWS us-east-1 (Primary)         │
                    │                                  │
                    │  AZ-1a        AZ-1b      AZ-1c   │
                    │  ┌────────┐ ┌────────┐ ┌──────┐  │
                    │  │ App    │ │ App    │ │ App  │  │
                    │  │ Subnet │ │ Subnet │ │Subnet│  │
                    │  ├────────┤ ├────────┤ ├──────┤  │
                    │  │ Data   │ │ Data   │ │ Data │  │
                    │  │ Subnet │ │ Subnet │ │Subnet│  │
                    │  └────────┘ └────────┘ └──────┘  │
                    │                                  │
                    │  EKS (3 AZ)  Aurora (Multi-AZ)   │
                    │  ElastiCache (cluster, 3 AZ)     │
                    │  MSK (3 AZ)  DynamoDB (3 AZ)     │
                    └──────────────────────────────────┘
```

### 8.2 Latency Optimization

| Technique | Target | AWS Implementation |
|---|---|---|
| Kernel bypass (HFT) | <10µs | EC2 + EFA + DPDK |
| CPU pinning | Eliminate jitter | EKS `cpuManager: static` policy |
| NUMA awareness | L3 cache efficiency | Nitro, instance store NVMe |
| Huge pages | TLB miss reduction | EC2 user data config |
| Interrupt affinity | Dedicated RX/TX cores | EC2 + ENA tuning |
| Proximity | Network RTT | Placement group (cluster) |
| Connection pooling | Avoid TCP handshake | ElastiCache persistent conn |
| Pipelining | Batch Redis commands | Redis pipeline / MULTI-EXEC |
| Async I/O | Non-blocking | Java NIO / Python asyncio / Netty |
| Provisioned concurrency | Eliminate Lambda cold start | Lambda PC (risk checks) |

### 8.3 High Availability

```
Component              HA Mechanism                        RTO
─────────────────────────────────────────────────────────────────
EKS (OMS, EMS)        Pod disruption budgets, 3 AZ        <30s
Aurora PostgreSQL     Multi-AZ, auto-failover              <30s
ElastiCache Redis     Cluster mode, replica per shard      <30s
DynamoDB              Managed, 3 AZ by default             0 (always on)
MSK (Kafka)           3-broker cluster, 3 AZ               <1min
FIX Gateway           Dual EC2, Route53 health check       <60s
Lambda (risk checks)  Multi-AZ by default                  0 (always on)
Direct Connect        Dual connection, dual provider        <5min (BGP failover)
```

### 8.4 Observability — Pre-Trade Specific

```
Key Metrics (CloudWatch Custom Metrics):
├─ pre_trade_risk_latency_p99         ← target <5ms
├─ pre_trade_reject_rate              ← alert if >5% (possible config issue)
├─ market_data_staleness_seconds      ← alert if >1s for liquid instruments
├─ oms_order_ingestion_rate           ← orders/second
├─ oms_order_queue_depth              ← SQS queue depth, alert if >100
├─ pricing_engine_update_lag          ← vol surface recalibration latency
└─ risk_check_pass_rate_by_check_type ← per check-type pass/fail breakdown

Distributed Tracing (X-Ray):
  Client → API GW → OMS → Risk Check → SOR → EMS
  Trace: order_id as correlation ID across all segments

Alarms:
├─ market_data_staleness > 2s → P1 → PagerDuty
├─ risk_latency_p99 > 10ms   → P1 → PagerDuty
├─ reject_rate > 10%          → P2 → Slack #risk-alerts
└─ pricing_lag > 5s           → P2 → Slack #pricing-alerts
```

---

## Pre-Trade: End-to-End Latency Summary

```
Step                                        Latency (target)
──────────────────────────────────────────────────────────────
Exchange quote → Feed handler decode        10–50 µs
Feed handler → Kinesis publish              100–500 µs
Kinesis → ElastiCache (pricing update)      500µs–1ms
Trader submit → API Gateway                 1–2ms
API Gateway → OMS (validation)              1–2ms
OMS → Pre-trade risk (Lambda PC)            2–5ms
Risk → Route decision (SOR)                 1–2ms
Total: order-to-EMS hand-off               ~6–12ms

HFT path (kernel bypass, placement group):
Exchange quote → position update            <500µs
Order decision → wire to exchange           <100µs
```

---

## Pre-Trade: AWS Services Quick Reference

| Domain | Service | Role |
|---|---|---|
| Feed ingestion | EC2 Nitro (C7gn) + EFA | Feed handler, ultra-low latency |
| Market data bus | Kinesis Data Streams | Fan-out, <1ms, durable |
| Internal bus | Amazon MSK (Kafka) | Durable, replay, multi-consumer |
| L1 price cache | ElastiCache Redis (cluster) | Sub-ms price/quote lookup |
| Durable cache | MemoryDB for Redis | Durable intraday data |
| Reference data | DynamoDB + DAX | Instruments, limits, calendars |
| Pricing ML | SageMaker Inference | Spread, impact, vol models |
| Options pricing | EKS (C++ pods) | Black-Scholes, Heston, Monte Carlo |
| GPU Monte Carlo | EC2 P4d (A100) | Path-dependent exotics |
| OMS state | Aurora PostgreSQL | Order lifecycle, audit |
| OMS hot state | ElastiCache Redis | Open orders, positions |
| Risk checks | Lambda (provisioned concurrency) | <5ms synchronous gate |
| Streaming analytics | Kinesis Data Analytics (Flink) | VWAP, OFI, vol regime |
| Feature store | SageMaker Feature Store | ML signal features |
| Workflows | Step Functions | Allocation, downstream trigger |
| API layer | API Gateway (REST + WebSocket) | Trader UI, FIX clients |
| Audit | CloudTrail + S3 (Object Lock) | Immutable risk check log |
| Alerting | CloudWatch + SNS + PagerDuty | Latency, staleness, reject alerts |
