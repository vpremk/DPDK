"""
multicast_gateway.py — IP Multicast order fan-out for the DPDK trading pipeline
================================================================================
Sits after the pre-trade risk engine and publishes approved orders to a UDP
multicast group so multiple downstream consumers (EMS nodes, market-access
gateways, surveillance systems) all receive the same order flow simultaneously
with no unicast replication overhead.

Production analogy:
  - CME Globex MDP3 / OPRA: exchange publishes market data once to multicast;
    every subscriber's NIC hardware-copies the datagram.
  - SOR fan-out: approved order sent once; co-located matching-engine adapters
    at NYSE, NASDAQ, BATS all receive it in the same network tick.

Concepts implemented:
  1. MulticastEnvelope  — lightweight wire format (struct.pack, no protobuf)
  2. MulticastGateway   — sender: setsockopt IP_MULTICAST_TTL/LOOP, publish()
  3. MulticastReceiver  — receiver: IP_ADD_MEMBERSHIP, SO_REUSEPORT, gap detection
  4. Sequence gap detection — mirrors how production feed handlers detect lost datagrams

macOS single-machine notes:
  - IP_MULTICAST_LOOP=1 on the SENDER socket → loopback delivery to all receivers
  - SO_REUSEPORT (not just SO_REUSEADDR) required for N receivers on same port
  - Receivers bind to ("", port), not to the group address — macOS convention
  - IP_ADD_MEMBERSHIP with INADDR_ANY selects lo0 for 239.x.x.x on loopback
"""

import socket
import struct
import time
import threading
import statistics
import sys
import os
from dataclasses import dataclass, field
from typing import Optional

# Allow importing dpdk_sim types when run standalone
sys.path.insert(0, os.path.dirname(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS  (mirror DPDK-style naming from dpdk_sim.py)
# ─────────────────────────────────────────────────────────────────────────────

MCAST_GROUP         = "239.1.1.1"   # RFC 2365 admin-scoped — won't escape subnet
MCAST_PORT          = 7777
MCAST_TTL           = 1             # TTL=1: stay on local subnet
MCAST_LOOPBACK      = 1             # IP_MULTICAST_LOOP=1: required for macOS loopback
RECEIVER_RING_SIZE  = 512           # RingBuffer capacity per receiver
MAX_DGRAM_SIZE      = 4096          # max UDP payload accepted by receivers

# Wire header layout: seq_no(4B) + send_ts_ns(8B) + verdict(1B) + fix_len(2B)
_HDR_FMT  = "!IQBh"
_HDR_SIZE = struct.calcsize(_HDR_FMT)   # 15 bytes

_VERDICT_PASS = 0
_VERDICT_WARN = 1


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — re-serialize FIXMsg → FIX wire bytes for envelope packing
# ─────────────────────────────────────────────────────────────────────────────

def _encode_fix(fix) -> bytes:
    """
    Re-serialize a FIXMsg dataclass to FIX 4.2 SOH-delimited bytes.
    Mirrors the tag structure used in PacketGenerator._build_packet().
    The receiver calls FIXMsg.parse() on these bytes to reconstruct the order.
    """
    SOH = "\x01"
    body = (
        f"8=FIX.4.2{SOH}"
        f"35={fix.msg_type}{SOH}"
        f"49={fix.sender}{SOH}"
        f"56={fix.target}{SOH}"
        f"11={fix.order_id}{SOH}"
        f"55={fix.symbol}{SOH}"
        f"54={fix.side}{SOH}"
        f"38={fix.qty:.0f}{SOH}"
        f"44={fix.price:.2f}{SOH}"
    )
    return body.encode()


# ─────────────────────────────────────────────────────────────────────────────
# 1. MULTICAST ENVELOPE — on-wire datagram format
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MulticastEnvelope:
    """
    Lightweight datagram envelope prepended to every multicast publish.

    Wire layout (15-byte fixed header + variable FIX payload):
    ┌──────────┬──────────────────┬─────────┬─────────┬──────────────┐
    │ seq_no   │   send_ts_ns     │ verdict │ fix_len │   fix_raw    │
    │  4 bytes │     8 bytes      │  1 byte │ 2 bytes │  N bytes     │
    └──────────┴──────────────────┴─────────┴─────────┴──────────────┘

    seq_no     — monotonically increasing per-gateway counter; receivers use
                 this to detect dropped datagrams (gap detection).
    send_ts_ns — gateway send timestamp (time.perf_counter_ns); receiver
                 subtracts this to compute one-way multicast latency.
    verdict    — 0=PASS, 1=WARN (REJECT orders are never published)
    fix_len    — byte length of the FIX payload that follows
    fix_raw    — FIX 4.2 SOH-delimited bytes; parsed by FIXMsg.parse()
    """
    seq_no    : int
    send_ts_ns: int
    verdict   : int   # _VERDICT_PASS or _VERDICT_WARN
    fix_raw   : bytes

    def pack(self) -> bytes:
        hdr = struct.pack(_HDR_FMT,
                          self.seq_no,
                          self.send_ts_ns,
                          self.verdict,
                          len(self.fix_raw))
        return hdr + self.fix_raw

    @classmethod
    def unpack(cls, data: bytes) -> "MulticastEnvelope":
        if len(data) < _HDR_SIZE:
            raise ValueError(f"Truncated envelope: {len(data)} < {_HDR_SIZE} bytes")
        seq_no, send_ts_ns, verdict, fix_len = struct.unpack_from(_HDR_FMT, data)
        fix_raw = data[_HDR_SIZE: _HDR_SIZE + fix_len]
        if len(fix_raw) != fix_len:
            raise ValueError(f"FIX payload truncated: got {len(fix_raw)}, expected {fix_len}")
        return cls(seq_no, send_ts_ns, verdict, fix_raw)


# ─────────────────────────────────────────────────────────────────────────────
# 2. MULTICAST GATEWAY — sender
# ─────────────────────────────────────────────────────────────────────────────

class MulticastGateway:
    """
    Publishes risk-approved FIX orders to an IP multicast group.

    Only PASS and WARN verdicts are published — REJECT orders are silently
    dropped here (they never reach the market).

    Socket setup (sender side):
      setsockopt(IPPROTO_IP, IP_MULTICAST_TTL,  1)   ← stay on subnet
      setsockopt(IPPROTO_IP, IP_MULTICAST_LOOP, 1)   ← loopback delivery (macOS)
    The sender does NOT join the group — only receivers do.

    Production analogy: exchange matching engine sends market data once to
    239.x.x.x; every participant's NIC delivers a hardware copy. No unicast
    replication, no O(N) sender CPU cost.
    """

    def __init__(self,
                 group   : str  = MCAST_GROUP,
                 port    : int  = MCAST_PORT,
                 ttl     : int  = MCAST_TTL,
                 loopback: bool = True):
        self.group = group
        self.port  = port

        # ── Create UDP send socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # IP_MULTICAST_TTL: how many router hops the datagram may traverse.
        # TTL=1 means the datagram dies at the first router — local subnet only.
        self._sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
            struct.pack("b", ttl))

        # IP_MULTICAST_LOOP: on macOS this is a sender-socket option.
        # Setting it to 1 ensures the sender's own host receives the multicast
        # (required for single-machine demo where sender and receivers share lo0).
        self._sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP,
            struct.pack("b", 1 if loopback else 0))

        # Sequence counter — incremented before every publish()
        self._seq: int = 0

        # Stats
        self.n_sent          : int       = 0
        self.n_bytes_sent    : int       = 0
        self.n_rejected_skip : int       = 0   # REJECT verdicts silently dropped
        self.n_errors        : int       = 0
        self.n_receivers     : int       = 0
        self._send_lats_ns   : list[int] = []  # gateway publish latencies

        print(f"  [mcast-gw] Sender socket → {group}:{port}  TTL={ttl}  loopback={loopback}")

    def publish(self, order, result, proc) -> bool:
        """
        Publish a risk-approved order to the multicast group.

        order  — GBOOrder from PreTradeRiskGateway.evaluate()
        result — PreTradeResult (verdict must be PASS or WARN)
        proc   — ProcessedOrder (contains the original FIXMsg)

        Returns True on successful sendto(), False on error.
        REJECT verdicts increment n_rejected_skip and return False immediately.
        """
        # Import here to avoid circular dependency when run standalone
        try:
            from gbo_ref_data import RiskResult
        except ImportError:
            RiskResult = None

        # Only approved orders reach the market
        if RiskResult and result.verdict == RiskResult.REJECT:
            self.n_rejected_skip += 1
            return False

        self._seq += 1
        t0 = time.perf_counter_ns()

        verdict_byte = (_VERDICT_WARN
                        if RiskResult and result.verdict == RiskResult.WARN
                        else _VERDICT_PASS)

        fix_raw = _encode_fix(proc.fix_msg)
        envelope = MulticastEnvelope(
            seq_no     = self._seq,
            send_ts_ns = t0,
            verdict    = verdict_byte,
            fix_raw    = fix_raw,
        )

        data = envelope.pack()
        try:
            self._sock.sendto(data, (self.group, self.port))
            lat_ns = time.perf_counter_ns() - t0
            self._send_lats_ns.append(lat_ns)
            self.n_sent       += 1
            self.n_bytes_sent += len(data)
            return True
        except OSError as e:
            self.n_errors += 1
            return False

    def register_receiver(self) -> None:
        """Called by each MulticastReceiver at init to track join count."""
        self.n_receivers += 1

    def stats_line(self) -> str:
        lats = self._send_lats_ns[-10_000:] if self._send_lats_ns else [0]
        p99  = int(sorted(lats)[int(len(lats) * 0.99)]) if len(lats) > 1 else lats[0]
        return (
            f"Multicast GW [{self.group}:{self.port}]: "
            f"sent={self.n_sent:,}  bytes={self.n_bytes_sent:,}  "
            f"receivers={self.n_receivers}  errors={self.n_errors}  "
            f"p99_lat={p99//1000}µs"
        )

    def close(self) -> None:
        self._sock.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. MULTICAST RECEIVER — subscriber thread
# ─────────────────────────────────────────────────────────────────────────────

class MulticastReceiver:
    """
    Subscribes to the multicast group and feeds received envelopes into a
    downstream RingBuffer — simulating a remote EMS node, a market-access
    gateway, or a drop-copy surveillance system.

    Each receiver runs in its own daemon thread (one thread = one lcore in
    real DPDK). In production, each physical server would run this on a
    dedicated core pinned via isolcpus/rte_eal_remote_launch.

    Socket setup (receiver side):
      setsockopt(SOL_SOCKET,  SO_REUSEPORT, 1)         ← multiple receivers, same port
      bind(("", MCAST_PORT))                           ← bind to all interfaces
      setsockopt(IPPROTO_IP, IP_ADD_MEMBERSHIP, mreq)  ← join multicast group
      settimeout(0.1)                                  ← non-blocking poll for shutdown

    Gap detection: every datagram carries a seq_no. The receiver tracks the
    last seen seq_no and counts any jumps as dropped datagrams. This mirrors
    how production feed handlers detect gaps in OPRA, CME Globex MDP3, and
    NYSE Integrated Feed before triggering a retransmission request.
    """

    def __init__(self,
                 receiver_id    : int,
                 downstream_ring,          # RingBuffer from dpdk_sim
                 group          : str  = MCAST_GROUP,
                 port           : int  = MCAST_PORT,
                 gateway        : Optional[MulticastGateway] = None):
        self.receiver_id = receiver_id
        self.group       = group
        self.port        = port
        self._ring       = downstream_ring
        self._running    = False
        self._thread     : Optional[threading.Thread] = None

        # Stats
        self.n_received       : int = 0
        self.n_bytes_received : int = 0
        self.n_seq_gaps       : int = 0   # sum of missing seq_nos detected
        self.n_parse_errors   : int = 0
        self._last_seq        : int = 0

        # ── Create and configure receive socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # SO_REUSEPORT allows multiple receivers to bind the same (addr, port).
        # On macOS, REUSEPORT (not REUSEADDR) is required for multicast receivers.
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        # Bind to all interfaces on the multicast port.
        # On macOS, bind("", port) — NOT to the group address itself.
        self._sock.bind(("", port))

        # IP_ADD_MEMBERSHIP: instruct the OS to join the multicast group.
        # mreq = group_addr(4B) + local_interface(4B, INADDR_ANY = 0.0.0.0)
        # On macOS with INADDR_ANY the OS picks lo0 for 239.x.x.x traffic.
        mreq = struct.pack("4sL",
                           socket.inet_aton(group),
                           socket.INADDR_ANY)
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        # Non-blocking timeout so the recv_loop can check _running cleanly
        self._sock.settimeout(0.1)

        if gateway:
            gateway.register_receiver()

        print(f"  [mcast-rx-{receiver_id}] Joined {group}:{port} → ring '{downstream_ring.name}'")

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(
            target=self._recv_loop,
            name=f"mcast-rx-{self.receiver_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self._sock.close()
        except OSError:
            pass

    def _recv_loop(self) -> None:
        """
        Tight receive loop — equivalent to a dedicated lcore in DPDK.

        On each iteration:
          1. recvfrom() — blocks up to 0.1s (settimeout) then continues
          2. MulticastEnvelope.unpack() — parse header + FIX payload
          3. Gap detection — compare seq_no to _last_seq + 1
          4. FIXMsg.parse() — reconstruct order from FIX bytes
          5. ProcessedOrder wrapping — queue_id=-1 marks multicast-originated
          6. ring.enqueue() — hand off to downstream consumer
        """
        # Import FIXMsg and ProcessedOrder from dpdk_sim at thread start
        # (avoids circular import at module load time)
        try:
            from dpdk_sim import FIXMsg, ProcessedOrder
        except ImportError:
            # Fallback when run standalone (ProcessedOrder unavailable)
            FIXMsg = None
            ProcessedOrder = None

        while self._running:
            try:
                data, _addr = self._sock.recvfrom(MAX_DGRAM_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break

            # ── Unpack envelope
            try:
                env = MulticastEnvelope.unpack(data)
            except ValueError:
                self.n_parse_errors += 1
                continue

            # ── Gap detection: have we missed any sequence numbers?
            if self._last_seq > 0 and env.seq_no != self._last_seq + 1:
                gap = env.seq_no - (self._last_seq + 1)
                if gap > 0:
                    self.n_seq_gaps += gap
            self._last_seq = env.seq_no

            # ── Re-parse FIX payload
            if FIXMsg is None:
                self.n_received       += 1
                self.n_bytes_received += len(data)
                continue

            fix = FIXMsg.parse(env.fix_raw)
            if fix is None:
                self.n_parse_errors += 1
                continue

            # ── Wrap as ProcessedOrder (queue_id=-1 = multicast-originated)
            if ProcessedOrder is not None:
                lat_ns = time.perf_counter_ns() - env.send_ts_ns
                proc   = ProcessedOrder(
                    fix_msg    = fix,
                    queue_id   = -1,
                    latency_ns = lat_ns,
                    timestamp  = time.perf_counter_ns(),
                )
                self._ring.enqueue(proc)

            self.n_received       += 1
            self.n_bytes_received += len(data)

    def stats_line(self) -> str:
        return (
            f"Mcast RX-{self.receiver_id}: "
            f"received={self.n_received:,}  "
            f"bytes={self.n_bytes_received:,}  "
            f"gaps={self.n_seq_gaps}  "
            f"parse_err={self.n_parse_errors}  "
            f"ring_depth={self._ring.count}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE DEMO — run as: python ems/multicast_gateway.py
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Standalone receiver demo.
    Run dpdk_sim.py in another terminal to generate orders:
        python ems/dpdk_sim.py

    This process joins the multicast group and prints every approved order
    it receives, with one-way latency and sequence number.
    """
    import collections

    # Minimal RingBuffer shim so MulticastReceiver works without dpdk_sim
    class _Ring:
        name = "standalone_rx"
        count = 0
        def enqueue(self, item): self.count += 1
        def dequeue_burst(self, n): return []

    ring = _Ring()
    received = []

    # Patch _recv_loop to collect envelopes directly (FIXMsg unavailable)
    class _DemoReceiver(MulticastReceiver):
        def _recv_loop(self):
            while self._running:
                try:
                    data, addr = self._sock.recvfrom(MAX_DGRAM_SIZE)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    env = MulticastEnvelope.unpack(data)
                except ValueError as e:
                    print(f"  [!] parse error: {e}")
                    self.n_parse_errors += 1
                    continue

                if self._last_seq > 0 and env.seq_no != self._last_seq + 1:
                    gap = env.seq_no - (self._last_seq + 1)
                    if gap > 0:
                        self.n_seq_gaps += gap
                        print(f"  [!] GAP detected: seq {self._last_seq} → {env.seq_no} ({gap} missing)")
                self._last_seq = env.seq_no

                lat_us = (time.perf_counter_ns() - env.send_ts_ns) / 1000
                verdict_str = "PASS" if env.verdict == _VERDICT_PASS else "WARN"
                fix_preview = env.fix_raw[:60].decode(errors="replace")
                print(f"  seq={env.seq_no:>5}  [{verdict_str}]  lat={lat_us:.1f}µs  "
                      f"fix={fix_preview}...")
                self.n_received       += 1
                self.n_bytes_received += len(data)
                received.append(env)

    print("\n" + "═" * 60)
    print("  Multicast Gateway — Standalone Receiver Demo")
    print("═" * 60)
    print(f"\n  Joining {MCAST_GROUP}:{MCAST_PORT} ...")
    print("  Waiting for orders from dpdk_sim.py  (Ctrl-C to stop)\n")

    rx = _DemoReceiver(receiver_id=0, downstream_ring=ring)
    rx.start()

    try:
        while True:
            time.sleep(1)
            if received:
                print(f"  [{rx.stats_line()}]")
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
        print(f"\n  Final: {rx.stats_line()}")
        print(f"{'═' * 60}\n")
