// kpipe-synth — a synthetic recorder with kpipe's stdout protocol: access units with VPS/SPS/PPS + IDR
// once per GOP and the camsync SEI in every AU, written through segfile::Writer. The PC's recorder for
// tile-agent and the conformance suite, and the source of the fixture ring (TESTING.md).
//
//   kpipe-synth --ring DIR [--fps 30] [--segment-s 4] [--budget BYTES] [--bitrate 6000000] [--frames N]
//               [--fast] [--epoch-at N] [--edges grid|ptp] [--latency-ns N] [--exposure N] [--gain N]
//
// --fast: no pacing, t_mono advances by one period per frame from now (tests). Otherwise one AU per
// period on CLOCK_MONOTONIC. --epoch-at N: the epoch increments and K restarts at frame N. --edges,
// --latency-ns, --exposure, --gain are accepted for command-line compatibility with kpipe.sh and ignored.
// SIGINT/SIGTERM: close the open segment, print {"ev":"status","state":"idle"}, exit 0.
//
// Epoch and K come from kmatch::CapturedEdges over one synthetic edge per frame, so the PC exercises the
// same numbering, the same events and the same beacon correction as the board. stdin carries kpipe's line
// protocol: `beacon EPOCH K PTP_NS` (tileagent/beacon.py). This machine has no PTP clock, so the beacon's
// time is read as CLOCK_MONOTONIC, which is what the source writes here too. Until a beacon lands, every
// frame carries FAULT_K_LOCAL.
#include <atomic>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <iostream>
#include "kmatch/edges.hpp"
#include "segfile/segfile.hpp"
#include "sei/sei.hpp"
#include "synth_au.hpp"

namespace {
std::atomic<bool> g_stop{false};
void on_sig(int) { g_stop = true; }
int64_t mono_now() { timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return int64_t(t.tv_sec) * 1000000000LL + t.tv_nsec; }

}  // namespace

int main(int argc, char** argv) {
  std::string ring; unsigned fps = 30, segment_s = 4; uint64_t budget = 0; unsigned bitrate = 6000000; long frames = 0;
  bool fast = false; long epoch_at = -1;
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i]; auto next = [&]() { return std::string(i + 1 < argc ? argv[++i] : ""); };
    if (a == "--ring") ring = next(); else if (a == "--fps") fps = std::stoul(next()); else if (a == "--segment-s") segment_s = std::stoul(next());
    else if (a == "--budget") budget = std::stoull(next()); else if (a == "--bitrate") bitrate = std::stoul(next()); else if (a == "--frames") frames = std::stol(next());
    else if (a == "--fast") fast = true; else if (a == "--epoch-at") epoch_at = std::stol(next());
    else if (a == "--edges" || a == "--latency-ns" || a == "--exposure" || a == "--gain" || a == "--width" || a == "--height" || a == "--period-ns" || a == "--device") next();
    else { fprintf(stderr, "kpipe-synth: unknown option %s\n", a.c_str()); return 2; }
  }
  if (ring.empty()) { fprintf(stderr, "kpipe-synth: --ring DIR required\n"); return 2; }
  setvbuf(stdout, nullptr, _IOLBF, 0);
  signal(SIGINT, on_sig); signal(SIGTERM, on_sig);

  std::mutex out_mu;
  auto emit = [&](const std::string& line) { std::lock_guard<std::mutex> g(out_mu); puts(line.c_str()); };
  segfile::Config cfg; cfg.ring = ring; cfg.segment_frames = segment_s * fps; cfg.budget_bytes = budget; cfg.expected_bytes = uint64_t(bitrate) / 8 * segment_s;
  if (fast) cfg.queue_max = 1 << 20;   // a burst, not a camera: nothing may be dropped
  segfile::Writer w(cfg, emit);
  w.start();

  const int64_t period = 1000000000LL / fps; const size_t payload = bitrate / 8 / fps;
  std::mutex edge_mu;
  kmatch::EdgeConfig ec; ec.period = period; ec.retain = 4000000000LL;
  kmatch::CapturedEdges edges(ec, [&](const kmatch::Event& e) {
    emit("{\"ev\":\"" + std::string(e.kind) + "\",\"t\":" + std::to_string(e.t) + ",\"epoch\":" + std::to_string(e.epoch)
         + ",\"k\":" + std::to_string(e.k) + ",\"value\":" + std::to_string(e.value) + "}");
  });
  // The beacon from the tile agent. No PTP clock here, so its time is already CLOCK_MONOTONIC.
  std::thread([&] {
    for (std::string line; std::getline(std::cin, line);) {
      long long e = 0, k = 0, t = 0;
      if (sscanf(line.c_str(), "beacon %lld %lld %lld", &e, &k, &t) != 3) continue;
      std::lock_guard<std::mutex> g(edge_mu);
      edges.beacon(kmatch::Beacon{uint32_t(e), int64_t(k), t});
    }
  }).detach();

  int64_t t0 = mono_now(), t_mono = t0, t_status = t0; long n_1s = 0; uint64_t bytes_prev = 0;
  uint32_t epoch = 0, epoch_prev = 0; int64_t k = 0, k_prev = -1; bool anchored = false;
  for (long i = 0; !g_stop && (frames == 0 || i < frames); i++) {
    bool idr = (i % fps) == 0;   // GOP by frame count, as the encoder does; an epoch change need not fall on an IDR
    if (!fast) { t_mono = t0 + i * period; while (!g_stop && mono_now() < t_mono) usleep(500); if (g_stop) break; t_mono = mono_now(); }
    else t_mono += period;
    {
      std::lock_guard<std::mutex> g(edge_mu);
      if (i == epoch_at) edges.rearm();
      kmatch::Edge e = edges.edge(t_mono);
      epoch = e.epoch; k = e.k; anchored = edges.anchored();
    }
    // A beacon moved the whole epoch's numbering between this frame and the last: what kmatch::Matcher
    // reports as K_CORRECTED on the board, reached here by comparing the step (there is no matcher).
    const bool corrected = k_prev >= 0 && epoch == epoch_prev && k != k_prev + 1;
    epoch_prev = epoch; k_prev = k;
    const uint16_t flags = uint16_t((i == epoch_at ? kmatch::FAULT_EPOCH_CHANGE : 0u) |
                                    (corrected ? kmatch::FAULT_K_CORRECTED : 0u) |
                                    (anchored ? 0u : kmatch::FAULT_K_LOCAL));
    sei::Record r{uint16_t(epoch), uint32_t(k), uint64_t(t_mono), uint16_t(i & 0xffff), flags};
    auto au = synth_au(idr, idr ? payload * 3 : payload, r, uint32_t(i));
    w.on_au(au.data(), au.size(), segfile::Meta{epoch, k, t_mono, r.flags, idr});
    if (w.failed()) { fprintf(stderr, "kpipe-synth: storage error, stopping\n"); return 3; }
    n_1s++;
    int64_t now = fast ? t_mono : mono_now();
    if (now - t_status >= 1000000000LL) {
      auto s = w.stats();
      emit("{\"ev\":\"status\",\"state\":\"running\",\"fps_in\":" + std::to_string(n_1s) + ",\"fps_out\":" + std::to_string(n_1s) + ",\"epoch\":" + std::to_string(epoch)
           + ",\"k\":" + std::to_string(k) + ",\"faults_1s\":0,\"faults_total\":0,\"anchored\":" + (anchored ? "true" : "false") + ",\"edges_1s\":0,\"bytes_1s\":" + std::to_string(s.bytes_total - bytes_prev)
           + ",\"open_segno\":" + std::to_string(s.open_segno) + ",\"pre_idr_dropped\":" + std::to_string(s.pre_idr_dropped) + ",\"dropped\":" + std::to_string(s.dropped) + "}");
      bytes_prev = s.bytes_total; t_status = now; n_1s = 0;
    }
  }
  w.stop();
  emit("{\"ev\":\"status\",\"state\":\"idle\"}");
  return w.failed() ? 3 : 0;
}
