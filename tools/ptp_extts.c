/*
 * ptp_extts — read hardware edge timestamps from a PTP clock's external timestamp input.
 *
 * On the J722S EVM the CPTS (1588 clock in the Ethernet switch) has four HW_TS_PUSH inputs;
 * the pin overlay routes J28.33 (HW1TSPUSH pad, via the timesync router) to channel 0: the trigger
 * loopback. XVS on J28.32 goes to eCAP0, not CPTS.
 * Events are stamped by hardware in PTP time. Once a second the tool also prints the offset
 * between the PTP clock and CLOCK_MONOTONIC so edge times can be placed on the frame clock.
 *
 *   ptp_extts [-d /dev/ptp0] [-i channel] [-n count] [-e rising|falling|both] [-q]
 *
 * Output, one line per event, CSV for kmatch-replay style tooling:
 *   E,<t_ptp_ns>,<channel>,<t_mono_ns>       t_mono_ns = t_ptp_ns - offset (offset from the last sync)
 *   S,<t_mono_ns>,<offset_ns>,<uncertainty_ns> sync line: PTP - MONOTONIC and the read window
 * Build on the board: gcc -O2 -o ptp_extts tools/ptp_extts.c
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include <linux/ptp_clock.h>

static int64_t ts_ns(const struct timespec *t) { return (int64_t)t->tv_sec * 1000000000LL + t->tv_nsec; }
static int64_t pct_ns(const struct ptp_clock_time *t) { return (int64_t)t->sec * 1000000000LL + t->nsec; }

/* PTP - CLOCK_MONOTONIC. Prefer PTP_SYS_OFFSET_PRECISE (cross-timestamp), else the sandwich
 * read PTP_SYS_OFFSET_EXTENDED, else the old PTP_SYS_OFFSET. The system side of those ioctls is
 * CLOCK_REALTIME, so REALTIME->MONOTONIC is bridged with a back-to-back read here. */
static int sync_offset(int fd, int64_t *offset, int64_t *uncertainty) {
    struct timespec rt0, mo, rt1;
    clock_gettime(CLOCK_REALTIME, &rt0); clock_gettime(CLOCK_MONOTONIC, &mo); clock_gettime(CLOCK_REALTIME, &rt1);
    int64_t rt_minus_mono = (ts_ns(&rt0) + ts_ns(&rt1)) / 2 - ts_ns(&mo);
#ifdef PTP_SYS_OFFSET_PRECISE
    {
        struct ptp_sys_offset_precise p; memset(&p, 0, sizeof p);
        if (ioctl(fd, PTP_SYS_OFFSET_PRECISE, &p) == 0) {
            *offset = pct_ns(&p.device) - (pct_ns(&p.sys_realtime) - rt_minus_mono);
            *uncertainty = ts_ns(&rt1) - ts_ns(&rt0);
            return 0;
        }
    }
#endif
#ifdef PTP_SYS_OFFSET_EXTENDED
    {
        struct ptp_sys_offset_extended e; memset(&e, 0, sizeof e); e.n_samples = 5;
        if (ioctl(fd, PTP_SYS_OFFSET_EXTENDED, &e) == 0) {
            int64_t best = INT64_MAX, off = 0;
            for (unsigned i = 0; i < e.n_samples; i++) {
                int64_t a = pct_ns(&e.ts[i][0]), d = pct_ns(&e.ts[i][1]), b = pct_ns(&e.ts[i][2]);
                if (b - a < best) { best = b - a; off = d - (a + b) / 2; }
            }
            *offset = off + rt_minus_mono; *uncertainty = best;
            return 0;
        }
    }
#endif
    {
        struct ptp_sys_offset o; memset(&o, 0, sizeof o); o.n_samples = 5;
        if (ioctl(fd, PTP_SYS_OFFSET, &o) != 0) return -1;
        int64_t best = INT64_MAX, off = 0;
        for (unsigned i = 0; i < o.n_samples; i++) {
            int64_t a = pct_ns(&o.ts[2 * i]), d = pct_ns(&o.ts[2 * i + 1]), b = pct_ns(&o.ts[2 * i + 2]);
            if (b - a < best) { best = b - a; off = d - (a + b) / 2; }
        }
        *offset = off + rt_minus_mono; *uncertainty = best;
        return 0;
    }
}

int main(int argc, char **argv) {
    const char *dev = "/dev/ptp0"; int chan = 0; long count = 0; unsigned edge = PTP_RISING_EDGE; int quiet = 0, c;
    while ((c = getopt(argc, argv, "d:i:n:e:qh")) != -1) switch (c) {
        case 'd': dev = optarg; break;
        case 'i': chan = atoi(optarg); break;
        case 'n': count = atol(optarg); break;
        case 'e': edge = !strcmp(optarg, "falling") ? PTP_FALLING_EDGE : !strcmp(optarg, "both") ? PTP_EXTTS_EDGES : PTP_RISING_EDGE; break;
        case 'q': quiet = 1; break;
        default: fprintf(stderr, "usage: %s [-d dev] [-i channel] [-n count] [-e rising|falling|both] [-q]\n", argv[0]); return 2;
    }
    int fd = open(dev, O_RDWR);
    if (fd < 0) { perror(dev); return 1; }

    struct ptp_clock_caps caps; memset(&caps, 0, sizeof caps);
    if (ioctl(fd, PTP_CLOCK_GETCAPS, &caps) == 0 && !quiet)
        fprintf(stderr, "%s: %d extts channels, %d periodic outputs, cross-timestamp %d\n",
                dev, caps.n_ext_ts, caps.n_per_out, caps.cross_timestamping);

    struct ptp_extts_request req; memset(&req, 0, sizeof req);
    req.index = (unsigned)chan; req.flags = PTP_ENABLE_FEATURE | edge;
    if (ioctl(fd, PTP_EXTTS_REQUEST2, &req) != 0) {          /* strict flags first, then the old ioctl */
        req.flags = PTP_ENABLE_FEATURE | (edge & PTP_RISING_EDGE ? PTP_RISING_EDGE : 0) | (edge & PTP_FALLING_EDGE ? PTP_FALLING_EDGE : 0);
        if (ioctl(fd, PTP_EXTTS_REQUEST, &req) != 0) { perror("PTP_EXTTS_REQUEST"); return 1; }
    }

    /* the PTP event queue (128 deep) may hold events from before this request: drain it */
    for (;;) { struct pollfd pf = { fd, POLLIN, 0 }; if (poll(&pf, 1, 20) <= 0) break; struct ptp_extts_event junk[16]; if (read(fd, junk, sizeof junk) <= 0) break; }

    int64_t offset = 0, unc = 0; struct timespec last_sync = {0, 0};
    if (sync_offset(fd, &offset, &unc) == 0) { struct timespec m; clock_gettime(CLOCK_MONOTONIC, &m); last_sync = m; printf("S,%lld,%lld,%lld\n", (long long)ts_ns(&m), (long long)offset, (long long)unc); }
    else fprintf(stderr, "no PTP/system offset ioctl worked (%s); t_mono column will be 0\n", strerror(errno));
    fflush(stdout);

    long n = 0;
    while (count == 0 || n < count) {
        struct pollfd pfd = { fd, POLLIN, 0 };
        int r = poll(&pfd, 1, 1000);
        struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
        if (ts_ns(&now) - ts_ns(&last_sync) >= 1000000000LL && sync_offset(fd, &offset, &unc) == 0) {
            last_sync = now; printf("S,%lld,%lld,%lld\n", (long long)ts_ns(&now), (long long)offset, (long long)unc); fflush(stdout);
        }
        if (r <= 0) continue;
        struct ptp_extts_event ev[16];
        ssize_t got = read(fd, ev, sizeof ev);
        if (got < 0) { if (errno == EINTR) continue; perror("read"); break; }
        for (size_t i = 0; i < (size_t)got / sizeof ev[0]; i++) {
            int64_t tp = pct_ns(&ev[i].t);
            printf("E,%lld,%u,%lld\n", (long long)tp, ev[i].index, (long long)(tp - offset));
            n++;
        }
        fflush(stdout);
    }
    req.flags = 0; ioctl(fd, PTP_EXTTS_REQUEST, &req);
    close(fd);
    return 0;
}
