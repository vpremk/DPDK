/*
 * DPDK-Style Packet Processing using libpcap — macOS
 * ====================================================
 * Demonstrates DPDK architectural patterns using native macOS libpcap.
 * On EC2 Nitro + EFA: replace pcap_* calls with rte_eth_rx_burst / rte_eth_tx_burst.
 *
 * Compile:
 *   clang -O2 -Wall dpdk_pcap.c -lpcap -o dpdk_pcap
 *
 * Run (requires sudo for pcap):
 *   sudo ./dpdk_pcap <interface>   e.g.  sudo ./dpdk_pcap en0
 *   ./dpdk_pcap --offline test.pcap       (offline replay mode)
 *
 * What this shows:
 *   1. Lock-free SPSC ring buffer
 *   2. Pre-allocated mbuf pool
 *   3. BPF filter installation
 *   4. Burst packet capture (rx_burst equivalent)
 *   5. Packet header parsing (ETH/IP/UDP)
 *   6. FIX message detection
 *   7. Per-packet latency measurement
 *   8. Hardware timestamp simulation
 *   9. Statistics (PPS, throughput, drops)
 *   10. Graceful shutdown
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <signal.h>
#include <time.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <arpa/inet.h>
#include <net/ethernet.h>
#include <netinet/ip.h>
#include <netinet/udp.h>
#include <pcap/pcap.h>

/* ── Configuration ─────────────────────────────────────────────────── */
#define MBUF_POOL_SIZE      4096           /* total packet buffers        */
#define MBUF_DATA_SIZE      2048           /* max packet bytes            */
#define MBUF_HEADROOM       128            /* bytes before packet data    */
#define RING_SIZE           1024           /* must be power of 2          */
#define BURST_SIZE          32             /* packets per rx_burst call   */
#define PCAP_SNAPLEN        65535          /* capture full packet         */
#define PCAP_TIMEOUT_MS     0              /* 0 = non-blocking poll mode  */
#define FIX_PORT            4567           /* FIX-over-UDP port           */
#define STATS_INTERVAL_SEC  1              /* print stats every N seconds */

/* ── Colour output ──────────────────────────────────────────────────── */
#define GRN  "\033[0;32m"
#define YLW  "\033[0;33m"
#define CYN  "\033[0;36m"
#define RED  "\033[0;31m"
#define RST  "\033[0m"

/* ─────────────────────────────────────────────────────────────────────
 * 1. MBUF — packet buffer descriptor
 * ───────────────────────────────────────────────────────────────────── */
typedef struct mbuf {
    uint8_t  buf[MBUF_HEADROOM + MBUF_DATA_SIZE];  /* raw storage        */
    uint16_t data_off;    /* offset from buf[] to first packet byte       */
    uint16_t data_len;    /* bytes of actual packet data                  */
    uint16_t pkt_len;     /* total packet length (same as data_len here)  */
    uint8_t  port;        /* NIC port this arrived on                     */
    uint64_t timestamp_ns;/* hardware RX timestamp                        */
    uint32_t rss_hash;    /* RSS hash for queue affinity                  */
    uint8_t  _pad[2];
    struct mbuf *next;    /* chaining for multi-segment packets            */
} __attribute__((aligned(64))) mbuf_t;   /* cache-line aligned           */

/* Accessor: pointer to packet data region */
#define MBUF_DATA_PTR(m)  ((uint8_t*)((m)->buf) + (m)->data_off)

/* ─────────────────────────────────────────────────────────────────────
 * 2. MEMPOOL — pre-allocated buffer pool
 * ───────────────────────────────────────────────────────────────────── */
typedef struct {
    mbuf_t  *pool;               /* contiguous slab allocation            */
    mbuf_t **free_stack;         /* stack of free mbuf pointers           */
    _Atomic int top;             /* stack top (atomic for thread-safety)  */
    int      capacity;
} mempool_t;

/* Round sz up to next multiple of align (required by aligned_alloc). */
static inline size_t align_up(size_t sz, size_t align) {
    return (sz + align - 1) & ~(align - 1);
}

static mempool_t *mempool_create(int n) {
    mempool_t *mp = calloc(1, sizeof(mempool_t));
    if (!mp) { perror("mempool calloc"); exit(1); }
    mp->capacity = n;

    /* aligned_alloc: size MUST be a multiple of alignment */
    size_t pool_sz = align_up((size_t)n * sizeof(mbuf_t), 64);
    mp->pool       = aligned_alloc(64, pool_sz);
    mp->free_stack = malloc((size_t)n * sizeof(mbuf_t *));
    if (!mp->pool || !mp->free_stack) { perror("mempool alloc"); exit(1); }

    /* pre-populate free stack */
    for (int i = 0; i < n; i++) {
        mbuf_t *m     = &mp->pool[i];
        m->data_off   = MBUF_HEADROOM;
        m->data_len   = 0;
        m->next       = NULL;
        mp->free_stack[i] = m;
    }
    atomic_store(&mp->top, n);
    printf(GRN "  [mempool] %d mbufs × %zu B = %zu KB\n" RST,
           n, sizeof(mbuf_t), (size_t)n * sizeof(mbuf_t) / 1024);
    return mp;
}

/* O(1) alloc — no malloc during packet processing */
static inline mbuf_t *mbuf_alloc(mempool_t *mp) {
    /* fetch_sub on atomic int: guard against underflow below 0 */
    int t = atomic_load_explicit(&mp->top, memory_order_relaxed);
    while (t > 0) {
        if (atomic_compare_exchange_weak_explicit(
                &mp->top, &t, t - 1,
                memory_order_acquire, memory_order_relaxed)) {
            mbuf_t *m   = mp->free_stack[t - 1];
            m->data_off = MBUF_HEADROOM;
            m->data_len = 0;
            m->next     = NULL;
            return m;
        }
        /* t reloaded by CAS failure — retry */
    }
    return NULL;   /* pool exhausted */
}

/* O(1) free — return to pool (with bounds guard against double-free) */
static inline void mbuf_free(mempool_t *mp, mbuf_t *m) {
    int t = atomic_load_explicit(&mp->top, memory_order_relaxed);
    if (t >= mp->capacity) return;   /* pool full — drop silently */
    if (atomic_compare_exchange_strong_explicit(
            &mp->top, &t, t + 1,
            memory_order_release, memory_order_relaxed)) {
        mp->free_stack[t] = m;
    }
}

/* ─────────────────────────────────────────────────────────────────────
 * 3. LOCK-FREE SPSC RING BUFFER
 * ───────────────────────────────────────────────────────────────────── */
typedef struct {
    mbuf_t  *ring[RING_SIZE];
    _Atomic uint32_t head;      /* producer writes here (cache-line pad)  */
    uint8_t  _pad1[60];
    _Atomic uint32_t tail;      /* consumer reads here (separate cache line) */
    uint8_t  _pad2[60];
    _Atomic uint64_t drops;
} ring_t __attribute__((aligned(64)));

static ring_t *ring_create(void) {
    /* aligned_alloc requires size to be a multiple of alignment.
     * sizeof(ring_t) may not satisfy this — use align_up to be safe. */
    size_t sz = align_up(sizeof(ring_t), 64);
    ring_t *r = aligned_alloc(64, sz);
    if (!r) { perror("ring aligned_alloc"); exit(1); }
    memset(r, 0, sz);
    return r;
}

static inline bool ring_enqueue(ring_t *r, mbuf_t *m) {
    uint32_t h    = atomic_load_explicit(&r->head, memory_order_relaxed);
    uint32_t next = (h + 1) & (RING_SIZE - 1);
    if (next == atomic_load_explicit(&r->tail, memory_order_acquire)) {
        atomic_fetch_add(&r->drops, 1);
        return false;   /* ring full */
    }
    r->ring[h] = m;
    atomic_store_explicit(&r->head, next, memory_order_release);
    return true;
}

static inline mbuf_t *ring_dequeue(ring_t *r) {
    uint32_t t = atomic_load_explicit(&r->tail, memory_order_relaxed);
    if (t == atomic_load_explicit(&r->head, memory_order_acquire))
        return NULL;   /* ring empty */
    mbuf_t *m = r->ring[t];
    atomic_store_explicit(&r->tail, (t+1) & (RING_SIZE-1), memory_order_release);
    return m;
}

/* ─────────────────────────────────────────────────────────────────────
 * 4. NANOSECOND CLOCK
 * ───────────────────────────────────────────────────────────────────── */
static inline uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* ─────────────────────────────────────────────────────────────────────
 * 5. PACKET PARSER — ETH / IP / UDP / FIX detection
 * ───────────────────────────────────────────────────────────────────── */
typedef struct {
    uint8_t  dst_mac[6];
    uint8_t  src_mac[6];
    uint16_t ethertype;
} __attribute__((packed)) eth_hdr_t;

typedef struct {
    uint8_t  src_ip[4];
    uint8_t  dst_ip[4];
    uint8_t  proto;
    uint16_t src_port;
    uint16_t dst_port;
    uint16_t payload_len;
    bool     is_fix;
    char     fix_msg_type;   /* tag 35 */
} parsed_pkt_t;

static bool parse_packet(const uint8_t *data, uint16_t len, parsed_pkt_t *out) {
    memset(out, 0, sizeof(*out));

    if (len < sizeof(eth_hdr_t) + sizeof(struct ip))
        return false;

    const eth_hdr_t *eth = (const eth_hdr_t *)data;
    if (ntohs(eth->ethertype) != ETHERTYPE_IP)
        return false;

    const struct ip *ip = (const struct ip *)(data + sizeof(eth_hdr_t));
    memcpy(out->src_ip, &ip->ip_src, 4);
    memcpy(out->dst_ip, &ip->ip_dst, 4);
    out->proto = ip->ip_p;

    if (ip->ip_p != IPPROTO_UDP)
        return true;

    int ip_hdr_len = ip->ip_hl * 4;
    const struct udphdr *udp = (const struct udphdr *)(
        data + sizeof(eth_hdr_t) + ip_hdr_len);

    out->src_port    = ntohs(udp->uh_sport);
    out->dst_port    = ntohs(udp->uh_dport);
    out->payload_len = ntohs(udp->uh_ulen) - sizeof(struct udphdr);

    /* FIX detection: check dst port and prefix "8=FIX" */
    if (out->dst_port == FIX_PORT) {
        const uint8_t *payload = (const uint8_t *)udp + sizeof(struct udphdr);
        uint16_t plen = out->payload_len;
        if (plen >= 7 && memcmp(payload, "8=FIX", 5) == 0) {
            out->is_fix = true;
            /* scan for tag 35= */
            for (int i = 0; i < (int)plen - 4; i++) {
                if (payload[i]   == '\x01' &&
                    payload[i+1] == '3' &&
                    payload[i+2] == '5' &&
                    payload[i+3] == '=') {
                    out->fix_msg_type = (char)payload[i+4];
                    break;
                }
            }
        }
    }
    return true;
}

/* ─────────────────────────────────────────────────────────────────────
 * 6. STATISTICS
 * ───────────────────────────────────────────────────────────────────── */
typedef struct {
    _Atomic uint64_t rx_packets;
    _Atomic uint64_t rx_bytes;
    _Atomic uint64_t rx_drops;
    _Atomic uint64_t fix_orders;
    _Atomic uint64_t parse_errors;
    uint64_t lat_sum_ns;
    uint64_t lat_min_ns;
    uint64_t lat_max_ns;
    uint64_t lat_count;
} stats_t;

static stats_t g_stats;
static volatile sig_atomic_t g_running = 1;

static void stats_print(void) {
    static uint64_t last_rx  = 0;
    static uint64_t last_ts  = 0;
    uint64_t now  = now_ns();
    uint64_t rx   = atomic_load(&g_stats.rx_packets);
    double   dt   = (double)(now - last_ts) / 1e9;
    double   pps  = (last_ts == 0) ? 0 : (double)(rx - last_rx) / dt;
    uint64_t bps  = (uint64_t)(atomic_load(&g_stats.rx_bytes) * 8 / (now_ns() / 1e9));

    printf(CYN "\n  ┌─ PMD Stats ──────────────────────────────\n" RST);
    printf("  │ RX packets    : %12" PRIu64 "\n", rx);
    printf("  │ RX drops      : %12" PRIu64 "\n", atomic_load(&g_stats.rx_drops));
    printf("  │ Throughput    : %12.0f pps\n",    pps);
    printf("  │ Bandwidth     : %12" PRIu64 " bps\n", bps);
    printf("  │ FIX orders    : %12" PRIu64 "\n", atomic_load(&g_stats.fix_orders));
    printf("  │ Parse errors  : %12" PRIu64 "\n", atomic_load(&g_stats.parse_errors));
    if (g_stats.lat_count > 0) {
        printf("  ├─ Latency ───────────────────────────────\n");
        printf("  │ min           : %12" PRIu64 " ns\n", g_stats.lat_min_ns);
        printf("  │ avg           : %12" PRIu64 " ns\n",
               g_stats.lat_sum_ns / g_stats.lat_count);
        printf("  │ max           : %12" PRIu64 " ns\n", g_stats.lat_max_ns);
    }
    printf(CYN "  └─────────────────────────────────────────\n" RST);

    last_rx = rx;
    last_ts = now;
}

/* ─────────────────────────────────────────────────────────────────────
 * 7. PCAP RX BURST — equivalent to rte_eth_rx_burst
 * ───────────────────────────────────────────────────────────────────── */

typedef struct {
    mempool_t *pool;
    ring_t    *rx_ring;
    int        burst_size;
    uint64_t   burst_count;
} pcap_ctx_t;

/*
 * pcap callback — called per-packet by pcap_dispatch.
 * Maps 1:1 to how DPDK PMD callback fills RX ring.
 */
static void pcap_pkt_callback(uint8_t *user,
                               const struct pcap_pkthdr *hdr,
                               const uint8_t *pkt_data)
{
    pcap_ctx_t *ctx = (pcap_ctx_t *)user;

    /* alloc mbuf from pool (zero malloc) */
    mbuf_t *m = mbuf_alloc(ctx->pool);
    if (!m) {
        atomic_fetch_add(&g_stats.rx_drops, 1);
        return;
    }

    /* copy packet into mbuf data region */
    uint16_t caplen = (uint16_t)(hdr->caplen < MBUF_DATA_SIZE
                                 ? hdr->caplen : MBUF_DATA_SIZE);
    memcpy(MBUF_DATA_PTR(m), pkt_data, caplen);
    m->data_len     = caplen;
    m->pkt_len      = caplen;
    m->timestamp_ns = (uint64_t)hdr->ts.tv_sec * 1000000000ULL
                    + (uint64_t)hdr->ts.tv_usec * 1000ULL;

    /* enqueue to ring — PMD equivalent of filling SW RX ring */
    if (!ring_enqueue(ctx->rx_ring, m)) {
        mbuf_free(ctx->pool, m);
        atomic_fetch_add(&g_stats.rx_drops, 1);
    }
}

/*
 * rx_burst: drain up to BURST_SIZE packets from ring.
 * Returns number of packets placed in mbufs[].
 */
static int rx_burst(ring_t *ring, mbuf_t **mbufs, int max) {
    int n = 0;
    while (n < max) {
        mbuf_t *m = ring_dequeue(ring);
        if (!m) break;
        mbufs[n++] = m;
    }
    return n;
}

/* ─────────────────────────────────────────────────────────────────────
 * 8. PROCESSING PIPELINE — decode + classify packets
 * ───────────────────────────────────────────────────────────────────── */
static void process_burst(mempool_t *pool, mbuf_t **mbufs, int n) {
    uint64_t now = now_ns();

    for (int i = 0; i < n; i++) {
        mbuf_t *m = mbufs[i];

        /* update stats */
        atomic_fetch_add(&g_stats.rx_packets, 1);
        atomic_fetch_add(&g_stats.rx_bytes,   m->data_len);

        /* latency: arrival time vs processing time */
        if (m->timestamp_ns > 0 && now >= m->timestamp_ns) {
            uint64_t lat = now - m->timestamp_ns;
            g_stats.lat_sum_ns += lat;
            g_stats.lat_count++;
            if (lat < g_stats.lat_min_ns || g_stats.lat_min_ns == 0)
                g_stats.lat_min_ns = lat;
            if (lat > g_stats.lat_max_ns)
                g_stats.lat_max_ns = lat;
        }

        /* parse headers */
        parsed_pkt_t parsed;
        if (!parse_packet(MBUF_DATA_PTR(m), m->data_len, &parsed)) {
            atomic_fetch_add(&g_stats.parse_errors, 1);
            mbuf_free(pool, m);
            continue;
        }

        /* classify */
        if (parsed.is_fix) {
            atomic_fetch_add(&g_stats.fix_orders, 1);
            char type_str[32] = "Unknown";
            switch (parsed.fix_msg_type) {
                case 'D': strcpy(type_str, "NewOrderSingle"); break;
                case '8': strcpy(type_str, "ExecutionReport"); break;
                case 'G': strcpy(type_str, "OrderCancelReplace"); break;
                case 'F': strcpy(type_str, "OrderCancelRequest"); break;
            }
            printf(GRN "  [FIX] %s (35=%c) UDP %d.%d.%d.%d:%d → %d.%d.%d.%d:%d\n"
                   RST,
                   type_str, parsed.fix_msg_type,
                   parsed.src_ip[0], parsed.src_ip[1],
                   parsed.src_ip[2], parsed.src_ip[3], parsed.src_port,
                   parsed.dst_ip[0], parsed.dst_ip[1],
                   parsed.dst_ip[2], parsed.dst_ip[3], parsed.dst_port);
        } else if (parsed.proto == IPPROTO_UDP) {
            /* non-FIX UDP — market data feed etc */
        }

        /* free mbuf back to pool (O(1) — no free()) */
        mbuf_free(pool, m);
    }
}

/* ─────────────────────────────────────────────────────────────────────
 * SIGNAL HANDLER
 * ───────────────────────────────────────────────────────────────────── */
static void sig_handler(int sig) {
    (void)sig;
    g_running = 0;
}

/* ─────────────────────────────────────────────────────────────────────
 * MAIN
 * ───────────────────────────────────────────────────────────────────── */
int main(int argc, char *argv[]) {
    printf("\n" CYN "═══════════════════════════════════════════════\n"
           "  DPDK-Style Packet Processor — libpcap/macOS\n"
           "═══════════════════════════════════════════════\n" RST);

    if (argc < 2) {
        fprintf(stderr,
            YLW "\nUsage:\n"
            "  sudo ./dpdk_pcap <interface>       live capture\n"
            "  ./dpdk_pcap --offline <file.pcap>  offline replay\n\n"
            "Examples:\n"
            "  sudo ./dpdk_pcap en0\n"
            "  ./dpdk_pcap --offline capture.pcap\n" RST);
        return 1;
    }

    signal(SIGINT,  sig_handler);
    signal(SIGTERM, sig_handler);

    /* ── Init subsystems ──────────────────────────────────────────── */
    printf("\n[EAL] Initialising subsystems...\n");
    mempool_t *pool    = mempool_create(MBUF_POOL_SIZE);
    ring_t    *rx_ring = ring_create();
    pcap_ctx_t ctx     = { pool, rx_ring, BURST_SIZE, 0 };
    mbuf_t    *burst[BURST_SIZE];

    /* ── Open pcap handle ─────────────────────────────────────────── */
    char errbuf[PCAP_ERRBUF_SIZE];
    pcap_t *handle;
    bool offline = (strcmp(argv[1], "--offline") == 0);

    if (offline && argc >= 3) {
        printf("[pcap] Opening offline file: %s\n", argv[2]);
        handle = pcap_open_offline(argv[2], errbuf);
    } else {
        printf("[pcap] Opening interface: %s (non-blocking poll)\n", argv[1]);
        handle = pcap_open_live(argv[1], PCAP_SNAPLEN, 1,
                                PCAP_TIMEOUT_MS, errbuf);
    }

    if (!handle) {
        fprintf(stderr, RED "[ERROR] pcap open failed: %s\n" RST, errbuf);
        return 1;
    }

    /* ── Install BPF filter (whitelist traffic) ───────────────────── */
    const char *filter_expr = "udp port 4567 or udp";
    struct bpf_program bpf;
    if (pcap_compile(handle, &bpf, filter_expr, 1, PCAP_NETMASK_UNKNOWN) == 0) {
        pcap_setfilter(handle, &bpf);
        pcap_freecode(&bpf);
        printf("[bpf] Filter installed: \"%s\"\n", filter_expr);
    }

    /* ── Set non-blocking (poll mode equivalent) ─────────────────── */
    if (!offline)
        pcap_setnonblock(handle, 1, errbuf);

    printf(GRN "\n[PMD] Polling... (Ctrl+C to stop)\n\n" RST);
    memset(&g_stats, 0, sizeof(g_stats));

    uint64_t last_stats_ts = now_ns();

    /* ── Main poll loop ───────────────────────────────────────────── */
    while (g_running) {
        /*
         * pcap_dispatch: capture up to BURST_SIZE packets, call callback per pkt.
         * Returns -2 on offline EOF, -1 on error, 0 if no packets (live).
         */
        int ret = pcap_dispatch(handle, BURST_SIZE, pcap_pkt_callback,
                                (uint8_t *)&ctx);

        bool eof = (ret == -2);   /* PCAP_ERROR_BREAK = offline EOF */
        if (ret == -1) break;     /* hard error */

        /*
         * rx_burst: drain SW ring into local burst array.
         * Equivalent to rte_eth_rx_burst(port, queue, mbufs, BURST_SIZE).
         */
        int nb_rx = rx_burst(rx_ring, burst, BURST_SIZE);
        if (nb_rx > 0)
            process_burst(pool, burst, nb_rx);

        /* drain ring fully on EOF before exiting */
        if (eof) {
            while ((nb_rx = rx_burst(rx_ring, burst, BURST_SIZE)) > 0)
                process_burst(pool, burst, nb_rx);
            printf(YLW "\n[pcap] EOF reached.\n" RST);
            break;
        }

        /* periodic stats print (live mode only) */
        uint64_t ts_now = now_ns();
        if (!offline &&
            ts_now - last_stats_ts >= (uint64_t)STATS_INTERVAL_SEC * 1000000000ULL) {
            stats_print();
            last_stats_ts = ts_now;
        }
    }

    /* ── Final stats & cleanup ───────────────────────────────────── */
    printf(YLW "\n[EAL] Shutting down...\n" RST);
    stats_print();
    pcap_close(handle);
    free(pool->pool);
    free(pool->free_stack);
    free(pool);
    free(rx_ring);

    printf(CYN "\n═══════════════════════════════════════════════\n"
           "  Done.\n"
           "═══════════════════════════════════════════════\n\n" RST);
    return 0;
}
