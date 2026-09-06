// segfile on a scratch directory ($TMPDIR, else /dev/shm): rotation at IDRs, epoch split, reclaim order,
// hard-linked files surviving two wraps, storage_full termination and recovery, .part repair, segno seeding.
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <string>
#include <vector>
#include <sys/stat.h>
#include <unistd.h>
#include "segfile/segfile.hpp"
#include "sei/sei.hpp"
#include "../tools/synth_au.hpp"

static int fails = 0;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #c); fails++; } } while (0)

namespace {
struct Events {
  std::mutex mu; std::vector<std::string> lines;
  segfile::EventSink sink() { return [this](const std::string& l) { std::lock_guard<std::mutex> g(mu); lines.push_back(l); }; }
  int count(const std::string& needle) { int n = 0; for (auto& l : lines) if (l.find(needle) != std::string::npos) n++; return n; }
};
constexpr unsigned GOP = 10; constexpr size_t PAYLOAD = 4000;

// frames [from, to) with IDRs every GOP frames by index; the epoch bumps at frame epoch_at (not an IDR frame)
void feed(segfile::Writer& w, long from, long to, long epoch_at = -1) {
  uint32_t epoch = 1; long k = 0;
  for (long i = from; i < to; i++) {
    if (i == epoch_at) { epoch = 2; k = 0; }
    bool idr = (i % GOP) == 0;
    sei::Record r{uint16_t(epoch), uint32_t(k), uint64_t(1000000000LL + i * 33333333LL), uint16_t(i), 0};
    auto au = synth_au(idr, PAYLOAD, r, uint32_t(i));
    w.on_au(au.data(), au.size(), segfile::Meta{epoch, k, int64_t(r.frame_ts_ns), 0, idr});
    k++;
  }
}
std::vector<uint8_t> slurp(const std::string& p) { std::ifstream f(p, std::ios::binary); return {std::istreambuf_iterator<char>(f), {}}; }
bool exists(const std::string& p) { struct stat st; return stat(p.c_str(), &st) == 0; }
std::string seg(const std::string& ring, uint64_t s) { return ring + "/seg/" + segfile::seg_name(s); }
size_t frames_in(const std::string& p) { auto d = slurp(p); return sei::split(d.data(), d.size()).size(); }
segfile::Config cfg(const std::string& ring, unsigned frames, uint64_t budget) {
  segfile::Config c; c.ring = ring; c.segment_frames = frames; c.budget_bytes = budget; c.expected_bytes = 90000; c.queue_max = 1 << 20; return c;   // the tests feed in bursts
}
std::string fresh(const std::string& root, const char* name) { std::string r = root + "/" + name; mkdir(r.c_str(), 0755); return r; }
}  // namespace

int main() {
  const char* base = getenv("TMPDIR"); std::string tmpl = std::string(base ? base : "/dev/shm") + "/segfile-test-XXXXXX";
  std::vector<char> buf(tmpl.begin(), tmpl.end()); buf.push_back(0);
  std::string root = mkdtemp(buf.data());

  {  // rotation at IDRs: 100 frames, 20 per segment -> segments 1..5 of 20 frames, each starting VPS + IDR
    std::string ring = fresh(root, "rot"); Events ev;
    { segfile::Writer w(cfg(ring, 20, 0), ev.sink()); w.start(); feed(w, 0, 100); w.stop(); }
    auto segs = segfile::list_segnos(ring + "/seg");
    CHECK(segs == std::vector<uint64_t>({1, 2, 3, 4, 5}));
    CHECK(ev.count("segment_closed") == 5);
    for (uint64_t s : segs) {
      auto d = slurp(seg(ring, s)); auto aus = sei::split(d.data(), d.size());
      CHECK(aus.size() == 20);
      auto head = sei::inspect(d.data(), aus[0]); CHECK(head.idr && head.has_vps && head.record && head.record->k == (s - 1) * 20);
    }
    CHECK(!exists(seg(ring, 5) + ".part"));
  }
  {  // rotation is at the IDR at or after segment_frames: 25 per segment with GOP 10 rotates every 30
    std::string ring = fresh(root, "rot25"); Events ev;
    { segfile::Writer w(cfg(ring, 25, 0), ev.sink()); w.start(); feed(w, 0, 90); w.stop(); }
    CHECK(segfile::list_segnos(ring + "/seg").size() == 3);
    CHECK(frames_in(seg(ring, 1)) == 30 && frames_in(seg(ring, 2)) == 30);
  }
  {  // epoch split: 30 per segment, epoch changes at frame 33 -> segment 2 closes at the IDR at 40
    std::string ring = fresh(root, "epoch"); Events ev;
    { segfile::Writer w(cfg(ring, 30, 0), ev.sink()); w.start(); feed(w, 0, 100, 33); w.stop(); }
    CHECK(segfile::list_segnos(ring + "/seg") == std::vector<uint64_t>({1, 2, 3, 4}));
    CHECK(frames_in(seg(ring, 1)) == 30 && frames_in(seg(ring, 2)) == 10 && frames_in(seg(ring, 3)) == 30 && frames_in(seg(ring, 4)) == 30);
    auto d = slurp(seg(ring, 3)); auto aus = sei::split(d.data(), d.size()); auto head = sei::inspect(d.data(), aus[0]);
    CHECK(head.record && head.record->epoch == 2 && head.record->k == 7);
    d = slurp(seg(ring, 2)); aus = sei::split(d.data(), d.size()); auto tail = sei::inspect(d.data(), aus.back());
    CHECK(tail.record && tail.record->epoch == 2 && tail.record->k == 6);   // the change lives inside segment 2
  }
  {  // reclaim order under a budget: 20 segments of ~85 KB into 400 KB
    std::string ring = fresh(root, "reclaim"); Events ev;
    { segfile::Writer w(cfg(ring, 20, 400000), ev.sink()); w.start(); feed(w, 0, 400); w.stop(); }
    auto segs = segfile::list_segnos(ring + "/seg");
    CHECK(segs.size() >= 2 && segs.size() <= 5); CHECK(segs.back() == 20);
    CHECK(segfile::used_bytes(ring) <= 400000);
    uint64_t expect = 1; int reclaimed = 0;
    for (auto& l : ev.lines) if (l.find("\"reclaimed\"") != std::string::npos) {
      auto n = std::stoull(l.substr(l.find("segno\":") + 7)); CHECK(n == expect); expect++; reclaimed++;
    }
    CHECK(reclaimed + int(segs.size()) == 20);
    CHECK(ev.count("storage_full") == 0);
  }
  {  // a hard-linked segment survives two wraps and stays byte-identical; the ring keeps rotating
    std::string ring = fresh(root, "lock"); Events ev;
    { segfile::Writer w(cfg(ring, 20, 0), ev.sink()); w.start(); feed(w, 0, 100); w.stop(); }
    mkdir((ring + "/lock").c_str(), 0755); mkdir((ring + "/lock/L").c_str(), 0755);
    CHECK(link(seg(ring, 3).c_str(), (ring + "/lock/L/" + segfile::seg_name(3)).c_str()) == 0);
    auto before = slurp(seg(ring, 3));
    { segfile::Writer w(cfg(ring, 20, 400000), ev.sink()); w.start(); feed(w, 100, 700); w.stop(); }   // 30 more segments, ~3 wraps
    CHECK(!exists(seg(ring, 3)));
    CHECK(slurp(ring + "/lock/L/" + segfile::seg_name(3)) == before);
    CHECK(segfile::list_segnos(ring + "/seg").back() == 35);
    CHECK(ev.count("storage_full") == 0);
    CHECK(segfile::used_bytes(ring) <= 400000);
  }
  {  // everything locked and over budget: storage_full once, no hang; unlock -> the next IDR records again
    std::string ring = fresh(root, "full"); Events ev;
    { segfile::Writer w(cfg(ring, 20, 0), ev.sink()); w.start(); feed(w, 0, 100); w.stop(); }
    mkdir((ring + "/lock").c_str(), 0755); mkdir((ring + "/lock/L").c_str(), 0755);
    for (uint64_t s = 1; s <= 5; s++) link(seg(ring, s).c_str(), (ring + "/lock/L/" + segfile::seg_name(s)).c_str());
    { segfile::Writer w(cfg(ring, 20, 300000), ev.sink()); w.start();
      feed(w, 100, 160); w.stop(); }
    CHECK(ev.count("storage_full") == 1);
    CHECK(segfile::list_segnos(ring + "/seg") == std::vector<uint64_t>({5}));   // the newest stays so the segno is remembered
    for (uint64_t s = 1; s <= 5; s++) unlink((ring + "/lock/L/" + segfile::seg_name(s)).c_str());
    rmdir((ring + "/lock/L").c_str());
    { segfile::Writer w(cfg(ring, 20, 300000), ev.sink()); w.start(); feed(w, 160, 200); w.stop(); }
    auto segs = segfile::list_segnos(ring + "/seg");
    CHECK(segs == std::vector<uint64_t>({5, 6, 7}));   // segno never reused
  }
  {  // kill mid-segment then repair: a .part cut inside its 7th AU keeps 6 frames; a .part with no whole AU goes
    std::string ring = fresh(root, "part"); Events ev;
    { segfile::Writer w(cfg(ring, 20, 0), ev.sink()); w.start(); feed(w, 0, 40); w.stop(); }
    auto d = slurp(seg(ring, 1)); auto aus = sei::split(d.data(), d.size());
    size_t cut = aus[6].begin + (aus[6].end - aus[6].begin) / 2;
    { std::ofstream f(seg(ring, 9) + ".part", std::ios::binary); f.write((const char*)d.data(), long(cut)); }
    { std::ofstream f(seg(ring, 10) + ".part", std::ios::binary); f.write((const char*)d.data(), 30); }
    ev.lines.clear();
    { segfile::Writer w(cfg(ring, 20, 0), ev.sink()); w.start(); feed(w, 40, 60); w.stop(); }
    CHECK(ev.count("\"segment_closed\",\"segno\":9,\"partial\":true,\"frames\":6") == 1);
    CHECK(ev.count("\"part_discarded\",\"segno\":10") == 1);
    CHECK(exists(seg(ring, 9)) && !exists(seg(ring, 9) + ".part") && !exists(seg(ring, 10) + ".part"));
    CHECK(frames_in(seg(ring, 9)) == 6);
    CHECK(segfile::list_segnos(ring + "/seg") == std::vector<uint64_t>({1, 2, 9, 10}));   // 10 reused: it never was a segment
    CHECK(sei::last_complete_au_end(d.data(), d.size()) == aus[aus.size() - 2].end);
  }
  {  // segno seeding: the highest name anywhere, lock/ included
    std::string ring = fresh(root, "seed"); Events ev;
    { segfile::Writer w(cfg(ring, 20, 0), ev.sink()); w.start(); feed(w, 0, 40); w.stop(); }
    mkdir((ring + "/lock").c_str(), 0755); mkdir((ring + "/lock/L").c_str(), 0755);
    { std::ofstream f(ring + "/lock/L/" + segfile::seg_name(9)); f << "x"; }
    CHECK(segfile::next_segno(ring) == 10);
    { std::ofstream f(seg(ring, 12) + ".part"); f << "x"; }
    CHECK(segfile::next_segno(ring) == 13);
  }
  {  // AUs before the first IDR are dropped and counted
    std::string ring = fresh(root, "preidr"); Events ev;
    segfile::Writer w(cfg(ring, 20, 0), ev.sink()); w.start(); feed(w, 5, 40); w.stop();
    CHECK(w.stats().pre_idr_dropped == 5);
    CHECK(frames_in(seg(ring, 1)) == 20 && frames_in(seg(ring, 2)) == 10);
  }

  fprintf(stderr, "test_segfile: %d failures (%s)\n", fails, root.c_str());
  if (!fails) { std::string cmd = "rm -rf " + root; if (system(cmd.c_str()) != 0) {} }
  return fails ? 1 : 0;
}
