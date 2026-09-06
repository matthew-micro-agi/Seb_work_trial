#include "kmatch/matcher.hpp"

#include <cstdlib>

namespace kmatch {

Matcher::Matcher(MatchConfig cfg, const EdgeSource& edges) : cfg_(cfg), edges_(edges) {
  if (cfg_.tolerance <= 0) cfg_.tolerance = cfg_.period / 4;
}

void Matcher::reset() {
  have_prev_ = false;
  unmatched_ = 0;
}

FrameOut Matcher::frame(const FrameIn& in) {
  FrameOut out;
  const ns_t t_expect = in.t_frame - cfg_.latency;

  auto e = edges_.nearest(t_expect);
  if (!e) {
    out.faults |= FAULT_UNMATCHED;
    ++unmatched_;
    return out;
  }
  if (auto last = edges_.last(); last && t_expect > last->t + cfg_.period / 2) {
    // Nearest would be an edge not registered yet (or the latency is wrong).
    out.faults |= FAULT_UNMATCHED | FAULT_EDGE_PENDING;
    out.epoch = last->epoch;
    ++unmatched_;
    return out;
  }
  out.unmatched_before = unmatched_;
  unmatched_ = 0;

  out.k = e->k;
  out.epoch = e->epoch;
  out.t_edge = e->t;
  out.residual = t_expect - e->t;
  if (std::abs(out.residual) > cfg_.tolerance) out.faults |= FAULT_LATE;

  // The previous frame's edge as the list numbers it now: a beacon may have moved K or
  // relabeled the epoch since that frame was matched.
  int64_t k_prev = prev_out_.k;
  uint32_t epoch_prev = prev_out_.epoch;
  if (have_prev_) {
    if (auto pe = edges_.nearest(prev_out_.t_edge); pe && pe->t == prev_out_.t_edge) {
      if (pe->k != prev_out_.k) out.faults |= FAULT_K_CORRECTED;
      k_prev = pe->k;
      epoch_prev = pe->epoch;
    }
  }

  if (have_prev_ && epoch_prev == out.epoch) {
    const int32_t d_seq = static_cast<int32_t>(in.seq - prev_in_.seq);
    const int32_t d_fs = in.has_frame_start && prev_in_.has_frame_start
                             ? static_cast<int32_t>(in.frame_start - prev_in_.frame_start)
                             : d_seq;
    const int64_t d_k = out.k - k_prev;

    if (d_seq <= 0 || d_k < 0) {
      out.faults |= FAULT_SEQ_REGRESSION;
      if (d_k == 0) out.faults |= FAULT_DUPLICATE;
    } else if (d_k == 0) {
      out.faults |= FAULT_DUPLICATE;
    } else {
      const int64_t lost = static_cast<int64_t>(d_fs) - 1 - static_cast<int64_t>(out.unmatched_before);
      if (lost > 0) {
        out.delivery_lost = static_cast<uint32_t>(lost);
        out.faults |= FAULT_DELIVERY_LOST;
      }
      const int64_t missed = d_k - 1 - static_cast<int64_t>(out.unmatched_before) -
                             static_cast<int64_t>(out.delivery_lost);
      if (missed > 0) {
        out.sensor_missed = static_cast<uint32_t>(missed);
        out.faults |= FAULT_SENSOR_MISSED;
      }
    }
  } else {
    if (have_prev_) out.faults |= FAULT_EPOCH_CHANGE;
    // Edges of this epoch before the first delivered frame produced nothing we saw.
    if (out.k > 0) {
      out.sensor_missed = static_cast<uint32_t>(out.k);
      out.faults |= FAULT_SENSOR_MISSED;
    }
  }

  have_prev_ = true;
  prev_in_ = in;
  prev_out_ = out;
  return out;
}

}  // namespace kmatch
