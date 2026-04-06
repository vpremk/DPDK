# Pre-Trade — Mermaid Swimlane Diagrams

---

## 1. Pre-Trade End-to-End Swimlane

```mermaid
flowchart TD
    subgraph TRADER["🧑‍💼 Trader / Algo Engine"]
        T1([New Order Intent]) --> T2[Select Instrument\nSide · Qty · Price]
        T2 --> T3[Submit via\nFIX / REST / WebSocket]
        T8([Receive ExecutionReport])
    end

    subgraph OMS["📋 Order Management System"]
        O1[Receive & Parse Order] --> O2[Assign Internal Order ID]
        O2 --> O3[Persist → Aurora\nStatus: PENDING_NEW]
        O3 --> O4{Route to\nPre-Trade Risk}
        O7[Update Order State\nStatus: ACCEPTED] --> O8[Route to EMS]
        O9([REJECTED:\nSend FIX Reject])
    end

    subgraph MKTDATA["📡 Market Data"]
        M1[(ElastiCache Redis\nNBBO · L2 Book\nLast Sale · VWAP)] 
        M2[(DynamoDB\nInstrument Master\nRestricted List\nCalendars)]
    end

    subgraph PRICING["💹 Pricing Engine"]
        P1[Fetch NBBO\nfrom ElastiCache] --> P2{Option or\nExotic?}
        P2 -->|Equity / FI| P3[Black-Scholes\nDuration · DV01]
        P2 -->|Derivative| P4[Monte Carlo\nSimulation Engine]
        P4 --> P5[Simulate GBM Paths\nn_paths=100k]
        P5 --> P6{Model?}
        P6 -->|Vanilla| P7[European Payoff\nmax S_T - K, 0]
        P6 -->|Path-dep| P8[Asian / Barrier\nPayoff Calc]
        P6 -->|American| P9[Longstaff-Schwartz\nLSM Regression]
        P6 -->|Stoch Vol| P10[Heston\nCorrelated dW]
        P7 & P8 & P9 & P10 --> P11[Discount &\nAverage Payoffs]
        P11 --> P12[Return:\nPrice · Greeks\nVaR Contribution]
        P3 --> P12
    end

    subgraph RISK["🛡️ Pre-Trade Risk Engine"]
        R1[Buying Power Check\nElastiCache] --> R2{Pass?}
        R2 -->|No| RX([Hard Reject])
        R2 -->|Yes| R3[Position Limits\nDynamoDB]
        R3 --> R4{Pass?}
        R4 -->|No| RX
        R4 -->|Yes| R5[Concentration\nLimits]
        R5 --> R6[Restricted\nSecurities Check]
        R6 --> R7[Short-Sell\nLocate Check]
        R7 --> R8[Fat Finger\nAnomaly Detection]
        R8 --> R9[Regulatory\nChecks MiFID II]
        R9 --> R10{All\nChecks Pass?}
        R10 -->|Yes| R11([APPROVED])
        R10 -->|Soft Fail| R12([WARN → Trader\nOverride Required])
        R10 -->|Hard Fail| RX
    end

    subgraph AUDIT["📁 Audit & Compliance"]
        A1[(DynamoDB Streams\norder_events)] 
        A2[(S3 Object Lock\nImmutable Log\n7-year retention)]
        A3[(CloudTrail\nAll API calls)]
    end

    T3 --> O1
    O4 --> M1
    O4 --> M2
    M1 & M2 --> P1
    P12 --> R1
    R11 --> O7
    RX --> O9
    O9 --> T8
    O8 --> T8
    O3 --> A1
    A1 --> A2
    O1 --> A3

    style TRADER  fill:#1a3a5c,stroke:#4a9eff,color:#e0f0ff
    style OMS     fill:#1a3a2a,stroke:#4aff9e,color:#e0ffe8
    style MKTDATA fill:#3a1a3a,stroke:#ff4aff,color:#ffe0ff
    style PRICING fill:#3a2a1a,stroke:#ffaa4a,color:#fff0e0
    style RISK    fill:#3a1a1a,stroke:#ff4a4a,color:#ffe0e0
    style AUDIT   fill:#1a2a3a,stroke:#4aaaff,color:#e0eeff
```

---

## 2. Monte Carlo Engine — Internal Swimlane

```mermaid
flowchart LR
    subgraph INPUT["Input Layer"]
        direction TB
        I1[Spot Price S₀\nElastiCache]
        I2[Volatility σ\nVol Surface]
        I3[Risk-Free Rate r\nYield Curve]
        I4[Option Spec\nK · T · Type]
        I5[Heston Params\nκ · θ · ξ · ρ]
    end

    subgraph SIM["Simulation Layer"]
        direction TB
        S1[Generate\nRandom Z ~ N·0·1\nnumpy default_rng]
        S2{Antithetic\nVariates?}
        S2 -->|Yes| S3[Mirror Paths\n-Z pairs\nHalve variance]
        S2 -->|No| S4[Standard Paths]
        S3 & S4 --> S5[Log-Euler Scheme\nS_t+1 = S_t · exp\ndrift + diffusion·Z]
        S5 --> S6{Stochastic\nVol?}
        S6 -->|Yes| S7[Heston dV\nCorrelated W\nFull Truncation]
        S6 -->|No| S8[Constant σ GBM]
        S7 & S8 --> S9[Price Paths\nshape: n_paths × n_steps]
    end

    subgraph PAYOFF["Payoff Layer"]
        direction TB
        PY1{Option\nType?}
        PY1 -->|European| PY2[Terminal Price S_T\nmax S_T-K · 0]
        PY1 -->|Asian| PY3[Average Price Ā\nArithmetic or Geometric]
        PY1 -->|Barrier| PY4[Check min/max\nvs Barrier B\nKnock-in or Out]
        PY1 -->|American| PY5[LSM Backward\nInduction\nOLS Regression]
        PY2 & PY3 & PY4 & PY5 --> PY6[Apply Payoff\nFunction]
    end

    subgraph STATS["Statistics Layer"]
        direction TB
        ST1[Discount Payoffs\ne^-rT · payoff_i]
        ST2[Price = mean\npayoffs]
        ST3[Std Error =\nstd / √n_paths]
        ST4[95% CI\nprice ± 1.96·SE]
        ST1 --> ST2 --> ST3 --> ST4
    end

    subgraph GREEKS["Greeks Layer"]
        direction TB
        G1[Bump S up +1%]
        G2[Bump S down -1%]
        G3[Delta = V↑-V↓ / 2h]
        G4[Gamma = V↑-2V+V↓ / h²]
        G5[Bump σ ±1%\nVega]
        G6[Bump T -1day\nTheta]
        G7[Bump r ±1%\nRho]
        G1 & G2 --> G3 --> G4
        G5 --> G3
        G6 --> G3
        G7 --> G3
    end

    subgraph OUTPUT["Output Layer"]
        direction TB
        OP1[Option Price]
        OP2[Greeks\nΔ Γ ν Θ ρ]
        OP3[Std Error\n95% CI]
        OP4[Execution Time ms]
        OP5[VaR Contribution\nto Portfolio]
    end

    I1 & I2 & I3 & I4 --> S1
    I5 --> S6
    S9 --> PY1
    PY6 --> ST1
    ST4 --> OP1 & OP3 & OP4
    ST2 --> G1 & G2 & G5 & G6 & G7
    G3 & G4 --> OP2
    OP1 --> OP5

    style INPUT   fill:#0d2137,stroke:#1e6eb5,color:#a8d8ff
    style SIM     fill:#0d3721,stroke:#1e8c50,color:#a8ffd0
    style PAYOFF  fill:#372106,stroke:#b56b1e,color:#ffd8a8
    style STATS   fill:#210d37,stroke:#6b1eb5,color:#d8a8ff
    style GREEKS  fill:#370d21,stroke:#b51e6b,color:#ffa8d8
    style OUTPUT  fill:#1a1a0d,stroke:#b5b51e,color:#ffffa8
```

---

## 3. Pre-Trade Risk Check Swimlane (Decision Flow)

```mermaid
flowchart TD
    subgraph SUBMIT["Order Submission"]
        S1([Trader Submits\nNew Order]) --> S2[FIX / REST Gateway]
        S2 --> S3[OMS validates\nfields & sessions]
    end

    subgraph CHECK1["Check 1 · Buying Power"]
        C1A[Fetch cash balance\nElastiCache] --> C1B{Notional ≤\nAvailable Cash?}
        C1B -->|✗ No| C1C([Hard Reject\nInsufficient funds])
        C1B -->|✓ Yes| C1D([Pass →])
    end

    subgraph CHECK2["Check 2 · Position Limits"]
        C2A[Project position\ncurrent + pending + order] --> C2B{Within\nMax Long/Short?}
        C2B -->|✗ No| C2C([Hard Reject\nLimit breached])
        C2B -->|✓ Yes| C2D([Pass →])
    end

    subgraph CHECK3["Check 3 · Order Size"]
        C3A[Check vs ADV\nMax notional\nLot size] --> C3B{Size OK?}
        C3B -->|✗ > 15% ADV| C3C([Soft Reject\nFat finger warn])
        C3B -->|✓ Yes| C3D([Pass →])
    end

    subgraph CHECK4["Check 4 · Restricted List"]
        C4A[SISMEMBER\nElastiCache SET\nOFAC · Halt · Watch] --> C4B{On any\nRestricted List?}
        C4B -->|✗ Yes| C4C([Hard Reject\nRestricted security])
        C4B -->|✓ No| C4D([Pass →])
    end

    subgraph CHECK5["Check 5 · Short-Sell"]
        C5A{Is Short\nSell?} --> |No| C5E([Pass →])
        C5A --> |Yes| C5B[Check SSR list\nLocate availability]
        C5B --> C5C{Locate ≥\nShort Qty?}
        C5C -->|✗ No| C5D([Hard Reject\nNo locate])
        C5C -->|✓ Yes| C5E
    end

    subgraph CHECK6["Check 6 · Regulatory"]
        C6A[MiFID II venue check\nPDT rule\nWash trade scan] --> C6B{Compliant?}
        C6B -->|✗ No| C6C([Hard Reject\nRegulatory breach])
        C6B -->|✓ Yes| C6D([Pass →])
    end

    subgraph CHECK7["Check 7 · VaR Pre-Check"]
        C7A[Monte Carlo\nMarginal VaR calc] --> C7B{Portfolio VaR\n≤ Limit?}
        C7B -->|✗ No| C7C([Soft Reject\nVaR limit warn])
        C7B -->|✓ Yes| C7D([Pass →])
    end

    subgraph DECISION["Final Decision"]
        D1{All Checks\nPassed?}
        D1 -->|✓ Yes| D2([APPROVED\nRoute to EMS])
        D1 -->|Soft| D3([WARN to Trader\nAwait Override])
        D1 -->|Hard| D4([REJECTED\nFIX ExecutionReport\nOrdStatus=8])
    end

    subgraph AUDIT2["Audit Trail"]
        AU1[(DynamoDB\norder_events)] 
        AU2[(S3 Object Lock)]
        AU1 --> AU2
    end

    S3 --> C1A
    C1D --> C2A
    C2D --> C3A
    C3D --> C4A
    C4D --> C5A
    C5E --> C6A
    C6D --> C7A
    C7D --> D1
    C1C & C2C & C3C & C4C & C5D & C6C & C7C --> D1
    D2 & D3 & D4 --> AU1

    style SUBMIT  fill:#0d1a2e,stroke:#2e6eb5,color:#a8c8ff
    style CHECK1  fill:#0d2e1a,stroke:#2eb56e,color:#a8ffc8
    style CHECK2  fill:#2e1a0d,stroke:#b5832e,color:#ffd8a8
    style CHECK3  fill:#2e0d1a,stroke:#b52e83,color:#ffa8d8
    style CHECK4  fill:#1a0d2e,stroke:#832eb5,color:#d8a8ff
    style CHECK5  fill:#2e2e0d,stroke:#b5b52e,color:#ffffa8
    style CHECK6  fill:#0d2e2e,stroke:#2eb5b5,color:#a8ffff
    style CHECK7  fill:#1a2e0d,stroke:#6eb52e,color:#d8ffa8
    style DECISION fill:#1a0d0d,stroke:#b52e2e,color:#ffa8a8
    style AUDIT2  fill:#0d0d1a,stroke:#2e2eb5,color:#a8a8ff
```

---

## 4. Almgren-Chriss Execution Cost Swimlane

```mermaid
flowchart LR
    subgraph INPUTS["Trade Intent"]
        direction TB
        AI1[Total Shares X\n100,000]
        AI2[Horizon T\n5 days]
        AI3[Urgency Level\nLow · Med · High]
        AI4[Market Params\nσ · η · γ · ADV]
    end

    subgraph STRATEGY["Strategy Selection"]
        direction TB
        ST1{Urgency?}
        ST1 -->|Low| ST2[TWAP\nEqual slices\nX/N per interval]
        ST1 -->|Medium| ST3[IS Optimal\nFront-loaded\nsinh schedule]
        ST1 -->|High| ST4[Aggressive\nFOK / IOC sweeps]
        ST2 & ST3 & ST4 --> ST5[Execution Schedule\nt₁:q₁ · t₂:q₂ · ...]
    end

    subgraph IMPACT["Impact Simulation\n10,000 paths"]
        direction TB
        IM1[Per Interval\nRandom Price Move\nGBM step] --> IM2[Apply Temp Impact\nexec_px = mid + η·rate]
        IM2 --> IM3[Apply Perm Impact\nnext mid shifts by γ·x_i]
        IM3 --> IM4[Accumulate\nTotal Execution Cost]
        IM4 --> IM5[Impl Shortfall\ncost - X·S₀ benchmark]
    end

    subgraph STATS2["Cost Statistics"]
        direction TB
        CS1[Mean IS $]
        CS2[Std Dev $]
        CS3[IS in bps\nIS$ / X·S₀ × 10000]
        CS4[95th Pct Cost\nWorst-case estimate]
        CS1 --> CS3
    end

    subgraph COMPARE["Strategy Comparison"]
        direction TB
        CMP1[TWAP cost]
        CMP2[IS Optimal cost]
        CMP3[Risk-adjusted\nE·IS + λ·Var·IS]
        CMP1 & CMP2 --> CMP3
        CMP3 --> CMP4{Min cost\nstrategy?}
        CMP4 --> CMP5([Selected Strategy\n→ OMS / EMS])
    end

    AI1 & AI2 & AI3 & AI4 --> ST1
    ST5 --> IM1
    IM5 --> CS1 & CS2 & CS4
    CS1 --> CMP1
    CS1 --> CMP2
    CS3 --> CMP3

    style INPUTS   fill:#0d1a37,stroke:#1e4eb5,color:#a8c0ff
    style STRATEGY fill:#0d3720,stroke:#1eb550,color:#a8ffcc
    style IMPACT   fill:#371a0d,stroke:#b5501e,color:#ffc0a8
    style STATS2   fill:#1a0d37,stroke:#501eb5,color:#cca8ff
    style COMPARE  fill:#0d3737,stroke:#1eb5b5,color:#a8ffff
```

---

## 5. Heston Model Swimlane — Path Simulation

```mermaid
flowchart TD
    subgraph PARAMS["Heston Parameters"]
        direction LR
        HP1[S₀ Spot Price]
        HP2[V₀ Initial Variance\nσ₀² e.g. 0.04 = 20%]
        HP3[κ Mean Reversion\nSpeed e.g. 2.0]
        HP4[θ Long-Run Variance\ne.g. 0.04]
        HP5[ξ Vol-of-Vol\ne.g. 0.3]
        HP6[ρ Correlation\nS & V e.g. -0.7]
    end

    subgraph VALIDATE["Feller Condition"]
        FC1{2κθ > ξ²?}
        FC1 -->|✓ 0.16 > 0.09| FC2([Variance stays\nstrictly positive])
        FC1 -->|✗ Violated| FC3([Variance can hit zero\nuse Full Truncation])
    end

    subgraph CORR["Correlated Brownians"]
        CB1[Z₁ ~ N·0·1\nindependent]
        CB2[Z₂ ~ N·0·1\nindependent]
        CB3[W_S = Z₁\nasset Brownian]
        CB4[W_V = ρZ₁ + √1-ρ²·Z₂\nvariance Brownian\nLevy effect embedded]
        CB1 --> CB3 & CB4
        CB2 --> CB4
    end

    subgraph VARPROC["Variance Process"]
        VP1[V_plus = max·V_t · 0\nFull Truncation] --> VP2[dV = κ·θ-V·dt\n+ ξ·√V·√dt·W_V]
        VP2 --> VP3[V_t+1 = max·V+dV · 0]
    end

    subgraph ASSETPROC["Asset Process"]
        AP1[dS = r·S·dt\n+ S·√V·√dt·W_S] --> AP2[S_t+1 = S + dS]
    end

    subgraph OUTPUT2["Path Output"]
        direction LR
        OP21[S paths\nn_paths × n_steps]
        OP22[V paths\nn_paths × n_steps]
        OP23[Terminal S_T\nfor payoff calc]
        OP24[Vol smile\nOTM options priced\nhigher than BS]
    end

    HP1 & HP2 & HP3 & HP4 & HP5 & HP6 --> FC1
    FC2 & FC3 --> CB1 & CB2
    CB3 --> AP1
    CB4 --> VP1
    HP3 & HP4 --> VP2
    HP5 --> VP2
    VP3 --> AP1
    AP2 --> OP21 & OP23
    VP3 --> OP22
    OP23 --> OP24

    style PARAMS   fill:#0d1a37,stroke:#4a7fff,color:#c0d8ff
    style VALIDATE fill:#370d0d,stroke:#ff4a4a,color:#ffc0c0
    style CORR     fill:#0d370d,stroke:#4aff4a,color:#c0ffc0
    style VARPROC  fill:#37200d,stroke:#ffaa4a,color:#ffe0c0
    style ASSETPROC fill:#200d37,stroke:#aa4aff,color:#e0c0ff
    style OUTPUT2  fill:#0d3737,stroke:#4affff,color:#c0ffff
```

---

## Quick Reference — Swimlane Legend

| Swimlane | Colour | Responsibility |
|---|---|---|
| Trader / Algo Engine | Blue | Order intent, UI, FIX client |
| OMS | Green | Lifecycle, state machine, persistence |
| Market Data | Purple | NBBO, L2 book, reference data |
| Pricing Engine | Orange | MC simulation, BS, Greeks |
| Risk Engine | Red | 7-check synchronous gate |
| Audit / Compliance | Navy | Immutable trail, S3 WORM |
| MC Simulation | Teal | GBM paths, antithetic, Heston |
| Execution Cost | Mixed | Almgren-Chriss TWAP / IS Optimal |
