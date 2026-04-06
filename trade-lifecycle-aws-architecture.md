# AWS Trade Lifecycle — End-to-End Architecture

## Architecture Principles

| Principle | Approach |
|---|---|
| **Latency** | Sub-millisecond execution path; microsecond market data |
| **Resilience** | Multi-AZ active-active; cross-region DR (<4h RTO, <15min RPO) |
| **Compliance** | Immutable audit trail; regulatory reporting pipeline |
| **Scale** | Horizontal at every tier; auto-scaling on trade volume |
| **Security** | Zero-trust; encryption at rest + in transit; least privilege IAM |

---

## Trade Lifecycle Overview

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  PRE-TRADE  │───►│  EXECUTION  │───►│ POST-TRADE  │───►│   RISK &    │
│             │    │             │    │             │    │ COMPLIANCE  │
│ Market Data │    │ Order Route │    │ Confirm     │    │ Real-time   │
│ OMS         │    │ EMS / SOR   │    │ Clear       │    │ Risk        │
│ Risk Checks │    │ FIX Gateway │    │ Settle      │    │ Regulatory  │
│ Pricing     │    │ Exchange    │    │ Positions   │    │ Reporting   │
│             │    │ Connectivity│    │ P&L         │    │ Audit Trail │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
```

---

## Phase 1 — Pre-Trade

### 1.1 Market Data Ingestion

```
Exchange Feeds (FIX / FAST / ITCH)
    └─► AWS Direct Connect (10G dedicated)
            └─► EC2 Nitro (placement group, SR-IOV)   ← ultra-low latency boundary
                    ├─► Amazon Kinesis Data Streams    (internal fan-out, <1ms)
                    └─► Amazon MSK (Kafka)             (durable, replay-capable)
```

**Storage tiers:**

| Tier | Service | Purpose |
|---|---|---|
| Hot | ElastiCache for Redis (cluster mode) | Real-time ticker cache, microsecond reads |
| Warm | DynamoDB (on-demand) | Reference data — instruments, counterparties, calendars |
| Cold | S3 + S3 Intelligent-Tiering | Historical OHLCV, tick data, corporate actions |

### 1.2 Pricing & Analytics

```
Kinesis
    ├─► Lambda             ← simple models, <5ms latency
    ├─► EKS (Fargate Spot) ← complex derivatives pricing (Black-Scholes, Monte Carlo)
    └─► SageMaker Inference← ML-driven spread prediction, volatility surface
```

### 1.3 Order Management System (OMS)

```
Trader UI / Algo Engine / External API
    └─► API Gateway (REST + WebSocket)
            └─► EKS (OMS pods, multi-AZ)
                    ├─► Amazon Aurora PostgreSQL   (writer + 2 readers, Multi-AZ)
                    ├─► Amazon MQ (ActiveMQ)       ← FIX order queuing
                    └─► ElastiCache (Redis)        ← open order cache
```

### 1.4 Pre-Trade Risk Checks

```
OMS Order ──► EventBridge Pipe ──► Lambda (synchronous, <5ms budget)
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
             Buying Power         Position Limits    Restricted Lists
             (ElastiCache)        (DynamoDB)         (DynamoDB)
                    │
                    ▼
             ┌──────────────┐
             │ PASS → EMS   │
             │ FAIL → Reject│── SNS ──► Trader Notification
             └──────────────┘
```

---

## Phase 2 — Trade Execution

### 2.1 Execution Management System (EMS)

```
OMS (approved orders)
    └─► Amazon SQS FIFO (exactly-once, ordered per order ID)
            └─► EKS (EMS pods, multi-AZ)
                    ├─► Smart Order Router (SOR)             ← venue selection
                    ├─► Direct Market Access (DMA)
                    └─► Algorithmic Execution Engine          ← VWAP, TWAP, IS
```

### 2.2 Exchange Connectivity

```
EMS
  ├─► Direct Connect ──► Exchange A    (Primary)
  ├─► Direct Connect ──► Exchange B    (Alternative)
  ├─► Direct Connect ──► ECN / Dark Pool
  └─► VPN (backup)   ──► Exchange Failover
```

> **HFT Path:** EC2 Nitro (C6in / C7gn) + EFA + DPDK kernel bypass → wire-to-wire <10µs

### 2.3 Execution Event Stream

```
Exchange Fill Notification
    └─► EC2 FIX Gateway
            └─► Kinesis Data Streams (execution events)
                    ├─► Lambda        ← immediate position delta update
                    ├─► MSK (Kafka)   ← fan-out to risk, compliance, reporting
                    └─► DynamoDB      ← execution record (idempotent write)
```

---

## Phase 3 — Post-Trade

### 3.1 Trade Confirmation & Matching

```
Kinesis (executions)
    └─► Step Functions (Express Workflow)
            ├─► [1] Generate confirmation   (FIX / SWIFT MT5xx)
            ├─► [2] Send to counterparty    (Amazon MQ / SES / SNS)
            ├─► [3] Await ACK               (SQS + visibility timeout)
            └─► [4] Matched ──► Clearing
                    Disputed ──► Exception Queue ──► EventBridge ──► Ops Alert
```

### 3.2 Clearing & Settlement

```
Confirmed Trade
    └─► Step Functions (Standard Workflow — T+1 / T+2 lifecycle)
            ├─► [1] CCP Submission          (DTCC / LCH / Eurex via Direct Connect)
            ├─► [2] Margin Calculation      (Lambda + ElastiCache)
            ├─► [3] Netting                 (Lambda batch)
            ├─► [4] Settlement Instruction  (DvP / FoP via SWIFT FIN)
            ├─► [5] Custody Notification    (SNS)
            └─► [6] Settlement Confirm      (DynamoDB write)
```

**Persistence:**
- **Aurora PostgreSQL** — clearing & settlement schema (ACID, source of truth)
- **DynamoDB** — settlement status with GSI on `date` / `status` for fast queries

### 3.3 Position Management

```
Real-Time (intraday):
    Fill Event → Lambda (idempotent delta calculator)
                     ├─► ElastiCache  ← live positions per book/trader
                     └─► Aurora       ← SOD / EOD positions

EOD Reconciliation:
    EventBridge Scheduler → AWS Batch (Spot) → reconcile vs custodian
                                └─► S3  ← reconciliation reports
```

### 3.4 P&L Calculation

| Frequency | Pipeline | Output |
|---|---|---|
| Intraday | Kinesis → Lambda → ElastiCache | Running P&L per book/trader |
| End-of-Day | EventBridge → AWS Batch → Redshift | Full P&L attribution report |
| Reporting | Redshift → Amazon QuickSight | Trader + Risk desk dashboards |

---

## Phase 4 — Risk & Compliance

### 4.1 Real-Time Risk Monitoring

```
Kinesis (executions + market data)
    └─► Kinesis Data Analytics (Apache Flink)
            ├─► VaR / Greeks streaming      ──► ElastiCache ──► Risk Dashboard
            ├─► Limit breach detection      ──► SNS ──► Risk Manager Alert
            └─► Concentration risk          ──► EventBridge ──► Auto-pause orders
```

### 4.2 Regulatory Reporting

| Regulation | Pipeline |
|---|---|
| **MiFID II / MiFIR** | EventBridge Scheduler → Lambda → S3 → SFTP to NCA |
| **EMIR** | Step Functions → Lambda → trade repository API submission |
| **Dodd-Frank / SEF** | SQS → Lambda → swap data repository API |
| **CAT (FINRA)** | Kinesis Firehose → S3 → Lambda (CAT format) → S3 upload |
| **CFTC Part 45** | AWS Batch → S3 → API Gateway → DTCC reporting |

### 4.3 Immutable Audit Trail

```
All services     → CloudTrail (data events enabled, org-level)
All trade events → Kinesis Firehose → S3 (versioned, MFA-delete protected)
DynamoDB Streams → Lambda → S3 (Object Lock, Governance mode, 7-year retention)
API access logs  → CloudWatch Logs → S3 archival
```

### 4.4 Fraud & Anomaly Detection

```
MSK → SageMaker Streaming Inference  ← model trained on wash trading / spoofing
    └─► EventBridge
            └─► Step Functions → Compliance alert + order hold workflow
```

---

## Phase 5 — Cross-Cutting Concerns

### 5.1 Networking & Connectivity

```
AWS Region (us-east-1 — Primary)
└─► 3× Availability Zones (active-active)
        ├─► VPC (RFC1918, VPC Flow Logs enabled)
        │     ├─► Public Subnet    — ALB, NAT GW, Direct Connect GW
        │     ├─► Private App      — EKS nodes, Lambda VPC, EC2 FIX GW
        │     └─► Private Data     — Aurora, ElastiCache, MSK
        ├─► AWS PrivateLink        — all AWS service endpoints (no IGW traversal)
        ├─► Transit Gateway        — hub for on-prem + multi-VPC mesh
        └─► Direct Connect (×2 redundant, diverse providers)

DR Region (us-west-2 — Standby):
    ├─► Aurora Global Database      (<1s replication lag)
    ├─► DynamoDB Global Tables      (active-active replication)
    ├─► S3 Cross-Region Replication
    └─► Route 53 ARC                (Application Recovery Controller — automated failover)
```

### 5.2 Security

| Control Layer | AWS Service |
|---|---|
| Encryption at rest | KMS (CMK per domain), S3 SSE-KMS, Aurora TDE |
| Encryption in transit | TLS 1.3 enforced, ACM certificates, mTLS (service mesh) |
| Secrets management | AWS Secrets Manager (auto-rotation enabled) |
| Identity & access | IAM (least privilege), AWS SSO, STS AssumeRole |
| Network protection | Security Groups + NACLs, WAF on ALB, Shield Advanced |
| Threat detection | GuardDuty, Security Hub, Macie (PII in regulatory reports) |
| Vulnerability mgmt | Amazon Inspector (ECR + EC2), Systems Manager Patch Manager |
| API security | API Gateway — throttling, usage plans, resource policies |

### 5.3 Observability

```
Metrics   : CloudWatch → Amazon Managed Grafana
Traces    : AWS X-Ray  (distributed tracing — Lambda, EKS, API GW, Step Functions)
Logs      : CloudWatch Logs → Amazon OpenSearch (Kibana — trade event search)
Alarms    : CloudWatch Alarms → SNS → PagerDuty (P1) / Slack (P2)

Key Dashboards:
    ├─ Order fill rate + latency percentiles (p50 / p99 / p999)
    ├─ Pre-trade risk check pass/fail rate
    ├─ Settlement fail rate by counterparty
    └─ Real-time limit utilization heatmap
```

### 5.4 CI/CD & Infrastructure as Code

```
Source    : GitHub / CodeCommit
Pipeline  : AWS CodePipeline
Build     : CodeBuild  ← unit tests, integration tests, SAST (SonarQube)
Artifacts : ECR (image scanning) + S3 (Lambda ZIPs)

Deploy:
    ├─► EKS       : Helm + ArgoCD (GitOps, Argo Rollouts — progressive delivery)
    ├─► Lambda    : AWS SAM / CDK (blue-green via aliases + weighted routing)
    └─► Infra     : AWS CDK (TypeScript) — all infrastructure as code

Promotion: dev → staging (10% prod mirror) → prod
Gate     : manual approval + canary 5% → 25% → 100% with automated rollback
```

---

## Full Service Map

```
┌──────────────────────────────────────────────────────────────────────────┐
│  PRE-TRADE           EXECUTION          POST-TRADE        RISK/COMPLIANCE │
│                                                                           │
│  Direct Connect      Direct Connect     Step Functions    Kinesis Flink   │
│  Kinesis Streams     SQS FIFO           Aurora PG         SageMaker       │
│  MSK (Kafka)         EKS (EMS / SOR)    DynamoDB          EventBridge     │
│  ElastiCache         EC2 Nitro (FIX)    AWS Batch         S3 (WORM)      │
│  Lambda (risk)       Kinesis Streams    Amazon Redshift   CloudTrail      │
│  API GW + OMS (EKS)  MSK (fan-out)      QuickSight        SNS Alerts      │
│  Aurora PostgreSQL   EFA + DPDK         Kinesis Firehose  GuardDuty       │
└──────────────────────────────────────────────────────────────────────────┘
          ↕                   ↕                  ↕                ↕
┌──────────────────────────────────────────────────────────────────────────┐
│  PLATFORM                                                                 │
│  VPC · Transit GW · PrivateLink · Direct Connect · Route 53 ARC          │
│  KMS · IAM · Secrets Manager · WAF · Shield Advanced · GuardDuty         │
│                                                                           │
│  OBSERVABILITY                                                            │
│  CloudWatch · X-Ray · OpenSearch · Managed Grafana · PagerDuty           │
│                                                                           │
│  RESILIENCE                                                               │
│  Multi-AZ Active-Active · Aurora Global DB · DynamoDB Global Tables      │
│  S3 Cross-Region Replication · Route 53 ARC Automated Failover           │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Latency Budget — Critical Path

| Segment | Latency |
|---|---|
| Trader order → OMS validation | 2–5 ms |
| OMS → Pre-trade risk check | 1–3 ms |
| Risk approval → EMS routing decision | 1–2 ms |
| EMS → Exchange wire (standard DMA) | 50–200 µs |
| EMS → Exchange wire (HFT, Nitro + EFA) | <10 µs |
| Exchange fill → position update | 1–2 ms |
| **Total order-to-ack (standard)** | **~5–15 ms** |
| **Total order-to-ack (HFT co-location)** | **<500 µs** |

---

## Key Architectural Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Order sequencing | SQS FIFO + EKS | Exactly-once delivery, ordered per order ID |
| Low-latency execution | EC2 Nitro + EFA + DPDK | Kernel bypass for HFT path |
| Position state store | ElastiCache (intraday) + Aurora (SOD/EOD) | Speed vs durability trade-off |
| Workflow orchestration | AWS Step Functions | Durable, auditable, visual state tracing |
| Streaming risk engine | Kinesis + Apache Flink | Sub-second P&L and limit monitoring |
| Settlement workflow | Step Functions Standard | Multi-day T+2 with durable wait states |
| DR strategy | Active-passive + Route 53 ARC | Cost-optimised with automated failover |
| Audit immutability | S3 Object Lock (Governance mode) | 7-year regulatory retention requirement |
| Infrastructure as code | AWS CDK (TypeScript) | Type-safe, testable, reusable constructs |

---

## Resilience & DR Summary

| Tier | RTO | RPO | Mechanism |
|---|---|---|---|
| AZ failure | <30 sec | 0 | Multi-AZ auto-failover (Aurora, ElastiCache, EKS) |
| Regional failure | <4 hours | <15 min | Route 53 ARC + Aurora Global + DynamoDB Global |
| Data corruption | <1 hour | <5 min | S3 versioning + DynamoDB point-in-time recovery |
| Service degradation | <1 min | 0 | Circuit breaker (Resilience4j in EKS) + auto-scaling |
