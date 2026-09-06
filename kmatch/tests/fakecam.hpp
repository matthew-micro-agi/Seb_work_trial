// Fake slaved camera for tests, the C++ twin of `chris/camsync/fakecam.py`.
//
// Produces a time-ordered event stream of trigger edges and delivered frames from a
// fault schedule keyed on the edge index e, same syntax as the Python one:
//   skip@e[:n]      sensor ignores n edges: no exposure, no frame-start, no frame
//   drop@e          exposure happens (frame-start counts) but the frame is not delivered
//   dup@e           the frame is delivered twice
//   late@e:ms       the frame is timestamped ms late; later frames queue behind it, as
//                   on a serial delivery path
// plus two the Python one does by hand on the bench:
//   restart@e[:ms]  the source idles ms (default 1000) before edge e: a new epoch
//   lostcap@e       edge e fires (sensor exposes) but the capture unit misses it
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "kmatch/types.hpp"

namespace fakecam {

enum class Kind { None, Skip, Drop, Dup, Late, Restart, LostCap };

struct Fault { Kind kind = Kind::None; double arg = 0; };

std::map<int64_t, Fault> parse_faults(const std::string& spec);

struct Config {
  kmatch::ns_t period = 33'333'333;
  kmatch::ns_t latency = 20'000'000;
  kmatch::ns_t t0 = 10 * 33'333'333;
  int64_t edges = 100;
  uint32_t seq_start = 0;      // e.g. 0xFFFFFFF0 to exercise the 32-bit wrap
  bool frame_start = true;     // whether frames carry the receiver's frame-start count
  std::string faults;
};

struct Ev {
  enum Type { Edge, Frame, Tick } type;
  kmatch::ns_t t;
  int64_t e;                   // edge index the event belongs to (Tick: -1)
  kmatch::FrameIn frame{};     // valid for Frame
};

// Sorted by time; ticks once a period for stall detection.
std::vector<Ev> generate(const Config& cfg);

}  // namespace fakecam
