#include "kmatch/edges.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdlib>

namespace kmatch {

CapturedEdges::CapturedEdges(EdgeConfig cfg, EventSink sink) : cfg_(cfg), sink_(std::move(sink)) {
  if (cfg_.grid_tolerance <= 0) cfg_.grid_tolerance = cfg_.period / 4;
}

void CapturedEdges::emit(const char* kind, ns_t t, int64_t k, int64_t value) const {
  if (sink_) sink_(Event{kind, t, epoch_, k, value});
}

void CapturedEdges::start_epoch(ns_t t, ns_t idle) {
  ++epoch_;
  t0_ = t;
  grid_offset_ = 0;
  diverge_t_ = t;
  force_epoch_ = false;
  anchored_ = false;   // the source has not confirmed this epoch's numbering yet
  edges_.clear();  // edges of the old epoch are gone; a frame of that epoch is now unmatched
  edges_.push_back(Edge{t, 0, epoch_});
  emit("epoch_start", t, 0, idle);
}

Edge CapturedEdges::edge(ns_t t) {
  if (edges_.empty() || force_epoch_) {
    start_epoch(t, edges_.empty() ? 0 : t - edges_.back().t);
    return edges_.back();
  }
  const Edge& prev = edges_.back();
  const ns_t idle = t - prev.t;
  if (stalled_) {
    stalled_ = false;
    emit("trigger_resumed", t, prev.k, idle);
  }
  if (idle >= cfg_.epoch_gap) {
    start_epoch(t, idle);
    return edges_.back();
  }
  int64_t k = prev.k + 1;
  if (cfg_.fill_from_grid) {
    const int64_t n = std::max<int64_t>(1, div_round(idle, cfg_.period));
    k = prev.k + n;
  }
  edges_.push_back(Edge{t, k, epoch_});

  // Consistency: the received count must equal elapsed time over the period.
  const ns_t grid_residual = (t - t0_) - k * cfg_.period;
  if (std::abs(grid_residual) > cfg_.grid_tolerance) {
    const int64_t grid_k = div_round(t - t0_, cfg_.period);
    if (grid_k - k != grid_offset_) {
      grid_offset_ = grid_k - k;
      diverge_t_ = t;
      emit("edge_grid_mismatch", t, k, grid_offset_);
    }
  }

  while (edges_.size() > 1 && edges_.front().t < t - cfg_.retain) edges_.pop_front();
  return edges_.back();
}

void CapturedEdges::tick(ns_t now) {
  if (edges_.empty() || stalled_) return;
  if (now - edges_.back().t > cfg_.stall_periods * cfg_.period) {
    stalled_ = true;
    emit("trigger_stall", now, edges_.back().k, now - edges_.back().t);
  }
}

void CapturedEdges::rearm() { force_epoch_ = true; }

int64_t CapturedEdges::beacon(const Beacon& b) {
  auto e = nearest(b.t_ref);
  if (!e || std::abs(e->t - b.t_ref) > cfg_.period / 2) {
    emit("beacon_unmatched", b.t_ref, b.k_ref, e ? e->t - b.t_ref : 0);
    return 0;
  }
  if (b.epoch != epoch_) {
    const uint32_t old = epoch_;
    epoch_ = b.epoch;
    for (auto& x : edges_) x.epoch = epoch_;
    emit("epoch_relabeled", b.t_ref, e->k, old);
  }
  anchored_ = true;
  const int64_t shift = b.k_ref - e->k;
  if (shift == 0) {
    emit("beacon_ok", b.t_ref, b.k_ref, 0);
    return 0;
  }
  const ns_t from = (grid_offset_ != 0 && shift == grid_offset_) ? diverge_t_ : INT64_MIN;
  for (auto& x : edges_)
    if (x.t >= from) x.k += shift;
  // Re-anchor the grid on the corrected numbering.
  t0_ = e->t - b.k_ref * cfg_.period;
  grid_offset_ = div_round(edges_.back().t - t0_, cfg_.period) - edges_.back().k;
  diverge_t_ = edges_.back().t;
  emit("k_corrected", b.t_ref, b.k_ref, shift);
  return shift;
}

std::optional<Edge> CapturedEdges::nearest(ns_t t) const {
  if (edges_.empty()) return std::nullopt;
  auto it = std::lower_bound(edges_.begin(), edges_.end(), t,
                             [](const Edge& e, ns_t v) { return e.t < v; });
  if (it == edges_.end()) return edges_.back();
  if (it == edges_.begin()) return *it;
  auto before = std::prev(it);
  return (t - before->t) <= (it->t - t) ? *before : *it;
}

std::optional<Edge> CapturedEdges::last() const {
  if (edges_.empty()) return std::nullopt;
  return edges_.back();
}

std::optional<Edge> GridEdges::nearest(ns_t t) const {
  int64_t k = div_round(t - t0_, period_);
  if (k < 0) k = 0;
  return Edge{t0_ + k * period_, k, epoch_};
}

}  // namespace kmatch
