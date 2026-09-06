// Segment files on the ring: the writer (rotation at IDRs, reclaim under a budget, .part repair at start)
// and the tree helpers ringfsck and the tests share. DESIGN_RING_CONTROL.md §3, §4, §6, §7.
//
// The writer owns one thread: on_au() copies the AU into a bounded queue and returns; all file I/O and
// every event happen on the writer thread. Events are JSON lines handed to the sink (called from the
// writer thread, and from start() for repairs); the recorder prints them on stdout.
#pragma once
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace segfile {

struct Config {
  std::string ring;                   // directory holding seg/ (and lock/, written by the agent)
  unsigned segment_frames = 120;      // rotate at the first IDR once this many frames are in the file
  uint64_t budget_bytes = 0;          // bytes the ring may occupy; 0 = never reclaim
  uint64_t expected_bytes = 3000000;  // one segment's worth, the reclaim target margin
  int64_t idle_close_ns = 5000000000; // close the open segment after this long without an AU
  size_t queue_max = 64;              // AUs; a full queue drops the newest
};

struct Meta { uint32_t epoch = 0; int64_t k = -1; int64_t t_mono = 0; uint32_t faults = 0; bool idr = false; };
using EventSink = std::function<void(const std::string& json_line)>;

// Tree helpers
std::string seg_name(uint64_t segno);                              // "%012llu.h265"
std::vector<uint64_t> list_segnos(const std::string& dir);         // closed .h265 files, ascending
uint64_t next_segno(const std::string& ring);                      // max over seg/ (.part included) and lock/*/, plus one
uint64_t used_bytes(const std::string& ring);                      // statvfs if ring is a mount point, else a tree walk, inodes once
struct Repaired { uint64_t segno; size_t frames; bool kept; };     // kept=false: no complete AU, unlinked
std::vector<Repaired> repair_parts(const std::string& ring);       // every seg/*.h265.part, §7 step 1

class Writer {
 public:
  Writer(Config cfg, EventSink sink);
  ~Writer();
  void start();                                                     // repair, seed segno, victim list, thread
  void on_au(const uint8_t* data, size_t len, const Meta& m);      // any thread; never blocks on I/O
  void stop();                                                      // drain, close, join
  bool failed() const { return failed_; }

  struct Stats { uint64_t open_segno; uint64_t bytes_total; uint64_t dropped; uint32_t pre_idr_dropped; uint64_t segments_closed; };
  Stats stats() const { return {open_segno_.load(), bytes_total_.load(), dropped_.load(), pre_idr_dropped_.load(), closed_.load()}; }

 private:
  struct Item { std::vector<uint8_t> au; Meta m; int64_t t_arrival; };
  void run();
  void handle(const Item& it);
  bool reclaim_until_fits(uint64_t need);
  void open_next();
  void close_segment();
  void fail(const std::string& what);

  Config cfg_; EventSink sink_; std::string seg_dir_;
  std::mutex mu_; std::condition_variable cv_; std::deque<Item> q_; bool stopping_ = false; std::thread th_;
  std::deque<uint64_t> victims_;   // closed segments in seg/, ascending
  int64_t last_arrival_ = 0; uint64_t segno_ = 0; int fd_ = -1; bool open_ = false; unsigned frames_ = 0; uint32_t epoch_ = 0; bool wrote_any_ = false, full_ = false;
  std::atomic<bool> failed_{false};
  std::atomic<uint64_t> open_segno_{0}, bytes_total_{0}, dropped_{0}, closed_{0}; std::atomic<uint32_t> pre_idr_dropped_{0};
};

}  // namespace segfile
