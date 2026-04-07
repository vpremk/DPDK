"""
send_fix_orders.py — send FIX-over-UDP orders to en0 for dpdk_pcap to capture.

On macOS, traffic from localhost to 127.x goes via lo0, not en0.
To reach en0 (192.168.1.165) from the same machine we bind the socket
to en0's IP as source so the kernel routes it out en0.

Usage:
    # In terminal 1 (capture):
    sudo ./dpdk_pcap en0

    # In terminal 2 (send):
    python send_fix_orders.py                        # 10 orders, default rate
    python send_fix_orders.py --count 100 --rate 50  # 100 orders at 50/sec
    python send_fix_orders.py --dst 192.168.1.165    # explicit destination
"""

import socket
import time
import argparse
import random
import struct

# ── FIX port that dpdk_pcap BPF-filter listens on ─────────────────────────────
FIX_PORT    = 4567

def _local_en0_ip() -> str:
    """Auto-detect this machine's en0 IP at runtime."""
    import subprocess, re
    try:
        out = subprocess.check_output(["ipconfig", "getifaddr", "en0"],
                                      text=True).strip()
        if re.match(r"\d+\.\d+\.\d+\.\d+", out):
            return out
    except Exception:
        pass
    return ""   # fallback: let OS pick source

EN0_IP = _local_en0_ip()

# ── FIX message templates (SOH = \x01) ────────────────────────────────────────
SOH = "\x01"

SYMBOLS = ["AAPL", "MSFT", "AMZN", "NVDA", "TSLA", "SPY", "QQQ"]
SIDES   = [("1", "Buy"), ("2", "Sell")]

def fix_checksum(msg: str) -> str:
    """FIX checksum: sum of ASCII values mod 256, zero-padded to 3 digits."""
    return f"{sum(ord(c) for c in msg) % 256:03d}"

def build_fix_new_order(seq: int, symbol: str, side: str,
                        qty: int, price: float) -> bytes:
    """Build a minimal FIX 4.2 NewOrderSingle (MsgType=D)."""
    body = (
        f"35=D{SOH}"                         # MsgType: NewOrderSingle
        f"49=CLIENT{SOH}"                    # SenderCompID
        f"56=BROKER{SOH}"                    # TargetCompID
        f"34={seq}{SOH}"                     # MsgSeqNum
        f"52={time.strftime('%Y%m%d-%H:%M:%S')}{SOH}"  # SendingTime
        f"11=ORD{seq:06d}{SOH}"              # ClOrdID
        f"55={symbol}{SOH}"                  # Symbol
        f"54={side}{SOH}"                    # Side (1=Buy 2=Sell)
        f"38={qty}{SOH}"                     # OrderQty
        f"44={price:.2f}{SOH}"               # Price
        f"40=2{SOH}"                         # OrdType: Limit
        f"59=0{SOH}"                         # TimeInForce: Day
    )
    header = (
        f"8=FIX.4.2{SOH}"
        f"9={len(body)}{SOH}"
    )
    full = header + body
    return (full + f"10={fix_checksum(full)}{SOH}").encode()

def build_fix_cancel(seq: int, orig_seq: int, symbol: str) -> bytes:
    """Build a FIX OrderCancelRequest (MsgType=F)."""
    body = (
        f"35=F{SOH}"
        f"49=CLIENT{SOH}"
        f"56=BROKER{SOH}"
        f"34={seq}{SOH}"
        f"52={time.strftime('%Y%m%d-%H:%M:%S')}{SOH}"
        f"11=CXL{seq:06d}{SOH}"
        f"41=ORD{orig_seq:06d}{SOH}"         # OrigClOrdID
        f"55={symbol}{SOH}"
        f"54=1{SOH}"
    )
    header = f"8=FIX.4.2{SOH}9={len(body)}{SOH}"
    full   = header + body
    return (full + f"10={fix_checksum(full)}{SOH}").encode()

# ── Sender ─────────────────────────────────────────────────────────────────────

def send_orders(dst: str, port: int, count: int, rate: float, verbose: bool):
    """
    Send `count` FIX orders to dst:port at `rate` packets/sec.
    Binds source to EN0_IP so traffic is routed through en0 (not lo0).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Bind source to en0 IP — forces kernel to route via en0
    try:
        sock.bind((EN0_IP, 0))
        print(f"[+] Bound source to {EN0_IP} (en0)")
    except OSError as e:
        print(f"[!] Could not bind to {EN0_IP}: {e}")
        print(f"    Packets may go via lo0 instead of en0")

    interval = 1.0 / rate if rate > 0 else 0
    print(f"[+] Sending {count} FIX orders → {dst}:{port}  rate={rate:.0f}/s")
    print(f"    (run 'sudo ./dpdk_pcap en0' in another terminal)\n")

    sent = 0
    start = time.perf_counter()

    for seq in range(1, count + 1):
        symbol = random.choice(SYMBOLS)
        side_code, side_name = random.choice(SIDES)
        qty    = random.randint(100, 10000)
        price  = round(random.uniform(50, 500), 2)

        # alternate between NewOrderSingle and CancelRequest
        if seq % 5 == 0 and seq > 1:
            msg = build_fix_cancel(seq, seq - 1, symbol)
            tag = "CancelReq"
        else:
            msg = build_fix_new_order(seq, symbol, side_code, qty, price)
            tag = f"NewOrder {side_name:4s}"

        sock.sendto(msg, (dst, port))
        sent += 1

        if verbose:
            print(f"  seq={seq:4d}  {tag}  {symbol}  qty={qty}  px={price:.2f}"
                  f"  len={len(msg)}B")

        if interval > 0:
            next_send = start + seq * interval
            sleep_for = next_send - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)

    elapsed = time.perf_counter() - start
    print(f"\n[+] Sent {sent} packets in {elapsed:.3f}s "
          f"({sent/elapsed:.0f} pps actual)")
    sock.close()

# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Send FIX-over-UDP orders to en0")
    ap.add_argument("--dst",     default=EN0_IP,  help="destination IP (default: en0 IP)")
    ap.add_argument("--port",    type=int, default=FIX_PORT, help="UDP port (default: 4567)")
    ap.add_argument("--count",   type=int, default=10,   help="number of orders")
    ap.add_argument("--rate",    type=float, default=5,  help="orders per second (0=max)")
    ap.add_argument("--verbose", action="store_true",    help="print each message")
    args = ap.parse_args()

    send_orders(args.dst, args.port, args.count, args.rate, args.verbose)

if __name__ == "__main__":
    main()
