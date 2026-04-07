"""
GBO Reference Data Simulation
==============================
GBO (Global Book of Orders) is the firm's golden source for static and
reference data consumed by every risk system.  Both pre-trade and
post-trade risk engines call into GBO before processing any order.

Data domains simulated here:
  1.  InstrumentMaster  — security static data (ISIN, CUSIP, asset class,
                          tick size, lot size, currency, exchange codes)
  2.  CounterpartyMaster — legal entity, credit tier, jurisdiction, LEI
  3.  AccountMaster      — trading accounts, books, desks, portfolios
  4.  LimitTable         — per-account / per-instrument risk limits
                          (position, notional, DV01, concentration)
  5.  HolidayCalendar    — settlement calendars per currency/exchange
  6.  FXRates            — live-ish spot FX for notional normalisation
  7.  GBORefDataStore    — in-memory store with O(1) lookup, used by
                          PreTradeRiskEngine and PostTradeRiskEngine

Pre-trade usage:
  gbo = GBORefDataStore()
  result = PreTradeRiskEngine(gbo).check(order)

Post-trade usage:
  gbo = GBORefDataStore()
  blotter = PostTradeRiskEngine(gbo)
  blotter.book_fill(fill)
  print(blotter.position_report())

Run standalone to see a full demo:
  python gbo_ref_data.py
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# 1. ENUMERATIONS
# ─────────────────────────────────────────────────────────────────────────────

class AssetClass(str, Enum):
    EQUITY      = "Equity"
    EQUITY_OPT  = "EquityOption"
    FIXED_INCOME= "FixedIncome"
    FX          = "FX"
    COMMODITY   = "Commodity"
    CRYPTO      = "Crypto"

class CreditTier(str, Enum):
    TIER1 = "Tier1"   # Prime broker, major bank
    TIER2 = "Tier2"   # Regional bank, large hedge fund
    TIER3 = "Tier3"   # Smaller CP, higher margin req

class OrderSide(str, Enum):
    BUY  = "Buy"
    SELL = "Sell"

class RiskResult(str, Enum):
    PASS    = "PASS"
    WARN    = "WARN"
    REJECT  = "REJECT"


# ─────────────────────────────────────────────────────────────────────────────
# 2. INSTRUMENT MASTER
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Instrument:
    """Static security attributes — sourced from Bloomberg / Refinitiv in prod."""
    isin:          str
    cusip:         str
    ticker:        str
    description:   str
    asset_class:   AssetClass
    currency:      str           # ISO 4217
    exchange:      str           # MIC code  e.g. XNAS, XNYS, XLON
    tick_size:     float         # minimum price increment
    lot_size:      int           # minimum order quantity
    multiplier:    float         # contract multiplier (1 for equities)
    is_shortable:  bool          # locate available
    sector:        str
    country:       str           # ISO 3166-2
    spot_price:    float         # indicative mid (updated by FX/market data)
    dvd01:         float         # DV01 per $1m notional (bonds only, else 0)

    @property
    def notional(self) -> float:
        """Return notional per unit in USD (via spot price × multiplier)."""
        return self.spot_price * self.multiplier


@dataclass
class CounterpartyMaster:
    cp_id:       str
    name:        str
    lei:         str             # Legal Entity Identifier (20-char)
    credit_tier: CreditTier
    jurisdiction: str            # ISO 3166-2
    is_active:   bool = True
    margin_pct:  float = 0.10   # initial margin requirement


@dataclass
class AccountMaster:
    account_id:   str
    desk:         str            # e.g. "EquityArb", "CreditTrading"
    portfolio:    str
    trader:       str
    base_currency: str
    is_active:    bool = True
    is_proprietary: bool = True  # prop desk vs. agency


@dataclass
class LimitRecord:
    """Risk limits for an (account, instrument or asset_class) pair."""
    account_id:       str
    scope:            str        # instrument ISIN or AssetClass value or "*"
    max_position:     int        # shares / contracts
    max_notional_usd: float      # gross notional cap
    max_order_qty:    int        # single-order qty cap
    max_dv01_usd:     float      # DV01 limit (bonds)
    concentration_pct: float     # max % of ADV (average daily volume)
    daily_loss_limit: float      # stop-loss in USD


@dataclass
class HolidayCalendar:
    calendar_id: str             # e.g. "USD", "GBP", "XNYS"
    holidays:    set[date]       # non-settlement dates

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def settlement_date(self, trade_date: date, t_plus: int = 2) -> date:
        """Roll forward T+N, skipping holidays and weekends."""
        d = trade_date
        count = 0
        while count < t_plus:
            d += timedelta(days=1)
            if self.is_business_day(d):
                count += 1
        return d


# ─────────────────────────────────────────────────────────────────────────────
# 3. GBO REFERENCE DATA STORE  (in-memory golden source)
# ─────────────────────────────────────────────────────────────────────────────

class GBORefDataStore:
    """
    In-memory GBO store.  In production this wraps gRPC calls to the GBO
    service (Redis-backed) with a local L1 cache per process.

    All lookups are O(1) dict access — no DB round-trips during risk checks.
    """

    def __init__(self):
        self.instruments:    dict[str, Instrument]       = {}  # keyed by ISIN
        self.instruments_by_ticker: dict[str, Instrument] = {}
        self.counterparties: dict[str, CounterpartyMaster] = {}
        self.accounts:       dict[str, AccountMaster]    = {}
        self.limits:         list[LimitRecord]           = []
        self.calendars:      dict[str, HolidayCalendar]  = {}
        self.fx_rates:       dict[str, float]            = {}  # CCY/USD mid
        self._limit_index:   dict[tuple, list[LimitRecord]] = {}

        self._seed_instruments()
        self._seed_counterparties()
        self._seed_accounts()
        self._seed_limits()
        self._seed_calendars()
        self._seed_fx_rates()
        self._build_limit_index()

    # ── Lookup APIs ───────────────────────────────────────────────────────────

    def get_instrument(self, isin: str) -> Optional[Instrument]:
        return self.instruments.get(isin)

    def get_instrument_by_ticker(self, ticker: str) -> Optional[Instrument]:
        return self.instruments_by_ticker.get(ticker.upper())

    def get_counterparty(self, cp_id: str) -> Optional[CounterpartyMaster]:
        return self.counterparties.get(cp_id)

    def get_account(self, account_id: str) -> Optional[AccountMaster]:
        return self.accounts.get(account_id)

    def get_limits(self, account_id: str, isin: str,
                   asset_class: AssetClass) -> list[LimitRecord]:
        """Return all limit records that apply to this (account, instrument)."""
        keys = [
            (account_id, isin),
            (account_id, asset_class.value),
            (account_id, "*"),
        ]
        seen, result = set(), []
        for k in keys:
            for lr in self._limit_index.get(k, []):
                if id(lr) not in seen:
                    seen.add(id(lr))
                    result.append(lr)
        return result

    def fx_to_usd(self, amount: float, currency: str) -> float:
        """Convert amount in `currency` to USD using stored mid rates."""
        if currency == "USD":
            return amount
        rate = self.fx_rates.get(currency, 1.0)
        return amount * rate

    def settlement_date(self, trade_date: date,
                        currency: str = "USD", t_plus: int = 2) -> date:
        cal = self.calendars.get(currency, self.calendars["USD"])
        return cal.settlement_date(trade_date, t_plus)

    # ── Seed data ─────────────────────────────────────────────────────────────

    def _seed_instruments(self):
        raw = [
            # isin, cusip, ticker, desc, asset_class, ccy, exchange,
            # tick, lot, mult, shortable, sector, country, spot, dv01
            ("US0378331005", "037833100", "AAPL",  "Apple Inc",
             AssetClass.EQUITY, "USD", "XNAS", 0.01, 1, 1.0, True,
             "Technology", "US", 189.50, 0.0),
            ("US5949181045", "594918104", "MSFT",  "Microsoft Corp",
             AssetClass.EQUITY, "USD", "XNAS", 0.01, 1, 1.0, True,
             "Technology", "US", 415.20, 0.0),
            ("US67066G1040", "67066G104", "NVDA",  "NVIDIA Corp",
             AssetClass.EQUITY, "USD", "XNAS", 0.01, 1, 1.0, True,
             "Technology", "US", 875.40, 0.0),
            ("US0231351067", "023135106", "AMZN",  "Amazon.com Inc",
             AssetClass.EQUITY, "USD", "XNAS", 0.01, 1, 1.0, True,
             "ConsDisc", "US", 182.30, 0.0),
            ("US88160R1014", "88160R101", "TSLA",  "Tesla Inc",
             AssetClass.EQUITY, "USD", "XNAS", 0.01, 1, 1.0, True,
             "ConsDisc", "US", 175.80, 0.0),
            ("US78462F1030", "78462F103", "SPY",   "SPDR S&P 500 ETF",
             AssetClass.EQUITY, "USD", "XNYS", 0.01, 1, 1.0, True,
             "ETF", "US", 524.60, 0.0),
            ("GB0008706128", "000870612", "LLOYL", "Lloyds Banking Group",
             AssetClass.EQUITY, "GBP", "XLON", 0.001, 1, 1.0, True,
             "Financials", "GB", 0.538, 0.0),
            ("US78378X1072", "78378X107", "ES1",   "S&P 500 E-mini Futures",
             AssetClass.EQUITY, "USD", "XCME", 0.25, 1, 50.0, False,
             "Futures", "US", 5240.00, 0.0),
            ("US912828ZT04", "912828ZT0", "UST10", "US Treasury 10Y 4.625% 2026",
             AssetClass.FIXED_INCOME, "USD", "XNYC", 0.0001, 1000, 1.0, False,
             "Govt", "US", 98.45, 89.5),
            ("EU0009652759", "000965275", "EURUSD","EUR/USD Spot FX",
             AssetClass.FX, "EUR", "XOFF", 0.00001, 1000000, 1.0, True,
             "FX", "EU", 1.0842, 0.0),
        ]
        for r in raw:
            inst = Instrument(*r)
            self.instruments[inst.isin] = inst
            self.instruments_by_ticker[inst.ticker] = inst

    def _seed_counterparties(self):
        raw = [
            ("CP001", "Goldman Sachs",       "W22LROWP2IHZNBB6K528", CreditTier.TIER1, "US", 0.05),
            ("CP002", "Morgan Stanley",      "IGJSJL3JD5P30I6NJZ34", CreditTier.TIER1, "US", 0.05),
            ("CP003", "Barclays Capital",    "G5GSEF7VJP5I7OUK5573", CreditTier.TIER1, "GB", 0.06),
            ("CP004", "Deutsche Bank",       "7LTWFZYICNSX8D621K86", CreditTier.TIER2, "DE", 0.08),
            ("CP005", "Citadel Securities", "549300MLUDYVRQOOXS22", CreditTier.TIER2, "US", 0.07),
            ("CP006", "Virtu Financial",     "549300TKTMKB1L9GNF67", CreditTier.TIER3, "US", 0.10),
        ]
        for cp_id, name, lei, tier, juris, margin in raw:
            self.counterparties[cp_id] = CounterpartyMaster(
                cp_id=cp_id, name=name, lei=lei,
                credit_tier=tier, jurisdiction=juris, margin_pct=margin)

    def _seed_accounts(self):
        raw = [
            ("ACC-EQARB-01",  "EquityArb",     "StatArb-US",     "trader_a", "USD", True),
            ("ACC-EQARB-02",  "EquityArb",     "StatArb-EU",     "trader_b", "USD", True),
            ("ACC-CREDIT-01", "CreditTrading", "HY-Book",        "trader_c", "USD", True),
            ("ACC-MACRO-01",  "Macro",         "FX-Rates",       "trader_d", "USD", True),
            ("ACC-AGENCY-01", "AgencyExec",    "Client-Flow",    "trader_e", "USD", False),
        ]
        for acc_id, desk, port, trader, ccy, prop in raw:
            self.accounts[acc_id] = AccountMaster(
                account_id=acc_id, desk=desk, portfolio=port,
                trader=trader, base_currency=ccy, is_proprietary=prop)

    def _seed_limits(self):
        # Global limits per account (scope="*")
        global_limits = [
            ("ACC-EQARB-01",  "*", 500_000, 50_000_000,  50_000, 0,     5.0, 1_000_000),
            ("ACC-EQARB-02",  "*", 200_000, 20_000_000,  20_000, 0,     5.0,   500_000),
            ("ACC-CREDIT-01", "*",  50_000, 100_000_000, 10_000, 50000, 3.0,   750_000),
            ("ACC-MACRO-01",  "*", 100_000, 30_000_000,  30_000, 0,     4.0,   500_000),
            ("ACC-AGENCY-01", "*", 999_999, 200_000_000, 99_999, 0,    10.0, 5_000_000),
        ]
        for r in global_limits:
            acc, scope, pos, notl, ord_qty, dv01, conc, dloss = r
            self.limits.append(LimitRecord(
                account_id=acc, scope=scope,
                max_position=pos, max_notional_usd=notl,
                max_order_qty=ord_qty, max_dv01_usd=dv01,
                concentration_pct=conc, daily_loss_limit=dloss))

        # Instrument-specific tighter limits
        instrument_limits = [
            # EquityArb desk: tighter on TSLA (high vol)
            ("ACC-EQARB-01", "US88160R1014", 10_000, 2_000_000, 5_000,
             0, 1.0, 250_000),
            # Credit desk: specific bond limit
            ("ACC-CREDIT-01", "US912828ZT04", 10_000_000, 100_000_000,
             1_000_000, 50_000, 2.0, 500_000),
        ]
        for r in instrument_limits:
            acc, scope, pos, notl, ord_qty, dv01, conc, dloss = r
            self.limits.append(LimitRecord(
                account_id=acc, scope=scope,
                max_position=pos, max_notional_usd=notl,
                max_order_qty=ord_qty, max_dv01_usd=dv01,
                concentration_pct=conc, daily_loss_limit=dloss))

        # Asset-class limits
        ac_limits = [
            ("ACC-EQARB-01", AssetClass.EQUITY.value,
             500_000, 50_000_000, 50_000, 0, 5.0, 1_000_000),
            ("ACC-CREDIT-01", AssetClass.FIXED_INCOME.value,
             50_000_000, 500_000_000, 5_000_000, 200_000, 3.0, 2_000_000),
        ]
        for r in ac_limits:
            acc, scope, pos, notl, ord_qty, dv01, conc, dloss = r
            self.limits.append(LimitRecord(
                account_id=acc, scope=scope,
                max_position=pos, max_notional_usd=notl,
                max_order_qty=ord_qty, max_dv01_usd=dv01,
                concentration_pct=conc, daily_loss_limit=dloss))

    def _build_limit_index(self):
        """Index limits by (account_id, scope) for O(1) lookup."""
        for lr in self.limits:
            key = (lr.account_id, lr.scope)
            self._limit_index.setdefault(key, []).append(lr)

    def _seed_calendars(self):
        # US holidays 2024-2025 (abbreviated)
        us_holidays = {
            date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17),
            date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19),
            date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
            date(2025, 12, 25),
        }
        uk_holidays = {
            date(2025, 1, 1), date(2025, 4, 18), date(2025, 4, 21),
            date(2025, 5, 5), date(2025, 5, 26), date(2025, 8, 25),
            date(2025, 12, 25), date(2025, 12, 26),
        }
        self.calendars["USD"]  = HolidayCalendar("USD",  us_holidays)
        self.calendars["XNYS"] = HolidayCalendar("XNYS", us_holidays)
        self.calendars["GBP"]  = HolidayCalendar("GBP",  uk_holidays)
        self.calendars["XLON"] = HolidayCalendar("XLON", uk_holidays)

    def _seed_fx_rates(self):
        """Spot mid rates to USD (1 unit of CCY = N USD)."""
        self.fx_rates = {
            "USD": 1.0000,
            "EUR": 1.0842,
            "GBP": 1.2730,
            "JPY": 0.00648,
            "CHF": 1.1250,
            "CAD": 0.7380,
            "AUD": 0.6520,
            "HKD": 0.1282,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 4. PRE-TRADE RISK ENGINE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    order_id:    str
    account_id:  str
    cp_id:       str
    ticker:      str
    side:        OrderSide
    qty:         int
    limit_price: float
    asset_class: Optional[AssetClass] = None   # filled by risk engine


@dataclass
class RiskCheck:
    name:    str
    result:  RiskResult
    message: str


@dataclass
class PreTradeResult:
    order_id:    str
    verdict:     RiskResult
    checks:      list[RiskCheck]
    notional_usd: float
    latency_us:  float


class PreTradeRiskEngine:
    """
    Runs a series of GBO-backed checks before an order reaches the market.

    Check order mirrors real-world risk waterfall:
      1. Instrument eligibility   (GBO instrument master)
      2. Account / desk validity  (GBO account master)
      3. Counterparty credit      (GBO counterparty master)
      4. Order size vs limit      (GBO limit table)
      5. Notional vs limit        (GBO limit table + FX rates)
      6. Short-sell check         (GBO instrument is_shortable)
      7. Concentration check      (simplified ADV proxy)
      8. DV01 check               (bonds only, GBO limit table)
    """

    # Simplified ADV (average daily volume) proxy — in prod from market data
    _ADV_PROXY = {
        "AAPL": 55_000_000, "MSFT": 22_000_000, "NVDA": 42_000_000,
        "AMZN": 40_000_000, "TSLA": 85_000_000, "SPY":  80_000_000,
        "UST10": 200_000_000_000,
    }

    def __init__(self, gbo: GBORefDataStore):
        self.gbo = gbo

    def check(self, order: Order) -> PreTradeResult:
        t0 = time.perf_counter()
        checks: list[RiskCheck] = []
        verdict = RiskResult.PASS

        inst = self.gbo.get_instrument_by_ticker(order.ticker)
        acct = self.gbo.get_account(order.account_id)
        cp   = self.gbo.get_counterparty(order.cp_id)

        # ── 1. Instrument eligibility ────────────────────────────────────────
        if inst is None:
            checks.append(RiskCheck("InstrumentEligibility", RiskResult.REJECT,
                                    f"Ticker {order.ticker} not in GBO instrument master"))
            verdict = RiskResult.REJECT
        else:
            order.asset_class = inst.asset_class
            checks.append(RiskCheck("InstrumentEligibility", RiskResult.PASS,
                                    f"{order.ticker} ({inst.isin}) eligible on {inst.exchange}"))

        # ── 2. Account validity ──────────────────────────────────────────────
        if acct is None or not acct.is_active:
            checks.append(RiskCheck("AccountValidity", RiskResult.REJECT,
                                    f"Account {order.account_id} not active in GBO"))
            verdict = RiskResult.REJECT
        else:
            checks.append(RiskCheck("AccountValidity", RiskResult.PASS,
                                    f"Account {order.account_id} ({acct.desk}/{acct.portfolio}) active"))

        # ── 3. Counterparty credit ───────────────────────────────────────────
        if cp is None or not cp.is_active:
            checks.append(RiskCheck("CounterpartyCredit", RiskResult.REJECT,
                                    f"CP {order.cp_id} not active in GBO"))
            verdict = RiskResult.REJECT
        elif cp.credit_tier == CreditTier.TIER3:
            checks.append(RiskCheck("CounterpartyCredit", RiskResult.WARN,
                                    f"CP {cp.name} is Tier3 — elevated margin ({cp.margin_pct*100:.0f}%)"))
            if verdict == RiskResult.PASS:
                verdict = RiskResult.WARN
        else:
            checks.append(RiskCheck("CounterpartyCredit", RiskResult.PASS,
                                    f"CP {cp.name} ({cp.credit_tier.value}) credit OK"))

        # Stop further checks if instrument or account invalid
        if verdict == RiskResult.REJECT:
            elapsed = (time.perf_counter() - t0) * 1_000_000
            return PreTradeResult(order.order_id, verdict, checks, 0.0, elapsed)

        notional_usd = self.gbo.fx_to_usd(
            order.qty * order.limit_price, inst.currency)
        limits = self.gbo.get_limits(order.account_id, inst.isin, inst.asset_class)

        for lim in limits:
            scope_label = lim.scope if lim.scope != "*" else "global"

            # ── 4. Order qty vs limit ────────────────────────────────────────
            if order.qty > lim.max_order_qty:
                checks.append(RiskCheck(f"OrderQtyLimit[{scope_label}]",
                    RiskResult.REJECT,
                    f"qty {order.qty:,} > limit {lim.max_order_qty:,}"))
                verdict = RiskResult.REJECT
            else:
                checks.append(RiskCheck(f"OrderQtyLimit[{scope_label}]",
                    RiskResult.PASS,
                    f"qty {order.qty:,} ≤ limit {lim.max_order_qty:,}"))

            # ── 5. Notional vs limit ─────────────────────────────────────────
            if notional_usd > lim.max_notional_usd:
                checks.append(RiskCheck(f"NotionalLimit[{scope_label}]",
                    RiskResult.REJECT,
                    f"notional ${notional_usd:,.0f} > limit ${lim.max_notional_usd:,.0f}"))
                verdict = RiskResult.REJECT
            else:
                checks.append(RiskCheck(f"NotionalLimit[{scope_label}]",
                    RiskResult.PASS,
                    f"notional ${notional_usd:,.0f} ≤ limit ${lim.max_notional_usd:,.0f}"))

            # ── 8. DV01 (bonds) ──────────────────────────────────────────────
            if inst.asset_class == AssetClass.FIXED_INCOME and lim.max_dv01_usd > 0:
                order_dv01 = inst.dvd01 * notional_usd / 1_000_000
                if order_dv01 > lim.max_dv01_usd:
                    checks.append(RiskCheck(f"DV01Limit[{scope_label}]",
                        RiskResult.REJECT,
                        f"DV01 ${order_dv01:,.0f} > limit ${lim.max_dv01_usd:,.0f}"))
                    verdict = RiskResult.REJECT
                else:
                    checks.append(RiskCheck(f"DV01Limit[{scope_label}]",
                        RiskResult.PASS,
                        f"DV01 ${order_dv01:,.0f} ≤ limit ${lim.max_dv01_usd:,.0f}"))

        # ── 6. Short-sell check ──────────────────────────────────────────────
        if order.side == OrderSide.SELL and not inst.is_shortable:
            checks.append(RiskCheck("ShortSellEligibility", RiskResult.REJECT,
                                    f"{order.ticker} has no locate — short-sell blocked"))
            verdict = RiskResult.REJECT
        else:
            checks.append(RiskCheck("ShortSellEligibility", RiskResult.PASS,
                                    f"{order.ticker} shortable={inst.is_shortable}"))

        # ── 7. Concentration (% of ADV) ──────────────────────────────────────
        adv = self._ADV_PROXY.get(order.ticker, 10_000_000)
        conc_pct = (order.qty / adv) * 100 if adv > 0 else 0
        limit_conc = limits[0].concentration_pct if limits else 5.0
        if conc_pct > limit_conc:
            checks.append(RiskCheck("ConcentrationLimit",
                RiskResult.WARN if conc_pct < limit_conc * 2 else RiskResult.REJECT,
                f"order is {conc_pct:.2f}% of ADV (limit {limit_conc:.1f}%)"))
            if verdict == RiskResult.PASS:
                verdict = RiskResult.WARN
        else:
            checks.append(RiskCheck("ConcentrationLimit", RiskResult.PASS,
                                    f"{conc_pct:.3f}% of ADV ≤ {limit_conc:.1f}% limit"))

        elapsed = (time.perf_counter() - t0) * 1_000_000
        return PreTradeResult(order.order_id, verdict, checks, notional_usd, elapsed)


# ─────────────────────────────────────────────────────────────────────────────
# 5. POST-TRADE RISK ENGINE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Fill:
    fill_id:    str
    order_id:   str
    account_id: str
    ticker:     str
    side:       OrderSide
    qty:        int
    fill_price: float
    fill_time:  float   # epoch seconds


@dataclass
class Position:
    ticker:       str
    net_qty:      int     # positive = long, negative = short
    avg_price:    float
    realised_pnl: float
    market_price: float   # updated from GBO spot

    @property
    def unrealised_pnl(self) -> float:
        return self.net_qty * (self.market_price - self.avg_price)

    @property
    def total_pnl(self) -> float:
        return self.realised_pnl + self.unrealised_pnl

    @property
    def notional_usd(self) -> float:
        return abs(self.net_qty) * self.market_price


@dataclass
class PostTradeViolation:
    account_id: str
    ticker:     str
    check:      str
    severity:   RiskResult
    message:    str


class PostTradeRiskEngine:
    """
    Books fills into positions and runs post-trade checks after each fill.

    Checks:
      1. Position limit breach    (GBO limit table)
      2. Notional limit breach    (GBO limit table + FX)
      3. Daily P&L loss limit     (GBO limit table)
      4. Settlement date calc     (GBO holiday calendar)
      5. Wash-trade detection     (same account buy+sell same instrument ≤60s)
    """

    def __init__(self, gbo: GBORefDataStore):
        self.gbo       = gbo
        self.positions: dict[tuple, Position] = {}   # (account, ticker)
        self.fills:     list[Fill]            = []
        self.violations: list[PostTradeViolation] = []
        self.daily_pnl: dict[str, float]     = {}    # account → daily P&L

    def book_fill(self, fill: Fill) -> list[PostTradeViolation]:
        """Record a fill and run post-trade risk checks. Returns any violations."""
        self.fills.append(fill)
        inst = self.gbo.get_instrument_by_ticker(fill.ticker)
        market_price = inst.spot_price if inst else fill.fill_price

        key = (fill.account_id, fill.ticker)
        pos = self.positions.get(key)

        new_violations: list[PostTradeViolation] = []

        if pos is None:
            # New position
            signed_qty = fill.qty if fill.side == OrderSide.BUY else -fill.qty
            self.positions[key] = Position(
                ticker=fill.ticker, net_qty=signed_qty,
                avg_price=fill.fill_price, realised_pnl=0.0,
                market_price=market_price)
        else:
            signed_qty = fill.qty if fill.side == OrderSide.BUY else -fill.qty
            old_qty    = pos.net_qty
            new_qty    = old_qty + signed_qty

            if old_qty != 0 and (old_qty > 0) != (new_qty > 0):
                # Crossed zero — realise P&L on the closed portion
                closed = min(abs(old_qty), abs(signed_qty))
                pos.realised_pnl += closed * (fill.fill_price - pos.avg_price) * (1 if old_qty > 0 else -1)

            if new_qty == 0:
                pos.avg_price = 0.0
            elif (old_qty >= 0 and signed_qty > 0) or (old_qty <= 0 and signed_qty < 0):
                # Same direction — VWAP average
                pos.avg_price = (abs(old_qty) * pos.avg_price + abs(signed_qty) * fill.fill_price) / abs(new_qty)

            pos.net_qty      = new_qty
            pos.market_price = market_price

        pos = self.positions[key]

        # ── 5. Wash-trade detection ──────────────────────────────────────────
        window = 60  # seconds
        opposite = OrderSide.SELL if fill.side == OrderSide.BUY else OrderSide.BUY
        for f in reversed(self.fills[:-1]):
            if fill.fill_time - f.fill_time > window:
                break
            if (f.account_id == fill.account_id and
                    f.ticker == fill.ticker and f.side == opposite):
                v = PostTradeViolation(
                    account_id=fill.account_id, ticker=fill.ticker,
                    check="WashTradeDetection", severity=RiskResult.WARN,
                    message=f"Opposing fill {f.fill_id} on same instrument within {window}s — potential wash trade")
                new_violations.append(v)
                break

        if inst:
            limits = self.gbo.get_limits(fill.account_id, inst.isin, inst.asset_class)
            notional_usd = self.gbo.fx_to_usd(
                abs(pos.net_qty) * pos.market_price, inst.currency)

            for lim in limits:
                scope = lim.scope if lim.scope != "*" else "global"

                # ── 1. Position limit ────────────────────────────────────────
                if abs(pos.net_qty) > lim.max_position:
                    v = PostTradeViolation(
                        account_id=fill.account_id, ticker=fill.ticker,
                        check=f"PositionLimit[{scope}]", severity=RiskResult.REJECT,
                        message=f"Position {pos.net_qty:,} exceeds limit ±{lim.max_position:,}")
                    new_violations.append(v)

                # ── 2. Notional limit ────────────────────────────────────────
                if notional_usd > lim.max_notional_usd:
                    v = PostTradeViolation(
                        account_id=fill.account_id, ticker=fill.ticker,
                        check=f"NotionalLimit[{scope}]", severity=RiskResult.REJECT,
                        message=f"Notional ${notional_usd:,.0f} exceeds limit ${lim.max_notional_usd:,.0f}")
                    new_violations.append(v)

        # ── 3. Daily P&L loss limit ──────────────────────────────────────────
        acc_pnl = sum(p.total_pnl for (acc, _), p in self.positions.items()
                      if acc == fill.account_id)
        self.daily_pnl[fill.account_id] = acc_pnl
        lims_global = self.gbo.get_limits(
            fill.account_id, "", AssetClass.EQUITY)  # get global limits
        for lim in lims_global:
            if lim.scope == "*" and acc_pnl < -lim.daily_loss_limit:
                v = PostTradeViolation(
                    account_id=fill.account_id, ticker=fill.ticker,
                    check="DailyLossLimit", severity=RiskResult.REJECT,
                    message=f"Daily P&L ${acc_pnl:,.0f} breaches loss limit -${lim.daily_loss_limit:,.0f}")
                new_violations.append(v)

        self.violations.extend(new_violations)
        return new_violations

    def settlement_date_for(self, fill: Fill) -> date:
        inst = self.gbo.get_instrument_by_ticker(fill.ticker)
        ccy  = inst.currency if inst else "USD"
        return self.gbo.settlement_date(date.today(), ccy)

    def position_report(self) -> str:
        lines = [
            f"\n{'─'*80}",
            f"  POST-TRADE POSITION REPORT  ({date.today()})",
            f"{'─'*80}",
            f"  {'Account':<20} {'Ticker':<8} {'Net Qty':>10} {'Avg Px':>10}"
            f" {'Mkt Px':>10} {'Unreal P&L':>14} {'Real P&L':>14}",
            f"{'─'*80}",
        ]
        for (acc, tkr), p in sorted(self.positions.items()):
            lines.append(
                f"  {acc:<20} {tkr:<8} {p.net_qty:>10,} {p.avg_price:>10.2f}"
                f" {p.market_price:>10.2f} {p.unrealised_pnl:>14,.2f}"
                f" {p.realised_pnl:>14,.2f}")
        lines.append(f"{'─'*80}")
        if self.violations:
            lines.append(f"\n  VIOLATIONS ({len(self.violations)}):")
            for v in self.violations:
                tag = f"[{v.severity.value}]"
                lines.append(f"  {tag:<10} {v.account_id} | {v.check}: {v.message}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 6. DEMO
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_check(c: RiskCheck) -> str:
    icons = {RiskResult.PASS: "✓", RiskResult.WARN: "⚠", RiskResult.REJECT: "✗"}
    return f"    {icons[c.result]} [{c.result.value:<6}] {c.name}: {c.message}"


def run_demo():
    print("\n" + "═"*80)
    print("  GBO REFERENCE DATA — Pre-Trade & Post-Trade Risk Demo")
    print("═"*80)

    gbo = GBORefDataStore()
    print(f"\n  GBO loaded: {len(gbo.instruments)} instruments  "
          f"{len(gbo.counterparties)} counterparties  "
          f"{len(gbo.accounts)} accounts  "
          f"{len(gbo.limits)} limit records\n")

    pre_risk  = PreTradeRiskEngine(gbo)
    post_risk = PostTradeRiskEngine(gbo)

    # ── Pre-trade checks ──────────────────────────────────────────────────────
    orders = [
        Order("ORD-001", "ACC-EQARB-01",  "CP001", "AAPL",  OrderSide.BUY,   1_000,  189.50),
        Order("ORD-002", "ACC-EQARB-01",  "CP001", "TSLA",  OrderSide.SELL,  20_000, 175.80),  # qty breach on TSLA limit
        Order("ORD-003", "ACC-CREDIT-01", "CP003", "UST10", OrderSide.BUY,   500_000, 98.45),  # bond w/ DV01
        Order("ORD-004", "ACC-EQARB-01",  "CP006", "MSFT",  OrderSide.BUY,   500,    415.20),  # Tier3 CP → warn
        Order("ORD-005", "ACC-EQARB-01",  "CP001", "UNKNWN",OrderSide.BUY,   100,    50.00),   # unknown ticker → reject
        Order("ORD-006", "ACC-AGENCY-01", "CP002", "SPY",   OrderSide.BUY,   5_000,  524.60),  # agency desk, large notional
    ]

    print("  PRE-TRADE RISK CHECKS")
    print("  " + "─"*78)
    for order in orders:
        result = pre_risk.check(order)
        icon = {"PASS": "✓", "WARN": "⚠", "REJECT": "✗"}[result.verdict.value]
        print(f"\n  {icon} {result.order_id}  {order.ticker:<6} {order.side.value:<5}"
              f" qty={order.qty:>8,}  notional=${result.notional_usd:>12,.0f}"
              f"  verdict={result.verdict.value:<6}  latency={result.latency_us:.1f}µs")
        for c in result.checks:
            print(_fmt_check(c))

    # ── Post-trade: book fills & check positions ──────────────────────────────
    print("\n\n  POST-TRADE RISK — BOOKING FILLS")
    print("  " + "─"*78)

    fills = [
        Fill("FILL-001", "ORD-001", "ACC-EQARB-01",  "AAPL", OrderSide.BUY,   1_000, 189.40, time.time()),
        Fill("FILL-002", "ORD-001", "ACC-EQARB-01",  "AAPL", OrderSide.BUY,   2_000, 189.60, time.time() + 1),
        Fill("FILL-003", "ORD-006", "ACC-AGENCY-01", "SPY",  OrderSide.BUY,   5_000, 524.55, time.time() + 2),
        Fill("FILL-004", "ORD-006", "ACC-AGENCY-01", "SPY",  OrderSide.SELL,  3_000, 524.80, time.time() + 5),   # partial close
        Fill("FILL-005", "ORD-006", "ACC-AGENCY-01", "SPY",  OrderSide.SELL,  2_000, 524.85, time.time() + 8),   # close rest + wash?
        Fill("FILL-006", "ORD-004", "ACC-EQARB-01",  "MSFT", OrderSide.BUY, 500_000, 415.20, time.time() + 10), # position limit breach
    ]

    for fill in fills:
        viols = post_risk.book_fill(fill)
        sdate = post_risk.settlement_date_for(fill)
        print(f"\n  Booked {fill.fill_id}: {fill.ticker} {fill.side.value}"
              f" {fill.qty:,} @ ${fill.fill_price:.2f}  settle={sdate}")
        if viols:
            for v in viols:
                icon = "⚠" if v.severity == RiskResult.WARN else "✗"
                print(f"    {icon} [{v.severity.value}] {v.check}: {v.message}")

    print(post_risk.position_report())
    print("═"*80 + "\n")


if __name__ == "__main__":
    run_demo()
