// Minimal V4L2 mmap capture: one node, one format, N buffers. Frames are handed out as
// (sequence, CLOCK_MONOTONIC timestamp, pointer, size) and returned with release().
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

class V4l2Capture {
 public:
  struct Frame { uint32_t seq; int64_t ts_ns; const uint8_t* data; size_t size; unsigned index; };
  V4l2Capture(const std::string& dev, unsigned width, unsigned height, uint32_t fourcc, unsigned nbuf = 6);
  ~V4l2Capture();
  // Stream on. Retries once: the IMX296 fails its first stream-on after a power-up (HOWTO §5).
  void start();
  void stop();
  bool dequeue(Frame& f, int timeout_ms);   // false on timeout
  void release(const Frame& f);
  unsigned width() const { return w_; }
  unsigned height() const { return h_; }
  size_t bytesperline() const { return stride_; }
 private:
  int fd_ = -1; unsigned w_, h_; size_t stride_ = 0;
  std::vector<std::pair<uint8_t*, size_t>> maps_;
};
