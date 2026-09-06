// The RUNBOOK section 5 fault table, driven by the fake camera's schedule end to end:
// edges into CapturedEdges, frames into the Matcher, ticks for the stall alarm.
#include <map>
#include <string>
#include <vector>

#include "check.hpp"
#include "fakecam.hpp"
#include "kmatch/matcher.hpp"

using namespace kmatch;

struct Run {
  std::vector<Event> events;
  std::vector<FrameOut> frames;     // one per delivered frame, in order
  std::vector<int64_t> frame_edge;  // the fake camera's edge index for each
  int count(const char* kind) const {
    int n = 0;
    for (auto& e : events) if (std::string(e.kind) == kind) ++n;
    return n;
  }
  // Frames with any fault bit set, indexed by position.
  std::vector<size_t> faulty() const {
    std::vector<size_t> f;
    for (size_t i = 0; i < frames.size(); ++i) if (frames[i].faults) f.push_back(i);
    return f;
  }
  const FrameOut& at_edge(int64_t e) const {
    for (size_t i = 0; i < frames.size(); ++i) if (frame_edge[i] == e) return frames[i];
    static FrameOut none;
    return none;
  }
};

static Run run(fakecam::Config cfg, EdgeConfig ecfg = {}) {
  Run r;
  ecfg.period = cfg.period;
  CapturedEdges ed(ecfg, [&](const Event& e) { r.events.push_back(e); });
  Matcher m({cfg.period, cfg.latency}, ed);
  for (const auto& ev : fakecam::generate(cfg)) {
    switch (ev.type) {
      case fakecam::Ev::Edge: ed.edge(ev.t); break;
      case fakecam::Ev::Tick: ed.tick(ev.t); break;
      case fakecam::Ev::Frame:
        r.frames.push_back(m.frame(ev.frame));
        r.frame_edge.push_back(ev.e);
        break;
    }
  }
  return r;
}

TEST(clean_run_k_equals_edge_index) {
  fakecam::Config c;
  c.edges = 900;
  Run r = run(c);
  CHECK_EQ(r.frames.size(), 900u);
  for (size_t i = 0; i < r.frames.size(); ++i) {
    CHECK_EQ(r.frames[i].k, r.frame_edge[i]);
    CHECK_EQ(r.frames[i].residual, 0);
  }
  CHECK_EQ(r.faulty().size(), 0u);
  CHECK_EQ(r.count("epoch_start"), 1);
  CHECK_EQ(r.count("trigger_stall"), 0);
}

TEST(runbook_skip_100) {
  fakecam::Config c; c.edges = 200; c.faults = "skip@100";
  Run r = run(c);
  CHECK_EQ(r.frames.size(), 199u);
  CHECK_EQ(r.faulty(), std::vector<size_t>{100});  // the frame of edge 101
  const FrameOut& o = r.at_edge(101);
  CHECK_EQ(o.k, 101);
  CHECK_EQ(o.faults, uint32_t(FAULT_SENSOR_MISSED));
  CHECK_EQ(o.sensor_missed, 1u);
  CHECK_EQ(o.delivery_lost, 0u);
}

TEST(runbook_drop_200) {
  fakecam::Config c; c.edges = 300; c.faults = "drop@200";
  Run r = run(c);
  CHECK_EQ(r.frames.size(), 299u);
  CHECK_EQ(r.faulty(), std::vector<size_t>{200});
  const FrameOut& o = r.at_edge(201);
  CHECK_EQ(o.k, 201);
  CHECK_EQ(o.faults, uint32_t(FAULT_DELIVERY_LOST));
  CHECK_EQ(o.delivery_lost, 1u);
}

TEST(runbook_drop_without_frame_start_looks_like_sensor_miss) {
  // Without the receiver's frame-start count the two causes cannot be told apart.
  fakecam::Config c; c.edges = 300; c.faults = "drop@200"; c.frame_start = false;
  Run r = run(c);
  const FrameOut& o = r.at_edge(201);
  CHECK_EQ(o.k, 201);
  CHECK_EQ(o.faults, uint32_t(FAULT_SENSOR_MISSED));
}

TEST(runbook_dup_300) {
  fakecam::Config c; c.edges = 400; c.faults = "dup@300";
  Run r = run(c);
  CHECK_EQ(r.frames.size(), 401u);
  CHECK_EQ(r.faulty(), std::vector<size_t>{301});  // the second copy
  CHECK_EQ(r.frames[301].k, 300);
  CHECK_EQ(r.frames[301].faults, uint32_t(FAULT_DUPLICATE));
  CHECK_EQ(r.frames[302].k, 301);
  CHECK_EQ(r.frames[302].faults, uint32_t(FAULT_NONE));
}

TEST(runbook_late_400_by_40ms) {
  // 40 ms late at 33 ms period: the frame lands on edge 401. Python's design ran K one
  // high until the window check; here the real frame 401 shows as DUPLICATE and K is
  // right again from 402.
  fakecam::Config c; c.edges = 500; c.faults = "late@400:40";
  Run r = run(c);
  CHECK_EQ(r.faulty(), (std::vector<size_t>{400, 401}));
  CHECK_EQ(r.at_edge(400).k, 401);
  CHECK_EQ(r.at_edge(400).faults, uint32_t(FAULT_SENSOR_MISSED));
  CHECK_EQ(r.at_edge(401).k, 401);
  CHECK_EQ(r.at_edge(401).faults, uint32_t(FAULT_DUPLICATE));
  CHECK_EQ(r.at_edge(402).k, 402);
  CHECK_EQ(r.at_edge(402).faults, uint32_t(FAULT_NONE));
}

TEST(late_by_12ms_is_flagged_with_correct_k) {
  fakecam::Config c; c.edges = 500; c.faults = "late@400:12";
  Run r = run(c);
  CHECK_EQ(r.faulty(), std::vector<size_t>{400});
  CHECK_EQ(r.at_edge(400).k, 400);
  CHECK_EQ(r.at_edge(400).faults, uint32_t(FAULT_LATE));
  CHECK_EQ(r.at_edge(400).residual, 12'000'000);
}

TEST(runbook_skip_500_x3) {
  fakecam::Config c; c.edges = 600; c.faults = "skip@500:3";
  Run r = run(c);
  CHECK_EQ(r.frames.size(), 597u);
  CHECK_EQ(r.faulty(), std::vector<size_t>{500});
  const FrameOut& o = r.at_edge(503);
  CHECK_EQ(o.k, 503);
  CHECK_EQ(o.sensor_missed, 3u);
}

TEST(runbook_sequence_wrap) {
  fakecam::Config c; c.edges = 300; c.seq_start = 0xFFFFFF80;
  Run r = run(c);
  CHECK_EQ(r.faulty().size(), 0u);
  for (size_t i = 0; i < r.frames.size(); ++i) CHECK_EQ(r.frames[i].k, r.frame_edge[i]);
}

TEST(runbook_full_default_schedule) {
  fakecam::Config c; c.edges = 900; c.seq_start = 0xFFFFFF00;
  c.faults = "skip@100,drop@200,dup@300,late@400:40,skip@500:3";
  Run r = run(c);
  CHECK_EQ(r.frames.size(), 900u - 1 - 1 + 1 - 3);
  CHECK_EQ(r.faulty().size(), 6u);  // 101, 201, dup copy, late + its dup, 503
  int64_t last = -1;
  for (auto& f : r.frames) {
    CHECK(f.k >= last);  // K never goes backwards
    last = f.k;
  }
  CHECK_EQ(r.frames.back().k, 899);
}

TEST(restart_mid_run_opens_new_epoch) {
  fakecam::Config c; c.edges = 200; c.faults = "restart@100";
  Run r = run(c);
  CHECK_EQ(r.count("trigger_stall"), 1);
  CHECK_EQ(r.count("trigger_resumed"), 1);
  CHECK_EQ(r.count("epoch_start"), 2);
  CHECK_EQ(r.faulty(), std::vector<size_t>{100});
  const FrameOut& o = r.at_edge(100);
  CHECK_EQ(o.k, 0);
  CHECK_EQ(o.epoch, 2u);
  CHECK_EQ(o.faults, uint32_t(FAULT_EPOCH_CHANGE));
  CHECK_EQ(r.at_edge(199).k, 99);
  CHECK_EQ(r.at_edge(199).epoch, 2u);
}

TEST(short_source_pause_is_a_stall_not_an_epoch) {
  fakecam::Config c; c.edges = 200; c.faults = "restart@100:200";  // 200 ms pause
  Run r = run(c);
  CHECK_EQ(r.count("trigger_stall"), 1);
  CHECK_EQ(r.count("epoch_start"), 1);
  CHECK_EQ(r.count("edge_grid_mismatch"), 1);  // received count now lags the grid
  CHECK_EQ(r.at_edge(100).k, 100);             // count received: the pause had no pulses
  CHECK_EQ(r.at_edge(100).faults, uint32_t(FAULT_NONE));
}

TEST(lost_capture_then_beacon) {
  fakecam::Config c; c.edges = 100; c.faults = "lostcap@50";
  Run r;
  EdgeConfig ecfg{c.period};
  CapturedEdges ed(ecfg, [&](const Event& e) { r.events.push_back(e); });
  Matcher m({c.period, c.latency}, ed);
  for (const auto& ev : fakecam::generate(c)) {
    if (ev.type == fakecam::Ev::Edge) ed.edge(ev.t);
    if (ev.type == fakecam::Ev::Tick) ed.tick(ev.t);
    if (ev.type == fakecam::Ev::Frame) {
      r.frames.push_back(m.frame(ev.frame));
      r.frame_edge.push_back(ev.e);
    }
    if (ev.type == fakecam::Ev::Edge && ev.e == 60) ed.beacon({1, 60, ev.t});  // source's word
  }
  CHECK_EQ(r.count("edge_grid_mismatch"), 1);
  CHECK_EQ(r.at_edge(50).k, -1);              // its edge was never captured: nothing to match
  CHECK_EQ(r.at_edge(50).faults, uint32_t(FAULT_UNMATCHED | FAULT_EDGE_PENDING));
  CHECK_EQ(r.at_edge(51).k, 50);              // one low until the beacon
  CHECK_EQ(r.at_edge(51).unmatched_before, 1u);
  CHECK_EQ(r.at_edge(51).faults, uint32_t(FAULT_NONE));
  CHECK_EQ(r.at_edge(55).k, 54);
  CHECK_EQ(r.at_edge(60).k, 60);  // beacon arrived with edge 60, before this frame
  CHECK_EQ(r.at_edge(60).faults, uint32_t(FAULT_K_CORRECTED));
  CHECK_EQ(r.at_edge(61).k, 61);
  CHECK_EQ(r.at_edge(61).faults, uint32_t(FAULT_NONE));
  CHECK_EQ(r.at_edge(99).k, 99);
  CHECK_EQ(r.count("k_corrected"), 1);
}

CHECK_MAIN
