// The matcher: one per camera, fed by one shared EdgeSource.
//
// Rule: K is the index of the edge for which t_frame - t_edge is closest to the latency
// constant. Accepted when the residual is within the tolerance, flagged FAULT_LATE
// otherwise (K still the nearest edge). Uniqueness needs latency < period.
//
// Gap accounting between consecutive matched frames, with dK the step in K, dF the step
// in the receiver's frame-start count (V4L2 sequence when frame_start is absent) and U
// the frames in between that got no K:
//   delivery_lost = dF - 1 - U            exposures the receiver counted that we never got
//   sensor_missed = dK - 1 - U - lost     edges that produced no exposure
// This is the binder's table in `chris/camsync/matcher.py`, with n taken from captured
// edges instead of rounded elapsed time.
//
// K comes from the edge list, so a misassigned frame does not drift K: the next frame
// re-anchors on its own edge. A frame timestamped more than half a period off lands on
// the neighbouring edge and shows up as DUPLICATE (or a one-frame gap) on the next frame.
//
// Events emitted: none. Everything is in FrameOut; the caller logs it.
#pragma once

#include <optional>

#include "kmatch/edges.hpp"
#include "kmatch/types.hpp"

namespace kmatch {

struct MatchConfig {
  ns_t period = 33'333'333;
  ns_t latency = 20'000'000;  // t_frame - t_edge for the frame a pulse produced (measured in M2)
  ns_t tolerance = 0;         // 0: period / 4
};

class Matcher {
 public:
  Matcher(MatchConfig cfg, const EdgeSource& edges);

  FrameOut frame(const FrameIn& in);
  // Forget the previous frame (stream restarted). The edge list is untouched.
  void reset();

  const MatchConfig& config() const { return cfg_; }

 private:
  MatchConfig cfg_;
  const EdgeSource& edges_;
  bool have_prev_ = false;
  FrameIn prev_in_{};
  FrameOut prev_out_{};
  uint32_t unmatched_ = 0;
};

}  // namespace kmatch
