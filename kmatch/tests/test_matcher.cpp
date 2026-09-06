#include "kmatch/matcher.hpp"

#include "check.hpp"

using namespace kmatch;
static const ns_t P = 33'333'333;
static const ns_t L = 20'000'000;

// A clean edge list and a matcher; frames are described by edge index and offsets.
static EdgeConfig keep_all() {
  EdgeConfig c{P};
  c.retain = 3600LL * 1'000'000'000;  // tests look back at edge 0
  return c;
}

struct Rig {
  CapturedEdges ed{keep_all()};
  Matcher m{{P, L}, ed};
  uint32_t seq = 0, fs = 0;
  Rig(int n = 200) { for (int i = 0; i < n; ++i) ed.edge(i * P); }
  FrameOut frame(int64_t e, ns_t off = 0, int d_seq = 1, int d_fs = 1) {
    seq = static_cast<uint32_t>(static_cast<int64_t>(seq) + d_seq);
    fs = static_cast<uint32_t>(static_cast<int64_t>(fs) + d_fs);
    return m.frame({e * P + L + off, seq, fs, true});
  }
};

TEST(match_first_frame) {
  Rig r;
  FrameOut o = r.frame(0);
  CHECK_EQ(o.k, 0);
  CHECK_EQ(o.epoch, 1u);
  CHECK_EQ(o.residual, 0);
  CHECK_EQ(o.faults, uint32_t(FAULT_NONE));
}

TEST(match_residual_and_tolerance) {
  Rig r;
  r.frame(0);
  FrameOut o = r.frame(1, 5'000'000);
  CHECK_EQ(o.k, 1);
  CHECK_EQ(o.residual, 5'000'000);
  CHECK_EQ(o.faults, uint32_t(FAULT_NONE));
  o = r.frame(2, -3'000'000);
  CHECK_EQ(o.residual, -3'000'000);
  o = r.frame(3, P / 4 + 1);
  CHECK_EQ(o.k, 3);
  CHECK_EQ(o.faults, uint32_t(FAULT_LATE));
  o = r.frame(4, -(P / 4 + 1));
  CHECK_EQ(o.k, 4);
  CHECK_EQ(o.faults, uint32_t(FAULT_LATE));
}

TEST(match_more_than_half_period_late_lands_on_next_edge) {
  Rig r;
  r.frame(0);
  FrameOut o = r.frame(1, 4 * P / 5);  // nearest is edge 2, in tolerance
  CHECK_EQ(o.k, 2);
  CHECK_EQ(o.faults, uint32_t(FAULT_SENSOR_MISSED));  // looks like edge 1 exposed nothing
  o = r.frame(2);                       // the real frame 2: same edge again
  CHECK_EQ(o.k, 2);
  CHECK_EQ(o.faults, uint32_t(FAULT_DUPLICATE));
  o = r.frame(3);
  CHECK_EQ(o.k, 3);
  CHECK_EQ(o.faults, uint32_t(FAULT_NONE));  // no drift: K is re-anchored per frame
}

TEST(match_sensor_missed) {
  Rig r;
  r.frame(0);
  FrameOut o = r.frame(2);  // edge 1 exposed nothing: one frame start, K stepped by 2
  CHECK_EQ(o.k, 2);
  CHECK_EQ(o.faults, uint32_t(FAULT_SENSOR_MISSED));
  CHECK_EQ(o.sensor_missed, 1u);
  CHECK_EQ(o.delivery_lost, 0u);
}

TEST(match_delivery_lost) {
  Rig r;
  r.frame(0);
  FrameOut o = r.frame(3, 0, 1, 3);  // three exposures counted, one delivered
  CHECK_EQ(o.k, 3);
  CHECK_EQ(o.faults, uint32_t(FAULT_DELIVERY_LOST));
  CHECK_EQ(o.delivery_lost, 2u);
  CHECK_EQ(o.sensor_missed, 0u);
}

TEST(match_both_kinds_in_one_gap) {
  Rig r;
  r.frame(0);
  FrameOut o = r.frame(4, 0, 1, 2);  // 4 edges: 2 exposures (1 lost), 2 edges missed
  CHECK_EQ(o.k, 4);
  CHECK_EQ(o.delivery_lost, 1u);
  CHECK_EQ(o.sensor_missed, 2u);
  CHECK_EQ(o.faults, uint32_t(FAULT_DELIVERY_LOST | FAULT_SENSOR_MISSED));
}

TEST(match_without_frame_start_uses_seq) {
  Rig r;
  r.m.frame({0 * P + L, 0, 0, false});
  FrameOut o = r.m.frame({3 * P + L, 3, 0, false});  // seq jumped by 3
  CHECK_EQ(o.delivery_lost, 2u);
  o = r.m.frame({5 * P + L, 4, 0, false});           // seq stepped by 1, K by 2
  CHECK_EQ(o.sensor_missed, 1u);
}

TEST(match_duplicate) {
  Rig r;
  r.frame(0);
  FrameOut o = r.frame(0, 1'000'000, 1, 0);  // delivered again, next buffer, same frame start
  CHECK_EQ(o.k, 0);
  CHECK_EQ(o.faults, uint32_t(FAULT_DUPLICATE));
  o = r.frame(1);
  CHECK_EQ(o.k, 1);
  CHECK_EQ(o.faults, uint32_t(FAULT_NONE));
}

TEST(match_seq_regression) {
  Rig r;
  r.frame(5);
  FrameOut o = r.frame(6, 0, -1, 1);
  CHECK_EQ(o.faults, uint32_t(FAULT_SEQ_REGRESSION));
  o = r.frame(3);  // K backwards within the epoch
  CHECK_EQ(o.k, 3);
  CHECK_EQ(o.faults, uint32_t(FAULT_SEQ_REGRESSION));
}

TEST(match_seq_wrap_is_silent) {
  Rig r;
  r.seq = r.fs = 0xFFFFFFFD;
  for (int64_t e = 0; e < 8; ++e) {
    FrameOut o = r.frame(e);
    CHECK_EQ(o.k, e);
    CHECK_EQ(o.faults, uint32_t(FAULT_NONE));
  }
}

TEST(match_unmatched_without_edges) {
  CapturedEdges ed({P});
  Matcher m({P, L}, ed);
  FrameOut o = m.frame({L, 0, 0, true});
  CHECK_EQ(o.k, -1);
  CHECK_EQ(o.faults, uint32_t(FAULT_UNMATCHED));
  ed.edge(0);
  ed.edge(P);
  o = m.frame({P + L, 1, 1, true});  // first matched frame; the unmatched one is not a loss
  CHECK_EQ(o.k, 1);
  CHECK_EQ(o.unmatched_before, 1u);
  CHECK_EQ(o.sensor_missed, 1u);  // edge 0 of the epoch, from this matcher's point of view
}

TEST(match_edge_pending) {
  Rig r(5);  // edges 0..4 known
  r.frame(4);
  FrameOut o = r.frame(6);  // frame from an edge not registered yet
  CHECK_EQ(o.k, -1);
  CHECK_EQ(o.faults, uint32_t(FAULT_UNMATCHED | FAULT_EDGE_PENDING));
  CHECK_EQ(o.epoch, 1u);
  r.ed.edge(5 * P);
  r.ed.edge(6 * P);
  r.ed.edge(7 * P);
  o = r.frame(7);  // related to frame 4: edge 5 missed, frame 6 was delivered but unmatched
  CHECK_EQ(o.k, 7);
  CHECK_EQ(o.faults, uint32_t(FAULT_SENSOR_MISSED));
  CHECK_EQ(o.sensor_missed, 1u);
  CHECK_EQ(o.unmatched_before, 1u);
  CHECK_EQ(o.delivery_lost, 0u);
}

TEST(match_first_frame_after_edges_reports_missed) {
  Rig r;
  FrameOut o = r.frame(3);
  CHECK_EQ(o.k, 3);
  CHECK_EQ(o.sensor_missed, 3u);
  CHECK_EQ(o.faults, uint32_t(FAULT_SENSOR_MISSED));
}

TEST(match_epoch_change) {
  Rig r(5);
  r.frame(4);
  r.ed.edge(5 * P + 1'000'000'000);  // restart: pulse 0 of epoch 2
  r.ed.edge(6 * P + 1'000'000'000);
  FrameOut o = r.m.frame({5 * P + 1'000'000'000 + L, 5, 5, true});
  CHECK_EQ(o.k, 0);
  CHECK_EQ(o.epoch, 2u);
  CHECK_EQ(o.faults, uint32_t(FAULT_EPOCH_CHANGE));
  CHECK_EQ(o.sensor_missed, 0u);
  o = r.m.frame({6 * P + 1'000'000'000 + L, 6, 6, true});
  CHECK_EQ(o.k, 1);
  CHECK_EQ(o.faults, uint32_t(FAULT_NONE));
}

TEST(match_beacon_correction_is_reported_once) {
  Rig r(0);
  for (int i = 0; i < 10; ++i)
    if (i != 4) r.ed.edge(i * P);  // capture of pulse 4 lost
  r.frame(3);
  FrameOut o = r.frame(4);  // no edge at 4: nearest is 3 or 5, both a full period off
  CHECK(o.faults & FAULT_LATE);
  o = r.frame(5);           // local K is 4 here, one low
  CHECK_EQ(o.k, 4);
  r.ed.beacon({1, 6, 6 * P});
  o = r.frame(6);
  CHECK_EQ(o.k, 6);
  CHECK_EQ(o.faults, uint32_t(FAULT_K_CORRECTED));  // K stepped by 1 in the corrected numbering
  o = r.frame(7);
  CHECK_EQ(o.k, 7);
  CHECK_EQ(o.faults, uint32_t(FAULT_NONE));
}

TEST(match_beacon_epoch_relabel_is_not_an_epoch_change) {
  Rig r;
  r.frame(0);
  r.ed.beacon({9, 1, P});
  FrameOut o = r.frame(1);
  CHECK_EQ(o.epoch, 9u);
  CHECK_EQ(o.faults, uint32_t(FAULT_NONE));
}

TEST(match_grid_source) {
  GridEdges grid(0, P);
  Matcher m({P, L}, grid);
  for (int64_t e = 0; e < 50; ++e) {
    FrameOut o = m.frame({e * P + L + (e % 3) * 1'000'000, static_cast<uint32_t>(e), 0, false});
    CHECK_EQ(o.k, e);
    CHECK_EQ(o.faults, uint32_t(FAULT_NONE));
  }
}

TEST(match_reset_forgets_previous) {
  Rig r;
  r.frame(0);
  r.m.reset();
  FrameOut o = r.frame(5);
  CHECK(!(o.faults & FAULT_DELIVERY_LOST));
  CHECK_EQ(o.sensor_missed, 5u);  // edges 0..4 of the epoch with nothing delivered as far as we know
}

CHECK_MAIN
