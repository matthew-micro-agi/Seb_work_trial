// The local socket the proxy substream comes out of (DESIGN_RING_CONTROL.md §8.3a).
//
// The proxy branch has no GStreamer sink of its own: Branch::on_au is the tap, this is the pipe, and
// tile-agent is the reader that fans it out to every GET /v1/live client. Nothing is written to disk.
// A unix stream socket, non-blocking throughout: a reader that stops reading is dropped rather than
// allowed to stall the encoder, because the recording must never wait on the live view.
//
// Each access unit is length-prefixed: "KAU1", a big-endian u32 length, and a big-endian u32 of flags
// (bit 0 = keyframe), then that many bytes of Annex-B. The link is private to kpipe and the agent, so it
// can say where an access unit ends rather than making the reader wait for the next one to begin — which
// is a whole proxy frame period, 67 ms at 15 fps, straight off the live view's latency. One write call
// per access unit, so a reader that connects mid-stream always begins on a header.
#pragma once
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

class LiveSock {
 public:
  static constexpr size_t kBacklogCap = 2u << 20;   // a reader this far behind at 1 Mbps is not coming back

  ~LiveSock() { close(); }
  // Unlink a stale socket, bind, listen. False and `err` on failure; the caller decides whether that is fatal.
  bool open(const std::string& path, std::string& err);
  void close();
  static constexpr size_t kHeader = 12;
  // Accept whoever arrived, then hand one access unit to every reader. Never blocks, never throws.
  void write(const uint8_t* data, size_t len, bool keyframe);
  int clients();
  uint64_t dropped();

 private:
  struct Reader { int fd; std::string pending; };
  void flush(Reader& r);

  std::mutex lock_;
  int fd_ = -1;
  std::string path_;
  std::vector<Reader> readers_;
  uint64_t dropped_ = 0;      // readers dropped for falling behind
};
