// Feeds kmatch::CapturedEdges from a PTP clock's external timestamp channel (CPTS HW_TS_PUSH),
// converting PTP time to CLOCK_MONOTONIC with a once-per-second offset measurement.
#pragma once
#include <atomic>
#include <mutex>
#include <string>
#include <thread>
#include "kmatch/edges.hpp"

class PtpEdgeFeed {
 public:
  PtpEdgeFeed(const std::string& dev, unsigned channel, kmatch::CapturedEdges& edges, std::mutex& lock);
  ~PtpEdgeFeed();
  void start();
  void stop();
  int64_t offset_ns() const { return offset_.load(); }   // PTP - MONOTONIC
  long events() const { return events_.load(); }
 private:
  void run();
  bool sync(int fd);
  std::string dev_; unsigned chan_; kmatch::CapturedEdges& edges_; std::mutex& lock_;
  std::thread th_; std::atomic<bool> stop_{false}; std::atomic<int64_t> offset_{0}; std::atomic<long> events_{0};
};
