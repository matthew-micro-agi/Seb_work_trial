// kmatch — trigger index K from captured edges. Shared types.
//
// All times are int64 nanoseconds on one clock (CLOCK_MONOTONIC on the SoC). The
// library never converts clocks; the caller hands in edge times, frame times and
// beacon times already on that clock.
#pragma once

#include <cstdint>
#include <functional>
#include <string>

namespace kmatch {

using ns_t = int64_t;

// Per-frame fault bits. A frame may carry several.
enum Fault : uint32_t {
  FAULT_NONE = 0,
  FAULT_DUPLICATE = 1u << 0,       // same edge matched again; K unchanged
  FAULT_DELIVERY_LOST = 1u << 1,   // receiver counted frames that never reached us
  FAULT_SENSOR_MISSED = 1u << 2,   // edge(s) between this frame and the previous with no exposure
  FAULT_LATE = 1u << 3,            // |residual| over tolerance; K is still the nearest edge
  FAULT_UNMATCHED = 1u << 4,       // no candidate edge; K is -1
  FAULT_EPOCH_CHANGE = 1u << 5,    // first frame of a new epoch; K restarts
  FAULT_SEQ_REGRESSION = 1u << 6,  // sequence or K went backwards within an epoch
  FAULT_EDGE_PENDING = 1u << 7,    // frame arrived before its edge was registered (with UNMATCHED)
  FAULT_K_CORRECTED = 1u << 8,     // a beacon moved K since the previous frame
  FAULT_K_LOCAL = 1u << 9,         // standing, not an event: EdgeSource::anchored() is false, so K counts
                                   // pulses from wherever the recorder started. Unique inside the session,
                                   // meaningless against another tile or another recording. Stamped by the
                                   // recorder, not by the matcher: it describes the numbering, not the match.
};

// "DUPLICATE|LATE" style rendering for logs.
std::string fault_names(uint32_t faults);

// One frame as delivered by the receiver.
struct FrameIn {
  ns_t t_frame = 0;             // receiver hardware timestamp
  uint32_t seq = 0;             // V4L2 buffer sequence (wraps at 2^32)
  uint32_t frame_start = 0;     // receiver frame-start count, if the driver exposes it
  bool has_frame_start = false; // false: seq stands in for frame_start
};

// The match result for one frame.
struct FrameOut {
  int64_t k = -1;              // trigger index, -1 when unmatched
  uint32_t epoch = 0;          // epoch of the matched edge
  ns_t t_edge = 0;             // time of the matched edge
  ns_t residual = 0;           // t_frame - latency - t_edge; signed, 0 is perfect
  uint32_t faults = FAULT_NONE;
  uint32_t delivery_lost = 0;  // frames the receiver saw but did not deliver, since the previous frame
  uint32_t sensor_missed = 0;  // edges with no exposure, since the previous frame
  uint32_t unmatched_before = 0;  // frames since the previous matched one that got no K
};

// One captured trigger edge with its index.
struct Edge {
  ns_t t = 0;
  int64_t k = 0;
  uint32_t epoch = 0;
};

// Message from the trigger source, about once a second: "pulse k_ref of epoch happened
// at t_ref". t_ref is already converted to the local clock by the caller.
struct Beacon {
  uint32_t epoch = 0;
  int64_t k_ref = 0;
  ns_t t_ref = 0;
};

// Log event. `kind` is a static string; the vocabulary is listed in edges.hpp and
// matcher.hpp. `value` is event specific (a delta, a count) and 0 when unused.
struct Event {
  const char* kind = "";
  ns_t t = 0;
  uint32_t epoch = 0;
  int64_t k = 0;
  int64_t value = 0;
};
using EventSink = std::function<void(const Event&)>;

// Round a/b to the nearest integer, halves away from zero, exact in integers.
inline int64_t div_round(int64_t a, int64_t b) {
  return a >= 0 ? (a + b / 2) / b : -((-a + b / 2) / b);
}

}  // namespace kmatch
