"""
trader_ui.py — Trader Terminal (Textual TUI)
=============================================
Two trading modes selectable at runtime:

  MANUAL  — trader fills in symbol / side / qty / price and hits F9
  ALGO    — fires a burst loop in a background thread at a configurable rate

Both modes build FIX 4.2 NewOrderSingle (MsgType=D) messages and send them
as raw UDP datagrams to the EMS (dpdk_pcap) on port 4567.

EMS destination is read from environment / .env:
    EMS_HOST  — Mac B IP   (default: MAC_B_IP from .env)
    EMS_PORT  — FIX port   (default: FIX_PORT from .env, typically 4567)

Usage:
<<<<<<< HEAD
<<<<<<< HEAD
=======
>>>>>>> 3df878c03f14b2784762481c6c1396d8175f5551
    pip install textual
    python client/trader_ui.py
    python client/trader_ui.py --dst $EMS_HOST
    python client/trader_ui.py --mode algo

Key bindings:
    Tab / t   toggle MANUAL ↔ ALGO
    F9        submit (MANUAL) / start algo (ALGO)
    F10       stop algo
    Ctrl+C    cancel highlighted blotter row
    Ctrl+Q    quit
<<<<<<< HEAD
=======
  pip install textual
  python client/trader_ui.py
  python client/trader_ui.py --dst 192.X.Y.X --port 4567
  python client/trader_ui.py --mode algo --rate 5 --count 50
>>>>>>> f879d07 (chore: cleanup)
=======
>>>>>>> 3df878c03f14b2784762481c6c1396d8175f5551
"""

from __future__ import annotations

import argparse
import random
import socket
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from env import EMS_HOST, FIX_PORT as _DEFAULT_FIX_PORT

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import (
    Button, DataTable, Footer, Header,
    Input, Label, RadioButton, RadioSet,
    Select, Static, Switch,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

SOH     = "\x01"
SYMBOLS = ["AAPL", "MSFT", "AMZN", "NVDA", "TSLA", "SPY", "QQQ", "META", "GOOG", "NFLX"]

ORDER_TYPES = [("Limit", "2"), ("Market", "1"), ("Stop", "3"), ("Stop-Limit", "4")]
TIF_OPTIONS = [("Day", "0"), ("GTC", "1"), ("IOC", "3"), ("FOK", "4")]

BLOTTER_COLS = ["#", "ClOrdID", "Symbol", "Side", "Qty", "Price", "Type", "Status", "µs"]


class Mode(Enum):
    MANUAL = "MANUAL"
    ALGO   = "ALGO"


# ─────────────────────────────────────────────────────────────────────────────
# FIX BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _checksum(msg: str) -> str:
    return f"{sum(ord(c) for c in msg) % 256:03d}"

def build_new_order(seq: int, symbol: str, side: str, qty: int,
                    price: float, ord_type: str = "2", tif: str = "0") -> bytes:
    body = (
        f"35=D{SOH}49=TRADER{SOH}56=EMS{SOH}"
        f"34={seq}{SOH}"
        f"52={time.strftime('%Y%m%d-%H:%M:%S')}{SOH}"
        f"11=ORD{seq:06d}{SOH}"
        f"55={symbol}{SOH}54={side}{SOH}"
        f"38={qty}{SOH}44={price:.2f}{SOH}"
        f"40={ord_type}{SOH}59={tif}{SOH}"
    )
    hdr  = f"8=FIX.4.2{SOH}9={len(body)}{SOH}"
    full = hdr + body
    return (full + f"10={_checksum(full)}{SOH}").encode()

def build_cancel(seq: int, orig_cl_ord_id: str, symbol: str, side: str) -> bytes:
    body = (
        f"35=F{SOH}49=TRADER{SOH}56=EMS{SOH}"
        f"34={seq}{SOH}"
        f"52={time.strftime('%Y%m%d-%H:%M:%S')}{SOH}"
        f"11=CXL{seq:06d}{SOH}"
        f"41={orig_cl_ord_id}{SOH}"
        f"55={symbol}{SOH}54={side}{SOH}"
    )
    hdr  = f"8=FIX.4.2{SOH}9={len(body)}{SOH}"
    full = hdr + body
    return (full + f"10={_checksum(full)}{SOH}").encode()


# ─────────────────────────────────────────────────────────────────────────────
# ORDER RECORD
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OrderRecord:
    seq:       int
    cl_ord_id: str
    symbol:    str
    side:      str        # "Buy" / "Sell"
    qty:       int
    price:     float
    ord_type:  str        # label e.g. "Limit"
    status:    str = "SENT"
    sent_us:   int = 0

    @property
    def side_code(self) -> str:
        return "1" if self.side == "Buy" else "2"


# ─────────────────────────────────────────────────────────────────────────────
# UDP SOCKET
# ─────────────────────────────────────────────────────────────────────────────

class FIXSocket:
    def __init__(self, dst: str, port: int):
        self.dst  = dst
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, payload: bytes) -> int:
        t0 = time.perf_counter_ns()
        self._sock.sendto(payload, (self.dst, self.port))
        return (time.perf_counter_ns() - t0) // 1_000

    def close(self) -> None:
        self._sock.close()


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

APP_CSS = """
Screen { layout: vertical; }

#mode-bar {
    height: 3; layout: horizontal; align: center middle;
    background: $surface-darken-1; padding: 0 2;
}
#mode-bar Label { margin-right: 2; }
#ems-info { dock: right; color: $text-muted; }

#body { layout: horizontal; height: 1fr; }

#manual-panel, #algo-panel {
    width: 38; height: 100%; border: solid $primary-darken-2; padding: 1 2;
}
#algo-panel  { border: solid $accent-darken-2; }

#manual-panel Label, #algo-panel Label { margin-bottom: 0; color: $text-muted; }
#manual-panel Input,  #algo-panel Input  { margin-bottom: 1; }
#manual-panel Select, #algo-panel Select { margin-bottom: 1; }

#submit-btn    { width: 100%; margin-top: 1; }
#algo-btn-row  { height: 3; margin-top: 1; }
#algo-start-btn { width: 48%; }
#algo-stop-btn  { width: 48%; }
#algo-progress { height: 3; margin-top: 1; color: $success; }

#blotter-panel { border: solid $surface-lighten-2; padding: 0 1; height: 100%; }
#order-table   { height: 1fr; }
"""


# ─────────────────────────────────────────────────────────────────────────────
# PANELS
# ─────────────────────────────────────────────────────────────────────────────

class ManualPanel(Vertical):
    def compose(self) -> ComposeResult:
        yield Label("── Manual Order Entry ──")
        yield Label("Symbol")
        yield Select([(s, s) for s in SYMBOLS], value=SYMBOLS[0], id="sym-select")
        yield Label("Side")
        yield RadioSet(
            RadioButton("Buy",  value=True,  id="side-buy"),
            RadioButton("Sell", value=False, id="side-sell"),
            id="side-radio",
        )
        yield Label("Quantity")
        yield Input(value="100", placeholder="shares", id="qty-input",   restrict=r"[0-9]*")
        yield Label("Price")
        yield Input(value="100.00", placeholder="0.00", id="price-input", restrict=r"[0-9]*\.?[0-9]*")
        yield Label("Order Type")
        yield Select([(l, c) for l, c in ORDER_TYPES], value="2", id="ordtype-select")
        yield Label("Time In Force")
        yield Select([(l, c) for l, c in TIF_OPTIONS], value="0", id="tif-select")
        yield Button("Submit  [F9]", variant="primary", id="submit-btn")


class AlgoPanel(Vertical):
    def compose(self) -> ComposeResult:
        yield Label("── Algo Engine ──")
        yield Label("Symbol universe")
        yield Select([("All symbols", "ALL")] + [(s, s) for s in SYMBOLS],
                     value="ALL", id="algo-sym-select")
        yield Label("Rate  (orders / sec)")
        yield Input(value="5",  placeholder="e.g. 10",  id="algo-rate-input",  restrict=r"[0-9]*\.?[0-9]*")
        yield Label("Count  (0 = unlimited)")
        yield Input(value="20", placeholder="e.g. 100", id="algo-count-input", restrict=r"[0-9]*")
        yield Label("Side bias")
        yield Select([("Random", "random"), ("Buy only", "buy"), ("Sell only", "sell")],
                     value="random", id="algo-side-select")
        yield Label("Cancel every Nth  (0 = off)")
        yield Input(value="5", placeholder="0", id="algo-cancel-nth-input", restrict=r"[0-9]*")
        with Horizontal(id="algo-btn-row"):
            yield Button("▶ Start  [F9]", variant="success", id="algo-start-btn")
            yield Button("■ Stop  [F10]", variant="error",   id="algo-stop-btn")
        yield Static("", id="algo-progress")

    def on_mount(self) -> None:
        self.query_one("#algo-stop-btn", Button).disabled = True


class BlotterPanel(Vertical):
    def compose(self) -> ComposeResult:
        yield Label("── Order Blotter  (Ctrl+C = cancel selected) ──")
        yield DataTable(id="order-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        for col in BLOTTER_COLS:
            tbl.add_column(col, key=col)


# ─────────────────────────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────────────────────────

class TraderApp(App):
    CSS = APP_CSS

    BINDINGS = [
        Binding("t",      "toggle_mode",  "Toggle Mode",    show=True),
        Binding("tab",    "toggle_mode",  "Toggle Mode",    show=False),
        Binding("f9",     "submit",       "Submit / Start", show=True),
        Binding("f10",    "stop_algo",    "Stop Algo",      show=True),
        Binding("ctrl+c", "cancel_order", "Cancel Order",   show=True),
        Binding("ctrl+q", "quit",         "Quit",           show=True),
    ]

    mode: reactive[Mode] = reactive(Mode.MANUAL)
    seq:  reactive[int]  = reactive(0)

    def __init__(self, dst: str, port: int, start_mode: Mode):
        super().__init__()
        self.dst          = dst
        self.port         = port
        self._fix         = FIXSocket(dst, port)
        self._orders:     list[OrderRecord] = []
        self._algo_stop   = threading.Event()
        self._algo_thread: Optional[threading.Thread] = None
        self.mode         = start_mode

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="mode-bar"):
            yield Label("Mode:")
            yield Switch(value=(self.mode == Mode.ALGO), id="mode-switch")
            yield Label("MANUAL", id="lbl-manual")
            yield Label(" / ")
            yield Label("ALGO",   id="lbl-algo")
            yield Label(f"EMS → {self.dst}:{self.port}", id="ems-info")
        with Horizontal(id="body"):
            if self.mode == Mode.MANUAL:
                yield ManualPanel(id="manual-panel")
            else:
                yield AlgoPanel(id="algo-panel")
            yield BlotterPanel(id="blotter-panel")
        yield Footer()

    def on_mount(self) -> None:
        self.title     = "Trader Terminal"
        self.sub_title = f"FIX 4.2  EMS {self.dst}:{self.port}"

    # ── Mode toggle ───────────────────────────────────────────────────────────

    def action_toggle_mode(self) -> None:
        if self._algo_running():
            self.notify("Stop algo before switching mode", severity="warning")
            return
        self.mode = Mode.ALGO if self.mode == Mode.MANUAL else Mode.MANUAL
        self._swap_left_panel()
        try:
            self.query_one("#mode-switch", Switch).value = (self.mode == Mode.ALGO)
        except NoMatches:
            pass

    @on(Switch.Changed, "#mode-switch")
    def _on_switch(self, event: Switch.Changed) -> None:
        if self._algo_running():
            self.notify("Stop algo before switching mode", severity="warning")
            event.switch.value = True
            return
        self.mode = Mode.ALGO if event.value else Mode.MANUAL
        self._swap_left_panel()

    def _swap_left_panel(self) -> None:
        body = self.query_one("#body", Horizontal)
        for sel in ("#manual-panel", "#algo-panel"):
            try:
                body.query_one(sel).remove()
            except NoMatches:
                pass
        blotter = body.query_one("#blotter-panel")
        if self.mode == Mode.MANUAL:
            body.mount(ManualPanel(id="manual-panel"), before=blotter)
        else:
            body.mount(AlgoPanel(id="algo-panel"),     before=blotter)

    # ── Submit dispatcher ─────────────────────────────────────────────────────

    def action_submit(self) -> None:
        if self.mode == Mode.MANUAL:
            self._submit_manual()
        else:
            self._start_algo()

    # ── Manual submit ─────────────────────────────────────────────────────────

    def _submit_manual(self) -> None:
        try:
            symbol   = str(self.query_one("#sym-select",    Select).value)
            radio    = self.query_one("#side-radio",         RadioSet)
            side     = "Buy" if radio.pressed_index == 0 else "Sell"
            side_c   = "1"   if side == "Buy"          else "2"
            qty      = int(self.query_one("#qty-input",      Input).value.strip() or "0")
            price    = float(self.query_one("#price-input",  Input).value.strip() or "0")
            ord_type = str(self.query_one("#ordtype-select", Select).value)
            tif      = str(self.query_one("#tif-select",     Select).value)
        except (NoMatches, ValueError) as e:
            self.notify(f"Form error: {e}", severity="error")
            return

        if qty <= 0 or price <= 0:
            self.notify("Qty and Price must be > 0", severity="warning")
            return

        ord_lbl = next(l for l, c in ORDER_TYPES if c == ord_type)
        self.seq += 1
        payload  = build_new_order(self.seq, symbol, side_c, qty, price, ord_type, tif)
        lat_us   = self._fix.send(payload)

        rec = OrderRecord(self.seq, f"ORD{self.seq:06d}", symbol,
                          side, qty, price, ord_lbl, "SENT", lat_us)
        self._add_to_blotter(rec)
        self.notify(f"✓ {side} {qty} {symbol} @ {price:.2f}  ({lat_us} µs)",
                    severity="information")

    @on(Button.Pressed, "#submit-btn")
    def _on_submit_btn(self) -> None:
        self._submit_manual()

    # ── Algo burst ────────────────────────────────────────────────────────────

    def _start_algo(self) -> None:
        if self._algo_running():
            self.notify("Algo already running", severity="warning")
            return
        try:
            sym_val    = str(self.query_one("#algo-sym-select",    Select).value)
            rate       = float(self.query_one("#algo-rate-input",   Input).value or "5")
            count      = int(self.query_one("#algo-count-input",    Input).value or "0")
            side_val   = str(self.query_one("#algo-side-select",    Select).value)
            cancel_nth = int(self.query_one("#algo-cancel-nth-input", Input).value or "0")
        except (NoMatches, ValueError):
            return

        sym_pool = SYMBOLS if sym_val == "ALL" else [sym_val]
        try:
            self.query_one("#algo-start-btn", Button).disabled = True
            self.query_one("#algo-stop-btn",  Button).disabled = False
        except NoMatches:
            pass

        self._algo_stop.clear()
        self._algo_thread = threading.Thread(
            target=self._algo_loop,
            args=(sym_pool, rate, count, side_val, cancel_nth),
            daemon=True,
        )
        self._algo_thread.start()

    def _algo_loop(self, sym_pool: list[str], rate: float,
                   count: int, side_val: str, cancel_nth: int) -> None:
        rng      = random.Random()
        interval = 1.0 / rate if rate > 0 else 0
        sent     = 0
        start    = time.perf_counter()

        while not self._algo_stop.is_set():
            if count > 0 and sent >= count:
                break
            sent  += 1
            symbol = rng.choice(sym_pool)

            if side_val == "buy":
                side, side_c = "Buy",  "1"
            elif side_val == "sell":
                side, side_c = "Sell", "2"
            else:
                side, side_c = rng.choice([("Buy", "1"), ("Sell", "2")])

            qty   = rng.randint(100, 5000)
            price = round(rng.uniform(50, 500), 2)

            self.seq += 1
            seq = self.seq

            if cancel_nth > 0 and sent % cancel_nth == 0 and sent > 1:
                orig = f"ORD{(seq - 1):06d}"
                payload = build_cancel(seq, orig, symbol, side_c)
                lat_us  = self._fix.send(payload)
                rec = OrderRecord(seq, f"CXL{seq:06d}", symbol,
                                  side, qty, price, "Cancel", "SENT", lat_us)
            else:
                payload = build_new_order(seq, symbol, side_c, qty, price)
                lat_us  = self._fix.send(payload)
                rec = OrderRecord(seq, f"ORD{seq:06d}", symbol,
                                  side, qty, price, "Limit", "SENT", lat_us)

            self.call_from_thread(self._add_to_blotter, rec)
            self.call_from_thread(self._update_algo_progress, sent, count, rate)

            if interval > 0:
                slp = (start + sent * interval) - time.perf_counter()
                if slp > 0:
                    time.sleep(slp)

        self.call_from_thread(self._algo_finished)

    def _algo_finished(self) -> None:
        try:
            self.query_one("#algo-start-btn", Button).disabled = False
            self.query_one("#algo-stop-btn",  Button).disabled = True
            self.query_one("#algo-progress",  Static).update("■ Algo finished")
        except NoMatches:
            pass

    def _update_algo_progress(self, sent: int, total: int, rate: float) -> None:
        try:
            label = f"▶ {sent}" + (f"/{total}" if total > 0 else "") + \
                    f"  @ {rate:.0f}/s  seq={self.seq}"
            self.query_one("#algo-progress", Static).update(label)
        except NoMatches:
            pass

    def action_stop_algo(self) -> None:
        self._algo_stop.set()

    @on(Button.Pressed, "#algo-start-btn")
    def _on_algo_start(self) -> None:
        self._start_algo()

    @on(Button.Pressed, "#algo-stop-btn")
    def _on_algo_stop(self) -> None:
        self.action_stop_algo()

    def _algo_running(self) -> bool:
        return self._algo_thread is not None and self._algo_thread.is_alive()

    # ── Cancel selected blotter row ───────────────────────────────────────────

    def action_cancel_order(self) -> None:
        try:
            tbl = self.query_one("#order-table", DataTable)
        except NoMatches:
            return
        if not self._orders or tbl.cursor_row < 0:
            return

        rec = self._orders[tbl.cursor_row]
        if rec.status in ("CANCELLED", "CANCEL→EMS"):
            self.notify(f"{rec.cl_ord_id} already cancelled", severity="warning")
            return

        self.seq += 1
        lat_us = self._fix.send(build_cancel(self.seq, rec.cl_ord_id,
                                             rec.symbol, rec.side_code))
        cancel_rec = OrderRecord(self.seq, f"CXL{self.seq:06d}", rec.symbol,
                                 rec.side, rec.qty, rec.price, "Cancel", "SENT", lat_us)
        rec.status = "CANCEL→EMS"
        self._add_to_blotter(cancel_rec)
        self._refresh_statuses()
        self.notify(f"↩ Cancel sent for {rec.cl_ord_id}", severity="warning")

    # ── Blotter ───────────────────────────────────────────────────────────────

    def _add_to_blotter(self, rec: OrderRecord) -> None:
        self._orders.append(rec)
        try:
            tbl = self.query_one("#order-table", DataTable)
        except NoMatches:
            return
        side_txt = (f"[green]{rec.side}[/green]" if rec.side == "Buy"
                    else f"[red]{rec.side}[/red]")
        tbl.add_row(
            str(rec.seq), rec.cl_ord_id, rec.symbol, side_txt,
            str(rec.qty), f"{rec.price:.2f}", rec.ord_type,
            rec.status, str(rec.sent_us),
            key=str(rec.seq),
        )
        tbl.move_cursor(row=tbl.row_count - 1)

    def _refresh_statuses(self) -> None:
        try:
            tbl = self.query_one("#order-table", DataTable)
            for rec in self._orders:
                try:
                    tbl.update_cell(str(rec.seq), "Status", rec.status)
                except Exception:
                    pass
        except NoMatches:
            pass

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def on_unmount(self) -> None:
        self._algo_stop.set()
        self._fix.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Trader TUI — FIX 4.2 to EMS via UDP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (set in .env):
  EMS_HOST   — EMS IP  (default: MAC_B_IP)
  EMS_PORT   — FIX UDP port  (default: FIX_PORT = 4567)
""")
    ap.add_argument("--dst",  default=EMS_HOST,
                    help="EMS IP (env: EMS_HOST, default from .env)")
    ap.add_argument("--port", type=int, default=_DEFAULT_FIX_PORT,
                    help="FIX UDP port (env: FIX_PORT, default: 4567)")
    ap.add_argument("--mode", choices=["manual", "algo"], default="manual")
    args = ap.parse_args()

    TraderApp(
        dst=args.dst,
        port=args.port,
        start_mode=Mode.ALGO if args.mode == "algo" else Mode.MANUAL,
    ).run()


if __name__ == "__main__":
    main()
