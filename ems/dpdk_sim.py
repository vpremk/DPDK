"""
DPDK Concepts Simulation for macOS — Trading System Context
============================================================
Real DPDK requires Linux + kernel bypass (VFIO/UIO).
This module faithfully simulates every DPDK abstraction in pure Python
so you can understand the architecture before deploying to EC2 Nitro + EFA.

Concepts implemented:
  1.  rte_mempool  → MbufPool     (pre-allocated packet buffer pool)
  2.  rte_mbuf     → Mbuf         (packet descriptor + headroom/data/tailroom)
  3.  rte_ring     → RingBuffer   (lock-free SPSC circular queue)
  4.  PMD (Poll Mode Driver)      (busy-poll NIC, zero interrupt overhead)
  5.  Burst I/O    → rx_burst / tx_burst  (process N packets per call)
  6.  BPF Filter                  (Berkeley Packet Filter — whitelist traffic)
  7.  Packet Parser               (Ethernet → IP → UDP → FIX protocol)
  8.  Pipeline     → RX → Decode → Route → TX
  9.  Stats        → PPS, latency histogram, queue depth
  10. Multi-Queue  → RSS simulation (Receive Side Scaling)
"""

import sys
import os
import time
import struct
import random
import threading
import collections
import statistics
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import IntEnum

# Allow importing gbo_ref_data from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gbo_ref_data import (
    GBORefDataStore, PreTradeRiskEngine, Order as GBOOrder,
    OrderSide, RiskResult,
)


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS  (mirror DPDK defaults)
# ─────────────────────────────────────────────────────────────────────────────

RTE_MBUF_DEFAULT_BUF_SIZE  = 2176    # bytes  (RTE_PKTMBUF_HEADROOM=128 + 2048)
RTE_PKTMBUF_HEADROOM       = 128     # bytes reserved before packet data
RTE_ETH_RX_BURST_MAX       = 32      # max packets per rx_burst call
RTE_ETH_TX_BURST_MAX       = 32      # max packets per tx_burst call
NB_MBUF                    = 8192    # pool size (power of 2)
RING_SIZE                  = 1024    # ring capacity  (power of 2)
RSS_QUEUES                 = 4       # simulated NIC RX queues


# ─────────────────────────────────────────────────────────────────────────────
# 1. MBUF — packet buffer descriptor
# ─────────────────────────────────────────────────────────────────────────────

class Mbuf:
    """
    Mirror of rte_mbuf:
      buf_addr   — pointer to raw memory (bytearray here)
      data_off   — offset from buf_addr to first packet byte
      pkt_len    — total packet bytes
      nb_segs    — number of chained segments (1 for contiguous)
      port       — NIC port number
      timestamp  — hardware RX timestamp (ns)
      ol_flags   — offload flags (checksum, VLAN, RSS)
      hash.rss   — RSS hash value (queue affinity)

    Memory layout:
    ┌──────────────┬────────────────────────┬──────────┐
    │  HEADROOM    │       PACKET DATA      │ TAILROOM │
    │  (128 bytes) │    (up to 2048 bytes)  │          │
    └──────────────┴────────────────────────┴──────────┘
    ^buf_addr      ^data_off
    """
    __slots__ = (
        "buf", "data_off", "pkt_len", "data_len",
        "nb_segs", "port", "timestamp_ns", "ol_flags", "rss_hash", "_pool_ref"
    )

    def __init__(self, pool_ref=None):
        self.buf         = bytearray(RTE_MBUF_DEFAULT_BUF_SIZE)
        self.data_off    = RTE_PKTMBUF_HEADROOM   # data starts after headroom
        self.pkt_len     = 0
        self.data_len    = 0
        self.nb_segs     = 1
        self.port        = 0
        self.timestamp_ns= 0
        self.ol_flags    = 0
        self.rss_hash    = 0
        self._pool_ref   = pool_ref                # back-reference for free()

    @property
    def data(self) -> memoryview:
        """Zero-copy view of the packet data region."""
        return memoryview(self.buf)[self.data_off: self.data_off + self.data_len]

    def write(self, raw: bytes) -> None:
        """Write packet bytes into the data region (simulates DMA write)."""
        n = len(raw)
        self.buf[self.data_off: self.data_off + n] = raw
        self.data_len = n
        self.pkt_len  = n

    def prepend(self, n: int) -> memoryview:
        """Prepend n bytes (e.g. add outer header). Moves data_off backward."""
        assert self.data_off >= n, "Not enough headroom"
        self.data_off -= n
        self.data_len += n
        self.pkt_len  += n
        return memoryview(self.buf)[self.data_off: self.data_off + n]

    def free(self) -> None:
        """Return mbuf to pool (O(1), no malloc/free)."""
        if self._pool_ref:
            self._pool_ref._return(self)

    def reset(self) -> None:
        """Reset mbuf for reuse — O(1), no memset."""
        self.data_off     = RTE_PKTMBUF_HEADROOM
        self.pkt_len      = 0
        self.data_len     = 0
        self.nb_segs      = 1
        self.timestamp_ns = 0
        self.ol_flags     = 0
        self.rss_hash     = 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. MEMPOOL — pre-allocated buffer pool  (rte_mempool)
# ─────────────────────────────────────────────────────────────────────────────

class MbufPool:
    """
    Pre-allocates all mbufs at startup — zero malloc during packet processing.

    In real DPDK:
      - Backed by hugepages (2MB or 1GB pages) to minimize TLB misses
      - Objects aligned to cache lines (64 bytes) to prevent false sharing
      - Lock-free using rte_ring internally
      - NUMA-aware: pool created on same socket as NIC

    Here: Python list acts as the free-list ring.
    """
    def __init__(self, name: str, n: int = NB_MBUF):
        self.name     = name
        self.capacity = n
        self._free    = collections.deque(maxlen=n)
        self._lock    = threading.Lock()

        # Pre-allocate all mbufs — simulates hugepage allocation at init
        for _ in range(n):
            self._free.append(Mbuf(pool_ref=self))

        print(f"  [mempool] '{name}' created: {n} mbufs × "
              f"{RTE_MBUF_DEFAULT_BUF_SIZE}B = {n*RTE_MBUF_DEFAULT_BUF_SIZE//1024}KB")

    def alloc(self) -> Optional[Mbuf]:
        """
        O(1) alloc — no malloc, no system call.
        Returns None if pool exhausted (packet drop — same as DPDK).
        """
        with self._lock:
            if self._free:
                m = self._free.popleft()
                m.reset()
                return m
        return None   # pool empty → caller must drop packet

    def alloc_bulk(self, n: int) -> list[Mbuf]:
        """Allocate up to n mbufs in one call (amortises lock overhead)."""
        with self._lock:
            count = min(n, len(self._free))
            result = [self._free.popleft() for _ in range(count)]
            for m in result:
                m.reset()
            return result

    def _return(self, mbuf: Mbuf) -> None:
        """Called by mbuf.free() — returns buffer to pool."""
        with self._lock:
            if len(self._free) < self.capacity:
                self._free.append(mbuf)

    @property
    def available(self) -> int:
        return len(self._free)

    @property
    def in_use(self) -> int:
        return self.capacity - len(self._free)


# ─────────────────────────────────────────────────────────────────────────────
# 3. RING BUFFER — lock-free SPSC queue  (rte_ring)
# ─────────────────────────────────────────────────────────────────────────────

class RingBuffer:
    """
    Single-Producer Single-Consumer lock-free ring — core of DPDK's pipeline.

    In real DPDK (rte_ring):
      - Uses C11 atomics / GCC built-ins for CAS operations
      - Head/tail are cache-line padded to prevent false sharing
      - Supports SPSC, MPSC, SPMC, MPMC modes
      - Enqueue/dequeue O(1) — no malloc, no syscall, no lock

    Here: Python list with head/tail indices. Thread-safe via lock
    (in real DPDK the lock-free CAS removes the lock entirely).
    """
    def __init__(self, name: str, size: int = RING_SIZE):
        assert size & (size - 1) == 0, "Ring size must be power of 2"
        self.name    = name
        self.size    = size
        self.mask    = size - 1           # bit-mask replaces modulo (fast)
        self._buf    = [None] * size
        self._head   = 0                  # producer writes here
        self._tail   = 0                  # consumer reads here
        self._lock   = threading.Lock()
        self.enqueue_drops = 0

    def enqueue(self, obj) -> bool:
        """O(1) enqueue. Returns False if full (producer must drop)."""
        with self._lock:
            if (self._head - self._tail) >= self.size:
                self.enqueue_drops += 1
                return False
            self._buf[self._head & self.mask] = obj
            self._head += 1
            return True

    def enqueue_burst(self, objs: list) -> int:
        """Enqueue up to len(objs). Returns number actually enqueued."""
        with self._lock:
            free = self.size - (self._head - self._tail)
            n = min(len(objs), free)
            self.enqueue_drops += len(objs) - n
            for i in range(n):
                self._buf[self._head & self.mask] = objs[i]
                self._head += 1
            return n

    def dequeue(self):
        """O(1) dequeue. Returns None if empty."""
        with self._lock:
            if self._head == self._tail:
                return None
            obj = self._buf[self._tail & self.mask]
            self._buf[self._tail & self.mask] = None  # release ref
            self._tail += 1
            return obj

    def dequeue_burst(self, n: int) -> list:
        """Dequeue up to n objects in one call."""
        with self._lock:
            available = self._head - self._tail
            count = min(n, available)
            result = []
            for _ in range(count):
                result.append(self._buf[self._tail & self.mask])
                self._buf[self._tail & self.mask] = None
                self._tail += 1
            return result

    @property
    def count(self) -> int:
        return self._head - self._tail

    @property
    def free_count(self) -> int:
        return self.size - self.count


# ─────────────────────────────────────────────────────────────────────────────
# 4. PACKET STRUCTURES — Ethernet / IP / UDP / FIX
# ─────────────────────────────────────────────────────────────────────────────

class EtherType(IntEnum):
    IPv4 = 0x0800
    ARP  = 0x0806
    IPv6 = 0x86DD

class IPProto(IntEnum):
    TCP = 6
    UDP = 17

@dataclass
class EtherHdr:
    dst_mac: bytes   # 6 bytes
    src_mac: bytes   # 6 bytes
    etype  : int     # 2 bytes
    SIZE = 14

    @classmethod
    def unpack(cls, data: bytes) -> "EtherHdr":
        return cls(data[0:6], data[6:12], struct.unpack_from("!H", data, 12)[0])

    def pack(self) -> bytes:
        return self.dst_mac + self.src_mac + struct.pack("!H", self.etype)

@dataclass
class IPv4Hdr:
    ihl    : int   # header length in 32-bit words
    ttl    : int
    proto  : int
    src_ip : int   # 4 bytes as uint32
    dst_ip : int
    SIZE = 20      # without options

    @classmethod
    def unpack(cls, data: bytes) -> "IPv4Hdr":
        # !BBHHHBBH4s4s → [ver+ihl, dscp, totlen, id, frag, ttl, proto, cksum, src, dst]
        fields = struct.unpack_from("!BBHHHBBH4s4s", data)
        ihl    = (fields[0] & 0x0F) * 4
        # fields[5]=TTL  fields[6]=proto  fields[8]=src_ip  fields[9]=dst_ip
        return cls(ihl, fields[5], fields[6],
                   struct.unpack("!I", fields[8])[0],
                   struct.unpack("!I", fields[9])[0])

@dataclass
class UDPHdr:
    src_port: int
    dst_port: int
    length  : int
    SIZE = 8

    @classmethod
    def unpack(cls, data: bytes) -> "UDPHdr":
        sp, dp, ln, _ = struct.unpack_from("!HHHH", data)
        return cls(sp, dp, ln)

@dataclass
class FIXMsg:
    """Simplified FIX 4.2 message parsed from UDP payload."""
    msg_type : str          # tag 35 — D=NewOrder, 8=ExecutionReport
    sender   : str          # tag 49
    target   : str          # tag 56
    symbol   : str          # tag 55
    side     : str          # tag 54 — 1=Buy, 2=Sell
    qty      : float        # tag 38
    price    : float        # tag 44
    order_id : str          # tag 11

    FIELD_SEP = b"\x01"    # SOH character

    @classmethod
    def parse(cls, payload: bytes) -> Optional["FIXMsg"]:
        """Parse FIX tag=value|SOH format."""
        tags = {}
        try:
            for part in payload.split(cls.FIELD_SEP):
                if b"=" in part:
                    k, _, v = part.partition(b"=")
                    tags[k.decode()] = v.decode()
        except Exception:
            return None

        if "35" not in tags:
            return None

        return cls(
            msg_type = tags.get("35", ""),
            sender   = tags.get("49", ""),
            target   = tags.get("56", ""),
            symbol   = tags.get("55", ""),
            side     = tags.get("54", ""),
            qty      = float(tags.get("38", 0)),
            price    = float(tags.get("44", 0)),
            order_id = tags.get("11", ""),
        )

    def __repr__(self):
        side_str = "BUY" if self.side == "1" else "SELL"
        return (f"FIX[{self.msg_type}] {self.sender}→{self.target} | "
                f"{side_str} {self.qty:.0f} {self.symbol} @ {self.price:.2f} "
                f"(OrdID={self.order_id})")


# ─────────────────────────────────────────────────────────────────────────────
# 5. BPF FILTER — whitelist packet traffic  (rte_flow / BPF)
# ─────────────────────────────────────────────────────────────────────────────

class BPFFilter:
    """
    Simplified BPF-style filter applied per packet.

    Real DPDK uses:
      - rte_flow API (hardware offload to NIC flow director)
      - eBPF programs loaded into NIC firmware
      - RSS (Receive Side Scaling) to distribute flows across queues

    Filter expressions:
      "udp"           — accept only UDP
      "dst port 4567" — accept only traffic to port 4567
      "src ip X.X.X.X"— accept only from specific IP
    """
    def __init__(self, rules: list[dict]):
        self.rules = rules
        print(f"  [bpf] Installed {len(rules)} filter rules")

    def match(self, mbuf: Mbuf) -> bool:
        """Returns True if packet passes all filters."""
        raw = bytes(mbuf.data)
        if len(raw) < EtherHdr.SIZE + IPv4Hdr.SIZE + UDPHdr.SIZE:
            return False

        eth = EtherHdr.unpack(raw)
        if eth.etype != EtherType.IPv4:
            return False

        ip_raw = raw[EtherHdr.SIZE:]
        ip = IPv4Hdr.unpack(ip_raw)

        for rule in self.rules:
            if rule.get("proto") == "udp" and ip.proto != IPProto.UDP:
                return False
            if rule.get("proto") == "tcp" and ip.proto != IPProto.TCP:
                return False

            if "dst_port" in rule:
                udp_raw = ip_raw[ip.ihl:]
                udp = UDPHdr.unpack(udp_raw)
                if udp.dst_port != rule["dst_port"]:
                    return False

            if "src_ip" in rule:
                if ip.src_ip != struct.unpack("!I", rule["src_ip"])[0]:
                    return False

        return True


# ─────────────────────────────────────────────────────────────────────────────
# 6. PACKET GENERATOR — synthetic FIX-over-UDP traffic
# ─────────────────────────────────────────────────────────────────────────────

class PacketGenerator:
    """
    Generates synthetic FIX 4.2 NewOrderSingle messages over UDP.
    Used to drive the PMD simulation without a real NIC.

    In production: replaced by Direct Connect feed → NIC DMA → mbuf.
    """
    SYMBOLS  = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA"]
    SENDERS  = ["ALGO1", "ALGO2", "OMS1", "DESK1"]
    SRC_MAC  = bytes.fromhex("aabbccddeeff")
    DST_MAC  = bytes.fromhex("112233445566")
    SRC_IP   = struct.pack("!4B", 10, 0, 1, 1)
    DST_IP   = struct.pack("!4B", 10, 0, 1, 2)
    SRC_PORT = 12345
    DST_PORT = 4567            # FIX over UDP port

    def __init__(self, pool: MbufPool, seed: int = 42):
        self._pool  = pool
        self._rng   = random.Random(seed)
        self._seq   = 0

    def generate(self, n: int = 1) -> list[Mbuf]:
        """Generate n FIX NewOrderSingle packets into mbufs."""
        mbufs = self._pool.alloc_bulk(n)
        result = []
        for m in mbufs:
            pkt = self._build_packet(m)
            if pkt:
                result.append(pkt)
        return result

    def _build_packet(self, m: Mbuf) -> Optional[Mbuf]:
        self._seq += 1
        symbol  = self._rng.choice(self.SYMBOLS)
        sender  = self._rng.choice(self.SENDERS)
        side    = self._rng.choice(["1", "2"])
        qty     = self._rng.choice([100, 500, 1000, 5000])
        price   = round(self._rng.uniform(10, 500), 2)
        ts      = int(time.time() * 1e9)

        # FIX message payload (tag=value|SOH)
        soh = "\x01"
        fix = (f"8=FIX.4.2{soh}9=xxx{soh}35=D{soh}"
               f"49={sender}{soh}56=OMS{soh}11=ORD{self._seq:06d}{soh}"
               f"55={symbol}{soh}54={side}{soh}38={qty}{soh}44={price}{soh}"
               f"60={ts}{soh}10=000{soh}")
        fix_bytes = fix.encode()

        # UDP header
        udp_len = UDPHdr.SIZE + len(fix_bytes)
        udp = struct.pack("!HHHH",
                          self.SRC_PORT, self.DST_PORT,
                          udp_len, 0)   # checksum=0 (offloaded)

        # IP header (simplified — no options)
        ip_len = IPv4Hdr.SIZE + udp_len
        ip = struct.pack("!BBHHHBBH4s4s",
                         0x45, 0,          # version+IHL, DSCP+ECN
                         ip_len, self._seq & 0xFFFF,
                         0, 64, IPProto.UDP, 0,   # frag, TTL, proto, checksum=0
                         self.SRC_IP, self.DST_IP)

        # Ethernet header
        eth = self.DST_MAC + self.SRC_MAC + struct.pack("!H", EtherType.IPv4)

        raw = eth + ip + udp + fix_bytes
        m.write(raw)
        m.timestamp_ns = ts
        m.rss_hash = hash(symbol) & 0xFFFFFFFF   # RSS hash for queue affinity
        return m


# ─────────────────────────────────────────────────────────────────────────────
# 7. POLL MODE DRIVER (PMD)
# ─────────────────────────────────────────────────────────────────────────────

class PollModeDriver:
    """
    Simulates a DPDK Poll Mode Driver (PMD).

    Key difference from interrupt-driven drivers:
      - Interrupt mode: NIC interrupts CPU on each packet → context switch overhead
      - Poll mode:      CPU busily polls NIC descriptor ring → zero interrupt latency

    Real DPDK PMD:
      - Reads HW descriptor ring (DMA ring) in tight busy-poll loop
      - NIC DMAs packets directly into pre-pinned mbuf memory (zero-copy)
      - TX: writes descriptors pointing to mbuf data → NIC DMAs out
      - Each lcore (logical core) handles dedicated queue(s)

    Here: PacketGenerator simulates DMA writes; PMD calls rx_burst/tx_burst.
    """
    def __init__(self,
                 port_id    : int,
                 pool       : MbufPool,
                 generator  : PacketGenerator,
                 bpf_filter : BPFFilter,
                 n_queues   : int = RSS_QUEUES):
        self.port_id    = port_id
        self._pool      = pool
        self._gen       = generator
        self._bpf       = bpf_filter
        self.n_queues   = n_queues

        # One ring per RX queue (simulates NIC multi-queue / RSS)
        self._rx_rings  = [
            RingBuffer(f"rx_q{i}", RING_SIZE) for i in range(n_queues)
        ]
        self._tx_ring   = RingBuffer("tx", RING_SIZE)

        # Stats per queue
        self.rx_packets = [0] * n_queues
        self.rx_dropped = [0] * n_queues
        self.tx_packets = 0
        self.tx_dropped = 0
        self.filter_dropped = 0

        print(f"  [pmd] Port {port_id} initialised: {n_queues} RX queues, "
              f"ring_size={RING_SIZE}")

    def fill_rx_queues(self, burst: int = RTE_ETH_RX_BURST_MAX) -> None:
        """
        Simulate DMA: generate packets and place into RSS-distributed queues.
        In real DPDK, the NIC DMA engine does this autonomously.
        """
        pkts = self._gen.generate(burst)
        for m in pkts:
            # RSS: route packet to queue based on RSS hash (simulates NIC hardware)
            q = m.rss_hash % self.n_queues
            if not self._rx_rings[q].enqueue(m):
                m.free()

    def rx_burst(self, queue_id: int, n: int = RTE_ETH_RX_BURST_MAX) -> list[Mbuf]:
        """
        Core DPDK call: rte_eth_rx_burst(port, queue, pkts, n)
        Dequeues up to n packets from the specified RX queue.
        Applies BPF filter — non-matching packets are freed immediately.

        Returns list of mbufs for caller to process then free.
        """
        raw = self._rx_rings[queue_id].dequeue_burst(n)
        accepted = []
        for m in raw:
            if self._bpf.match(m):
                self.rx_packets[queue_id] += 1
                accepted.append(m)
            else:
                self.filter_dropped += 1
                m.free()
        return accepted

    def tx_burst(self, pkts: list[Mbuf]) -> int:
        """
        Core DPDK call: rte_eth_tx_burst(port, queue, pkts, n)
        Hands packets to NIC for transmission.
        In real DPDK, the NIC DMAs from mbuf memory directly.

        Returns number of packets sent (rest must be freed by caller).
        """
        sent = self._tx_ring.enqueue_burst(pkts)
        self.tx_packets  += sent
        self.tx_dropped  += len(pkts) - sent
        # Free unsent packets (in real DPDK, caller frees these)
        for m in pkts[sent:]:
            m.free()
        # Free sent packets (in real DPDK, tx_done callback frees)
        for m in pkts[:sent]:
            m.free()
        return sent


# ─────────────────────────────────────────────────────────────────────────────
# 8. PACKET PROCESSOR — decode + route FIX messages
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProcessedOrder:
    fix_msg   : FIXMsg
    queue_id  : int
    latency_ns: int       # processing latency (receive → decode)
    timestamp : int       # nanoseconds

class PacketProcessor:
    """
    Stateless packet processing pipeline — one lcore per queue in real DPDK.

    Stages:
      RX burst → Parse headers → Decode FIX → Risk check stub → Route to OMS ring
    """
    def __init__(self):
        self.orders_processed = 0
        self.parse_errors     = 0
        self.latencies_ns     = []          # circular buffer in production

    def process_burst(self,
                      mbufs: list[Mbuf],
                      queue_id: int) -> list[ProcessedOrder]:
        """Process a burst of mbufs. Returns decoded orders."""
        results = []
        now_ns = time.time_ns()

        for m in mbufs:
            order = self._process_one(m, queue_id, now_ns)
            if order:
                results.append(order)

        return results

    def _process_one(self, m: Mbuf, queue_id: int, now_ns: int) -> Optional[ProcessedOrder]:
        raw = bytes(m.data)

        # ── Parse Ethernet header
        if len(raw) < EtherHdr.SIZE:
            self.parse_errors += 1
            return None
        eth = EtherHdr.unpack(raw)
        if eth.etype != EtherType.IPv4:
            self.parse_errors += 1
            return None

        # ── Parse IP header
        ip_off = EtherHdr.SIZE
        ip = IPv4Hdr.unpack(raw[ip_off:])
        if ip.proto != IPProto.UDP:
            self.parse_errors += 1
            return None

        # ── Parse UDP header
        udp_off = ip_off + ip.ihl
        udp = UDPHdr.unpack(raw[udp_off:])

        # ── Extract FIX payload
        fix_off = udp_off + UDPHdr.SIZE
        fix_payload = raw[fix_off:]

        # ── Parse FIX message
        fix = FIXMsg.parse(fix_payload)
        if fix is None or fix.msg_type != "D":   # only NewOrderSingle
            self.parse_errors += 1
            return None

        latency = now_ns - m.timestamp_ns
        self.latencies_ns.append(latency)
        self.orders_processed += 1

        return ProcessedOrder(fix, queue_id, latency, now_ns)


# ─────────────────────────────────────────────────────────────────────────────
# 9. RSS — Receive Side Scaling queue assignment
# ─────────────────────────────────────────────────────────────────────────────

class RSSMapper:
    """
    RSS distributes incoming packets across NIC queues using a hash of
    (src_ip, dst_ip, src_port, dst_port) — ensuring flow affinity.

    Benefits:
      - Each queue handled by a dedicated lcore (no sharing)
      - All packets of a flow go to same core → no lock on order state
      - Linear scaling: add queues = add cores = add throughput

    Real DPDK uses Toeplitz hash with a secret key programmed into NIC.
    """
    def __init__(self, n_queues: int = RSS_QUEUES):
        self.n_queues = n_queues
        # Toeplitz key (40 bytes — same default as Intel X710)
        self._key = bytes([
            0x6d, 0x5a, 0x56, 0xda, 0x25, 0x5b, 0x0e, 0xc2,
            0x41, 0x67, 0x25, 0x3d, 0x43, 0xa3, 0x8f, 0xb0,
            0xd0, 0xca, 0x2b, 0xcb, 0xae, 0x7b, 0x30, 0xb4,
            0x77, 0xcb, 0x2d, 0xa3, 0x80, 0x30, 0xf2, 0x0c,
            0x6a, 0x42, 0xb7, 0x3b, 0xbe, 0xac, 0x01, 0xfa,
        ])

    def hash(self, src_ip: int, dst_ip: int, src_port: int, dst_port: int) -> int:
        """Simplified Toeplitz hash."""
        data = struct.pack("!IIHH", src_ip, dst_ip, src_port, dst_port)
        h = 0
        for i, byte in enumerate(data):
            for bit in range(8):
                if byte & (0x80 >> bit):
                    key_idx = i * 8 + bit
                    if key_idx < len(self._key) * 8:
                        ki = key_idx // 8
                        kb = 7 - (key_idx % 8)
                        if ki < len(self._key):
                            h ^= (self._key[ki] >> kb) & 1
        return h % self.n_queues


# ─────────────────────────────────────────────────────────────────────────────
# 10. STATISTICS ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class StatsEngine:
    """Real-time stats — mirrors rte_eth_stats and custom app counters."""
    def __init__(self):
        self._start      = time.perf_counter()
        self._last_print = self._start
        self._last_rx    = 0
        self.total_rx    = 0
        self.total_tx    = 0
        self.total_drops = 0
        self.latencies   : list[int] = []

    def update(self, pmd: PollModeDriver, processor: PacketProcessor):
        self.total_rx    = sum(pmd.rx_packets)
        self.total_tx    = pmd.tx_packets
        self.total_drops = pmd.filter_dropped + sum(r.enqueue_drops for r in pmd._rx_rings)
        self.latencies   = processor.latencies_ns

    def report(self, pmd: PollModeDriver, processor: PacketProcessor) -> None:
        self.update(pmd, processor)
        now     = time.perf_counter()
        elapsed = now - self._start
        delta_t = now - self._last_print
        delta_rx = self.total_rx - self._last_rx

        pps = delta_rx / delta_t if delta_t > 0 else 0
        self._last_rx    = self.total_rx
        self._last_print = now

        lats = self.latencies[-1000:] if self.latencies else [0]

        print(f"\n  ┌─ PMD Stats (t={elapsed:.1f}s) {'─'*40}")
        print(f"  │ RX total      : {self.total_rx:>10,} pkts")
        print(f"  │ TX total      : {self.total_tx:>10,} pkts")
        print(f"  │ Dropped       : {self.total_drops:>10,} pkts")
        print(f"  │ Throughput    : {pps:>10,.0f} pkt/s")
        print(f"  │ Parse errors  : {processor.parse_errors:>10,}")
        print(f"  │ Orders decoded: {processor.orders_processed:>10,}")
        print(f"  ├─ Per-Queue RX {'─'*43}")
        for q in range(pmd.n_queues):
            ring_depth = pmd._rx_rings[q].count
            print(f"  │ Queue {q}        : {pmd.rx_packets[q]:>8,} pkts | "
                  f"ring depth={ring_depth:>4}")
        print(f"  ├─ Latency (ns) {'─'*43}")
        print(f"  │ min           : {min(lats):>10,} ns")
        print(f"  │ p50           : {int(statistics.median(lats)):>10,} ns")
        print(f"  │ p99           : {int(sorted(lats)[int(len(lats)*0.99)]):>10,} ns")
        print(f"  │ max           : {max(lats):>10,} ns")
        print(f"  └{'─'*57}")


# ─────────────────────────────────────────────────────────────────────────────
# 11. LCORE PIPELINE — per-core processing loop  (rte_eal_remote_launch)
# ─────────────────────────────────────────────────────────────────────────────

class LcorePipeline:
    """
    Simulates one DPDK lcore (logical core) running a packet processing loop.

    Real DPDK lcore model:
      - Each lcore is pinned to a physical CPU core (no OS scheduling jitter)
      - Runs rte_eal_remote_launch(func, arg, lcore_id)
      - Tight busy-poll loop: while(running) { rx_burst; process; tx_burst; }
      - No sleep, no yield → lowest possible latency

    Here: runs in a Python thread (GIL prevents true parallelism,
    but the structure is identical to a real DPDK lcore).
    """
    def __init__(self,
                 lcore_id  : int,
                 queue_id  : int,
                 pmd       : PollModeDriver,
                 processor : PacketProcessor,
                 oms_ring  : RingBuffer,
                 burst_size: int = RTE_ETH_RX_BURST_MAX):
        self.lcore_id   = lcore_id
        self.queue_id   = queue_id
        self._pmd       = pmd
        self._proc      = processor
        self._oms_ring  = oms_ring
        self._burst     = burst_size
        self._running   = False
        self._thread    = None
        self.iterations = 0

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop,
            name=f"lcore-{self.lcore_id}",
            daemon=True
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _poll_loop(self):
        """
        The busy-poll loop — equivalent to:
          while (likely(running)) {
              nb_rx = rte_eth_rx_burst(port, queue, mbufs, BURST);
              if (nb_rx == 0) continue;  // no packets — keep polling
              process_burst(mbufs, nb_rx);
              rte_eth_tx_burst(port, queue, tx_mbufs, nb_tx);
          }
        """
        while self._running:
            # ── RX burst (dequeue from NIC ring)
            mbufs = self._pmd.rx_burst(self.queue_id, self._burst)
            self.iterations += 1

            if not mbufs:
                continue    # empty poll — no sleep! keeps CPU spinning

            # ── Process burst (decode headers, parse FIX)
            orders = self._proc.process_burst(mbufs, self.queue_id)

            # ── Forward to OMS ring
            for order in orders:
                self._oms_ring.enqueue(order)

            # ── TX burst — pass all accepted mbufs (tx_burst frees them)
            self._pmd.tx_burst(mbufs)


# ─────────────────────────────────────────────────────────────────────────────
# 11b. PRE-TRADE RISK GATEWAY — FIX → GBO Order → Risk Engine
# ─────────────────────────────────────────────────────────────────────────────

class PreTradeRiskGateway:
    """
    Bridges the FIX packet pipeline to the GBO pre-trade risk engine.

    Responsibilities:
      - Map FIX SenderCompID to a GBO account_id and counterparty cp_id
      - Translate FIXMsg → GBOOrder (the risk engine's order type)
      - Call PreTradeRiskEngine.check() and return the verdict
      - Accumulate pass/warn/reject counters for stats reporting

    In production this would run on a dedicated risk lcore, receiving
    orders from the OMS ingress ring via zero-copy pointer passing.
    """

    # FIX SenderCompID → (account_id, cp_id)
    _SENDER_MAP: dict[str, tuple[str, str]] = {
        "ALGO1": ("ACC-EQARB-01", "CP001"),   # EquityArb US  → Goldman (Tier1)
        "ALGO2": ("ACC-EQARB-02", "CP002"),   # EquityArb EU  → Morgan Stanley (Tier1)
        "OMS1":  ("ACC-AGENCY-01", "CP001"),  # Agency flow   → Goldman (Tier1)
        "DESK1": ("ACC-MACRO-01",  "CP004"),  # Macro desk    → Deutsche Bank (Tier2)
    }
    _DEFAULT_ACCOUNT = "ACC-EQARB-01"
    _DEFAULT_CP      = "CP001"

    def __init__(self):
        self._gbo    = GBORefDataStore()
        self._engine = PreTradeRiskEngine(self._gbo)
        self.n_pass   = 0
        self.n_warn   = 0
        self.n_reject = 0

    def evaluate(self, proc: "ProcessedOrder"):
        """
        Run pre-trade risk checks on a decoded FIX order.
        Returns (GBOOrder, PreTradeResult).
        """
        fix = proc.fix_msg
        account_id, cp_id = self._SENDER_MAP.get(
            fix.sender, (self._DEFAULT_ACCOUNT, self._DEFAULT_CP))

        order = GBOOrder(
            order_id    = fix.order_id,
            account_id  = account_id,
            cp_id       = cp_id,
            ticker      = fix.symbol,
            side        = OrderSide.BUY if fix.side == "1" else OrderSide.SELL,
            qty         = int(fix.qty),
            limit_price = fix.price,
        )
        result = self._engine.check(order)

        if result.verdict == RiskResult.PASS:
            self.n_pass += 1
        elif result.verdict == RiskResult.WARN:
            self.n_warn += 1
        else:
            self.n_reject += 1

        return order, result

    def print_result(self, order: "GBOOrder", result) -> None:
        """Print a single risk decision to stdout."""
        verdict_tag = {
            RiskResult.PASS:   "✓ PASS  ",
            RiskResult.WARN:   "⚠ WARN  ",
            RiskResult.REJECT: "✗ REJECT",
        }[result.verdict]
        side_str = "BUY " if order.side == OrderSide.BUY else "SELL"
        print(f"  {verdict_tag} | {order.order_id}  {side_str} {order.qty:>6,} "
              f"{order.ticker:<6} @ {order.limit_price:>8.2f}"
              f"  notional=${result.notional_usd:>12,.0f}"
              f"  lat={result.latency_us:.1f}µs")
        for chk in result.checks:
            if chk.result != RiskResult.PASS:
                tag = "  WARN" if chk.result == RiskResult.WARN else "  REJECT"
                print(f"           {tag}: [{chk.name}] {chk.message}")

    def stats_line(self) -> str:
        total = self.n_pass + self.n_warn + self.n_reject
        return (f"Risk gateway: {total} orders | "
                f"pass={self.n_pass} warn={self.n_warn} reject={self.n_reject}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN — wire everything together and run
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 60)
    print("  DPDK Simulation — Trading Packet Pipeline")
    print("═" * 60)

    # ── EAL Init (Environment Abstraction Layer)
    print("\n[EAL] Initialising...")
    pool      = MbufPool("pktmbuf_pool", NB_MBUF)
    generator = PacketGenerator(pool, seed=99)
    bpf       = BPFFilter([
        {"proto": "udp"},
        {"dst_port": PacketGenerator.DST_PORT},
    ])
    pmd       = PollModeDriver(port_id=0, pool=pool,
                               generator=generator, bpf_filter=bpf,
                               n_queues=RSS_QUEUES)
    rss       = RSSMapper(n_queues=RSS_QUEUES)
    processor = PacketProcessor()
    oms_ring  = RingBuffer("oms_ingress", RING_SIZE * 2)
    stats     = StatsEngine()
    risk_gw   = PreTradeRiskGateway()

    # ── Multicast gateway — fan-out approved orders to N downstream receivers
    print("\n[Mcast] Initialising multicast gateway...")
    try:
        from multicast_gateway import MulticastGateway, MulticastReceiver, RECEIVER_RING_SIZE
        mcast_gw   = MulticastGateway()
        N_RECEIVERS = 3
        mcast_rings = [RingBuffer(f"mcast_rx_{i}", RECEIVER_RING_SIZE)
                       for i in range(N_RECEIVERS)]
        mcast_rxs   = [MulticastReceiver(receiver_id=i,
                                         downstream_ring=mcast_rings[i],
                                         gateway=mcast_gw)
                       for i in range(N_RECEIVERS)]
        for rx in mcast_rxs:
            rx.start()
        _mcast_enabled = True
    except Exception as e:
        print(f"  [mcast] disabled: {e}")
        mcast_gw, mcast_rings, mcast_rxs = None, [], []
        _mcast_enabled = False

    # ── Launch one lcore per queue
    print(f"\n[EAL] Launching {RSS_QUEUES} lcore pipelines...")
    lcores = []
    for q in range(RSS_QUEUES):
        lc = LcorePipeline(
            lcore_id=q, queue_id=q,
            pmd=pmd, processor=processor,
            oms_ring=oms_ring
        )
        lc.start()
        lcores.append(lc)
        print(f"  lcore {q} → queue {q} running")

    # ── Main loop: fill NIC + drain OMS ring + print stats
    print("\n[EAL] Main loop running (3 seconds)...\n")
    order_log = []
    t_end = time.perf_counter() + 3.0

    while time.perf_counter() < t_end:
        # Simulate NIC DMA filling RX rings
        pmd.fill_rx_queues(burst=RTE_ETH_RX_BURST_MAX)

        # Drain OMS ingress ring → pre-trade risk check → multicast fan-out
        orders = oms_ring.dequeue_burst(64)
        for o in orders:
            gbo_order, risk_result = risk_gw.evaluate(o)
            order_log.append((o, gbo_order, risk_result))

            # Publish approved orders to multicast group
            if _mcast_enabled:
                mcast_gw.publish(gbo_order, risk_result, o)

        time.sleep(0.001)   # 1ms tick (remove for true busy-poll)

    # ── Stop lcores
    for lc in lcores:
        lc.stop()

    # ── Stop multicast receivers and close gateway
    if _mcast_enabled:
        for rx in mcast_rxs:
            rx.stop()
        mcast_gw.close()

    # ── Final stats
    stats.report(pmd, processor)

    # ── Pre-trade risk summary
    print(f"\n  Pre-Trade Risk Gateway:")
    print(f"  {'─'*70}")
    print(f"  {risk_gw.stats_line()}")

    # ── Multicast gateway summary
    if _mcast_enabled:
        print(f"\n  Multicast Gateway:")
        print(f"  {'─'*70}")
        print(f"  {mcast_gw.stats_line()}")
        for rx in mcast_rxs:
            print(f"  {rx.stats_line()}")

    # ── Sample decoded orders with risk decisions (first 8)
    print(f"\n  Sample FIX orders + risk verdict (first 8 of {len(order_log)}):")
    print(f"  {'─'*70}")
    for proc_order, gbo_order, risk_result in order_log[:8]:
        print(f"  Q{proc_order.queue_id} | nic_lat={proc_order.latency_ns//1000:>5}µs | "
              f"{proc_order.fix_msg}")
        risk_gw.print_result(gbo_order, risk_result)

    # ── RSS distribution
    print(f"\n  RSS Queue Distribution:")
    print(f"  {'─'*40}")
    total = sum(pmd.rx_packets)
    for q in range(RSS_QUEUES):
        pct = pmd.rx_packets[q] / total * 100 if total else 0
        bar = "█" * int(pct / 2)
        print(f"  Q{q}: {bar:<50} {pct:5.1f}%  ({pmd.rx_packets[q]:,} pkts)")

    # ── Pool utilisation
    print(f"\n  Mempool '{pool.name}':")
    print(f"  Capacity  : {pool.capacity:,}")
    print(f"  Available : {pool.available:,}")
    print(f"  In-use    : {pool.in_use:,}")

    print(f"\n{'═' * 60}")
    print("  Simulation complete.")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
