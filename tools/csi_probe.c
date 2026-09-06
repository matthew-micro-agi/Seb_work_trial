/*
 * csi_probe — watch the Cadence CSI-2 RX bridge's stream FSM and frame monitor from userspace.
 *
 * Answers "when, relative to the trigger pulse, does the sensor actually put a frame on the
 * CSI-2 bus, and when does the receiver see the frame end". Both are needed to explain why the
 * capture driver's DMA completes one trigger period after the pulse (OPEN.md section 1).
 *
 * Registers (J722S TRM SPRUJB3D 12.6.1, csirx region at CSI_RX_IFn + 0x1000):
 *   0x104 + 0x100*n  CSIRX_STREAMn_STATUS         [1:0] PROTOCOL_FSM, [7:4] STREAM_FSM,
 *                                                 [8] READY_STATE, [31] RUNNING
 *   0x110 + 0x100*n  CSIRX_STREAMn_MONITOR_CTRL   [15] FRAME_MON_EN, [14:11] FRAME_MON_VC
 *   0x114 + 0x100*n  CSIRX_STREAMn_MONITOR_FRAME  [15:0] NB (last frame number), [31:16] PACKET_SIZE
 * and in the TI shim beside it (CSI_RX_IFn RX_SHIM region, the ticsi2rx node address):
 *   0x010            CSI_RX_IF_CNTL               [8+n] STREAMn_IDLE, 1 while that stream interface
 *                                                 is idle. It is the shim's own view of the frame:
 *                                                 the shim opens a PSI-L packet on SOF and closes it
 *                                                 with EOP on EOF (TRM 12.6.1.4.6.1), so the window
 *                                                 where this bit is 0 is the window the DMA is open.
 *
 * PROTOCOL_FSM leaves PROT_IDLE for every CSI-2 packet, short or long, so polling it hard gives the
 * bus activity envelope: the first packet of a frame (its Frame Start) and the last one (its Frame
 * End). Events are stamped with CLOCK_MONOTONIC, the same clock as V4L2 buffer timestamps and as
 * ptp_extts' fourth field, so all three can be put on one timeline.
 *
 *   csi_probe -b 0x30121000 [-w 0x30122000] [-s 0] [-t 3.0] [-g 300000] [-m] [-r]
 *     -b  csirx register base (cdns_csi2rx node address; see media-ctl -p)
 *     -w  TI shim register base (ticsi2rx node address), to follow STREAMn_IDLE as well
 *     -s  stream index (default 0)
 *     -t  seconds to watch (default 3)
 *     -g  gap in ns that separates two frames' bus activity (default 300000 = 0.3 ms)
 *     -m  enable the frame monitor for VC0 while probing, and restore it after
 *     -r  raw: one line per FSM transition instead of per-frame events (short runs only)
 *
 * Output, CSV, one event per line:
 *   FS,<t_mono_ns>,<nb>       frame number changed: the receiver latched a Frame Start
 *   ON,<t_mono_ns>,<nb>       bus activity resumed after a gap
 *   OFF,<t_mono_ns>,<nb>      last activity before a gap: the frame's last packet
 *   SOF,<t_mono_ns>,<nb>      shim stream interface left idle: the PSI-L packet opened
 *   EOF,<t_mono_ns>,<nb>      shim stream interface went idle: the PSI-L packet closed with EOP
 *   R,<t_mono_ns>,<prot>,<stream>,<ready>,<idle>,<nb>,<pktsize>   with -r
 *
 * Build on the board: gcc -O2 -o csi_probe tools/csi_probe.c      Needs root (/dev/mem).
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define STREAM_BASE(n)      (0x100 * ((n) + 1))
#define STREAM_STATUS(n)    (STREAM_BASE(n) + 0x04)
#define STREAM_MON_CTRL(n)  (STREAM_BASE(n) + 0x10)
#define STREAM_MON_FRAME(n) (STREAM_BASE(n) + 0x14)
#define MON_CTRL_EN         (1u << 15)
#define MON_CTRL_TIMER_EOF  (1u << 10)
#define MON_CTRL_TIMER_EN   (1u << 9)
#define STREAM_TIMER(n)     (STREAM_BASE(n) + 0x1c)
#define MONITOR_IRQS        0x018
#define MONITOR_IRQS_MASK   0x01c
#define MONITOR_TIMER_IRQ(n) (1u << ((n) * 8))
#define SHIM_CNTL           0x010
#define SHIM_STREAM_IDLE(n) (1u << (8 + (n)))

static int64_t now_ns(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}

struct rec { int64_t t; uint32_t st, fr; char kind; };

static volatile uint8_t *map_reg(int fd, unsigned long addr, unsigned long *off) {
    unsigned long page = addr & ~0xffful;
    *off = addr & 0xffful;
    volatile uint8_t *m = mmap(NULL, 0x1000 + *off, PROT_READ | PROT_WRITE, MAP_SHARED, fd, page);
    if (m == MAP_FAILED) { perror("mmap"); exit(1); }
    return m;
}

int main(int argc, char **argv) {
    unsigned long base = 0x30121000ul, shim = 0; int stream = 0, raw = 0, mon = 0, tim = 0, eof = 0;
    double secs = 3.0; int64_t gap = 300000;
    int c;
    while ((c = getopt(argc, argv, "b:w:s:t:g:mriE")) != -1) {
        switch (c) {
        case 'b': base = strtoul(optarg, NULL, 0); break;
        case 'w': shim = strtoul(optarg, NULL, 0); break;
        case 's': stream = atoi(optarg); break;
        case 't': secs = atof(optarg); break;
        case 'g': gap = strtoll(optarg, NULL, 0); break;
        case 'm': mon = 1; break;
        case 'r': raw = 1; break;
        case 'i': tim = 1; break;
        case 'E': eof = 1; break;
        default: fprintf(stderr, "usage: %s [-b base] [-w shim] [-s stream] [-t secs] [-g gap_ns] [-m] [-r] [-i [-E]]\n", argv[0]); return 2;
        }
    }
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) { perror("/dev/mem"); return 1; }
    unsigned long off;
    volatile uint8_t *m = map_reg(fd, base, &off);
    volatile uint32_t *status = (volatile uint32_t *)(m + off + STREAM_STATUS(stream));
    volatile uint32_t *mctrl  = (volatile uint32_t *)(m + off + STREAM_MON_CTRL(stream));
    volatile uint32_t *mframe = (volatile uint32_t *)(m + off + STREAM_MON_FRAME(stream));
    volatile uint32_t *scntl = NULL, dummy = 0;
    if (shim) {
        unsigned long soff;
        volatile uint8_t *sm = map_reg(fd, shim, &soff);
        scntl = (volatile uint32_t *)(sm + soff + SHIM_CNTL);
    } else {
        scntl = &dummy;
    }

    volatile uint32_t *mirqs = (volatile uint32_t *)(m + off + MONITOR_IRQS);
    volatile uint32_t *mmask = (volatile uint32_t *)(m + off + MONITOR_IRQS_MASK);
    volatile uint32_t *mtimer = (volatile uint32_t *)(m + off + STREAM_TIMER(stream));

    uint32_t saved_mctrl = *mctrl, saved_mmask = *mmask;
    fprintf(stderr, "csi_probe: status %08x monitor_ctrl %08x monitor_frame %08x shim_cntl %08x"
            " monitor_irqs %08x mask %08x\n",
            *status, saved_mctrl, *mframe, *scntl, *mirqs, saved_mmask);
    if (mon && !(saved_mctrl & MON_CTRL_EN)) *mctrl = saved_mctrl | MON_CTRL_EN;   /* VC0 */
    if (tim) {
        /*
         * Stream monitor timer, VC0, count 0: "won't count but interrupt will still trigger every
         * EOF or SOF" (TRM 12.6.1.4.8.11). TIMER_EOF picks which. Only the status bit is polled
         * here; the mask that would route it to the CPU is left alone.
         */
        *mtimer = 0;
        *mctrl = MON_CTRL_EN | MON_CTRL_TIMER_EN | (eof ? MON_CTRL_TIMER_EOF : 0);
        *mirqs = MONITOR_TIMER_IRQ(stream);
    }

    size_t cap = raw ? (size_t)4 << 20 : (size_t)1 << 18, n = 0;
    struct rec *log = malloc(cap * sizeof *log);
    if (!log) { perror("malloc"); return 1; }

    int64_t t_end = now_ns() + (int64_t)(secs * 1e9);
    uint32_t last_st = *status, last_fr = *mframe, last_sc = *scntl;
    int64_t last_active = 0; int active = 0;
    /* Tight poll. Three MMIO reads per pass over the APB bridges, a few hundred ns each. */
    for (;;) {
        int64_t t = now_ns();
        if (t > t_end) break;
        uint32_t st = *status, fr = *mframe, sc = *scntl;
        int busy = (st & 0x3) != 0 || ((st >> 4) & 0xf) != 0;
        if (tim && (*mirqs & MONITOR_TIMER_IRQ(stream))) {
            *mirqs = MONITOR_TIMER_IRQ(stream);          /* R/W1TC */
            if (n < cap) log[n++] = (struct rec){t, st, fr, 'T'};
        }
        if (raw) {
            if ((st != last_st || fr != last_fr || sc != last_sc) && n < cap)
                log[n++] = (struct rec){t, st | (sc & SHIM_STREAM_IDLE(stream) ? 0x10000 : 0), fr, 'R'};
        } else {
            if ((fr & 0xffff) != (last_fr & 0xffff) && n < cap)
                log[n++] = (struct rec){t, st, fr, 'F'};
            if (shim && (sc ^ last_sc) & SHIM_STREAM_IDLE(stream) && n < cap)
                log[n++] = (struct rec){t, st, fr, sc & SHIM_STREAM_IDLE(stream) ? 'E' : 'S'};
            if (busy) {
                if (!active && n < cap) log[n++] = (struct rec){t, st, fr, '+'};
                active = 1; last_active = t;
            } else if (active && t - last_active > gap) {
                if (n < cap) log[n++] = (struct rec){last_active, st, fr, '-'};
                active = 0;
            }
        }
        last_st = st; last_fr = fr; last_sc = sc;
    }
    if (tim) { *mctrl = saved_mctrl; *mmask = saved_mmask; }
    else if (mon && !(saved_mctrl & MON_CTRL_EN)) *mctrl = saved_mctrl;

    for (size_t i = 0; i < n; i++) {
        const struct rec *r = &log[i];
        const char *k = r->kind == 'F' ? "FS" : r->kind == '+' ? "ON" : r->kind == '-' ? "OFF"
                      : r->kind == 'S' ? "SOF" : r->kind == 'T' ? "TIM" : "EOF";
        if (r->kind == 'R')
            printf("R,%lld,%u,%u,%u,%u,%u,%u\n", (long long)r->t, r->st & 3, (r->st >> 4) & 0xf,
                   (r->st >> 8) & 1, (r->st >> 16) & 1, r->fr & 0xffff, r->fr >> 16);
        else
            printf("%s,%lld,%u\n", k, (long long)r->t, r->fr & 0xffff);
    }
    fprintf(stderr, "csi_probe: %zu events%s\n", n, n == cap ? " (buffer full)" : "");
    return 0;
}
