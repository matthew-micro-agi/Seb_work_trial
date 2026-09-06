// KMeta: the trigger index K riding the raw GStreamer buffer, and the bridge that carries it across
// one encoder by PTS (PLAN_K_MATCHING.md M4b). The capture thread stamps the buffer once; each encoder
// branch copies the stamp into its own table on the encoder's sink pad and pops it on the parser's
// src pad, where the SEI is inserted. Nothing downstream of the capture thread writes to the meta.
#pragma once
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <gst/gst.h>
#include "sei/sei.hpp"

// What the matcher decided for one frame. Meta payload, bridge table value, SEI input.
struct KStamp { uint32_t epoch; int64_t k; uint32_t seq; int64_t t_frame_ns; uint32_t faults; };

// SEI flag bit 15: the stamp did not reach the SEI probe (bridge miss). kmatch owns bits 0..8.
constexpr uint32_t FAULT_STAMP_LOST = 1u << 15;

struct KMeta { GstMeta meta; KStamp stamp; };
GType kmeta_api_type();
KMeta* kmeta_add(GstBuffer* buf, const KStamp& s);   // buf must be writable
const KStamp* kmeta_get(GstBuffer* buf);            // nullptr when absent

// One encoder branch. attach() adds the two probes; the counters are read by the status line. The codec
// is the branch's own: the recording is H.265, the proxy substream H.264 (DESIGN_RING_CONTROL.md §8.3a),
// and it decides only how the SEI NAL is framed — the record inside it is the same either way.
struct Branch {
  explicit Branch(const char* n, sei::Codec c = sei::Codec::H265) : name(n), codec(c) {}
  void attach(GstElement* encoder, GstElement* parser, std::function<void(const std::string&)> log_event);
  // sane(): nothing went wrong with the bridge. clean(): and the encoder gave back every frame it was
  // given. The recording branch must be clean; the proxy only sane — a frame still inside the proxy
  // encoder when the valve closed is a picture nobody will see, not a fault.
  bool sane() const { return miss == 0 && evicted == 0 && reordered == 0; }
  bool clean() const { return sane() && table.empty(); }
  std::string summary() const;

  const char* name;
  sei::Codec codec;
  std::function<void(const std::string&)> log;
  // Called with the finished access unit (SEI already inserted) of every frame this branch emits.
  // kpipe points it at the segment writer; kmeta itself knows nothing about storage.
  std::function<void(const uint8_t* au, size_t len, const KStamp&, bool keyframe)> on_au;
  std::mutex lock;
  std::map<GstClockTime, KStamp> table;   // frames inside the encoder, keyed by their raw PTS
  static constexpr size_t kDepth = 16;
  bool have_offset = false; int64_t pts_offset = 0; GstClockTime last_key = 0;
  // Set when EOS passes the parser's src pad, i.e. after the last access unit has been through the SEI
  // probe and on_au. That, not the pipeline's EOS, is when this branch's output is complete.
  std::atomic<bool> saw_eos{false};
  long in = 0, out = 0, miss = 0, evicted = 0, reordered = 0, meta_seen = 0, meta_agrees = 0;
  uint64_t bytes = 0;                     // access-unit bytes emitted, SEI included
  size_t max_depth = 0;
};
