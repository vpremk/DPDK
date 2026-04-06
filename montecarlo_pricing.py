"""
Monte Carlo Pricing Engine — Pre-Trade Derivatives Valuation
============================================================
Covers:
  1.  Geometric Brownian Motion (GBM) path simulation
  2.  European Call / Put  (benchmark vs Black-Scholes closed form)
  3.  Asian Option         (path-dependent — average price)
  4.  Barrier Option       (knock-in / knock-out)
  5.  American Option      (Longstaff-Schwartz LSM)
  6.  Greeks               (finite-difference bump-and-reprice)
  7.  Variance Reduction   (antithetic variates + control variates)
  8.  Stochastic Volatility (Heston model paths)
  9.  Market Impact        (Almgren-Chriss execution cost simulation)
  10. Portfolio VaR         (multi-asset correlated simulation)
"""

import numpy as np
import time
from dataclasses import dataclass
from typing import Literal
from scipy.stats import norm
from scipy.optimize import brentq

# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MarketData:
    S: float          # Spot price
    r: float          # Risk-free rate (continuous, annualised)
    q: float          # Dividend yield (continuous, annualised)
    sigma: float      # Implied volatility (annualised)

@dataclass
class OptionSpec:
    K: float                                    # Strike
    T: float                                    # Time to expiry (years)
    option_type: Literal["call", "put"] = "call"

@dataclass
class MCResult:
    price: float
    std_err: float
    conf_interval: tuple[float, float]          # 95%
    n_paths: int
    elapsed_ms: float


# ─────────────────────────────────────────────────────────────────────────────
# 1. PATH SIMULATION — Geometric Brownian Motion
# ─────────────────────────────────────────────────────────────────────────────

def simulate_gbm(
    S0: float,
    r: float,
    q: float,
    sigma: float,
    T: float,
    n_steps: int,
    n_paths: int,
    antithetic: bool = False,
    seed: int = 42,
) -> np.ndarray:
    """
    Simulate asset price paths under risk-neutral GBM.

    dS = S (r - q) dt + S σ dW

    Exact discretisation (log-Euler):
        S(t+dt) = S(t) * exp((r - q - 0.5σ²)dt + σ√dt Z)
        where Z ~ N(0,1)

    Returns:
        paths: shape (n_paths, n_steps+1)  — price at each time step
    """
    rng = np.random.default_rng(seed)
    dt = T / n_steps

    # Drift and diffusion coefficients
    drift = (r - q - 0.5 * sigma ** 2) * dt
    diffusion = sigma * np.sqrt(dt)

    if antithetic:
        # Generate half paths, mirror for variance reduction
        half = n_paths // 2
        Z = rng.standard_normal((half, n_steps))
        Z = np.vstack([Z, -Z])                  # antithetic pairs
    else:
        Z = rng.standard_normal((n_paths, n_steps))

    # Cumulative log-returns → price paths
    log_returns = drift + diffusion * Z         # shape: (n_paths, n_steps)
    log_paths = np.cumsum(log_returns, axis=1)  # cumulative sum
    paths = S0 * np.exp(
        np.hstack([np.zeros((n_paths, 1)), log_paths])  # prepend S0 at t=0
    )
    return paths                                # shape: (n_paths, n_steps+1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. BLACK-SCHOLES CLOSED FORM  (benchmark / sanity check)
# ─────────────────────────────────────────────────────────────────────────────

def black_scholes(mkt: MarketData, opt: OptionSpec) -> float:
    """Closed-form Black-Scholes price for European vanilla."""
    S, K, T = mkt.S, opt.K, opt.T
    r, q, sigma = mkt.r, mkt.q, mkt.sigma

    if T <= 0:
        # Intrinsic value at expiry
        return max(S - K, 0) if opt.option_type == "call" else max(K - S, 0)

    d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if opt.option_type == "call":
        price = S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)
    return price


# ─────────────────────────────────────────────────────────────────────────────
# 3. EUROPEAN OPTION — Monte Carlo
# ─────────────────────────────────────────────────────────────────────────────

def mc_european(
    mkt: MarketData,
    opt: OptionSpec,
    n_paths: int = 100_000,
    antithetic: bool = True,
    seed: int = 42,
) -> MCResult:
    """
    European option: payoff depends only on terminal price S(T).

    Call payoff: max(S_T - K, 0)
    Put  payoff: max(K - S_T, 0)
    Price = e^(-rT) * E[payoff]
    """
    t0 = time.perf_counter()

    # Only need terminal prices — simulate in 1 step for efficiency
    paths = simulate_gbm(
        mkt.S, mkt.r, mkt.q, mkt.sigma,
        opt.T, n_steps=1, n_paths=n_paths,
        antithetic=antithetic, seed=seed
    )
    S_T = paths[:, -1]                         # terminal prices

    if opt.option_type == "call":
        payoffs = np.maximum(S_T - opt.K, 0)
    else:
        payoffs = np.maximum(opt.K - S_T, 0)

    discount = np.exp(-mkt.r * opt.T)
    discounted = discount * payoffs

    price = discounted.mean()
    stderr = discounted.std() / np.sqrt(n_paths)
    ci = (price - 1.96 * stderr, price + 1.96 * stderr)

    return MCResult(price, stderr, ci, n_paths, (time.perf_counter() - t0) * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# 4. ASIAN OPTION — path-dependent (average price)
# ─────────────────────────────────────────────────────────────────────────────

def mc_asian(
    mkt: MarketData,
    opt: OptionSpec,
    averaging: Literal["arithmetic", "geometric"] = "arithmetic",
    n_steps: int = 252,
    n_paths: int = 100_000,
    antithetic: bool = True,
    seed: int = 42,
) -> MCResult:
    """
    Asian option: payoff based on average price over the life of the option.

    Arithmetic Asian Call:  max(A_T - K, 0)  where A_T = (1/N) Σ S_i
    Geometric  Asian Call:  max(G_T - K, 0)  where G_T = (Π S_i)^(1/N)

    Why used in trading:
      - Harder to manipulate (no single expiry price to pin)
      - Cheaper than vanilla (averaging reduces variance)
      - Common in commodity, FX, EM markets
    """
    t0 = time.perf_counter()

    paths = simulate_gbm(
        mkt.S, mkt.r, mkt.q, mkt.sigma,
        opt.T, n_steps=n_steps, n_paths=n_paths,
        antithetic=antithetic, seed=seed
    )
    # Average over time steps (exclude t=0 spot price)
    price_slice = paths[:, 1:]                  # shape: (n_paths, n_steps)

    if averaging == "arithmetic":
        avg = price_slice.mean(axis=1)
    else:                                        # geometric
        avg = np.exp(np.log(price_slice).mean(axis=1))

    if opt.option_type == "call":
        payoffs = np.maximum(avg - opt.K, 0)
    else:
        payoffs = np.maximum(opt.K - avg, 0)

    discount = np.exp(-mkt.r * opt.T)
    discounted = discount * payoffs

    price = discounted.mean()
    stderr = discounted.std() / np.sqrt(n_paths)
    ci = (price - 1.96 * stderr, price + 1.96 * stderr)

    return MCResult(price, stderr, ci, n_paths, (time.perf_counter() - t0) * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# 5. BARRIER OPTION — knock-in / knock-out
# ─────────────────────────────────────────────────────────────────────────────

def mc_barrier(
    mkt: MarketData,
    opt: OptionSpec,
    barrier: float,
    barrier_type: Literal["down-and-out", "down-and-in", "up-and-out", "up-and-in"] = "down-and-out",
    n_steps: int = 252,
    n_paths: int = 100_000,
    rebate: float = 0.0,                        # cash rebate if knocked out
    antithetic: bool = True,
    seed: int = 42,
) -> MCResult:
    """
    Barrier option: option activates (knock-in) or deactivates (knock-out)
    if the asset price crosses a barrier during the option's life.

    Common in FX and structured products — cheaper than vanilla.

    Barrier types:
      down-and-out : knocked out if S < barrier  (loses value if stock falls)
      down-and-in  : activated  if S < barrier  (gains value if stock falls)
      up-and-out   : knocked out if S > barrier  (loses value if stock rises)
      up-and-in    : activated  if S > barrier  (gains value if stock rises)
    """
    t0 = time.perf_counter()

    paths = simulate_gbm(
        mkt.S, mkt.r, mkt.q, mkt.sigma,
        opt.T, n_steps=n_steps, n_paths=n_paths,
        antithetic=antithetic, seed=seed
    )

    S_T = paths[:, -1]
    path_min = paths.min(axis=1)
    path_max = paths.max(axis=1)

    # Terminal payoff (ignoring barrier for now)
    if opt.option_type == "call":
        terminal_payoff = np.maximum(S_T - opt.K, 0)
    else:
        terminal_payoff = np.maximum(opt.K - S_T, 0)

    # Apply barrier condition
    if barrier_type == "down-and-out":
        knocked = path_min < barrier            # TRUE = barrier was crossed
        payoffs = np.where(knocked, rebate, terminal_payoff)

    elif barrier_type == "down-and-in":
        knocked = path_min < barrier
        payoffs = np.where(knocked, terminal_payoff, rebate)

    elif barrier_type == "up-and-out":
        knocked = path_max > barrier
        payoffs = np.where(knocked, rebate, terminal_payoff)

    elif barrier_type == "up-and-in":
        knocked = path_max > barrier
        payoffs = np.where(knocked, terminal_payoff, rebate)

    else:
        raise ValueError(f"Unknown barrier_type: {barrier_type}")

    discount = np.exp(-mkt.r * opt.T)
    discounted = discount * payoffs

    price = discounted.mean()
    stderr = discounted.std() / np.sqrt(n_paths)
    ci = (price - 1.96 * stderr, price + 1.96 * stderr)

    return MCResult(price, stderr, ci, n_paths, (time.perf_counter() - t0) * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# 6. AMERICAN OPTION — Longstaff-Schwartz (LSM)
# ─────────────────────────────────────────────────────────────────────────────

def mc_american_lsm(
    mkt: MarketData,
    opt: OptionSpec,
    n_steps: int = 50,
    n_paths: int = 50_000,
    degree: int = 3,                            # polynomial degree for regression
    seed: int = 42,
) -> MCResult:
    """
    American option pricing via Longstaff-Schwartz (2001) least-squares Monte Carlo.

    Key insight: at each time step, estimate the continuation value by
    regressing realized future cash flows on basis functions of the current price.
    If immediate exercise value > continuation value → exercise early.

    Basis functions: [1, S, S², S³, ...]  (Laguerre polynomials work too)

    Why important:
      - No closed form for American options (early exercise premium)
      - LSM is the industry standard MC approach
      - Used for employee stock options, callable bonds, Bermudan swaptions
    """
    t0 = time.perf_counter()

    paths = simulate_gbm(
        mkt.S, mkt.r, mkt.q, mkt.sigma,
        opt.T, n_steps=n_steps, n_paths=n_paths,
        antithetic=False, seed=seed
    )

    dt = opt.T / n_steps
    discount_factor = np.exp(-mkt.r * dt)

    def intrinsic(S):
        """Exercise value at price S."""
        if opt.option_type == "call":
            return np.maximum(S - opt.K, 0)
        return np.maximum(opt.K - S, 0)

    # Cash flow matrix — start with terminal payoff
    cash_flows = intrinsic(paths[:, -1])        # payoff at expiry

    # Backward induction: step back from T-1 to t=1
    for t in range(n_steps - 1, 0, -1):
        S_t = paths[:, t]
        exercise_val = intrinsic(S_t)

        # Only consider paths that are in-the-money (relevant for early exercise)
        itm = exercise_val > 0
        if itm.sum() < 10:                      # too few ITM paths to regress
            cash_flows *= discount_factor
            continue

        # Discounted future cash flows for ITM paths
        Y = cash_flows[itm] * discount_factor

        # Basis functions: polynomial in S
        X = np.vander(S_t[itm], degree + 1, increasing=True)   # [1, S, S², ...]

        # OLS regression: E[continuation | S_t] ≈ X β
        beta, _, _, _ = np.linalg.lstsq(X, Y, rcond=None)
        continuation = X @ beta

        # Exercise decision: exercise if intrinsic > estimated continuation
        exercise_now = exercise_val[itm] > continuation

        # Update cash flows
        cash_flows *= discount_factor           # discount all paths one step
        itm_indices = np.where(itm)[0]
        cash_flows[itm_indices[exercise_now]] = exercise_val[itm][exercise_now]

    # Final discount from t=1 to t=0
    price_paths = cash_flows * discount_factor
    price = price_paths.mean()
    stderr = price_paths.std() / np.sqrt(n_paths)
    ci = (price - 1.96 * stderr, price + 1.96 * stderr)

    return MCResult(price, stderr, ci, n_paths, (time.perf_counter() - t0) * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# 7. GREEKS via FINITE DIFFERENCE (bump-and-reprice)
# ─────────────────────────────────────────────────────────────────────────────

def compute_greeks(
    mkt: MarketData,
    opt: OptionSpec,
    n_paths: int = 100_000,
    use_closed_form: bool = True,
) -> dict:
    """
    Compute option Greeks via finite-difference bump-and-reprice.

    Delta (Δ): dV/dS     — hedge ratio, shares of underlying to hold
    Gamma (Γ): d²V/dS²   — rate of delta change, convexity
    Vega  (ν): dV/dσ     — sensitivity to volatility (×0.01 = per 1% vol move)
    Theta (Θ): dV/dT     — time decay (per calendar day)
    Rho   (ρ): dV/dr     — sensitivity to interest rate (×0.01 = per 1bp)

    For European options uses Black-Scholes (exact). For exotic options,
    set use_closed_form=False to use MC bump-and-reprice (slower, noisier).
    """

    def price(m: MarketData, o: OptionSpec) -> float:
        if use_closed_form:
            return black_scholes(m, o)
        return mc_european(m, o, n_paths=n_paths).price

    V = price(mkt, opt)

    # ── Delta: central difference dV/dS
    h_S = mkt.S * 0.01                         # 1% bump
    V_up   = price(MarketData(mkt.S + h_S, mkt.r, mkt.q, mkt.sigma), opt)
    V_down = price(MarketData(mkt.S - h_S, mkt.r, mkt.q, mkt.sigma), opt)
    delta = (V_up - V_down) / (2 * h_S)

    # ── Gamma: second derivative d²V/dS²
    gamma = (V_up - 2 * V + V_down) / (h_S ** 2)

    # ── Vega: dV/dσ  (report as per 1% vol move)
    h_v = 0.01
    V_vup   = price(MarketData(mkt.S, mkt.r, mkt.q, mkt.sigma + h_v), opt)
    V_vdown = price(MarketData(mkt.S, mkt.r, mkt.q, mkt.sigma - h_v), opt)
    vega = (V_vup - V_vdown) / 2               # already per 1% (h_v=0.01)

    # ── Theta: dV/dT  (report as per calendar day = T-1/365)
    h_t = 1 / 365
    if opt.T > h_t:
        opt_shifted = OptionSpec(opt.K, opt.T - h_t, opt.option_type)
        theta = (price(mkt, opt_shifted) - V) / h_t / 365  # per calendar day
    else:
        theta = 0.0

    # ── Rho: dV/dr  (report as per 1bp = 0.0001)
    h_r = 0.01
    V_rup   = price(MarketData(mkt.S, mkt.r + h_r, mkt.q, mkt.sigma), opt)
    V_rdown = price(MarketData(mkt.S, mkt.r - h_r, mkt.q, mkt.sigma), opt)
    rho = (V_rup - V_rdown) / 2               # per 1%

    return {
        "price"  : round(V, 4),
        "delta"  : round(delta, 4),
        "gamma"  : round(gamma, 6),
        "vega"   : round(vega, 4),     # per 1% vol
        "theta"  : round(theta, 4),    # per calendar day
        "rho"    : round(rho, 4),      # per 1% rate
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8. HESTON STOCHASTIC VOLATILITY MODEL
# ─────────────────────────────────────────────────────────────────────────────

def simulate_heston(
    S0: float,
    V0: float,      # initial variance (σ² not σ)
    r: float,
    kappa: float,   # mean-reversion speed of variance
    theta: float,   # long-run mean variance
    xi: float,      # vol-of-vol (volatility of variance)
    rho: float,     # correlation between asset and variance Brownian motions
    T: float,
    n_steps: int = 252,
    n_paths: int = 50_000,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Simulate asset + variance paths under the Heston (1993) model.

    dS = S r dt + S √V dW_S
    dV = κ(θ - V) dt + ξ √V dW_V
    corr(dW_S, dW_V) = ρ dt

    Parameters:
      kappa: mean-reversion speed (typical: 1–5)
      theta: long-run variance    (typical: 0.04 = 20% long-run vol)
      xi   : vol of vol           (typical: 0.3–0.6)
      rho  : correlation          (typical: -0.7 for equities — leverage effect)

    Feller condition for strictly positive variance: 2κθ > ξ²

    Returns:
      S_paths: shape (n_paths, n_steps+1)
      V_paths: shape (n_paths, n_steps+1)
    """
    rng = np.random.default_rng(seed)
    dt = T / n_steps

    S_paths = np.zeros((n_paths, n_steps + 1))
    V_paths = np.zeros((n_paths, n_steps + 1))
    S_paths[:, 0] = S0
    V_paths[:, 0] = V0

    # Cholesky decomposition for correlated Brownians
    # W_S = Z1
    # W_V = rho * Z1 + sqrt(1-rho²) * Z2
    sqrt_1_rho2 = np.sqrt(1 - rho ** 2)

    for t in range(n_steps):
        Z1 = rng.standard_normal(n_paths)
        Z2 = rng.standard_normal(n_paths)
        W_S = Z1
        W_V = rho * Z1 + sqrt_1_rho2 * Z2

        V = V_paths[:, t]
        S = S_paths[:, t]

        # Variance process (Full Truncation scheme — ensures V >= 0)
        V_plus = np.maximum(V, 0)
        sqrt_V = np.sqrt(V_plus)

        dV = kappa * (theta - V_plus) * dt + xi * sqrt_V * np.sqrt(dt) * W_V
        V_new = V + dV
        V_paths[:, t + 1] = np.maximum(V_new, 0)   # truncate negative variance

        # Asset process
        dS = r * S * dt + S * sqrt_V * np.sqrt(dt) * W_S
        S_paths[:, t + 1] = S + dS

    return S_paths, V_paths


def mc_european_heston(
    S0: float, V0: float, K: float, T: float,
    r: float, kappa: float, theta: float, xi: float, rho: float,
    option_type: str = "call",
    n_steps: int = 252,
    n_paths: int = 50_000,
    seed: int = 42,
) -> MCResult:
    """Price European option under Heston stochastic volatility."""
    t0 = time.perf_counter()

    S_paths, _ = simulate_heston(S0, V0, r, kappa, theta, xi, rho, T,
                                  n_steps, n_paths, seed)
    S_T = S_paths[:, -1]

    if option_type == "call":
        payoffs = np.maximum(S_T - K, 0)
    else:
        payoffs = np.maximum(K - S_T, 0)

    discount = np.exp(-r * T)
    discounted = discount * payoffs
    price = discounted.mean()
    stderr = discounted.std() / np.sqrt(n_paths)
    ci = (price - 1.96 * stderr, price + 1.96 * stderr)

    return MCResult(price, stderr, ci, n_paths, (time.perf_counter() - t0) * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# 9. MARKET IMPACT — Almgren-Chriss Execution Cost Simulation
# ─────────────────────────────────────────────────────────────────────────────

def simulate_almgren_chriss(
    X: float,           # total shares to execute
    T: float,           # execution horizon (days)
    S0: float,          # initial mid-price
    sigma: float,       # daily volatility
    eta: float,         # temporary impact coefficient
    gamma: float,       # permanent impact coefficient
    n_steps: int = 10,  # number of trading intervals
    n_paths: int = 10_000,
    strategy: Literal["twap", "vwap_approx", "is_optimal"] = "twap",
    seed: int = 42,
) -> dict:
    """
    Simulate execution cost under the Almgren-Chriss (2000) model.

    Market impact decomposition:
      Temporary impact: price moves against you during the trade,
                        recovers afterward. Proportional to trading rate.
      Permanent impact: price shift that persists after execution.
                        Proportional to total quantity traded.

    Impact model:
      Temporary: ΔS_temp = η × (x_i / Δt)   (per-interval trading cost)
      Permanent: ΔS_perm = γ × x_i           (permanent shift per interval)

    Strategies compared:
      TWAP: equal slices over time
      IS Optimal: front-load execution (minimize implementation shortfall)
    """
    rng = np.random.default_rng(seed)
    dt = T / n_steps

    if strategy == "twap":
        # Equal slices
        slices = np.full(n_steps, X / n_steps)

    elif strategy == "is_optimal":
        # Almgren-Chriss optimal trajectory: x(t) = X sinh(κ(T-t)) / sinh(κT)
        # κ = sqrt(γ/η) is the urgency parameter
        kappa = np.sqrt(gamma / eta) if eta > 0 else 1.0
        t_grid = np.linspace(0, T, n_steps + 1)
        holdings = X * np.sinh(kappa * (T - t_grid)) / np.sinh(kappa * T + 1e-10)
        slices = np.diff(-holdings)             # share reduction per interval
        slices = np.maximum(slices, 0)          # no buying back

    else:   # vwap_approx — U-shaped volume profile
        weights = np.sin(np.linspace(0, np.pi, n_steps)) + 0.3
        weights /= weights.sum()
        slices = X * weights

    # Simulate price paths with impact
    costs = np.zeros(n_paths)
    for path in range(n_paths):
        S = S0
        total_cost = 0.0
        perm_impact = 0.0

        for i, x_i in enumerate(slices):
            # Random price move (GBM step)
            S += perm_impact                    # apply accumulated permanent impact
            Z = rng.standard_normal()
            S_mid = S * np.exp((0 - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * Z)

            # Execution price = mid + temporary impact (adverse)
            rate = x_i / dt if dt > 0 else x_i
            exec_price = S_mid + eta * rate     # selling: price pushed down = cost
            total_cost += x_i * exec_price

            # Permanent impact shifts the mid going forward
            perm_impact = gamma * x_i
            S = S_mid

        # Benchmark: execute all at initial mid (arrival price)
        benchmark_cost = X * S0
        costs[path] = total_cost - benchmark_cost   # implementation shortfall

    is_mean = costs.mean()
    is_std  = costs.std()
    is_bps  = (is_mean / (X * S0)) * 10_000        # in basis points

    return {
        "strategy"          : strategy,
        "shares"            : X,
        "horizon_days"      : T,
        "slices"            : slices.round(0).astype(int).tolist(),
        "impl_shortfall_$"  : round(is_mean, 2),
        "impl_shortfall_bps": round(is_bps, 2),
        "std_dev_$"         : round(is_std, 2),
        "95th_pct_cost_$"   : round(np.percentile(costs, 95), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 10. PORTFOLIO VaR — Correlated Multi-Asset Simulation
# ─────────────────────────────────────────────────────────────────────────────

def portfolio_var_mc(
    positions: dict[str, float],        # {ticker: $ market value, negative = short}
    vols: dict[str, float],             # {ticker: annualised vol}
    corr_matrix: np.ndarray,            # correlation matrix (n×n)
    horizon_days: int = 1,
    confidence: float = 0.99,
    n_paths: int = 100_000,
    seed: int = 42,
) -> dict:
    """
    Portfolio Value-at-Risk via correlated Monte Carlo simulation.

    Method:
      1. Cholesky decompose correlation matrix → L  (LL' = Σ)
      2. Sample correlated returns: R = L Z  where Z ~ N(0,I)
      3. Scale by position × vol × sqrt(horizon)
      4. Portfolio P&L = sum of position P&Ls
      5. VaR = -quantile(P&L, 1 - confidence)

    Used in pre-trade to:
      - Check if a new order would breach portfolio VaR limits
      - Estimate marginal VaR contribution of proposed order
    """
    rng = np.random.default_rng(seed)
    tickers = list(positions.keys())
    n = len(tickers)
    MV = np.array([positions[t] for t in tickers])   # market values
    sigma = np.array([vols[t] for t in tickers])      # annual vols
    dt = horizon_days / 252

    # Cholesky decomposition of correlation matrix
    L = np.linalg.cholesky(corr_matrix)

    # Simulate correlated standard normal returns
    Z = rng.standard_normal((n, n_paths))              # shape: (n_assets, n_paths)
    corr_Z = L @ Z                                     # shape: (n_assets, n_paths)

    # Daily returns per asset
    returns = sigma[:, None] * np.sqrt(dt) * corr_Z   # shape: (n_assets, n_paths)

    # P&L per path
    pnl = (MV[:, None] * returns).sum(axis=0)          # shape: (n_paths,)

    var = -np.percentile(pnl, (1 - confidence) * 100)
    cvar = -pnl[pnl <= -var].mean()                   # Expected Shortfall (CVaR)

    # Marginal VaR per asset (approximate: correlation with portfolio P&L)
    marginal_vars = {}
    for i, t in enumerate(tickers):
        asset_pnl = MV[i] * returns[i]
        # Marginal VaR ≈ Beta × portfolio VaR
        beta = np.cov(asset_pnl, pnl)[0, 1] / np.var(pnl)
        marginal_vars[t] = round(beta * var, 2)

    return {
        "horizon_days"      : horizon_days,
        "confidence"        : confidence,
        "portfolio_var_$"   : round(var, 2),
        "expected_shortfall": round(cvar, 2),
        "marginal_var"      : marginal_vars,
        "portfolio_pnl_mean": round(pnl.mean(), 2),
        "portfolio_pnl_std" : round(pnl.std(), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# IMPLIED VOLATILITY via BISECTION (bonus utility)
# ─────────────────────────────────────────────────────────────────────────────

def implied_vol(
    market_price: float,
    mkt: MarketData,
    opt: OptionSpec,
    tol: float = 1e-6,
) -> float:
    """
    Back out implied vol from market price via Brent's method.
    Used in pre-trade to mark options at market and build vol surface.
    """
    def objective(sigma):
        m = MarketData(mkt.S, mkt.r, mkt.q, sigma)
        return black_scholes(m, opt) - market_price

    try:
        iv = brentq(objective, 1e-6, 10.0, xtol=tol)
    except ValueError:
        iv = float("nan")
    return round(iv, 6)


# ─────────────────────────────────────────────────────────────────────────────
# DEMO / MAIN
# ─────────────────────────────────────────────────────────────────────────────

def _section(title: str) -> None:
    width = 70
    print(f"\n{'═' * width}")
    print(f"  {title}")
    print(f"{'═' * width}")


def main():
    # ── Common market data ──────────────────────────────────────────────────
    mkt = MarketData(S=100.0, r=0.05, q=0.02, sigma=0.20)
    opt_call = OptionSpec(K=100.0, T=1.0, option_type="call")
    opt_put  = OptionSpec(K=100.0, T=1.0, option_type="put")

    # ── 1. European — MC vs Black-Scholes ───────────────────────────────────
    _section("1. European Option — MC vs Black-Scholes")
    bs_call = black_scholes(mkt, opt_call)
    bs_put  = black_scholes(mkt, opt_put)
    mc_call = mc_european(mkt, opt_call, n_paths=200_000)
    mc_put  = mc_european(mkt, opt_put,  n_paths=200_000)

    print(f"  {'':30s} {'Call':>10}  {'Put':>10}")
    print(f"  {'Black-Scholes (exact)':30s} {bs_call:>10.4f}  {bs_put:>10.4f}")
    print(f"  {'Monte Carlo price':30s} {mc_call.price:>10.4f}  {mc_put.price:>10.4f}")
    print(f"  {'MC std error':30s} {mc_call.std_err:>10.6f}  {mc_put.std_err:>10.6f}")
    print(f"  {'MC 95% CI call':30s} [{mc_call.conf_interval[0]:.4f}, {mc_call.conf_interval[1]:.4f}]")
    print(f"  {'MC elapsed (ms)':30s} {mc_call.elapsed_ms:>10.2f}")
    print(f"\n  Put-Call Parity check: C - P = {mc_call.price - mc_put.price:.4f}  "
          f"(theory: {mkt.S*np.exp(-mkt.q*opt_call.T) - opt_call.K*np.exp(-mkt.r*opt_call.T):.4f})")

    # ── 2. Asian Option ─────────────────────────────────────────────────────
    _section("2. Asian Option — Arithmetic vs Geometric Averaging")
    arith = mc_asian(mkt, opt_call, averaging="arithmetic", n_paths=100_000)
    geom  = mc_asian(mkt, opt_call, averaging="geometric",  n_paths=100_000)
    print(f"  European call (benchmark):          {bs_call:.4f}")
    print(f"  Asian call (arithmetic average):    {arith.price:.4f}  ± {arith.std_err:.4f}"
          f"  [{arith.elapsed_ms:.1f} ms]")
    print(f"  Asian call (geometric  average):    {geom.price:.4f}  ± {geom.std_err:.4f}"
          f"  [{geom.elapsed_ms:.1f} ms]")
    print(f"\n  Observation: Asian < European because averaging reduces variance of payoff")

    # ── 3. Barrier Option ───────────────────────────────────────────────────
    _section("3. Barrier Option — Down-and-Out vs Down-and-In")
    barrier = 85.0
    dao = mc_barrier(mkt, opt_call, barrier=barrier, barrier_type="down-and-out", n_paths=100_000)
    dai = mc_barrier(mkt, opt_call, barrier=barrier, barrier_type="down-and-in",  n_paths=100_000)
    print(f"  Barrier = {barrier}  (spot = {mkt.S})")
    print(f"  European call (benchmark):     {bs_call:.4f}")
    print(f"  Down-and-Out call:             {dao.price:.4f}  ± {dao.std_err:.4f}"
          f"  [{dao.elapsed_ms:.1f} ms]")
    print(f"  Down-and-In  call:             {dai.price:.4f}  ± {dai.std_err:.4f}"
          f"  [{dai.elapsed_ms:.1f} ms]")
    print(f"\n  Parity check: DAO + DAI ≈ European: "
          f"{dao.price + dai.price:.4f} ≈ {bs_call:.4f}")

    # ── 4. American Option (LSM) ────────────────────────────────────────────
    _section("4. American Option — Longstaff-Schwartz (LSM)")
    eur_put = mc_european(mkt, opt_put, n_paths=50_000)
    am_put  = mc_american_lsm(mkt, opt_put, n_steps=50, n_paths=50_000)
    print(f"  European put (BS exact):     {bs_put:.4f}")
    print(f"  European put (MC):           {eur_put.price:.4f}  ± {eur_put.std_err:.4f}")
    print(f"  American put (LSM):          {am_put.price:.4f}  ± {am_put.std_err:.4f}"
          f"  [{am_put.elapsed_ms:.1f} ms]")
    print(f"\n  Early exercise premium:      {am_put.price - bs_put:.4f}")
    print(f"  (American >= European always holds — early exercise right has value)")

    # ── 5. Greeks ───────────────────────────────────────────────────────────
    _section("5. Greeks — Bump-and-Reprice (Black-Scholes exact)")
    greeks = compute_greeks(mkt, opt_call, use_closed_form=True)
    print(f"  {'Greek':<10} {'Value':>12}  Interpretation")
    print(f"  {'-'*60}")
    print(f"  {'Price':<10} {greeks['price']:>12.4f}  option premium")
    print(f"  {'Delta':<10} {greeks['delta']:>12.4f}  shares to hold as hedge")
    print(f"  {'Gamma':<10} {greeks['gamma']:>12.6f}  delta change per $1 move in S")
    print(f"  {'Vega':<10} {greeks['vega']:>12.4f}  P&L per 1% vol move")
    print(f"  {'Theta':<10} {greeks['theta']:>12.4f}  daily time decay ($/day)")
    print(f"  {'Rho':<10} {greeks['rho']:>12.4f}  P&L per 1% rate move")

    # ── 6. Heston Stochastic Vol ─────────────────────────────────────────────
    _section("6. Heston Stochastic Volatility Model")
    V0    = 0.04    # initial variance = (20% vol)²
    kappa = 2.0     # mean reversion speed
    theta = 0.04    # long-run variance
    xi    = 0.3     # vol of vol
    rho   = -0.7    # leverage effect (negative for equities)

    heston_call = mc_european_heston(
        S0=mkt.S, V0=V0, K=opt_call.K, T=opt_call.T,
        r=mkt.r, kappa=kappa, theta=theta, xi=xi, rho=rho,
        option_type="call", n_paths=50_000
    )
    heston_put = mc_european_heston(
        S0=mkt.S, V0=V0, K=opt_call.K, T=opt_call.T,
        r=mkt.r, kappa=kappa, theta=theta, xi=xi, rho=rho,
        option_type="put", n_paths=50_000
    )
    print(f"  Heston params: κ={kappa}, θ={theta} (≡{100*theta**0.5:.0f}% long-run vol), "
          f"ξ={xi}, ρ={rho}")
    print(f"  Feller condition (2κθ > ξ²): {2*kappa*theta:.3f} > {xi**2:.3f} "
          f"→ {'satisfied ✓' if 2*kappa*theta > xi**2 else 'VIOLATED ✗'}")
    print(f"\n  {'Model':<30} {'Call':>10}  {'Put':>10}  {'Time(ms)':>10}")
    print(f"  {'-'*65}")
    print(f"  {'Black-Scholes (flat vol)':30} {bs_call:>10.4f}  {bs_put:>10.4f}")
    print(f"  {'Heston (stochastic vol)':30} {heston_call.price:>10.4f}  "
          f"{heston_put.price:>10.4f}  {heston_call.elapsed_ms:>10.1f}")
    print(f"\n  Heston captures vol smile: OTM options priced differently than BS flat-vol")

    # ── 7. Implied Volatility ────────────────────────────────────────────────
    _section("7. Implied Volatility Surface (sample strikes)")
    strikes = [80, 90, 95, 100, 105, 110, 120]
    print(f"  {'Strike':>8}  {'Moneyness':>10}  {'BS Price':>10}  {'IV (%)':>10}")
    print(f"  {'-'*50}")
    for K in strikes:
        o = OptionSpec(K=K, T=1.0, option_type="call")
        price_bs = black_scholes(mkt, o)
        # Add a small vol smile: OTM puts / ITM calls get vol premium
        smile_adj = 0.03 * abs(np.log(mkt.S / K))  # simplified skew
        market_price = black_scholes(MarketData(mkt.S, mkt.r, mkt.q, mkt.sigma + smile_adj), o)
        iv = implied_vol(market_price, mkt, o) * 100
        moneyness = f"{'ITM' if K < mkt.S else 'ATM' if K == mkt.S else 'OTM'}"
        print(f"  {K:>8}  {moneyness:>10}  {market_price:>10.4f}  {iv:>10.2f}")

    # ── 8. Almgren-Chriss Market Impact ──────────────────────────────────────
    _section("8. Almgren-Chriss: Execution Cost Simulation")
    X     = 100_000   # 100k shares to sell
    T_ac  = 5         # 5-day execution window
    S0_ac = 50.0
    sigma_daily = 0.015   # 1.5% daily vol
    eta   = 0.1 * S0_ac / (X / T_ac)   # temporary impact calibration
    gamma = 0.01 * S0_ac / X            # permanent impact calibration

    for strat in ["twap", "is_optimal"]:
        result = simulate_almgren_chriss(
            X=X, T=T_ac, S0=S0_ac,
            sigma=sigma_daily, eta=eta, gamma=gamma,
            n_steps=T_ac, n_paths=20_000, strategy=strat
        )
        print(f"\n  Strategy: {strat.upper()}")
        print(f"    Slices (shares/day): {result['slices']}")
        print(f"    Impl. Shortfall:     ${result['impl_shortfall_$']:,.2f}  "
              f"({result['impl_shortfall_bps']:.1f} bps)")
        print(f"    Std Dev of cost:     ${result['std_dev_$']:,.2f}")
        print(f"    95th pct cost:       ${result['95th_pct_cost_$']:,.2f}")

    # ── 9. Portfolio VaR ─────────────────────────────────────────────────────
    _section("9. Portfolio VaR — Correlated Multi-Asset Monte Carlo")
    positions = {
        "AAPL":  5_000_000,    # $5M long
        "MSFT":  3_000_000,    # $3M long
        "GOOGL": 2_000_000,    # $2M long
        "TLT":  -1_000_000,    # $1M short (bonds hedge)
    }
    vols = {"AAPL": 0.28, "MSFT": 0.25, "GOOGL": 0.30, "TLT": 0.12}
    corr = np.array([
        [1.00, 0.75, 0.65, -0.20],   # AAPL
        [0.75, 1.00, 0.70, -0.18],   # MSFT
        [0.65, 0.70, 1.00, -0.15],   # GOOGL
        [-0.20,-0.18,-0.15, 1.00],   # TLT
    ])

    var_result = portfolio_var_mc(positions, vols, corr,
                                  horizon_days=1, confidence=0.99, n_paths=200_000)

    total_mv = sum(abs(v) for v in positions.values())
    print(f"  Portfolio (total gross exposure: ${total_mv/1e6:.0f}M)")
    print(f"  {'Asset':<8} {'Pos ($M)':>10}  {'Vol':>8}  {'Marginal VaR':>14}")
    print(f"  {'-'*50}")
    for t in positions:
        print(f"  {t:<8} {positions[t]/1e6:>+10.1f}  {vols[t]:>8.1%}  "
              f"${var_result['marginal_var'][t]:>12,.0f}")
    print(f"\n  1-day 99% VaR:         ${var_result['portfolio_var_$']:>12,.0f}")
    print(f"  Expected Shortfall:    ${var_result['expected_shortfall']:>12,.0f}  "
          f"(avg loss beyond VaR)")
    print(f"  VaR as % of portfolio: {var_result['portfolio_var_$']/total_mv*100:.2f}%")

    print(f"\n{'═' * 70}")
    print("  All simulations complete.")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
