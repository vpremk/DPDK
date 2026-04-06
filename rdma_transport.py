"""
RDMA Transport Layer — Low-Latency Market Data & Pricing Result Transfer
========================================================================
Uses InfiniBand / RoCE via pyverbs (libibverbs Python bindings).

Architecture:
  Market Data Server  ──RDMA Write──►  Pricing Engine Node
  Pricing Engine Node ──RDMA Write──►  Risk Engine Node

Transfer targets:
  • NBBO / L2 book snapshots       (market data → pricing)
  • Monte Carlo result buffers     (pricing → risk)
  • Order execution confirmations  (risk → OMS)

Transport: RC (Reliable Connected) QP — ordered, lossless delivery.

Requires:
  pip install pyverbs   # part of rdma-core
  # On AWS: EFA-enabled instance (c5n, p4d, hpc6a) with EFA driver

Simulation mode (no HW):
  Set RDMA_SIMULATE=1 — uses shared memory + busy-poll to mimic RDMA semantics.

Usage:
  # Server (receiver)
  python rdma_transport.py --mode server --port 18515

  # Client (sender)
  python rdma_transport.py --mode client --server 10.0.0.1 --port 18515
"""

from __future__ import annotations

import argparse
import ctypes
import json
import mmap
import os
import socket
import struct
import time
from dataclasses import dataclass, asdict
from typing import Optional
import numpy as np

# ── Try importing pyverbs; fall back to simulation ────────────────────────────
try:
    import pyverbs.device as pvdev
    import pyverbs.pd as pvpd
    import pyverbs.mr as pvmr
    import pyverbs.cq as pvcq
    import pyverbs.qp as pvqp
    import pyverbs.enums as pve
    from pyverbs.addr import AHAttr, GlobalRoute
    from pyverbs.wr import SendWR, SGE, RecvWR
    PYVERBS_AVAILABLE = True
except ImportError:
    PYVERBS_AVAILABLE = False

SIMULATE = os.environ.get("RDMA_SIMULATE", "0") == "1" or not PYVERBS_AVAILABLE

# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES  (match montecarlo_pricing.py layout)
# ─────────────────────────────────────────────────────────────────────────────

# Flat C-struct packed into the RDMA buffer — no serialisation overhead.
# All floats are 64-bit IEEE 754.

NBBO_STRUCT_FMT = "!16sdddd Q"          # symbol(16), bid, ask, bid_sz, ask_sz, ts_ns
NBBO_STRUCT_SIZE = struct.calcsize(NBBO_STRUCT_FMT)

MC_RESULT_STRUCT_FMT = "!16s ddddd Q"   # symbol, price, std_err, ci_lo, ci_hi, elapsed_ms, ts_ns
MC_RESULT_STRUCT_SIZE = struct.calcsize(MC_RESULT_STRUCT_FMT)


@dataclass
class NBBOSnapshot:
    symbol: str
    bid: float
    ask: float
    bid_sz: float
    ask_sz: float
    ts_ns: int = 0                       # exchange timestamp, nanoseconds

    def pack(self) -> bytes:
        sym = self.symbol.encode().ljust(16)[:16]
        return struct.pack(
            NBBO_STRUCT_FMT,
            sym, self.bid, self.ask, self.bid_sz, self.ask_sz, self.ts_ns,
        )

    @classmethod
    def unpack(cls, buf: bytes) -> "NBBOSnapshot":
        sym, bid, ask, bid_sz, ask_sz, ts_ns = struct.unpack(NBBO_STRUCT_FMT, buf[:NBBO_STRUCT_SIZE])
        return cls(sym.rstrip(b"\x00").decode(), bid, ask, bid_sz, ask_sz, ts_ns)


@dataclass
class MCResultMsg:
    symbol: str
    price: float
    std_err: float
    ci_lo: float
    ci_hi: float
    elapsed_ms: float
    ts_ns: int = 0

    def pack(self) -> bytes:
        sym = self.symbol.encode().ljust(16)[:16]
        return struct.pack(
            MC_RESULT_STRUCT_FMT,
            sym, self.price, self.std_err, self.ci_lo, self.ci_hi,
            self.elapsed_ms, self.ts_ns,
        )

    @classmethod
    def unpack(cls, buf: bytes) -> "MCResultMsg":
        sym, price, se, lo, hi, ms, ts = struct.unpack(MC_RESULT_STRUCT_FMT, buf[:MC_RESULT_STRUCT_SIZE])
        return cls(sym.rstrip(b"\x00").decode(), price, se, lo, hi, ms, ts)


# ─────────────────────────────────────────────────────────────────────────────
# MEMORY REGION  — pinned, zero-copy numpy array
# ─────────────────────────────────────────────────────────────────────────────

class PinnedBuffer:
    """
    Allocates page-aligned memory and registers it with the HCA as an MR.
    Wraps the buffer as a numpy array so MC paths can be written directly.
    """
    PAGE = 4096

    def __init__(self, size: int, pd=None):
        self.size = size
        # Allocate page-aligned memory via mmap (anonymous, private)
        self._mmap = mmap.mmap(-1, size, mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS
                                if hasattr(mmap, "MAP_ANONYMOUS") else mmap.ACCESS_WRITE)
        self._mmap.write(b"\x00" * size)
        self._mmap.seek(0)
        # Wrap as ctypes byte array so we can pass the address to libibverbs
        self._ctype_buf = (ctypes.c_char * size).from_buffer(self._mmap)
        self.address = ctypes.addressof(self._ctype_buf)
        # numpy view — zero-copy
        self.array = np.frombuffer(self._ctype_buf, dtype=np.uint8)

        self.mr = None
        if pd is not None and PYVERBS_AVAILABLE:
            access = (pve.IBV_ACCESS_LOCAL_WRITE |
                      pve.IBV_ACCESS_REMOTE_WRITE |
                      pve.IBV_ACCESS_REMOTE_READ)
            self.mr = pvmr.MR(pd, size, access, address=self.address)

    @property
    def lkey(self) -> int:
        return self.mr.lkey if self.mr else 0

    @property
    def rkey(self) -> int:
        return self.mr.rkey if self.mr else 0

    def write_nbbo(self, snap: NBBOSnapshot) -> None:
        packed = snap.pack()
        self.array[:len(packed)] = np.frombuffer(packed, dtype=np.uint8)

    def read_nbbo(self) -> NBBOSnapshot:
        return NBBOSnapshot.unpack(bytes(self.array[:NBBO_STRUCT_SIZE]))

    def write_mc_result(self, result: MCResultMsg) -> None:
        packed = result.pack()
        self.array[:len(packed)] = np.frombuffer(packed, dtype=np.uint8)

    def read_mc_result(self) -> MCResultMsg:
        return MCResultMsg.unpack(bytes(self.array[:MC_RESULT_STRUCT_SIZE]))

    def close(self):
        if self.mr:
            self.mr.close()
        self._mmap.close()


# ─────────────────────────────────────────────────────────────────────────────
# QUEUE PAIR METADATA  (exchanged out-of-band over TCP)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class QPInfo:
    qp_num: int
    lid: int          # local ID (IB) — 0 for RoCE
    gid: str          # GID (IPv6-style, for RoCE / EFA)
    rkey: int
    addr: int         # remote buffer virtual address

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "QPInfo":
        return cls(**json.loads(data))


# ─────────────────────────────────────────────────────────────────────────────
# RDMA CONNECTION  (RC transport)
# ─────────────────────────────────────────────────────────────────────────────

class RDMAConnection:
    """
    Full RC (Reliable Connected) RDMA connection lifecycle:
      1. Open device & allocate Protection Domain
      2. Create CQ + QP
      3. Exchange QPInfo over TCP (out-of-band)
      4. Transition QP: RESET → INIT → RTR → RTS
      5. Post RDMA_WRITE / RDMA_READ work requests
      6. Poll completion queue
    """

    CQ_DEPTH   = 128
    QP_DEPTH   = 128
    GID_INDEX  = 3      # RoCEv2 GID index — check `show_gids` on your NIC

    def __init__(self, device_name: Optional[str] = None):
        if not PYVERBS_AVAILABLE:
            raise RuntimeError("pyverbs not installed. Set RDMA_SIMULATE=1 for simulation.")

        # ── 1. Open device ────────────────────────────────────────────────────
        dev_list = pvdev.get_device_list()
        if not dev_list:
            raise RuntimeError("No RDMA devices found. Is the driver loaded?")
        dev = next((d for d in dev_list if d.name.decode() == device_name), dev_list[0]) \
              if device_name else dev_list[0]
        self.ctx = pvdev.Context(name=dev.name)
        self.port_attr = self.ctx.query_port(1)
        self.dev_attr  = self.ctx.query_device()

        # ── 2. Protection Domain + CQ + QP ───────────────────────────────────
        self.pd = pvpd.PD(self.ctx)
        self.cq = pvcq.CQ(self.ctx, self.CQ_DEPTH)

        qp_init = pvqp.QPInitAttr(
            qp_type=pve.IBV_QPT_RC,
            scq=self.cq,
            rcq=self.cq,
            cap=pvqp.QPCap(
                max_send_wr=self.QP_DEPTH,
                max_recv_wr=self.QP_DEPTH,
                max_send_sge=1,
                max_recv_sge=1,
            ),
        )
        self.qp = pvqp.QP(self.pd, qp_init)

        self.buf: Optional[PinnedBuffer] = None
        self.remote: Optional[QPInfo]    = None

    # ── 3. Buffer allocation ──────────────────────────────────────────────────

    def alloc_buffer(self, size: int) -> PinnedBuffer:
        self.buf = PinnedBuffer(size, pd=self.pd)
        return self.buf

    # ── 4. QP state machine ───────────────────────────────────────────────────

    def _modify_qp_init(self) -> None:
        attr = pvqp.QPAttr(
            qp_state=pve.IBV_QPS_INIT,
            pkey_index=0,
            port_num=1,
            qp_access_flags=(pve.IBV_ACCESS_REMOTE_WRITE | pve.IBV_ACCESS_REMOTE_READ),
        )
        self.qp.modify(attr, pve.IBV_QP_STATE | pve.IBV_QP_PKEY_INDEX |
                       pve.IBV_QP_PORT | pve.IBV_QP_ACCESS_FLAGS)

    def _modify_qp_rtr(self, remote: QPInfo) -> None:
        gr = GlobalRoute(dgid=remote.gid, sgid_index=self.GID_INDEX)
        ah_attr = AHAttr(
            is_global=1,
            dlid=remote.lid,
            grh=gr,
            sl=0,
            src_path_bits=0,
            port_num=1,
        )
        attr = pvqp.QPAttr(
            qp_state=pve.IBV_QPS_RTR,
            path_mtu=pve.IBV_MTU_4096,
            dest_qp_num=remote.qp_num,
            rq_psn=0,
            max_dest_rd_atomic=1,
            min_rnr_timer=12,
            ah_attr=ah_attr,
        )
        self.qp.modify(attr,
                       pve.IBV_QP_STATE | pve.IBV_QP_AV | pve.IBV_QP_PATH_MTU |
                       pve.IBV_QP_DEST_QPN | pve.IBV_QP_RQ_PSN |
                       pve.IBV_QP_MAX_DEST_RD_ATOMIC | pve.IBV_QP_MIN_RNR_TIMER)

    def _modify_qp_rts(self) -> None:
        attr = pvqp.QPAttr(
            qp_state=pve.IBV_QPS_RTS,
            timeout=14,
            retry_cnt=7,
            rnr_retry=7,
            sq_psn=0,
            max_rd_atomic=1,
        )
        self.qp.modify(attr,
                       pve.IBV_QP_STATE | pve.IBV_QP_TIMEOUT | pve.IBV_QP_RETRY_CNT |
                       pve.IBV_QP_RNR_RETRY | pve.IBV_QP_SQ_PSN | pve.IBV_QP_MAX_QP_RD_ATOMIC)

    def local_qp_info(self) -> QPInfo:
        gid = self.ctx.query_gid(1, self.GID_INDEX)
        return QPInfo(
            qp_num=self.qp.qp_num,
            lid=self.port_attr.lid,
            gid=str(gid),
            rkey=self.buf.rkey,
            addr=self.buf.address,
        )

    def connect(self, remote: QPInfo) -> None:
        """Transition QP through INIT → RTR → RTS using remote QPInfo."""
        self.remote = remote
        self._modify_qp_init()
        self._modify_qp_rtr(remote)
        self._modify_qp_rts()

    # ── 5. Work requests ──────────────────────────────────────────────────────

    def rdma_write(self, size: int, wr_id: int = 1) -> None:
        """One-sided RDMA_WRITE: push local buffer to remote without remote CPU."""
        assert self.buf and self.remote, "Buffer and remote must be set before write"
        sge = SGE(addr=self.buf.address, length=size, lkey=self.buf.lkey)
        wr  = SendWR(
            wr_id=wr_id,
            opcode=pve.IBV_WR_RDMA_WRITE,
            send_flags=pve.IBV_SEND_SIGNALED,
            num_sge=1,
            sg=sge,
        )
        wr.set_wr_rdma(rkey=self.remote.rkey, addr=self.remote.addr)
        self.qp.post_send(wr)

    def rdma_read(self, size: int, wr_id: int = 2) -> None:
        """One-sided RDMA_READ: pull from remote buffer without remote CPU."""
        assert self.buf and self.remote, "Buffer and remote must be set before read"
        sge = SGE(addr=self.buf.address, length=size, lkey=self.buf.lkey)
        wr  = SendWR(
            wr_id=wr_id,
            opcode=pve.IBV_WR_RDMA_READ,
            send_flags=pve.IBV_SEND_SIGNALED,
            num_sge=1,
            sg=sge,
        )
        wr.set_wr_rdma(rkey=self.remote.rkey, addr=self.remote.addr)
        self.qp.post_send(wr)

    # ── 6. Completion polling — busy-spin for lowest latency ─────────────────

    def poll_completion(self, timeout_us: int = 1_000) -> bool:
        """
        Busy-poll the CQ until a completion arrives or timeout_us microseconds pass.
        Avoids kernel-wait overhead — critical for <5µs RTT targets.
        """
        deadline = time.perf_counter_ns() + timeout_us * 1_000
        while time.perf_counter_ns() < deadline:
            wcs = self.cq.poll(1)
            if wcs:
                wc = wcs[0]
                if wc.status != pve.IBV_WC_SUCCESS:
                    raise RuntimeError(f"WC error: {wc.status} — vendor_err={wc.vendor_err}")
                return True
        return False  # timeout

    def close(self) -> None:
        if self.buf:
            self.buf.close()
        self.qp.close()
        self.cq.close()
        self.pd.close()
        self.ctx.close()


# ─────────────────────────────────────────────────────────────────────────────
# OUT-OF-BAND TCP HANDSHAKE  (exchange QPInfo before RDMA traffic)
# ─────────────────────────────────────────────────────────────────────────────

def _exchange_qp_info_server(port: int, local: QPInfo) -> QPInfo:
    """Server: accept one connection, swap QPInfo, return remote info."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", port))
        srv.listen(1)
        print(f"[server] Waiting for client on port {port} …")
        conn, addr = srv.accept()
        print(f"[server] Client connected from {addr}")
        with conn:
            # Send local info first, then receive remote
            payload = local.to_json()
            conn.sendall(struct.pack("!I", len(payload)) + payload)
            raw_len = conn.recv(4)
            rlen = struct.unpack("!I", raw_len)[0]
            remote_json = conn.recv(rlen)
            return QPInfo.from_json(remote_json)


def _exchange_qp_info_client(server_host: str, port: int, local: QPInfo) -> QPInfo:
    """Client: connect, swap QPInfo, return remote info."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((server_host, port))
        # Receive server info first
        raw_len = sock.recv(4)
        rlen = struct.unpack("!I", raw_len)[0]
        remote_json = sock.recv(rlen)
        remote = QPInfo.from_json(remote_json)
        # Send local info
        payload = local.to_json()
        sock.sendall(struct.pack("!I", len(payload)) + payload)
        return remote


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION MODE  (shared memory + busy-poll, no HW required)
# ─────────────────────────────────────────────────────────────────────────────

SHM_SIZE   = 4096
FLAG_READY = 0xFF
FLAG_EMPTY = 0x00

# Single shared anonymous mmap — client and server in the same process share this.
# For cross-process use, replace with multiprocessing.shared_memory.SharedMemory.
_SIM_SHM = mmap.mmap(-1, SHM_SIZE)
_SIM_BUF = (ctypes.c_char * SHM_SIZE).from_buffer(_SIM_SHM)
_SIM_ARR = np.frombuffer(_SIM_BUF, dtype=np.uint8)


class SimulatedRDMAServer:
    """
    Simulates the RDMA receiver using a shared-memory ring.
    Spin-waits on a flag byte — mimics RDMA doorbell polling.
    """

    def wait_for_message(self, timeout_s: float = 5.0) -> Optional[NBBOSnapshot | MCResultMsg]:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            if _SIM_ARR[0] == FLAG_READY:
                msg_type = int(_SIM_ARR[1])
                raw = bytes(_SIM_ARR[2:2 + max(NBBO_STRUCT_SIZE, MC_RESULT_STRUCT_SIZE)])
                _SIM_ARR[0] = FLAG_EMPTY   # clear flag
                if msg_type == 0:
                    return NBBOSnapshot.unpack(raw)
                else:
                    return MCResultMsg.unpack(raw)
        return None

    def close(self):
        pass


class SimulatedRDMAClient:
    """
    Simulates the RDMA sender — writes to shared memory and sets the flag.
    """

    def _write(self, msg_type: int, packed: bytes) -> None:
        _SIM_ARR[0] = FLAG_EMPTY                                # clear first
        _SIM_ARR[1] = msg_type
        _SIM_ARR[2:2 + len(packed)] = np.frombuffer(packed, dtype=np.uint8)
        _SIM_ARR[0] = FLAG_READY                                # doorbell

    def send_nbbo(self, snap: NBBOSnapshot) -> None:
        self._write(0, snap.pack())

    def send_mc_result(self, result: MCResultMsg) -> None:
        self._write(1, result.pack())

    def close(self):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# LATENCY BENCHMARK
# ─────────────────────────────────────────────────────────────────────────────

def benchmark_roundtrip(n_iterations: int = 10_000) -> dict:
    """
    Measures simulated RDMA round-trip latency using shared-memory transport.
    On real RDMA hardware (EFA, InfiniBand) expect <2µs vs ~10µs here.
    """
    client = SimulatedRDMAClient()
    server = SimulatedRDMAServer()

    snap = NBBOSnapshot(
        symbol="AAPL",
        bid=189.50,
        ask=189.51,
        bid_sz=500.0,
        ask_sz=300.0,
        ts_ns=time.time_ns(),
    )

    latencies_ns: list[int] = []

    for _ in range(n_iterations):
        t0 = time.perf_counter_ns()
        client.send_nbbo(snap)
        msg = server.wait_for_message(timeout_s=0.001)
        t1 = time.perf_counter_ns()
        if msg is not None:
            latencies_ns.append(t1 - t0)

    client.close()
    server.close()

    arr = np.array(latencies_ns, dtype=np.float64)
    return {
        "n":           len(arr),
        "mean_us":     float(arr.mean() / 1_000),
        "median_us":   float(np.median(arr) / 1_000),
        "p99_us":      float(np.percentile(arr, 99) / 1_000),
        "p999_us":     float(np.percentile(arr, 99.9) / 1_000),
        "min_us":      float(arr.min() / 1_000),
        "max_us":      float(arr.max() / 1_000),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SERVER / CLIENT ENTRY POINTS
# ─────────────────────────────────────────────────────────────────────────────

def run_server(port: int, device: Optional[str] = None) -> None:
    if SIMULATE:
        print("[server] RDMA simulation mode (no HW)")
        server = SimulatedRDMAServer()
        print("[server] Waiting for messages …")
        while True:
            msg = server.wait_for_message(timeout_s=30.0)
            if msg is None:
                print("[server] Timeout — exiting")
                break
            if isinstance(msg, NBBOSnapshot):
                print(f"[server] NBBO  {msg.symbol:>6}  bid={msg.bid:.4f}  ask={msg.ask:.4f}"
                      f"  lag={time.time_ns() - msg.ts_ns:,} ns")
            elif isinstance(msg, MCResultMsg):
                print(f"[server] MCRes {msg.symbol:>6}  price={msg.price:.4f} ± {msg.std_err:.4f}"
                      f"  [{msg.ci_lo:.4f}, {msg.ci_hi:.4f}]  {msg.elapsed_ms:.2f}ms")
        server.close()
        return

    # ── Real RDMA path ────────────────────────────────────────────────────────
    conn = RDMAConnection(device_name=device)
    buf  = conn.alloc_buffer(SHM_SIZE)
    local_info = conn.local_qp_info()
    remote_info = _exchange_qp_info_server(port, local_info)
    conn.connect(remote_info)
    print(f"[server] Connected. QPN={conn.qp.qp_num} remote={remote_info.qp_num}")

    # Post an initial receive WR so remote can RDMA_WRITE to us
    # (RC RDMA_WRITE doesn't require a posted recv — receiver is passive)
    print("[server] Ready. Polling completion queue …")
    try:
        while True:
            # Spin-poll: RDMA_WRITE completes on *sender* side.
            # Receiver detects data by watching a flag byte in the buffer.
            flag = buf.array[0]
            if flag == 0xFF:
                msg_type = int(buf.array[1])
                if msg_type == 0:
                    snap = buf.read_nbbo()
                    print(f"[server] NBBO {snap.symbol} bid={snap.bid:.4f}")
                else:
                    res = buf.read_mc_result()
                    print(f"[server] MC   {res.symbol} price={res.price:.4f}")
                buf.array[0] = 0x00     # clear flag
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


def run_client(server_host: str, port: int, device: Optional[str] = None) -> None:
    if SIMULATE:
        print("[client] RDMA simulation mode (no HW)")
        client = SimulatedRDMAClient()
        symbols = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]
        rng = np.random.default_rng(0)
        for i, sym in enumerate(symbols):
            mid = rng.uniform(100, 500)
            snap = NBBOSnapshot(
                symbol=sym,
                bid=round(mid - 0.01, 4),
                ask=round(mid + 0.01, 4),
                bid_sz=float(rng.integers(100, 1000)),
                ask_sz=float(rng.integers(100, 1000)),
                ts_ns=time.time_ns(),
            )
            t0 = time.perf_counter_ns()
            client.send_nbbo(snap)
            elapsed_us = (time.perf_counter_ns() - t0) / 1_000
            print(f"[client] Sent NBBO {sym:>5}  bid={snap.bid:.4f}  ask={snap.ask:.4f}"
                  f"  write_lat={elapsed_us:.2f}µs")
            time.sleep(0.1)

        # Send a synthetic MC result
        mc = MCResultMsg(
            symbol="AAPL_C200",
            price=12.34,
            std_err=0.05,
            ci_lo=12.24,
            ci_hi=12.44,
            elapsed_ms=18.7,
            ts_ns=time.time_ns(),
        )
        client.send_mc_result(mc)
        print(f"[client] Sent MCResult {mc.symbol}  price={mc.price:.4f}")
        client.close()
        return

    # ── Real RDMA path ────────────────────────────────────────────────────────
    conn = RDMAConnection(device_name=device)
    buf  = conn.alloc_buffer(SHM_SIZE)
    local_info = conn.local_qp_info()
    remote_info = _exchange_qp_info_client(server_host, port, local_info)
    conn.connect(remote_info)
    print(f"[client] Connected. QPN={conn.qp.qp_num} remote={remote_info.qp_num}")

    snap = NBBOSnapshot(
        symbol="AAPL",
        bid=189.50,
        ask=189.51,
        bid_sz=500.0,
        ask_sz=300.0,
        ts_ns=time.time_ns(),
    )
    # Set flag byte before writing
    buf.array[0] = 0xFF
    buf.array[1] = 0x00
    buf.write_nbbo(snap)

    t0 = time.perf_counter_ns()
    conn.rdma_write(NBBO_STRUCT_SIZE + 2)
    ok = conn.poll_completion(timeout_us=5_000)
    elapsed_us = (time.perf_counter_ns() - t0) / 1_000
    print(f"[client] RDMA_WRITE {'OK' if ok else 'TIMEOUT'}  lat={elapsed_us:.2f}µs")
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RDMA low-latency market data transport")
    parser.add_argument("--mode",   choices=["server", "client", "bench"], default="bench")
    parser.add_argument("--server", default="127.0.0.1", help="Server IP (client mode)")
    parser.add_argument("--port",   type=int, default=18515)
    parser.add_argument("--device", default=None,  help="RDMA device name e.g. mlx5_0, efa_0")
    parser.add_argument("--iters",  type=int, default=10_000, help="Benchmark iterations")
    args = parser.parse_args()

    if args.mode == "bench":
        print(f"Running latency benchmark ({args.iters:,} iterations, simulate={SIMULATE}) …\n")
        stats = benchmark_roundtrip(args.iters)
        print(f"  n         : {stats['n']:,}")
        print(f"  mean      : {stats['mean_us']:.2f} µs")
        print(f"  median    : {stats['median_us']:.2f} µs")
        print(f"  p99       : {stats['p99_us']:.2f} µs")
        print(f"  p99.9     : {stats['p999_us']:.2f} µs")
        print(f"  min       : {stats['min_us']:.2f} µs")
        print(f"  max       : {stats['max_us']:.2f} µs")
        print(f"\n  (Real EFA/IB hardware target: <2 µs mean, <5 µs p99)")

    elif args.mode == "server":
        run_server(port=args.port, device=args.device)

    elif args.mode == "client":
        run_client(server_host=args.server, port=args.port, device=args.device)
