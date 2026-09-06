// kmatch-replay: run the matcher over a recorded event log, for offline re-derivation.
//
//   kmatch-replay --period-ns N --latency-ns N [--tolerance-ns N] [--grid-t0 T] < in.csv
//
// Input, one event per line, time-ordered, times in ns on one clock:
//   E,<t>                       captured trigger edge
//   F,<t>,<seq>[,<frame_start>] delivered frame
//   B,<epoch>,<k_ref>,<t_ref>   beacon from the source (t_ref already on the local clock)
//   T,<t>                       tick (stall check)
//   R                           rearm: next edge opens a new epoch
// Output:
//   F,<t>,<seq>,<k>,<epoch>,<t_edge>,<residual>,<faults>,<lost>,<missed>
//   EV,<kind>,<t>,<epoch>,<k>,<value>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include "kmatch/matcher.hpp"

using namespace kmatch;

static int64_t arg_ns(int argc, char** argv, const char* name, int64_t def) {
  for (int i = 1; i + 1 < argc; ++i)
    if (std::strcmp(argv[i], name) == 0) return std::strtoll(argv[i + 1], nullptr, 10);
  return def;
}

int main(int argc, char** argv) {
  MatchConfig mc;
  mc.period = arg_ns(argc, argv, "--period-ns", mc.period);
  mc.latency = arg_ns(argc, argv, "--latency-ns", mc.latency);
  mc.tolerance = arg_ns(argc, argv, "--tolerance-ns", 0);
  const int64_t grid_t0 = arg_ns(argc, argv, "--grid-t0", INT64_MIN);

  EdgeConfig ec;
  ec.period = mc.period;
  std::unique_ptr<CapturedEdges> cap;
  std::unique_ptr<GridEdges> grid;
  EdgeSource* src;
  if (grid_t0 != INT64_MIN) {
    grid = std::make_unique<GridEdges>(grid_t0, mc.period);
    src = grid.get();
  } else {
    cap = std::make_unique<CapturedEdges>(ec, [](const Event& e) {
      std::printf("EV,%s,%lld,%u,%lld,%lld\n", e.kind, (long long)e.t, e.epoch, (long long)e.k,
                  (long long)e.value);
    });
    src = cap.get();
  }
  Matcher m(mc, *src);

  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::vector<std::string> f;
    std::stringstream ss(line);
    std::string tok;
    while (std::getline(ss, tok, ',')) f.push_back(tok);
    auto num = [&](size_t i) { return i < f.size() ? std::strtoll(f[i].c_str(), nullptr, 10) : 0LL; };
    if (f[0] == "E" && cap) cap->edge(num(1));
    else if (f[0] == "T" && cap) cap->tick(num(1));
    else if (f[0] == "R" && cap) cap->rearm();
    else if (f[0] == "B" && cap) cap->beacon({static_cast<uint32_t>(num(1)), num(2), num(3)});
    else if (f[0] == "F") {
      FrameIn in;
      in.t_frame = num(1);
      in.seq = static_cast<uint32_t>(num(2));
      in.has_frame_start = f.size() > 3;
      in.frame_start = static_cast<uint32_t>(num(3));
      FrameOut o = m.frame(in);
      std::printf("F,%lld,%u,%lld,%u,%lld,%lld,%s,%u,%u\n", (long long)in.t_frame, in.seq,
                  (long long)o.k, o.epoch, (long long)o.t_edge, (long long)o.residual,
                  fault_names(o.faults).c_str(), o.delivery_lost, o.sensor_missed);
    }
  }
  return 0;
}
