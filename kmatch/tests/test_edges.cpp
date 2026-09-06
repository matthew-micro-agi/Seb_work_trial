#include "kmatch/edges.hpp"

#include <vector>

#include "check.hpp"

using namespace kmatch;
static const ns_t P = 33'333'333;

struct Log {
  std::vector<Event> ev;
  EventSink sink() { return [this](const Event& e) { ev.push_back(e); }; }
  std::vector<std::string> kinds() const {
    std::vector<std::string> k;
    for (auto& e : ev) k.emplace_back(e.kind);
    return k;
  }
  int count(const char* kind) const {
    int n = 0;
    for (auto& e : ev) if (std::string(e.kind) == kind) ++n;
    return n;
  }
};

TEST(edges_count_from_zero) {
  Log log;
  CapturedEdges ed({P}, log.sink());
  for (int i = 0; i < 10; ++i) CHECK_EQ(ed.edge(i * P).k, i);
  CHECK_EQ(ed.epoch(), 1u);
  CHECK_EQ(log.count("epoch_start"), 1);
  CHECK_EQ(log.count("edge_grid_mismatch"), 0);
}

TEST(edges_gap_opens_new_epoch) {
  Log log;
  CapturedEdges ed({P}, log.sink());
  for (int i = 0; i < 5; ++i) ed.edge(i * P);
  Edge e = ed.edge(5 * P + 1'000'000'000);  // one second idle, then pulse 0
  CHECK_EQ(e.k, 0);
  CHECK_EQ(e.epoch, 2u);
  CHECK_EQ(log.count("epoch_start"), 2);
  CHECK_EQ(log.ev.back().value, 1'000'000'000 + P);
  CHECK_EQ(ed.edge(6 * P + 1'000'000'000).k, 1);
}

TEST(edges_short_gap_counts_received_and_flags_grid) {
  Log log;
  CapturedEdges ed({P}, log.sink());
  for (int i = 0; i < 5; ++i) ed.edge(i * P);
  Edge e = ed.edge(7 * P);  // pulses 5 and 6 not captured
  CHECK_EQ(e.k, 5);
  CHECK_EQ(e.epoch, 1u);
  CHECK_EQ(log.count("edge_grid_mismatch"), 1);
  CHECK_EQ(log.ev.back().value, 2);
  ed.edge(8 * P);  // same offset, no second event
  CHECK_EQ(log.count("edge_grid_mismatch"), 1);
}

TEST(edges_fill_from_grid_option) {
  EdgeConfig cfg{P};
  cfg.fill_from_grid = true;
  CapturedEdges ed(cfg);
  for (int i = 0; i < 5; ++i) ed.edge(i * P);
  CHECK_EQ(ed.edge(7 * P).k, 7);
}

TEST(edges_stall_and_resume) {
  Log log;
  CapturedEdges ed({P}, log.sink());
  ed.edge(0);
  ed.edge(P);
  ed.tick(2 * P);
  CHECK(!ed.stalled());
  ed.tick(3 * P + 1);
  CHECK(ed.stalled());
  ed.tick(4 * P);  // no second alarm
  CHECK_EQ(log.count("trigger_stall"), 1);
  ed.edge(10 * P);  // back, under the epoch gap: same epoch, K continues
  CHECK(!ed.stalled());
  CHECK_EQ(log.count("trigger_resumed"), 1);
  CHECK_EQ(ed.epoch(), 1u);
  CHECK_EQ(ed.last()->k, 2);
}

TEST(edges_rearm_forces_epoch) {
  CapturedEdges ed({P});
  for (int i = 0; i < 3; ++i) ed.edge(i * P);
  ed.rearm();
  CHECK_EQ(ed.edge(3 * P).k, 0);
  CHECK_EQ(ed.epoch(), 2u);
}

TEST(edges_nearest) {
  CapturedEdges ed({P});
  for (int i = 0; i < 5; ++i) ed.edge(i * P);
  CHECK_EQ(ed.nearest(2 * P + P / 3)->k, 2);
  CHECK_EQ(ed.nearest(2 * P + 2 * P / 3)->k, 3);
  CHECK_EQ(ed.nearest(-P)->k, 0);
  CHECK_EQ(ed.nearest(100 * P)->k, 4);
  CHECK(!CapturedEdges({P}).nearest(0));
}

TEST(edges_retention) {
  EdgeConfig cfg{P};
  cfg.retain = 10 * P;
  CapturedEdges ed(cfg);
  for (int i = 0; i < 100; ++i) ed.edge(i * P);
  CHECK(ed.edges().size() <= 12);
  CHECK_EQ(ed.edges().back().k, 99);
  CHECK_EQ(ed.nearest(0)->k, ed.edges().front().k);  // clamps to the oldest kept
}

TEST(beacon_agrees) {
  Log log;
  CapturedEdges ed({P}, log.sink());
  for (int i = 0; i < 10; ++i) ed.edge(i * P);
  CHECK_EQ(ed.beacon({1, 7, 7 * P + 1000}), 0);
  CHECK_EQ(log.count("beacon_ok"), 1);
}

TEST(beacon_corrects_lost_capture) {
  Log log;
  CapturedEdges ed({P}, log.sink());
  for (int i = 0; i < 10; ++i)
    if (i != 4) ed.edge(i * P);  // capture of pulse 4 lost: local K runs one low
  CHECK_EQ(ed.last()->k, 8);
  CHECK_EQ(ed.beacon({1, 9, 9 * P}), 1);
  CHECK_EQ(ed.last()->k, 9);
  CHECK_EQ(ed.nearest(0)->k, 0);  // edges before the loss shift too (they are in the same epoch)
  CHECK_EQ(ed.edge(10 * P).k, 10);
  CHECK_EQ(log.count("k_corrected"), 1);
  CHECK_EQ(log.count("edge_grid_mismatch"), 1);
  CHECK_EQ(ed.t0(), 0);  // grid re-anchored on the corrected numbering
}

TEST(beacon_late_start) {
  // Tile armed after pulse 0: it saw pulses 50.. as 0.., the first beacon fixes the offset.
  CapturedEdges ed({P});
  for (int i = 50; i < 60; ++i) ed.edge(i * P);
  CHECK_EQ(ed.last()->k, 9);
  CHECK_EQ(ed.beacon({3, 55, 55 * P}), 50);
  CHECK_EQ(ed.last()->k, 59);
  CHECK_EQ(ed.epoch(), 3u);
  CHECK_EQ(ed.last()->epoch, 3u);
  CHECK_EQ(ed.edge(60 * P).k, 60);
}

TEST(anchored_only_while_a_beacon_stands) {
  EdgeConfig cfg{P};
  cfg.retain = 3600LL * 1'000'000'000;
  CapturedEdges ed(cfg);
  CHECK(!ed.anchored());                       // nothing has confirmed the numbering yet
  for (int i = 0; i < 10; ++i) ed.edge(i * P);
  CHECK(!ed.anchored());
  ed.beacon({1, 40, 40 * P});                  // no edge near it: still ours
  CHECK(!ed.anchored());
  ed.beacon({1, 7, 7 * P});
  CHECK(ed.anchored());
  ed.rearm();                                  // the trigger restarted: the new epoch is ours again
  ed.edge(11 * P);
  CHECK(!ed.anchored());
  ed.beacon({4, 0, 11 * P});
  CHECK(ed.anchored());
}

TEST(grid_is_never_anchored) {
  CHECK(!GridEdges(0, P).anchored());          // a grid has no source to agree with
}

TEST(beacon_unmatched_when_no_edge_near) {
  Log log;
  CapturedEdges ed({P}, log.sink());
  for (int i = 0; i < 10; ++i) ed.edge(i * P);
  CHECK_EQ(ed.beacon({1, 40, 40 * P}), 0);
  CHECK_EQ(log.count("beacon_unmatched"), 1);
  CHECK_EQ(ed.last()->k, 9);
}

TEST(grid_edges_match_captured_on_clean_run) {
  EdgeConfig cfg{P};
  cfg.retain = 100 * P;
  CapturedEdges cap(cfg);
  GridEdges grid(0, P);
  for (int i = 0; i < 100; ++i) cap.edge(i * P);
  for (int i = 0; i < 100; ++i) {
    ns_t t = i * P + P / 5;
    CHECK_EQ(grid.nearest(t)->k, cap.nearest(t)->k);
    CHECK_EQ(grid.nearest(t)->t, cap.nearest(t)->t);
  }
  CHECK(!grid.last());
  CHECK_EQ(grid.nearest(-5 * P)->k, 0);
}

CHECK_MAIN
