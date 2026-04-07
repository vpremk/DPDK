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
#include <inttypes.h>
#include <errno.h>

/* ── Configuration ─────────────────────────────────────────────────── */
#define MBUF_POOL_SIZE      4096           /* total packet buffers        */
#define MBUF_DATA_SIZE      2048           /* max packet bytes            */
#define MBUF_HEADROOM       128            /* bytes before packet data    */
#define RING_SIZE           1024           /* must be power of 2          */
#define BURST_SIZE          32             /* packets per rx_burst call   */
#define PCAP_SNAPLEN        65535          /* capture full packet         */
#define PCAP_TIMEOUT_MS     0              /* 0 = non-blocking poll mode  */
#define FIX_PORT            4567           /* FIX-over-UDP port           */
#define MKTDATA_PORT        5678           /* protobuf market-data port   */
#define STATS_INTERVAL_SEC  1              /* print stats every N seconds */

/* ── Market-data wire header (matches send_market_data.py) ──────────────
 *   byte 0      : MsgType enum (1=NBBO 2=TRADE 3=BOOK 4=DELTA 7=HB)
 *   bytes 1..4  : big-endian uint32  proto payload length
 *   bytes 5..N  : serialised MarketDataEnvelope protobuf
 * ─────────────────────────────────────────────────────────────────────── */
#define MKTDATA_HEADER_SIZE 5

typedef enum {
    MKTDATA_UNKNOWN  = 0,
    MKTDATA_NBBO     = 1,
    MKTDATA_TRADE    = 2,
    MKTDATA_BOOK     = 3,
    MKTDATA_DELTA    = 4,
    MKTDATA_IMBAL    = 5,
    MKTDATA_STATUS   = 6,
    MKTDATA_HB       = 7,
} mktdata_msg_type_t;

static const char *mktdata_type_str(uint8_t t) {
    switch (t) {
        case MKTDATA_NBBO:  return "NBBO ";
        case MKTDATA_TRADE: return "TRADE";
        case MKTDATA_BOOK:  return "BOOK ";
        case MKTDATA_DELTA: return "DELTA";
        case MKTDATA_IMBAL: return "IMBAL";
        case MKTDATA_STATUS:return "STAT ";
        case MKTDATA_HB:    return "HB   ";
        default:            return "?????";
    }
}

/* Counters for market-data stats */
static _Atomic uint64_t g_mktdata_pkts  = 0;
static _Atomic uint64_t g_mktdata_bytes = 0;

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
    uint8_t        src_ip[4];
    uint8_t        dst_ip[4];
    uint8_t        proto;
    uint16_t       src_port;
    uint16_t       dst_port;
    uint16_t       payload_len;
    bool           is_fix;
    char           fix_msg_type;      /* tag 35                              */
    const uint8_t *fix_payload;       /* pointer into mbuf — zero-copy       */
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
            out->is_fix      = true;
            out->fix_payload = payload;
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

/* ─────────────────────────────────────────────────────────────────────
 * 6b. AUDIT LOG — one JSON line per captured FIX order
 *
 * Format (newline-delimited JSON):
 * {"ts_utc":"2024-04-06T14:23:01.123456789Z","ts_ns":1234567890,
 *  "seq":42,"msg_type":"NewOrderSingle","tag35":"D",
 *  "cl_ord_id":"ORD000042","symbol":"AAPL","side":"Buy","qty":500,
 *  "price":"123.45","src":"192.168.1.200:54321",
 *  "dst":"192.168.1.165:4567","payload_len":180,"lat_ns":412}
 * ───────────────────────────────────────────────────────────────────── */
#define AUDIT_LOG_PATH "fix_audit.log"

static FILE *g_audit_fp = NULL;

/* Extract a FIX tag value (NUL-terminated) from payload.
 * tag_str e.g. "\x0155=" — SOH prefix + tag number + '='.
 * Returns pointer into payload on match, NULL if not found.          */
static const char *fix_tag(const uint8_t *payload, uint16_t plen,
                             const char *tag_with_soh, char *out, size_t outsz)
{
    out[0] = '\0';
    size_t tlen = strlen(tag_with_soh);
    for (int i = 0; i < (int)plen - (int)tlen; i++) {
        if (memcmp(payload + i, tag_with_soh, tlen) == 0) {
            /* value runs until next SOH or end of payload */
            const uint8_t *v = payload + i + tlen;
            size_t n = 0;
            while ((size_t)(v - payload) + n < plen &&
                   v[n] != '\x01' && n < outsz - 1)
                n++;
            memcpy(out, v, n);
            out[n] = '\0';
            return out;
        }
    }
    return NULL;
}

static void audit_log_fix(const parsed_pkt_t *p,
                           const uint8_t *payload, uint16_t plen,
                           uint64_t rx_ns, uint64_t lat_ns,
                           uint64_t seq)
{
    if (!g_audit_fp) return;

    /* Wall-clock timestamp */
    struct timespec wts;
    clock_gettime(CLOCK_REALTIME, &wts);
    char ts_buf[40];
    struct tm tm_info;
    gmtime_r(&wts.tv_sec, &tm_info);
    strftime(ts_buf, sizeof(ts_buf), "%Y-%m-%dT%H:%M:%S", &tm_info);

    /* msg_type label */
    const char *type_str = "Unknown";
    switch (p->fix_msg_type) {
        case 'D': type_str = "NewOrderSingle";      break;
        case '8': type_str = "ExecutionReport";     break;
        case 'G': type_str = "OrderCancelReplace";  break;
        case 'F': type_str = "OrderCancelRequest";  break;
        case 'V': type_str = "MarketDataRequest";   break;
        case 'W': type_str = "MarketDataSnapshot";  break;
    }

    /* extract FIX tags — SOH (\x01) is the field delimiter.
     * Use string concatenation ("\x01" "11=") to prevent the C compiler
     * from treating the digit after \x01 as part of the hex sequence.  */
    char cl_ord_id[32], symbol[16], side[4], qty[16], price[20];
    fix_tag(payload, plen, "\x01" "11=", cl_ord_id, sizeof(cl_ord_id));
    fix_tag(payload, plen, "\x01" "55=", symbol,    sizeof(symbol));
    fix_tag(payload, plen, "\x01" "54=", side,      sizeof(side));
    fix_tag(payload, plen, "\x01" "38=", qty,       sizeof(qty));
    fix_tag(payload, plen, "\x01" "44=", price,     sizeof(price));

    /* side code → human label */
    const char *side_label = strcmp(side, "1") == 0 ? "Buy"  :
                             strcmp(side, "2") == 0 ? "Sell" : side;

    fprintf(g_audit_fp,
        "{\"ts_utc\":\"%s.%09ldZ\","
        "\"ts_ns\":%" PRIu64 ","
        "\"seq\":%" PRIu64 ","
        "\"msg_type\":\"%s\","
        "\"tag35\":\"%c\","
        "\"cl_ord_id\":\"%s\","
        "\"symbol\":\"%s\","
        "\"side\":\"%s\","
        "\"qty\":\"%s\","
        "\"price\":\"%s\","
        "\"src\":\"%d.%d.%d.%d:%d\","
        "\"dst\":\"%d.%d.%d.%d:%d\","
        "\"payload_len\":%d,"
        "\"lat_ns\":%" PRIu64 "}\n",
        ts_buf, wts.tv_nsec,
        rx_ns, seq,
        type_str, p->fix_msg_type,
        cl_ord_id, symbol, side_label, qty, price,
        p->src_ip[0], p->src_ip[1], p->src_ip[2], p->src_ip[3], p->src_port,
        p->dst_ip[0], p->dst_ip[1], p->dst_ip[2], p->dst_ip[3], p->dst_port,
        plen, lat_ns);
    fflush(g_audit_fp);   /* flush every line — audit must not buffer        */
}

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
            uint64_t fix_seq = atomic_fetch_add(&g_stats.fix_orders, 1) + 1;
            uint64_t lat_ns  = (m->timestamp_ns > 0 && now >= m->timestamp_ns)
                               ? now - m->timestamp_ns : 0;

            /* extract key FIX tags for console + audit */
            char cl_ord_id[32] = "-", symbol[16] = "-";
            char side_raw[4]   = "-", qty[16]    = "-", price[20] = "-";
            if (parsed.fix_payload) {
                fix_tag(parsed.fix_payload, parsed.payload_len,
                        "\x01" "11=", cl_ord_id, sizeof(cl_ord_id));
                fix_tag(parsed.fix_payload, parsed.payload_len,
                        "\x01" "55=", symbol,    sizeof(symbol));
                fix_tag(parsed.fix_payload, parsed.payload_len,
                        "\x01" "54=", side_raw,  sizeof(side_raw));
                fix_tag(parsed.fix_payload, parsed.payload_len,
                        "\x01" "38=", qty,       sizeof(qty));
                fix_tag(parsed.fix_payload, parsed.payload_len,
                        "\x01" "44=", price,     sizeof(price));
            }
            const char *side_label = strcmp(side_raw,"1")==0 ? "Buy"  :
                                     strcmp(side_raw,"2")==0 ? "Sell" : side_raw;
            const char *type_str   = "Unknown";
            switch (parsed.fix_msg_type) {
                case 'D': type_str = "NewOrderSingle";     break;
                case '8': type_str = "ExecutionReport";    break;
                case 'G': type_str = "OrderCancelReplace"; break;
                case 'F': type_str = "OrderCancelRequest"; break;
                case 'V': type_str = "MktDataRequest";     break;
                case 'W': type_str = "MktDataSnapshot";    break;
            }

            /* ── console audit line ── */
            printf(GRN
                   "  [AUDIT #%5" PRIu64 "] %-20s | clOrdID=%-12s"
                   " sym=%-6s %s qty=%-6s px=%-9s"
                   " | %d.%d.%d.%d → %d.%d.%d.%d | lat=%" PRIu64 "ns\n" RST,
                   fix_seq, type_str,
                   cl_ord_id, symbol, side_label, qty, price,
                   parsed.src_ip[0], parsed.src_ip[1],
                   parsed.src_ip[2], parsed.src_ip[3],
                   parsed.dst_ip[0], parsed.dst_ip[1],
                   parsed.dst_ip[2], parsed.dst_ip[3],
                   lat_ns);

            /* ── structured audit log (NDJSON, flushed per line) ── */
            audit_log_fix(&parsed,
                          parsed.fix_payload, parsed.payload_len,
                          m->timestamp_ns, lat_ns, fix_seq);

        } else if (parsed.proto == IPPROTO_UDP &&
                   parsed.dst_port == MKTDATA_PORT) {
            /* ── Market-data feed (protobuf on port 5678) ────────────────
             *
             * Wire format from send_market_data.py:
             *   [0]      uint8   MsgType  (mktdata_msg_type_t)
             *   [1..4]   uint32  big-endian proto payload length
             *   [5..N]   bytes   serialised MarketDataEnvelope protobuf
             *
             * We decode the 5-byte header here in the fast path.
             * Full protobuf deserialisation (nanopb) would follow in a
             * dedicated worker thread to keep this rx loop zero-copy.
             * ─────────────────────────────────────────────────────────── */
            const uint8_t *pay = parsed.fix_payload;  /* zero-copy ptr into mbuf */
            uint16_t  paylen = parsed.payload_len;

            if (paylen < MKTDATA_HEADER_SIZE) {
                /* runt — drop silently */
            } else {
                uint8_t  msg_type  = pay[0];
                uint32_t proto_len = ((uint32_t)pay[1] << 24) |
                                     ((uint32_t)pay[2] << 16) |
                                     ((uint32_t)pay[3] <<  8) |
                                      (uint32_t)pay[4];

                uint64_t recv_ns   = m->timestamp_ns;
                uint64_t lat_ns    = recv_ns - m->timestamp_ns; /* wire→rx */

                atomic_fetch_add(&g_mktdata_pkts,  1);
                atomic_fetch_add(&g_mktdata_bytes, paylen);

                printf(CYN
                       "  [MKTDATA] %-5s | port=%u→%u"
                       " | proto_len=%-5u | %d.%d.%d.%d → %d.%d.%d.%d"
                       " | lat=%"PRIu64"ns\n" RST,
                       mktdata_type_str(msg_type),
                       parsed.src_port, parsed.dst_port,
                       proto_len,
                       parsed.src_ip[0], parsed.src_ip[1],
                       parsed.src_ip[2], parsed.src_ip[3],
                       parsed.dst_ip[0], parsed.dst_ip[1],
                       parsed.dst_ip[2], parsed.dst_ip[3],
                       lat_ns);

                /* TODO: hand proto_bytes to nanopb worker thread:
                 *   uint8_t *proto_bytes = pay + MKTDATA_HEADER_SIZE;
                 *   nanopb_decode(msg_type, proto_bytes, proto_len);
                 */
            }
        } else if (parsed.proto == IPPROTO_UDP) {
            /* other UDP — silently counted in stats */
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

    /* ── Audit log ───────────────────────────────────────────────── */
    g_audit_fp = fopen(AUDIT_LOG_PATH, "a");
    if (!g_audit_fp) {
        fprintf(stderr, YLW "[WARN] Cannot open audit log %s: %s\n" RST,
                AUDIT_LOG_PATH, strerror(errno));
    } else {
        printf("[audit] Logging FIX orders → %s\n", AUDIT_LOG_PATH);
    }

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

    if (g_audit_fp) {
        fclose(g_audit_fp);
        printf("[audit] Log written to %s\n", AUDIT_LOG_PATH);
    }

    printf(CYN "\n═══════════════════════════════════════════════\n"
           "  Done.\n"
           "═══════════════════════════════════════════════\n\n" RST);
    return 0;
}
