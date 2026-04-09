# Skills Demonstrated

This repository is a hands-on, end-to-end low-latency trading pipeline built
on macOS, faithfully mirroring production infrastructure patterns used in
HFT / institutional trading on AWS EC2 Nitro + EFA.

---

## 1. Network & Packet Processing

| Skill | Where |
|---|---|
| **DPDK concepts** — mbuf pool, SPSC ring, poll-mode driver, burst I/O, RSS | `ems/dpdk_sim.py` |
| **RDMA / zero-copy transfer** — RDMA Write, completion queue, QP management | `ems/rdma_transport.py` |
| **FIX 4.2 protocol** — NewOrderSingle, CancelRequest, checksum, tag parsing | `client/send_fix_orders.py`, `ems/dpdk_sim.py` |
| **Raw packet construction** — Ethernet + IPv4 + UDP framing from scratch | `ems/dpdk_sim.py` `PacketGenerator` |
| **BPF / rte_flow filtering** — per-packet whitelist before ring enqueue | `ems/dpdk_sim.py` `BPFFilter` |
| **Lock-free SPSC ring** — enqueue/dequeue, capacity, drop counting | `ems/dpdk_sim.py` `RingBuffer` |
| **Receive-Side Scaling (RSS)** — Toeplitz hash, flow affinity across queues | `ems/dpdk_sim.py` `RSSMapper` |
| **libpcap + C** — raw frame capture, BPF socket filter, pcap_loop | `ems/dpdk_pcap.c` |
| **IP Multicast** — `IP_ADD_MEMBERSHIP`, `SO_REUSEPORT` multi-receiver, `IP_MULTICAST_LOOP` / TTL tuning for macOS loopback | `ems/multicast_gateway.py` |
| **Multicast order fan-out** — `MulticastGateway` publish path, `MulticastEnvelope` wire format (struct.pack, no protobuf), PASS/WARN verdict filtering | `ems/multicast_gateway.py` |
| **Sequence gap detection** — per-receiver monotonic seq_no tracking, gap counter; mirrors OPRA / CME Globex MDP3 feed-handler retransmission logic | `ems/multicast_gateway.py` `MulticastReceiver` |

---

## 2. Pre-Trade Risk

| Skill | Where |
|---|---|
| **Risk waterfall** — ordered check pipeline (instrument → account → CP → limits → short-sell → concentration → DV01) | `gbo_ref_data.py` `PreTradeRiskEngine` |
| **GBO / golden source** — instrument master, account master, counterparty master, limit table, FX rates, holiday calendars | `gbo_ref_data.py` `GBORefDataStore` |
| **FIX → risk engine wiring** — SenderCompID mapping, FIXMsg-to-GBOOrder translation, inline verdict tagging | `ems/dpdk_sim.py` `PreTradeRiskGateway` |
| **Limit hierarchy** — per-ISIN, per-asset-class, and global limit fallback in O(1) indexed lookup | `gbo_ref_data.py` `_build_limit_index` |
| **Notional USD conversion** — multi-currency FX normalisation before limit checks | `gbo_ref_data.py` `fx_to_usd` |
| **DV01 check** — interest-rate sensitivity limit for fixed-income orders | `gbo_ref_data.py` `PreTradeRiskEngine.check` |
| **Concentration / ADV** — order as % of average daily volume, warn/reject tiers | `gbo_ref_data.py` `PreTradeRiskEngine.check` |

---

## 3. Derivatives Pricing & Quantitative Finance

| Skill | Where |
|---|---|
| **Monte Carlo simulation** — GBM path generation, variance reduction (antithetic, control variates) | `pre_trade_risk/montecarlo_pricing.py` |
| **Options pricing** — European, Asian, Barrier, American (LSM) via MC; Black-Scholes closed-form benchmark | `pre_trade_risk/montecarlo_pricing.py` |
| **Greeks** — Delta, Gamma, Vega, Theta, Rho via finite-difference bump-and-reprice | `pre_trade_risk/montecarlo_pricing.py` |
| **Heston stochastic volatility** — correlated vol process, CIR mean-reversion | `pre_trade_risk/montecarlo_pricing.py` |
| **Portfolio VaR** — multi-asset correlated MC, Cholesky decomposition | `pre_trade_risk/montecarlo_pricing.py` |
| **Almgren-Chriss market impact** — optimal liquidation trajectory, permanent vs temporary impact | `pre_trade_risk/montecarlo_pricing.py` |

---

## 4. Post-Trade

| Skill | Where |
|---|---|
| **Position management** — net qty, average price, realised / unrealised P&L | `gbo_ref_data.py` `PostTradeRiskEngine` |
| **Wash-trade detection** — buy/sell same instrument within detection window | `gbo_ref_data.py` `PostTradeRiskEngine` |
| **Settlement date calc** — T+N with holiday calendar roll (USD and GBP) | `gbo_ref_data.py` `HolidayCalendar` |
| **Immutable audit trail** — atomic O_APPEND NDJSON writes with decision, checks, latency | `post_trade/surveillance/fix_audit.log` |

---

## 5. Market Data

| Skill | Where |
|---|---|
| **Protobuf schema design** — NBBO, Trade, L2 Book tick types | `client/market_data.proto` |
| **UDP market data feed** — serialise / send protobuf messages at configurable rate | `client/send_market_data.py` |

---

## 6. Infrastructure & AWS

| Skill | Where |
|---|---|
| **AWS trade lifecycle architecture** — Direct Connect, EC2 Nitro, EFA, placement groups, SR-IOV | `trade-lifecycle-aws-architecture.md` |
| **Latency budget design** — capture → mbuf → ring → FIX parse → risk → audit < 1 ms | `README.md` |
| **Multi-AZ resilience** — active-active OMS, cross-region DR, RTO/RPO targets | `trade-lifecycle-aws-architecture.md` |

---

## 7. Software Engineering

| Skill | Where |
|---|---|
| **Python dataclasses** — `Order`, `Fill`, `Position`, `Instrument`, `RiskCheck`, `PreTradeResult` | `gbo_ref_data.py` |
| **Enum / type safety** — `OrderSide`, `RiskResult`, `AssetClass`, `CreditTier` | `gbo_ref_data.py` |
| **C systems programming** — libpcap socket, mbuf pool allocation, SPSC ring in C | `ems/dpdk_pcap.c` |
| **Thread-per-lcore model** — daemon threads, busy-poll loop, no sleep | `ems/dpdk_sim.py` `LcorePipeline` |
| **Interactive TUI** — Textual-based trader UI, manual and algo order modes | `client/trader_ui.py` |
| **Perf instrumentation** — `time.perf_counter` latency timers, p50/p99 histograms | `ems/dpdk_sim.py` `StatsEngine` |
| **Daemon thread receiver model** — `MulticastReceiver` per-node thread, `settimeout(0.1)` poll loop for clean shutdown | `ems/multicast_gateway.py` |
