// kpipe — camera to trigger-indexed H.265 on the J722S, in one process.
//
//   V4L2 (raw Bayer 10) -> luma NV12 -> appsrc [KMeta] -> tee -> queue -> v4l2h265enc (Wave5) -> h265parse -> [SEI] -> file / ring
//                                 \-> kmatch::Matcher            \-> valve -> videorate -> scaler -> v4l2h264enc
//                                                                   -> h264parse -> [SEI] -> local socket
//
// K is decided in the capture thread and stamped on the raw buffer as a KMeta; each encoder branch bridges
// it across its encoder by PTS (kmeta.hpp, PLAN_K_MATCHING.md M4b). The record bytes match
// backup/python/camsync/sei.py, so `python3 -m camsync.check_stream out.h265 out.jsonl` verifies the result.
//
// The second branch is the proxy substream of DESIGN_RING_CONTROL.md §8.3a: the same frames, decimated and
// scaled down, H.264 at about 1 Mbps, out of a unix socket that tile-agent fans out to GET /v1/live. It is
// off unless someone is watching — a closed valve costs one buffer reference per frame — and the agent
// opens it with a one-line command on kpipe's stdin, the mirror of the JSON events kpipe writes to stdout.
//
//   kpipe --out rec.h265 --log rec.jsonl [--frames N] [--edges grid|ptp] [--ptp /dev/ptp0 --chan 0]
//         [--latency-ns 20000000] [--period-ns 33333333] [--tcp 5600] [--bitrate 6000000] [--device /dev/video3]
//         [--fake] [--enc v4l2h265enc|x265enc]
//   kpipe --ring /ring [--segment-s 4] [--budget BYTES] ...   segment files instead of --out (segfile,
//         DESIGN_RING_CONTROL.md §4); every event is one JSON line on stdout unless --log names a file
//   proxy branch, built when --proxy-sock or --proxy-out is given:
//         [--proxy-sock /tmp/tile-live.sock] [--proxy-out proxy.h264] [--proxy-enc v4l2h264enc|x264enc]
//         [--proxy-width 728] [--proxy-height 544] [--proxy-fps 15] [--proxy-bitrate 1000000]
//         [--proxy-scaler videoscale | tiovxmultiscaler | none]
//
// stdin is a line protocol from the tile agent, the mirror of the JSON events on stdout:
//   proxy on | proxy off      open or close the proxy valve (SIGUSR1 toggles it too)
//   stop                      shut down as if interrupted
//   beacon EPOCH K PTP_NS     the trigger source's word on one pulse (tileagent/beacon.py, M7)
// Unknown lines are ignored, so a verb can be added without a version step. Until a beacon has landed,
// K is this process's own count of pulses and every frame says so with FAULT_K_LOCAL.
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <poll.h>
#include <time.h>
#include <unistd.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <gst/video/video-event.h>
#include "edgefeed.hpp"
#include "kmatch/edges.hpp"
#include "kmatch/matcher.hpp"
#include <iostream>
#include "kmeta.hpp"
#include "livesock.hpp"
#include "segfile/segfile.hpp"
#include "v4l2cap.hpp"

namespace {
struct Opts {
  std::string device = "/dev/video3", out = "rec.h265", log = "rec.jsonl", edges = "grid", ptp = "/dev/ptp0";
  std::string enc = "v4l2h265enc", proxy_out, proxy_sock, ring;
  std::string proxy_enc = "v4l2h264enc", proxy_scaler = "videoscale";
  unsigned proxy_width = 728, proxy_height = 544, proxy_fps = 15; int proxy_bitrate = 1'000'000;
  unsigned width = 1456, height = 1088, fps = 30, chan = 0, segment_s = 4; int tcp = 0; long frames = 0; bool fake = false;
  uint64_t budget = 0; bool log_given = false;
  // latency defaults to one trigger period: the receiver completes buffer N when frame N+1 starts,
  // so a frame's timestamp lands one period after its pulse, whatever the exposure (OPEN.md §1).
  int64_t latency = -1, period = 33'333'333; int bitrate = 6'000'000;
  bool proxy() const { return !proxy_sock.empty() || !proxy_out.empty(); }
};

// Whole lines from stdin on a thread of its own; the callback runs on that thread.
struct StdinLines {
  ~StdinLines() { stop = true; if (th.joinable()) th.join(); }
  void start(std::function<void(const std::string&)> on_line) {
    th = std::thread([this, on_line] {
      std::string buf; char c[512];
      while (!stop) {
        pollfd p{0, POLLIN, 0};
        if (poll(&p, 1, 200) <= 0) continue;
        ssize_t n = read(0, c, sizeof c);
        if (n <= 0) return;                                  // EOF: no agent, or it closed the pipe
        for (ssize_t i = 0; i < n; i++) {
          if (c[i] == '\n') { on_line(buf); buf.clear(); }
          else if (buf.size() < sizeof c) buf += c[i];
        }
      }
    });
  }
  std::thread th; std::atomic<bool> stop{false};
};

std::atomic<bool> g_stop{false}, g_toggle{false};
std::atomic<int> g_proxy_want{-1};      // stdin command: 1 open, 0 close, -1 nothing asked
void on_sigint(int) { g_stop = true; }
void on_sigusr1(int) { g_toggle = true; }

// Proxy decimation, on the valve's src pad. videorate downstream relabels the caps to the proxy rate but
// is left nothing to do: on its own it lets a second or two through at the full rate every time the valve
// opens, and this keeps the branch at exactly one frame in N from the first one. Bresenham, so a proxy
// rate that does not divide the capture rate is still evenly spread.
struct Decimate {
  unsigned in_fps = 30, out_fps = 15; long acc = 0;
  void reset() { acc = long(in_fps) - long(out_fps); }   // the first frame after the valve opens passes
  bool pass() { acc += long(out_fps); if (acc < long(in_fps)) return false; acc -= long(in_fps); return true; }
};

GstPadProbeReturn decimate_probe(GstPad*, GstPadProbeInfo*, gpointer user) {
  return static_cast<Decimate*>(user)->pass() ? GST_PAD_PROBE_OK : GST_PAD_PROBE_DROP;
}

struct Ctx {
  Opts o; std::mutex lock; std::ofstream log; long in = 0;
  Branch rec{"rec"}, proxy{"proxy", sei::Codec::H264};
  std::ostream* ev = nullptr;          // event lines: the --log file, or stdout with --ring
  segfile::Writer* w = nullptr;
  void event(const std::string& line) { std::lock_guard<std::mutex> g(lock); if (ev) *ev << line << '\n'; }
};

void to_nv12_luma(const uint8_t* src, size_t stride, unsigned w, unsigned h, uint8_t* dst) {
  for (unsigned y = 0; y < h; y++) {
    const uint16_t* s = reinterpret_cast<const uint16_t*>(src + y * stride); uint8_t* d = dst + size_t(y) * w;
    for (unsigned x = 0; x < w; x++) d[x] = uint8_t(s[x] >> 2);
  }
  memset(dst + size_t(w) * h, 0x80, size_t(w) * h / 2);
}

// --fake: frames in the camera's raw layout (10-bit in 16-bit words) at the configured rate. The frame
// index is in the picture as a 16-block barcode over the top half (MSB left), so a decoded file can be
// checked against the SEI by content (pipeline/check_m4b.py). Not a fake camera: no faults.
struct FakeSource {
  FakeSource(unsigned w, unsigned h, int64_t period) : w_(w), h_(h), period_(period), pix_(size_t(w) * h) {}
  void next(V4l2Capture::Frame& f) {
    timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    int64_t now = int64_t(ts.tv_sec) * 1'000'000'000 + ts.tv_nsec;
    if (t0_ < 0) t0_ = now;
    int64_t due = t0_ + int64_t(n_) * period_;
    if (due > now) std::this_thread::sleep_for(std::chrono::nanoseconds(due - now));
    for (unsigned y = 0; y < h_; y++) {
      uint16_t* row = pix_.data() + size_t(y) * w_;
      for (unsigned x = 0; x < w_; x++) {
        uint8_t v = y < h_ / 2 ? ((n_ >> (15 - x * 16 / w_)) & 1 ? 235 : 16) : uint8_t(x * 256 / w_ + 2 * n_);
        row[x] = uint16_t(v) << 2;
      }
    }
    f = V4l2Capture::Frame{n_, due, reinterpret_cast<const uint8_t*>(pix_.data()), pix_.size() * 2, 0};
    n_++;
  }
  size_t bytesperline() const { return w_ * 2; }
 private:
  unsigned w_, h_; int64_t period_, t0_ = -1; uint32_t n_ = 0; std::vector<uint16_t> pix_;
};

// The encoder fragment, the raw format it takes, its parser and its codec. Explicit choice; no probing.
// `gop` is in frames, so one keyframe a second at the branch's own frame rate.
struct Enc { std::string element, format, parser, caps; sei::Codec codec; };

bool encoder_spec(const std::string& name, unsigned gop, int bitrate, Enc& e) {
  const std::string g = std::to_string(gop), b = std::to_string(bitrate), kbit = std::to_string(bitrate / 1000);
  if (name == "v4l2h265enc" || name == "v4l2h264enc") {
    e.element = name + " extra-controls=\"controls,video_gop_size=" + g + ",video_bitrate=" + b + "\"";
    e.format = "NV12";
  } else if (name == "x265enc") {   // same shape as the Wave5: IPPP, one AU per frame, in order
    // open-gop=0: x265 otherwise makes every keyframe but the first a CRA, and a segment that opens on a
    // CRA is not what §7 asks of the ring. The Wave5 emits IDRs; the stand-in must too.
    e.element = "x265enc tune=zerolatency speed-preset=ultrafast option-string=open-gop=0 key-int-max=" + g + " bitrate=" + kbit + " log-level=none";
    e.format = "I420";              // luma + constant chroma: the same bytes as our NV12 fill
  } else if (name == "x264enc") {
    e.element = "x264enc tune=zerolatency speed-preset=ultrafast key-int-max=" + g + " bitrate=" + kbit;
    e.format = "I420";
  } else {
    fprintf(stderr, "unknown encoder %s (v4l2h265enc, x265enc, v4l2h264enc, x264enc)\n", name.c_str());
    return false;
  }
  const bool h264 = name.find("264") != std::string::npos;
  e.codec = h264 ? sei::Codec::H264 : sei::Codec::H265;
  e.parser = h264 ? "h264parse" : "h265parse";
  e.caps = h264 ? "video/x-h264" : "video/x-h265";
  return true;
}

// `name ! parser name=n config-interval=1 ! caps ! `
std::string parsed(const Enc& e, const char* n) {
  return " ! " + e.parser + " name=" + n + " config-interval=1 ! " + e.caps + ",stream-format=byte-stream,alignment=au ! ";
}

bool parse(int argc, char** argv, Opts& o) {
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i]; auto next = [&](){ return std::string(i + 1 < argc ? argv[++i] : ""); };
    if (a == "--device") o.device = next(); else if (a == "--out") o.out = next(); else if (a == "--log") { o.log = next(); o.log_given = true; }
    else if (a == "--ring") o.ring = next(); else if (a == "--segment-s") o.segment_s = std::stoul(next()); else if (a == "--budget") o.budget = std::stoull(next());
    else if (a == "--edges") o.edges = next(); else if (a == "--ptp") o.ptp = next(); else if (a == "--chan") o.chan = std::stoul(next());
    else if (a == "--width") o.width = std::stoul(next()); else if (a == "--height") o.height = std::stoul(next());
    else if (a == "--fps") o.fps = std::stoul(next()); else if (a == "--tcp") o.tcp = std::stoi(next());
    else if (a == "--frames") o.frames = std::stol(next()); else if (a == "--latency-ns") o.latency = std::stoll(next());
    else if (a == "--period-ns") o.period = std::stoll(next()); else if (a == "--bitrate") o.bitrate = std::stoi(next());
    else if (a == "--fake") o.fake = true; else if (a == "--enc") o.enc = next(); else if (a == "--proxy-out") o.proxy_out = next();
    else if (a == "--proxy-sock") o.proxy_sock = next(); else if (a == "--proxy-enc") o.proxy_enc = next();
    else if (a == "--proxy-scaler") o.proxy_scaler = next(); else if (a == "--proxy-width") o.proxy_width = std::stoul(next());
    else if (a == "--proxy-height") o.proxy_height = std::stoul(next()); else if (a == "--proxy-fps") o.proxy_fps = std::stoul(next());
    else if (a == "--proxy-bitrate") o.proxy_bitrate = std::stoi(next());
    else { fprintf(stderr, "unknown option %s\n", a.c_str()); return false; }
  }
  if (o.proxy() && (o.proxy_fps == 0 || o.proxy_fps > o.fps)) {
    fprintf(stderr, "--proxy-fps %u must be between 1 and --fps %u\n", o.proxy_fps, o.fps); return false;
  }
  return true;
}
}  // namespace

int main(int argc, char** argv) {
  Ctx c; if (!parse(argc, argv, c.o)) return 2;
  if (c.o.latency < 0) c.o.latency = c.o.period;
  const Opts& o = c.o;
  gst_init(nullptr, nullptr);
  signal(SIGINT, on_sigint); signal(SIGTERM, on_sigint); signal(SIGUSR1, on_sigusr1);
  signal(SIGPIPE, SIG_IGN);   // a live viewer that walks away must not take the recorder with it

  Enc rec_enc, proxy_enc;
  if (!encoder_spec(o.enc, o.fps, o.bitrate, rec_enc)) return 2;
  if (o.proxy()) {
    if (!encoder_spec(o.proxy_enc, o.proxy_fps, o.proxy_bitrate, proxy_enc)) return 2;
    if (proxy_enc.codec != sei::Codec::H264)
      fprintf(stderr, "kpipe: warning: the proxy substream is meant to be H.264 (Foxglove decodes through WebCodecs)\n");
    if (proxy_enc.format != rec_enc.format) {
      fprintf(stderr, "--proxy-enc %s takes %s but --enc %s produces %s; pick encoders that agree\n",
              o.proxy_enc.c_str(), proxy_enc.format.c_str(), o.enc.c_str(), rec_enc.format.c_str());
      return 2;
    }
  }
  // With --ring the AUs go to the segment writer from the recording branch's probe, so the pipeline
  // tail only has to consume them.
  std::string sink = o.ring.empty() ? "filesink location=" + o.out : std::string("fakesink sync=false");
  if (o.tcp) sink = "tee name=t ! queue ! " + sink + " t. ! queue leaky=downstream ! tcpserversink host=0.0.0.0 port=" + std::to_string(o.tcp) + " sync=false";
  char src[512];
  snprintf(src, sizeof src,
    "appsrc name=src is-live=true format=time block=true max-bytes=%u "
    "caps=video/x-raw,format=%s,width=%u,height=%u,framerate=%u/1,colorimetry=bt709,interlace-mode=progressive,pixel-aspect-ratio=1/1 ",
    o.width * o.height * 3 / 2 * 4, rec_enc.format.c_str(), o.width, o.height, o.fps);
  std::string desc = std::string(src) + "! tee name=raw ! queue max-size-buffers=3 ! " + rec_enc.element + " name=enc1" + parsed(rec_enc, "parse1") + sink;
  if (o.proxy()) {
    // The proxy tail is a fakesink: the finished AUs leave through Branch::on_au, into the socket and,
    // for the M4b test, a file. async=false because a sink behind a closed valve never prerolls and
    // would hold the whole pipeline — the recording branch included — in PAUSED.
    const std::string scale = o.proxy_scaler == "none" ? std::string()
        : o.proxy_scaler + " ! video/x-raw,width=" + std::to_string(o.proxy_width) + ",height=" + std::to_string(o.proxy_height) + " ! ";
    desc += " raw. ! valve name=pvalve drop=true ! queue leaky=downstream max-size-buffers=2 ! "
          + std::string("videorate drop-only=true ! video/x-raw,framerate=") + std::to_string(o.proxy_fps) + "/1 ! "
          + scale + proxy_enc.element + " name=enc2" + parsed(proxy_enc, "parse2") + "fakesink async=false sync=false";
  }
  if (getenv("KPIPE_SHOW_PIPELINE")) fprintf(stderr, "kpipe: %s\n", desc.c_str());
  GError* err = nullptr;
  GstElement* pipe = gst_parse_launch(desc.c_str(), &err);
  // A bad property or a missing element is only a warning to gst_parse_launch: it hands back a pipeline
  // with that part of the graph unlinked, which shows up much later as a branch that silently carries no
  // frames. Refuse to run on anything it complained about.
  if (!pipe || err) { fprintf(stderr, "pipeline: %s\n", err ? err->message : "could not be built"); return 1; }
  GstElement* appsrc = gst_bin_get_by_name(GST_BIN(pipe), "src");
  auto by_name = [&](const char* n) { return gst_bin_get_by_name(GST_BIN(pipe), n); };
  auto log_event = [&c](const std::string& s) { c.event(s); };
  std::unique_ptr<segfile::Writer> writer;
  if (!o.ring.empty()) {
    setvbuf(stdout, nullptr, _IOLBF, 0);
    if (!o.log_given) c.o.log.clear();                 // --ring alone: events go to stdout, for the agent
    segfile::Config sc; sc.ring = o.ring; sc.segment_frames = o.segment_s * o.fps; sc.budget_bytes = o.budget;
    sc.expected_bytes = uint64_t(o.bitrate) / 8 * o.segment_s;
    writer = std::make_unique<segfile::Writer>(sc, [&c](const std::string& l) { c.event(l); });
  }
  if (!o.log.empty()) { c.log.open(o.log); c.ev = &c.log; } else if (writer) c.ev = &std::cout;
  if (writer) {
    writer->start(); c.w = writer.get();
    c.rec.on_au = [&c](const uint8_t* au, size_t len, const KStamp& s, bool key) {
      c.w->on_au(au, len, segfile::Meta{s.epoch, s.k, s.t_frame_ns, s.faults, key});
    };
  }
  c.rec.attach(by_name("enc1"), by_name("parse1"), log_event);

  // ── the proxy branch: the socket, the optional file, the valve ───────────────────────────────────
  LiveSock live; std::ofstream proxy_file; GstElement* valve = nullptr; GstElement* parse2 = nullptr; bool proxy_open = false;
  Decimate decim{o.fps, o.proxy_fps, 0};
  if (o.proxy()) {
    if (!o.proxy_sock.empty()) {
      std::string e;
      if (!live.open(o.proxy_sock, e)) { fprintf(stderr, "kpipe: proxy socket: %s\n", e.c_str()); return 1; }
    }
    if (!o.proxy_out.empty()) proxy_file.open(o.proxy_out, std::ios::binary);
    c.proxy.on_au = [&live, &proxy_file](const uint8_t* au, size_t len, const KStamp&, bool key) {
      live.write(au, len, key);        // framed: the reader must not wait for the next access unit
      if (proxy_file.is_open()) proxy_file.write(reinterpret_cast<const char*>(au), long(len));  // plain Annex-B
    };
    parse2 = by_name("parse2");
    c.proxy.attach(by_name("enc2"), parse2, log_event);
    valve = by_name("pvalve");
    GstPad* vout = gst_element_get_static_pad(valve, "src");
    gst_pad_add_probe(vout, GST_PAD_PROBE_TYPE_BUFFER, decimate_probe, &decim, nullptr);
    gst_object_unref(vout);
  }
  gst_element_set_state(pipe, GST_STATE_PLAYING);
  if (valve)
    c.event("{\"ev\":\"proxy\",\"state\":\"ready\",\"sock\":\"" + o.proxy_sock + "\",\"codec\":\"h264\",\"width\":" + std::to_string(o.proxy_width)
            + ",\"height\":" + std::to_string(o.proxy_height) + ",\"fps\":" + std::to_string(o.proxy_fps)
            + ",\"bitrate_bps\":" + std::to_string(o.proxy_bitrate) + "}");

  // edges: a grid anchored on the first frame (free-running bench) or captured CPTS edges
  std::mutex edge_lock;
  kmatch::EdgeConfig ec; ec.period = o.period;
  ec.retain = 4'000'000'000;   // a beacon names a pulse up to ~2 s old (source writes 1/s, agent polls 1/s)
  kmatch::CapturedEdges captured(ec, [&](const kmatch::Event& e) {
    c.event("{\"ev\":\"" + std::string(e.kind) + "\",\"t\":" + std::to_string(e.t) + ",\"epoch\":" + std::to_string(e.epoch) + ",\"k\":" + std::to_string(e.k) + ",\"value\":" + std::to_string(e.value) + "}");
  });
  std::unique_ptr<kmatch::GridEdges> grid;
  std::unique_ptr<PtpEdgeFeed> feed;
  const kmatch::EdgeSource* source = &captured;
  if (o.edges == "ptp") { feed = std::make_unique<PtpEdgeFeed>(o.ptp, o.chan, captured, edge_lock); feed->start(); }
  StdinLines control; bool said_no_beacon = false;
  control.start([&](const std::string& in) {
    std::string line = in;
    while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
    if (line.empty()) return;
    if (line == "proxy on") { g_proxy_want = 1; return; }
    if (line == "proxy off") { g_proxy_want = 0; return; }
    if (line == "stop") { g_stop = true; return; }
    long long e = 0, k = 0, t = 0;
    if (sscanf(line.c_str(), "beacon %lld %lld %lld", &e, &k, &t) != 3) {
      fprintf(stderr, "kpipe: unknown command %s\n", line.c_str());
      return;
    }
    if (!feed) {                                   // a grid has no captured edge to check the beacon against
      if (!said_no_beacon) { said_no_beacon = true; c.event("{\"ev\":\"beacon_ignored\",\"reason\":\"edges=grid\"}"); }
      return;
    }
    std::lock_guard<std::mutex> g(edge_lock);
    captured.beacon(kmatch::Beacon{uint32_t(e), int64_t(k), t - feed->offset_ns()});
  });
  kmatch::MatchConfig mc; mc.period = o.period; mc.latency = o.latency;
  std::unique_ptr<kmatch::Matcher> matcher;

  std::unique_ptr<V4l2Capture> cap; std::unique_ptr<FakeSource> fake;
  if (o.fake) fake = std::make_unique<FakeSource>(o.width, o.height, o.period);
  else { cap = std::make_unique<V4l2Capture>(o.device, o.width, o.height, (uint32_t('B') | uint32_t('G') << 8 | uint32_t('1') << 16 | uint32_t('0') << 24)); cap->start(); }
  const size_t stride = fake ? fake->bytesperline() : cap->bytesperline();
  fprintf(stderr, "kpipe: %ux%u @ %u fps, %s, %s, edges=%s, latency %lld ns -> %s%s%s\n", o.width, o.height, o.fps, fake ? "fake source" : o.device.c_str(),
          o.enc.c_str(), o.edges.c_str(), (long long)o.latency, o.out.c_str(), valve ? ", proxy -> " : "",
          valve ? (o.proxy_sock.empty() ? o.proxy_out.c_str() : o.proxy_sock.c_str()) : "");

  GstBus* bus = gst_element_get_bus(pipe);
  int64_t first_ts = -1; auto t_stat = std::chrono::steady_clock::now(); long n_stat = 0;
  long out_prev = 0, edges_prev = 0, faults_1s = 0, faults_total = 0, proxy_prev = 0; uint64_t bytes_prev = 0, proxy_bytes_prev = 0;
  V4l2Capture::Frame f;
  while (!g_stop && (o.frames == 0 || c.in < o.frames)) {
    int want = g_proxy_want.exchange(-1);
    if (g_toggle.exchange(false)) want = proxy_open ? 0 : 1;
    if (valve && want >= 0 && bool(want) != proxy_open) {
      proxy_open = want != 0;
      if (proxy_open) decim.reset();   // still dropping, so no buffer is inside the probe: no race
      g_object_set(valve, "drop", !proxy_open, nullptr);
      if (proxy_open) {
        // Ask for an IDR now, so the first viewer does not wait out a whole GOP. Upstream, from the
        // parser's src pad: an upstream event is not serialized and takes no stream lock, so this can
        // never block the capture loop behind a busy proxy encoder. The recording branch never sees it.
        GstPad* p = gst_element_get_static_pad(parse2, "src");
        gst_pad_send_event(p, gst_video_event_new_upstream_force_key_unit(GST_CLOCK_TIME_NONE, TRUE, 0));
        gst_object_unref(p);
      }
      fprintf(stderr, "proxy valve %s\n", proxy_open ? "open" : "closed");
      c.event("{\"ev\":\"proxy\",\"state\":\"" + std::string(proxy_open ? "open" : "closed") + "\",\"readers\":" + std::to_string(live.clients()) + "}");
    }
    if (GstMessage* m = gst_bus_pop_filtered(bus, GST_MESSAGE_ERROR)) { GError* e; gst_message_parse_error(m, &e, nullptr); fprintf(stderr, "gst: %s\n", e->message); break; }
    if (fake) fake->next(f);
    else if (!cap->dequeue(f, 500)) continue;
    if (first_ts < 0) {
      first_ts = f.ts_ns;
      if (o.edges != "ptp") { grid = std::make_unique<kmatch::GridEdges>(f.ts_ns - o.latency, o.period, 1); source = grid.get(); }
      matcher = std::make_unique<kmatch::Matcher>(mc, *source);
    }
    kmatch::FrameOut r; bool anchored = false;
    { std::lock_guard<std::mutex> g(edge_lock); r = matcher->frame(kmatch::FrameIn{f.ts_ns, f.seq, 0, false}); anchored = source->anchored(); }
    if (r.k >= 0 && !anchored) r.faults |= kmatch::FAULT_K_LOCAL;   // K is ours, not the source's
    GstBuffer* buf = gst_buffer_new_allocate(nullptr, size_t(o.width) * o.height * 3 / 2, nullptr);
    GstMapInfo m; gst_buffer_map(buf, &m, GST_MAP_WRITE);
    to_nv12_luma(f.data, stride, o.width, o.height, m.data);
    gst_buffer_unmap(buf, &m);
    if (cap) cap->release(f);
    GST_BUFFER_PTS(buf) = GstClockTime(f.ts_ns - first_ts); GST_BUFFER_DURATION(buf) = GST_SECOND / o.fps; GST_BUFFER_OFFSET(buf) = f.seq;
    const KStamp s{r.epoch, r.k, f.seq, f.ts_ns, r.faults};
    kmeta_add(buf, s);
    c.event("{\"ev\":\"frame\",\"epoch\":" + std::to_string(s.epoch) + ",\"k\":" + std::to_string(s.k) + ",\"seq\":" + std::to_string(s.seq) + ",\"s\":" + std::to_string(s.seq & 0xffff)
            + ",\"ts_ns\":" + std::to_string(s.t_frame_ns) + ",\"flags\":" + std::to_string(s.faults) + "}");
    if (gst_app_src_push_buffer(GST_APP_SRC(appsrc), buf) != GST_FLOW_OK) { fprintf(stderr, "push failed\n"); break; }
    // K_LOCAL states where the numbering came from; it is not a fault and must not swamp the counters.
    const uint32_t err = r.faults & ~uint32_t(kmatch::FAULT_K_LOCAL);
    c.in++; n_stat++; if (err) { faults_1s++; faults_total++; }
    auto now = std::chrono::steady_clock::now();
    if (now - t_stat >= std::chrono::seconds(1)) {
      const std::string proxy = valve ? ", proxy " + std::to_string(c.proxy.out) + (proxy_open ? " out (open, " : " out (closed, ")
                                        + std::to_string(live.clients()) + " readers)" : "";
      fprintf(stderr, "%ld fps in, %ld out%s, %sK=%lld%s%s, edges seen %ld\n", n_stat, c.rec.out, proxy.c_str(), anchored ? "" : "local ",
              (long long)r.k, err ? " faults=" : "", err ? kmatch::fault_names(err).c_str() : "", feed ? feed->events() : 0L);
      if (writer) {
        auto st = writer->stats();
        c.event("{\"ev\":\"status\",\"state\":\"running\",\"fps_in\":" + std::to_string(n_stat) + ",\"fps_out\":" + std::to_string(c.rec.out - out_prev)
                + ",\"epoch\":" + std::to_string(r.epoch) + ",\"k\":" + std::to_string(r.k) + ",\"faults_1s\":" + std::to_string(faults_1s)
                + ",\"faults_total\":" + std::to_string(faults_total) + ",\"anchored\":" + (anchored ? "true" : "false") + ",\"edges_1s\":" + std::to_string((feed ? feed->events() : 0L) - edges_prev)
                + ",\"bytes_1s\":" + std::to_string(st.bytes_total - bytes_prev) + ",\"open_segno\":" + std::to_string(st.open_segno)
                + ",\"pre_idr_dropped\":" + std::to_string(st.pre_idr_dropped) + ",\"dropped\":" + std::to_string(st.dropped)
                + ",\"proxy_open\":" + (proxy_open ? "true" : "false") + ",\"proxy_fps_out\":" + std::to_string(c.proxy.out - proxy_prev)
                + ",\"proxy_bytes_1s\":" + std::to_string(c.proxy.bytes - proxy_bytes_prev) + ",\"proxy_readers\":" + std::to_string(live.clients()) + "}");
        bytes_prev = st.bytes_total; out_prev = c.rec.out; edges_prev = feed ? feed->events() : 0L; faults_1s = 0;
        proxy_prev = c.proxy.out; proxy_bytes_prev = c.proxy.bytes;
      }
      t_stat = now; n_stat = 0;
    }
    if (writer && writer->failed()) { fprintf(stderr, "kpipe: storage error, stopping\n"); break; }
  }
  // ── shutdown ─────────────────────────────────────────────────────────────────────────────────────
  // StopRecording gives the recorder three seconds before SIGKILL, so everything durable has to be on
  // disk inside that: the open segment is closed before the pipeline is torn down, and nothing here
  // waits on the proxy branch.
  //
  // The pipeline's own EOS is not usable. It is posted only once every sink has seen it, and the proxy
  // branch on the Wave5 does not always deliver: a closed valve drops every event, sticky ones included,
  // and repushes them only when its next buffer arrives, which after EOS never comes — and pushing EOS
  // past the valve by hand does not make the board's H.264 instance drain either. So kpipe waits for the
  // *recording* parser to pass EOS, which is when every access unit has been through the SEI probe and
  // on_au, and gives up on that after two seconds rather than four.
  const auto step = [&](const char* what) { if (getenv("KPIPE_TRACE_SHUTDOWN")) fprintf(stderr, "shutdown: %s\n", what); };
  step("capture stop");
  if (cap) cap->stop();
  if (valve) {                                  // give the proxy its chance to drain; it may not take it
    step("proxy eos");
    g_object_set(valve, "drop", FALSE, nullptr);
    GstPad* p = gst_element_get_static_pad(valve, "src");
    gst_pad_push_event(p, gst_event_new_eos());
    gst_object_unref(p);
  }
  step("appsrc eos");
  auto t_eos = std::chrono::steady_clock::now();
  gst_app_src_end_of_stream(GST_APP_SRC(appsrc));
  bool bus_error = false;
  while (!c.rec.saw_eos && std::chrono::steady_clock::now() - t_eos < std::chrono::seconds(2)) {
    if (GstMessage* m = gst_bus_timed_pop_filtered(bus, 50 * GST_MSECOND, GST_MESSAGE_ERROR)) {
      GError* e; gst_message_parse_error(m, &e, nullptr); fprintf(stderr, "gst: %s\n", e->message);
      gst_message_unref(m); bus_error = true; break;
    }
  }
  const double eos_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_eos).count();
  if (c.rec.saw_eos) fprintf(stderr, "recording drained after %.2f s%s\n", eos_s, !valve ? "" : (c.proxy.saw_eos ? ", proxy too" : ", proxy did not"));
  else fprintf(stderr, "no EOS on the recording branch within 2 s; going by the counters instead\n");

  step("close the ring");
  if (feed) feed->stop();
  live.close();
  if (writer) { writer->stop(); c.event("{\"ev\":\"status\",\"state\":\"idle\"}"); }
  if (c.log.is_open()) c.log.flush();
  // What makes a run good is the data, not the event: every frame pushed came back as an access unit,
  // nothing is left in the recording branch's bridge table, and the proxy's bridge never missed. EOS is
  // only how kpipe knows it can stop waiting, and the Wave5 does not always send it (see above).
  const bool ok = !bus_error && c.rec.out == c.in && c.rec.clean() && (!valve || c.proxy.sane()) && !(writer && writer->failed());
  const int rc = ok ? 0 : (writer && writer->failed() ? 3 : 1);
  fprintf(stderr, "done: %ld frames in; %s%s%s; %s\n", c.in, c.rec.summary().c_str(), valve ? "; " : "", valve ? c.proxy.summary().c_str() : "", ok ? "OK" : "FAIL");
  fflush(nullptr);

  // Everything durable is written. The teardown can still block on an encoder instance that will not
  // drain, and there is nothing left to lose by not waiting for it.
  step("pipeline to NULL");
  std::thread([rc]() {
    std::this_thread::sleep_for(std::chrono::milliseconds(1500));
    fprintf(stderr, "the pipeline did not tear down in 1.5 s; exiting anyway\n");
    fflush(nullptr);
    _exit(rc);
  }).detach();
  gst_element_set_state(pipe, GST_STATE_NULL);
  step("done");
  return ok ? 0 : (writer && writer->failed() ? 3 : 1);
}
