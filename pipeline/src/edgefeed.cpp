#include "edgefeed.hpp"
#include <cerrno>
#include <cstring>
#include <stdexcept>
#include <fcntl.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include <linux/ptp_clock.h>

static int64_t ts_ns(const timespec& t) { return int64_t(t.tv_sec) * 1000000000LL + t.tv_nsec; }
static int64_t pct_ns(const ptp_clock_time& t) { return int64_t(t.sec) * 1000000000LL + t.nsec; }
static int64_t mono_now() { timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return ts_ns(t); }

PtpEdgeFeed::PtpEdgeFeed(const std::string& dev, unsigned channel, kmatch::CapturedEdges& edges, std::mutex& lock)
    : dev_(dev), chan_(channel), edges_(edges), lock_(lock) {}
PtpEdgeFeed::~PtpEdgeFeed() { stop(); }
void PtpEdgeFeed::start() { stop_ = false; th_ = std::thread(&PtpEdgeFeed::run, this); }
void PtpEdgeFeed::stop() { stop_ = true; if (th_.joinable()) th_.join(); }

bool PtpEdgeFeed::sync(int fd) {
  timespec rt0, mo, rt1;
  clock_gettime(CLOCK_REALTIME, &rt0); clock_gettime(CLOCK_MONOTONIC, &mo); clock_gettime(CLOCK_REALTIME, &rt1);
  int64_t rt_minus_mono = (ts_ns(rt0) + ts_ns(rt1)) / 2 - ts_ns(mo);
  ptp_sys_offset_extended e{}; e.n_samples = 5;
  if (ioctl(fd, PTP_SYS_OFFSET_EXTENDED, &e) == 0) {
    int64_t best = INT64_MAX, off = 0;
    for (unsigned i = 0; i < e.n_samples; i++) {
      int64_t a = pct_ns(e.ts[i][0]), d = pct_ns(e.ts[i][1]), b = pct_ns(e.ts[i][2]);
      if (b - a < best) { best = b - a; off = d - (a + b) / 2; }
    }
    offset_ = off + rt_minus_mono; return true;
  }
  ptp_sys_offset o{}; o.n_samples = 5;
  if (ioctl(fd, PTP_SYS_OFFSET, &o) != 0) return false;
  int64_t best = INT64_MAX, off = 0;
  for (unsigned i = 0; i < o.n_samples; i++) {
    int64_t a = pct_ns(o.ts[2 * i]), d = pct_ns(o.ts[2 * i + 1]), b = pct_ns(o.ts[2 * i + 2]);
    if (b - a < best) { best = b - a; off = d - (a + b) / 2; }
  }
  offset_ = off + rt_minus_mono; return true;
}

void PtpEdgeFeed::run() {
  int fd = open(dev_.c_str(), O_RDWR);
  if (fd < 0) throw std::runtime_error(dev_ + ": " + strerror(errno));
  ptp_extts_request req{}; req.index = chan_; req.flags = PTP_ENABLE_FEATURE | PTP_RISING_EDGE;
  if (ioctl(fd, PTP_EXTTS_REQUEST2, &req) != 0 && ioctl(fd, PTP_EXTTS_REQUEST, &req) != 0)
    throw std::runtime_error(std::string("PTP_EXTTS_REQUEST: ") + strerror(errno));
  for (;;) { pollfd pf{fd, POLLIN, 0}; if (poll(&pf, 1, 20) <= 0) break; ptp_extts_event junk[16]; if (read(fd, junk, sizeof junk) <= 0) break; }  // drain stale queue
  sync(fd); int64_t last_sync = mono_now(), last_tick = last_sync;
  while (!stop_) {
    pollfd p{fd, POLLIN, 0}; int r = poll(&p, 1, 100);
    int64_t now = mono_now();
    if (now - last_sync >= 1000000000LL) { sync(fd); last_sync = now; }
    if (now - last_tick >= 30000000LL) { std::lock_guard<std::mutex> g(lock_); edges_.tick(now); last_tick = now; }
    if (r <= 0) continue;
    ptp_extts_event ev[16]; ssize_t got = read(fd, ev, sizeof ev);
    if (got <= 0) continue;
    std::lock_guard<std::mutex> g(lock_);
    for (size_t i = 0; i < size_t(got) / sizeof ev[0]; i++) {
      if (ev[i].index != chan_) continue;
      edges_.edge(pct_ns(ev[i].t) - offset_.load()); events_++;
    }
  }
  // Do not disable the channel: it is shared. One PTP_EXTTS_REQUEST with flags = 0 stops the events for
  // every other reader of the same clock, and the tile agent's trigger source is one of them (OPEN.md §8).
  close(fd);
}
