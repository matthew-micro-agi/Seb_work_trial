// The edge side: where K is born.
//
// A trigger pulse captured by eCAP becomes an Edge with an index K. K is the count of
// pulses *received* since the epoch started, never the count generated. The grid
// (t0 + K * period) is a consistency check, not the source of K. A beacon from the
// trigger source can shift K when the two disagree.
//
// Events emitted (kind, t, epoch, k, value):
//   epoch_start        first pulse of an epoch: k = 0, value = idle time before it (0 at start)
//   trigger_stall      no edge for stall_periods periods (needs tick()); k = last k
//   trigger_resumed    edges are back after a stall; value = idle time
//   edge_grid_mismatch grid index and received count diverged by a new amount; value = grid - k
//   beacon_ok          beacon agreed with the captured edge it named
//   beacon_unmatched   no captured edge within half a period of the beacon time; value = distance
//   k_corrected        beacon moved K of the current epoch; value = shift applied
//   epoch_relabeled    beacon carried a different epoch id; value = old id
//
// Until a beacon has been matched, K is this process's own count of pulses since it started and the
// epoch id is its own: anchored() is false. The recorder turns that into FAULT_K_LOCAL on every frame
// it stamps, so the recording says which of its frames are on the source's numbering. A new epoch is
// local again until the source confirms it.
#pragma once

#include <deque>
#include <optional>

#include "kmatch/types.hpp"

namespace kmatch {

// What the matcher needs from an edge source. Two implementations below.
class EdgeSource {
 public:
  virtual ~EdgeSource() = default;
  // The edge nearest in time to t, if any.
  virtual std::optional<Edge> nearest(ns_t t) const = 0;
  // The most recent edge, if the source knows one (a grid does not).
  virtual std::optional<Edge> last() const = 0;
  // True once a beacon from the trigger source has named an edge of the current epoch, so K and the
  // epoch id are the source's and not this process's own count. False for a source that cannot be
  // anchored at all (a grid).
  virtual bool anchored() const { return false; }
};

struct EdgeConfig {
  ns_t period = 33'333'333;          // nominal pulse period
  ns_t epoch_gap = 1'000'000'000;    // idle at least this long before a pulse: that pulse is K = 0
  ns_t retain = 2'000'000'000;       // edges older than this (before the newest) are dropped
  int stall_periods = 2;             // no edge for this many periods: trigger_stall
  ns_t grid_tolerance = 0;           // 0: period / 4. Grid residual above this counts as off-grid
  bool fill_from_grid = false;       // true: a gap of n periods (below epoch_gap) advances K by n,
                                     // treating it as lost captures. false: count received pulses
};

// Captured edges from eCAP (or a GPIO listener, with worse jitter).
class CapturedEdges : public EdgeSource {
 public:
  explicit CapturedEdges(EdgeConfig cfg, EventSink sink = {});

  // One captured pulse at t. Edges must arrive in time order.
  Edge edge(ns_t t);
  // Call periodically (about once a period) to detect a stalled trigger.
  void tick(ns_t now);
  // Force the next edge to open a new epoch, regardless of the gap (SIGHUP equivalent).
  void rearm();
  // Source's statement about one pulse. Corrects K and the epoch id when they differ.
  // If the received count had diverged from the grid by exactly the shift, only edges
  // from the divergence on are moved (a lost capture); otherwise the whole epoch is
  // (a late start). Returns the shift applied to K (0 when they agreed or no edge was found).
  int64_t beacon(const Beacon& b);

  std::optional<Edge> nearest(ns_t t) const override;
  std::optional<Edge> last() const override;
  bool anchored() const override { return anchored_; }

  const std::deque<Edge>& edges() const { return edges_; }
  uint32_t epoch() const { return epoch_; }
  bool stalled() const { return stalled_; }
  // Time of pulse 0 of the current epoch, as implied by the current K numbering.
  ns_t t0() const { return t0_; }

 private:
  void emit(const char* kind, ns_t t, int64_t k, int64_t value = 0) const;
  void start_epoch(ns_t t, ns_t idle);

  EdgeConfig cfg_;
  EventSink sink_;
  std::deque<Edge> edges_;
  uint32_t epoch_ = 0;
  ns_t t0_ = 0;
  int64_t grid_offset_ = 0;
  ns_t diverge_t_ = 0;  // first edge at which the current grid_offset_ was observed
  bool stalled_ = false;
  bool force_epoch_ = true;
  bool anchored_ = false;
};

// The one-line alternative: K by division from a known t0 and period. For a board
// without a spare capture input. No stall, no beacon, no faults of its own.
class GridEdges : public EdgeSource {
 public:
  GridEdges(ns_t t0, ns_t period, uint32_t epoch = 1) : t0_(t0), period_(period), epoch_(epoch) {}
  void restart(ns_t t0, uint32_t epoch) { t0_ = t0; epoch_ = epoch; }

  std::optional<Edge> nearest(ns_t t) const override;
  std::optional<Edge> last() const override { return std::nullopt; }

 private:
  ns_t t0_;
  ns_t period_;
  uint32_t epoch_;
};

}  // namespace kmatch
