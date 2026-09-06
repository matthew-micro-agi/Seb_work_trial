#include "kmeta.hpp"
#include <algorithm>
#include "sei/sei.hpp"

GType kmeta_api_type() {
  static GType type = 0;
  static const gchar* tags[] = {nullptr};   // untagged: GstVideoEncoder copies it to the output frame
  if (g_once_init_enter(&type)) { GType t = gst_meta_api_type_register("KMetaAPI", tags); g_once_init_leave(&type, t); }
  return type;
}

static gboolean kmeta_init(GstMeta* m, gpointer, GstBuffer*) { static_cast<KMeta*>(static_cast<void*>(m))->stamp = KStamp{}; return TRUE; }

static gboolean kmeta_transform(GstBuffer* dest, GstMeta* m, GstBuffer*, GQuark type, gpointer) {
  if (!GST_META_TRANSFORM_IS_COPY(type)) return FALSE;
  kmeta_add(dest, static_cast<KMeta*>(static_cast<void*>(m))->stamp);
  return TRUE;
}

static const GstMetaInfo* kmeta_info() {
  static const GstMetaInfo* info = nullptr;
  if (g_once_init_enter(&info)) {
    const GstMetaInfo* i = gst_meta_register(kmeta_api_type(), "KMeta", sizeof(KMeta), kmeta_init, nullptr, kmeta_transform);
    g_once_init_leave(&info, i);
  }
  return info;
}

KMeta* kmeta_add(GstBuffer* buf, const KStamp& s) {
  auto* m = static_cast<KMeta*>(static_cast<void*>(gst_buffer_add_meta(buf, kmeta_info(), nullptr)));
  m->stamp = s;
  return m;
}

const KStamp* kmeta_get(GstBuffer* buf) {
  GstMeta* m = gst_buffer_get_meta(buf, kmeta_api_type());
  return m ? &static_cast<KMeta*>(static_cast<void*>(m))->stamp : nullptr;
}

// Encoder sink pad: copy the stamp into the table. The raw buffer is shared after the tee; read only.
static GstPadProbeReturn sink_probe(GstPad*, GstPadProbeInfo* info, gpointer user) {
  auto* b = static_cast<Branch*>(user);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  const KStamp* s = buf ? kmeta_get(buf) : nullptr;
  if (!s) return GST_PAD_PROBE_OK;
  std::lock_guard<std::mutex> g(b->lock);
  b->table[GST_BUFFER_PTS(buf)] = *s; b->in++;
  while (b->table.size() > Branch::kDepth) { b->table.erase(b->table.begin()); b->evicted++; }
  b->max_depth = std::max(b->max_depth, b->table.size());
  return GST_PAD_PROBE_OK;
}

// Parser src pad (one access unit per buffer): pop the stamp by PTS, insert the SEI.
static GstPadProbeReturn src_probe(GstPad*, GstPadProbeInfo* info, gpointer user) {
  auto* b = static_cast<Branch*>(user);
  GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
  if (!buf) return GST_PAD_PROBE_OK;
  const GstClockTime pts = GST_BUFFER_PTS(buf);
  KStamp s{0, -1, 0, 0, FAULT_STAMP_LOST}; bool found = false; std::string ev;
  { std::lock_guard<std::mutex> g(b->lock);
    if (!b->have_offset && !b->table.empty()) {   // the first output is the oldest frame inside the encoder
      b->pts_offset = int64_t(pts) - int64_t(b->table.begin()->first); b->have_offset = true;
      ev = "{\"ev\":\"enc_pts_offset\",\"br\":\"" + std::string(b->name) + "\",\"value\":" + std::to_string(b->pts_offset) + "}";
    }
    const GstClockTime key = GstClockTime(int64_t(pts) - b->pts_offset);
    auto it = b->table.find(key);
    if (it != b->table.end()) { s = it->second; found = true; b->table.erase(it); }
    if (b->out > 0 && key < b->last_key) b->reordered++;
    b->last_key = key;
    if (!found) { b->miss++; ev = "{\"ev\":\"stamp_lost\",\"br\":\"" + std::string(b->name) + "\",\"pts\":" + std::to_string(key) + "}"; }
    if (const KStamp* m = kmeta_get(buf)) { b->meta_seen++; if (found && m->k == s.k && m->seq == s.seq) b->meta_agrees++; }
    b->out++;
  }
  if (!ev.empty() && b->log) b->log(ev);
  sei::Record r{uint16_t(s.epoch), uint32_t(s.k), uint64_t(s.t_frame_ns), uint16_t(s.seq & 0xffff), uint16_t(s.faults & 0xffff)};
  GstMapInfo m; gst_buffer_map(buf, &m, GST_MAP_READ);
  auto au = sei::insert(m.data, m.size, sei::build_nal(r, b->codec), b->codec);
  gst_buffer_unmap(buf, &m);
  { std::lock_guard<std::mutex> g(b->lock); b->bytes += au.size(); }
  if (b->on_au) b->on_au(au.data(), au.size(), s, !GST_BUFFER_FLAG_IS_SET(buf, GST_BUFFER_FLAG_DELTA_UNIT));
  GstBuffer* nb = gst_buffer_new_allocate(nullptr, au.size(), nullptr);
  gst_buffer_fill(nb, 0, au.data(), au.size());
  gst_buffer_copy_into(nb, buf, GstBufferCopyFlags(GST_BUFFER_COPY_FLAGS | GST_BUFFER_COPY_TIMESTAMPS), 0, 0);
  gst_buffer_unref(buf);
  GST_PAD_PROBE_INFO_DATA(info) = nb;
  return GST_PAD_PROBE_OK;
}

// Parser src pad, events: note EOS, so the caller can wait for this branch and no other.
static GstPadProbeReturn eos_probe(GstPad*, GstPadProbeInfo* info, gpointer user) {
  GstEvent* e = GST_PAD_PROBE_INFO_EVENT(info);
  if (e && GST_EVENT_TYPE(e) == GST_EVENT_EOS) static_cast<Branch*>(user)->saw_eos = true;
  return GST_PAD_PROBE_OK;
}

void Branch::attach(GstElement* encoder, GstElement* parser, std::function<void(const std::string&)> log_event) {
  log = std::move(log_event);
  GstPad* in = gst_element_get_static_pad(encoder, "sink");
  gst_pad_add_probe(in, GST_PAD_PROBE_TYPE_BUFFER, sink_probe, this, nullptr);
  gst_object_unref(in);
  GstPad* out = gst_element_get_static_pad(parser, "src");
  gst_pad_add_probe(out, GST_PAD_PROBE_TYPE_BUFFER, src_probe, this, nullptr);
  gst_pad_add_probe(out, GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM, eos_probe, this, nullptr);
  gst_object_unref(out);
}

std::string Branch::summary() const {
  char buf[256];
  snprintf(buf, sizeof buf, "%s: %ld in, %ld out, %ld miss, %ld evicted, %ld reordered, depth<=%zu, %zu left, meta %ld/%ld agree %ld, pts offset %lld ns",
           name, in, out, miss, evicted, reordered, max_depth, table.size(), meta_seen, out, meta_agrees, (long long)pts_offset);
  return buf;
}
