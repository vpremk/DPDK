"""
send_market_data.py — UDP market data feed sender (Mac A)
==========================================================
Serialises MarketDataEnvelope protobuf messages and sends them as raw UDP
packets on port 5678 so dpdk_pcap on Mac B captures and decodes them.

Wire format (each datagram):
  [1 byte  MsgType enum]
  [4 bytes big-endian proto length]
  [N bytes protobuf-serialised MarketDataEnvelope]

This lets the C receiver branch on msg_type without deserialising the full
envelope — identical to how SIP / OPRA / ITCH feed handlers work.

Usage:
  python send_market_data.py                         # mixed feed, 5 msg/s
  python send_market_data.py --type nbbo --rate 100
  python send_market_data.py --type trade --count 200
  python send_market_data.py --type book --depth 5
  python send_market_data.py --dst 192.168.1.165

Requires:
  pip install protobuf grpcio-tools

  # Generate the Python stubs from the proto (run once):
  python -m grpc_tools.protoc -I. --python_out=. market_data.proto
  # → market_data_pb2.py

Fallback:
  If market_data_pb2 is missing (proto not compiled yet), the script falls
  back to a hand-rolled binary encoder so you can still test the wire format.
"""

from __future__ import annotations

import argparse
import random
import socket
import struct
import subprocess
import sys
import time

from env import EMS_HOST, MKTDATA_PORT as _MKTDATA_PORT
from dataclasses import dataclass, field
from typing import Optional

# ── Try to import generated protobuf stubs ────────────────────────────────────
try:
    import market_data_pb2 as pb          # type: ignore
    PROTO_AVAILABLE = True
except ImportError:
    PROTO_AVAILABLE = False
    print("[warn] market_data_pb2 not found — using fallback binary encoder")
    print("       Run: python -m grpc_tools.protoc -I. --python_out=. market_data.proto\n")

# ── Constants ──────────────────────────────────────────────────────────────────
MKTDATA_PORT  = _MKTDATA_PORT
HEADER_FMT    = "!BI"      # 1-byte MsgType + 4-byte big-endian length
HEADER_SIZE   = struct.calcsize(HEADER_FMT)   # = 5

# MsgType enum values (must match market_data.proto)
MSG_NBBO            = 1
MSG_TRADE           = 2
MSG_ORDER_BOOK      = 3
MSG_ORDER_BOOK_DELTA = 4
MSG_IMBALANCE       = 5
MSG_STATUS          = 6
MSG_HEARTBEAT       = 7

SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "SPY", "QQQ", "META", "GOOG", "NFLX"]
EXCHANGES = ["XNAS", "XNYS", "ARCX", "BATS", "EDGX", "IEXG"]

# ─────────────────────────────────────────────────────────────────────────────
# WIRE ENCODING
# ─────────────────────────────────────────────────────────────────────────────

def _encode(msg_type: int, proto_bytes: bytes) -> bytes:
    """Prepend the 5-byte dispatch header to the serialised proto."""
    return struct.pack(HEADER_FMT, msg_type, len(proto_bytes)) + proto_bytes


# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK ENCODER  (used when proto stubs are not compiled)
# Produces valid-enough binary to test the C dispatcher without grpcio.
# ─────────────────────────────────────────────────────────────────────────────

def _varint(v: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    out = []
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)

def _field_bytes(field_num: int, data: bytes) -> bytes:
    tag = _varint((field_num << 3) | 2)    # wire type 2 = length-delimited
    return tag + _varint(len(data)) + data

def _field_str(field_num: int, s: str) -> bytes:
    return _field_bytes(field_num, s.encode())

def _field_double(field_num: int, v: float) -> bytes:
    tag = _varint((field_num << 3) | 1)    # wire type 1 = 64-bit
    return tag + struct.pack("<d", v)

def _field_uint32(field_num: int, v: int) -> bytes:
    tag = _varint(field_num << 3)           # wire type 0 = varint
    return tag + _varint(v)

def _field_uint64(field_num: int, v: int) -> bytes:
    return _field_uint32(field_num, v)      # varint handles uint64 too


class FallbackEncoder:
    """Minimal hand-rolled protobuf for NBBOUpdate, TradePrint, Heartbeat."""

    @staticmethod
    def nbbo(symbol: str, bid: float, ask: float, bid_sz: int, ask_sz: int,
             bid_exch: str, ask_exch: str, seq: int) -> bytes:
        ts = time.time_ns()
        inner = (
            _field_str(1, symbol)        +   # symbol
            _field_uint64(2, ts)         +   # exchange_ts
            _field_uint64(3, ts)         +   # recv_ts
            _field_double(4, bid)        +   # bid_px
            _field_uint32(5, bid_sz)     +   # bid_sz
            _field_str(6, bid_exch)      +   # bid_exch
            _field_double(7, ask)        +   # ask_px
            _field_uint32(8, ask_sz)     +   # ask_sz
            _field_str(9, ask_exch)      +   # ask_exch
            _field_double(10, (bid+ask)/2) + # mid
            _field_double(11, ask - bid) +   # spread
            _field_uint32(12, seq)       +   # seq
            _field_str(13, "SIP_UTP")        # feed_id
        )
        # Wrap in envelope: msg_type=1 (field 1), seq=seq (field 2), payload oneof nbbo (field 10)
        envelope = (
            _field_uint32(1, MSG_NBBO)   +
            _field_uint32(2, seq)        +
            _field_uint64(3, ts)         +
            _field_bytes(10, inner)          # oneof nbbo
        )
        return _encode(MSG_NBBO, envelope)

    @staticmethod
    def trade(symbol: str, price: float, size: int, exchange: str, seq: int) -> bytes:
        ts = time.time_ns()
        inner = (
            _field_str(1, symbol)        +
            _field_uint64(2, ts)         +
            _field_uint64(3, ts)         +
            _field_double(4, price)      +
            _field_uint32(5, size)       +
            _field_uint32(6, 1)          +   # aggressor = BUY
            _field_uint32(7, 0)          +   # condition = REGULAR
            _field_str(8, exchange)      +
            _field_uint32(11, seq)       +
            _field_str(12, "CTA")
        )
        envelope = (
            _field_uint32(1, MSG_TRADE)  +
            _field_uint32(2, seq)        +
            _field_uint64(3, ts)         +
            _field_bytes(11, inner)
        )
        return _encode(MSG_TRADE, envelope)

    @staticmethod
    def heartbeat(seq: int) -> bytes:
        ts = time.time_ns()
        inner = (
            _field_str(1, "SIP_UTP")     +
            _field_uint64(2, ts)         +
            _field_uint32(3, seq)
        )
        envelope = (
            _field_uint32(1, MSG_HEARTBEAT) +
            _field_uint32(2, seq)           +
            _field_uint64(3, ts)            +
            _field_bytes(16, inner)
        )
        return _encode(MSG_HEARTBEAT, envelope)


# ─────────────────────────────────────────────────────────────────────────────
# PROTOBUF ENCODER  (used when market_data_pb2 is available)
# ─────────────────────────────────────────────────────────────────────────────

class ProtoEncoder:
    @staticmethod
    def nbbo(symbol: str, bid: float, ask: float, bid_sz: int, ask_sz: int,
             bid_exch: str, ask_exch: str, seq: int) -> bytes:
        ts = time.time_ns()
        env = pb.MarketDataEnvelope(
            msg_type=pb.MSG_NBBO,
            seq=seq,
            send_ts=ts,
            nbbo=pb.NBBOUpdate(
                symbol=symbol,
                exchange_ts=ts,
                recv_ts=ts,
                bid_px=bid, bid_sz=bid_sz, bid_exch=bid_exch,
                ask_px=ask, ask_sz=ask_sz, ask_exch=ask_exch,
                mid=(bid + ask) / 2,
                spread=ask - bid,
                seq=seq,
                feed_id="SIP_UTP",
            ),
        )
        return _encode(MSG_NBBO, env.SerializeToString())

    @staticmethod
    def trade(symbol: str, price: float, size: int, exchange: str, seq: int) -> bytes:
        ts = time.time_ns()
        env = pb.MarketDataEnvelope(
            msg_type=pb.MSG_TRADE,
            seq=seq,
            send_ts=ts,
            trade=pb.TradePrint(
                symbol=symbol,
                exchange_ts=ts,
                recv_ts=ts,
                price=price,
                size=size,
                aggressor=pb.SIDE_BUY,
                condition=pb.COND_REGULAR,
                exchange=exchange,
                seq=seq,
                feed_id="CTA",
            ),
        )
        return _encode(MSG_TRADE, env.SerializeToString())

    @staticmethod
    def book_snapshot(symbol: str, mid: float, depth: int, seq: int) -> bytes:
        ts = time.time_ns()
        tick = 0.01
        bids = [pb.PriceLevel(price=round(mid - tick * (i+1), 2),
                              size=random.randint(100, 5000),
                              orders=random.randint(1, 10))
                for i in range(depth)]
        asks = [pb.PriceLevel(price=round(mid + tick * (i+1), 2),
                              size=random.randint(100, 5000),
                              orders=random.randint(1, 10))
                for i in range(depth)]
        env = pb.MarketDataEnvelope(
            msg_type=pb.MSG_ORDER_BOOK,
            seq=seq,
            send_ts=ts,
            book=pb.OrderBookSnapshot(
                symbol=symbol,
                exchange_ts=ts,
                recv_ts=ts,
                bids=bids,
                asks=asks,
                depth=depth,
                seq=seq,
                feed_id="PITCH",
            ),
        )
        return _encode(MSG_ORDER_BOOK, env.SerializeToString())

    @staticmethod
    def book_delta(symbol: str, side: int, action: int,
                   price: float, size: int, seq: int) -> bytes:
        ts = time.time_ns()
        env = pb.MarketDataEnvelope(
            msg_type=pb.MSG_ORDER_BOOK_DELTA,
            seq=seq,
            send_ts=ts,
            delta=pb.OrderBookDelta(
                symbol=symbol,
                exchange_ts=ts,
                recv_ts=ts,
                side=side,
                action=action,
                price=price,
                size=size,
                seq=seq,
                feed_id="PITCH",
            ),
        )
        return _encode(MSG_ORDER_BOOK_DELTA, env.SerializeToString())

    @staticmethod
    def heartbeat(seq: int) -> bytes:
        ts = time.time_ns()
        env = pb.MarketDataEnvelope(
            msg_type=pb.MSG_HEARTBEAT,
            seq=seq,
            send_ts=ts,
            heartbeat=pb.Heartbeat(feed_id="SIP_UTP", sender_ts=ts, seq=seq),
        )
        return _encode(MSG_HEARTBEAT, env.SerializeToString())


# ─────────────────────────────────────────────────────────────────────────────
# MARKET SIMULATION  — realistic price walk
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InstrumentState:
    symbol: str
    mid: float
    bid_sz: int = 500
    ask_sz: int = 400
    cum_vol: int = 0

    def tick(self, rng: random.Random) -> None:
        """Random walk: ±0.01 each step, small drift toward mid."""
        self.mid = round(self.mid + rng.gauss(0, 0.05), 2)
        self.mid = max(1.0, self.mid)
        self.bid_sz = rng.randint(100, 5000)
        self.ask_sz = rng.randint(100, 5000)

    @property
    def bid(self) -> float:
        return round(self.mid - 0.01, 2)

    @property
    def ask(self) -> float:
        return round(self.mid + 0.01, 2)


# ─────────────────────────────────────────────────────────────────────────────
# SENDER
# ─────────────────────────────────────────────────────────────────────────────

def _local_en0_ip() -> str:
    try:
        out = subprocess.check_output(["ipconfig", "getifaddr", "en0"], text=True).strip()
        import re
        if re.match(r"\d+\.\d+\.\d+\.\d+", out):
            return out
    except Exception:
        pass
    return ""


def send_feed(dst: str, port: int, msg_type: str, count: int,
              rate: float, depth: int, verbose: bool) -> None:
    enc = ProtoEncoder() if PROTO_AVAILABLE else FallbackEncoder()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    src_ip = _local_en0_ip()
    if src_ip:
        try:
            sock.bind((src_ip, 0))
            print(f"[+] Bound source to {src_ip} (en0)")
        except OSError as e:
            print(f"[!] Could not bind to {src_ip}: {e}")

    rng = random.Random(42)
    states = {sym: InstrumentState(sym, rng.uniform(50, 500)) for sym in SYMBOLS}

    interval = 1.0 / rate if rate > 0 else 0
    encoder_label = "protobuf" if PROTO_AVAILABLE else "fallback-binary"
    print(f"[+] Sending {count} market-data msgs → {dst}:{port}"
          f"  type={msg_type}  rate={rate:.0f}/s  encoder={encoder_label}\n")

    seq = 0
    start = time.perf_counter()
    bytes_sent = 0
    msg_counts: dict[str, int] = {}

    for i in range(count):
        seq += 1
        sym = rng.choice(SYMBOLS)
        st  = states[sym]
        st.tick(rng)
        exch = rng.choice(EXCHANGES)

        # ── choose message type ───────────────────────────────────────────
        if msg_type == "nbbo" or (msg_type == "mixed" and i % 3 == 0):
            payload = enc.nbbo(sym, st.bid, st.ask,
                               st.bid_sz, st.ask_sz,
                               exch, rng.choice(EXCHANGES), seq)
            tag = "NBBO"

        elif msg_type == "trade" or (msg_type == "mixed" and i % 3 == 1):
            trade_px = round(rng.uniform(st.bid, st.ask), 2)
            trade_sz = rng.randint(100, 10_000)
            st.cum_vol += trade_sz
            payload = enc.trade(sym, trade_px, trade_sz, exch, seq)
            tag = "TRADE"

        elif msg_type == "book":
            if PROTO_AVAILABLE:
                payload = enc.book_snapshot(sym, st.mid, depth, seq)
            else:
                payload = enc.nbbo(sym, st.bid, st.ask,
                                   st.bid_sz, st.ask_sz, exch, exch, seq)
            tag = "BOOK"

        elif msg_type == "delta":
            if PROTO_AVAILABLE:
                side   = rng.choice([pb.BOOK_BID, pb.BOOK_ASK])
                action = rng.choice([pb.DELTA_ADD, pb.DELTA_MODIFY, pb.DELTA_DELETE])
                px     = round(st.mid + rng.uniform(-0.05, 0.05), 2)
                sz     = 0 if action == pb.DELTA_DELETE else rng.randint(100, 2000)
                payload = enc.book_delta(sym, side, action, px, sz, seq)
            else:
                payload = enc.nbbo(sym, st.bid, st.ask,
                                   st.bid_sz, st.ask_sz, exch, exch, seq)
            tag = "DELTA"

        elif msg_type == "heartbeat" or (msg_type == "mixed" and i % 3 == 2):
            payload = enc.heartbeat(seq)
            tag = "HB"

        else:
            payload = enc.heartbeat(seq)
            tag = "HB"

        sock.sendto(payload, (dst, port))
        bytes_sent += len(payload)
        msg_counts[tag] = msg_counts.get(tag, 0) + 1

        if verbose:
            print(f"  seq={seq:5d}  {tag:<6}  {sym:<6}  {len(payload):4d}B")

        # ── rate limiting ─────────────────────────────────────────────────
        if interval > 0:
            nxt = start + seq * interval
            slp = nxt - time.perf_counter()
            if slp > 0:
                time.sleep(slp)

    elapsed = time.perf_counter() - start
    pps = seq / elapsed if elapsed > 0 else 0
    mbps = bytes_sent * 8 / elapsed / 1e6 if elapsed > 0 else 0

    print(f"\n[+] Sent {seq:,} msgs in {elapsed:.3f}s"
          f"  ({pps:.0f} msg/s  {mbps:.2f} Mbps)")
    print(f"    breakdown: {dict(sorted(msg_counts.items()))}")
    print(f"    total bytes: {bytes_sent:,}")
    sock.close()


# ─────────────────────────────────────────────────────────────────────────────
# DECODER  (for testing: receive and print decoded messages)
# ─────────────────────────────────────────────────────────────────────────────

def run_decoder(port: int) -> None:
    """
    Receive UDP datagrams on `port` and decode the wire format.
    Useful to verify the sender without dpdk_pcap running.
    """
    if not PROTO_AVAILABLE:
        print("[!] Decoder requires protobuf — pip install protobuf grpcio-tools")
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", port))
    sock.settimeout(5.0)
    print(f"[decoder] Listening on UDP port {port} …  Ctrl-C to stop\n")

    MSG_NAMES = {
        MSG_NBBO: "NBBO", MSG_TRADE: "TRADE", MSG_ORDER_BOOK: "BOOK",
        MSG_ORDER_BOOK_DELTA: "DELTA", MSG_IMBALANCE: "IMBAL",
        MSG_STATUS: "STATUS", MSG_HEARTBEAT: "HB",
    }

    try:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue

            if len(data) < HEADER_SIZE:
                print(f"[!] Short packet {len(data)}B from {addr}")
                continue

            msg_type, proto_len = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])
            proto_bytes = data[HEADER_SIZE:HEADER_SIZE + proto_len]

            env = pb.MarketDataEnvelope()
            env.ParseFromString(proto_bytes)

            name = MSG_NAMES.get(msg_type, f"?{msg_type}")
            recv_ns = time.time_ns()

            if msg_type == MSG_NBBO:
                n = env.nbbo
                lag_us = (recv_ns - n.exchange_ts) / 1000
                print(f"  {name:<6} seq={env.seq:<6} {n.symbol:<6}"
                      f"  bid={n.bid_px:.4f}x{n.bid_sz}"
                      f"  ask={n.ask_px:.4f}x{n.ask_sz}"
                      f"  spread={n.spread*100:.2f}¢"
                      f"  lag={lag_us:.1f}µs")

            elif msg_type == MSG_TRADE:
                t = env.trade
                lag_us = (recv_ns - t.exchange_ts) / 1000
                print(f"  {name:<6} seq={env.seq:<6} {t.symbol:<6}"
                      f"  px={t.price:.4f}  sz={t.size}"
                      f"  exch={t.exchange}"
                      f"  lag={lag_us:.1f}µs")

            elif msg_type == MSG_ORDER_BOOK:
                b = env.book
                print(f"  {name:<6} seq={env.seq:<6} {b.symbol:<6}"
                      f"  depth={b.depth}"
                      f"  best_bid={b.bids[0].price if b.bids else '-'}"
                      f"  best_ask={b.asks[0].price if b.asks else '-'}")

            elif msg_type == MSG_ORDER_BOOK_DELTA:
                d = env.delta
                actions = {0: "ADD", 1: "MOD", 2: "DEL"}
                sides   = {0: "BID", 1: "ASK"}
                print(f"  {name:<6} seq={env.seq:<6} {d.symbol:<6}"
                      f"  {sides.get(d.side,'?')}"
                      f"  {actions.get(d.action,'?')}"
                      f"  px={d.price:.4f}  sz={d.size}")

            elif msg_type == MSG_HEARTBEAT:
                h = env.heartbeat
                lag_us = (recv_ns - h.sender_ts) / 1000
                print(f"  {name:<6} seq={env.seq:<6} feed={h.feed_id}"
                      f"  lag={lag_us:.1f}µs")

            else:
                print(f"  {name:<6} seq={env.seq:<6} {len(proto_bytes)}B")

    except KeyboardInterrupt:
        print("\n[decoder] stopped")
    finally:
        sock.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Send protobuf market-data UDP feed (NBBO / Trade / Book / Delta)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python send_market_data.py                          # mixed feed, 10 msg/s to en0
  python send_market_data.py --type nbbo --rate 1000  # NBBO flood
  python send_market_data.py --type book --depth 10   # L2 snapshots
  python send_market_data.py --type delta --count 500 # incremental updates
  python send_market_data.py --mode decode            # receive + print decoded msgs
  python send_market_data.py --dst 192.168.1.165      # send to Mac B
""")
    ap.add_argument("--mode",    choices=["send", "decode"], default="send")
    ap.add_argument("--dst",  default=None,
                    help="EMS IP (env: EMS_HOST / MAC_B_IP, default: auto-detect en0)")
    ap.add_argument("--port", type=int, default=MKTDATA_PORT,
                    help="UDP port (env: MKTDATA_PORT, default: 5678)")
    ap.add_argument("--type",    default="mixed",
                    choices=["mixed", "nbbo", "trade", "book", "delta", "heartbeat"],
                    help="Message type to send")
    ap.add_argument("--count",   type=int,   default=50)
    ap.add_argument("--rate",    type=float, default=10.0,
                    help="Messages per second (0 = max rate)")
    ap.add_argument("--depth",   type=int,   default=5,
                    help="Order book depth levels (book/delta mode)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.mode == "decode":
        run_decoder(args.port)
        return

    dst = args.dst or EMS_HOST or _local_en0_ip() or "127.0.0.1"
    send_feed(dst, args.port, args.type, args.count,
              args.rate, args.depth, args.verbose)


if __name__ == "__main__":
    main()
