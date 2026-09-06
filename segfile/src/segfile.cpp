#include "segfile/segfile.hpp"
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fstream>
#include <set>
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <time.h>
#include <unistd.h>
#include "sei/sei.hpp"

namespace segfile {
namespace {
int64_t mono_now() { timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return int64_t(t.tv_sec) * 1000000000LL + t.tv_nsec; }
int64_t us_since(int64_t t0) { return (mono_now() - t0) / 1000; }

std::vector<std::string> ls(const std::string& dir) {
  std::vector<std::string> names; DIR* d = opendir(dir.c_str()); if (!d) return names;
  while (dirent* e = readdir(d)) { std::string n = e->d_name; if (n != "." && n != "..") names.push_back(n); }
  closedir(d); return names;
}
bool parse_segno(const std::string& name, uint64_t& out) {
  if (name.size() < 17) return false;
  for (size_t i = 0; i < 12; i++) if (name[i] < '0' || name[i] > '9') return false;
  if (name.compare(12, std::string::npos, ".h265") != 0 && name.compare(12, std::string::npos, ".h265.part") != 0) return false;
  out = std::stoull(name.substr(0, 12)); return true;
}
uint64_t max_segno_in(const std::string& dir) {
  uint64_t m = 0, s; for (const auto& n : ls(dir)) if (parse_segno(n, s)) m = std::max(m, s); return m;
}
std::vector<uint8_t> read_file(const std::string& path) {
  std::ifstream f(path, std::ios::binary); return {std::istreambuf_iterator<char>(f), {}};
}
bool is_mount_point(const std::string& dir) {
  struct stat a, b; if (stat(dir.c_str(), &a) != 0 || stat((dir + "/..").c_str(), &b) != 0) return false;
  return a.st_dev != b.st_dev || a.st_ino == b.st_ino;
}
void walk(const std::string& dir, std::set<std::pair<dev_t, ino_t>>& seen, uint64_t& sum) {
  for (const auto& n : ls(dir)) {
    std::string p = dir + "/" + n; struct stat st; if (lstat(p.c_str(), &st) != 0) continue;
    if (S_ISDIR(st.st_mode)) { walk(p, seen, sum); continue; }
    if (seen.insert({st.st_dev, st.st_ino}).second) sum += uint64_t(st.st_blocks) * 512;
  }
}
void fsync_dir(const std::string& dir) { int d = open(dir.c_str(), O_RDONLY | O_DIRECTORY); if (d >= 0) { fsync(d); close(d); } }
}  // namespace

std::string seg_name(uint64_t segno) { char b[32]; snprintf(b, sizeof b, "%012llu.h265", (unsigned long long)segno); return b; }

std::vector<uint64_t> list_segnos(const std::string& dir) {
  std::vector<uint64_t> v; uint64_t s;
  for (const auto& n : ls(dir)) if (n.size() == 17 && parse_segno(n, s)) v.push_back(s);
  std::sort(v.begin(), v.end()); return v;
}

uint64_t next_segno(const std::string& ring) {
  uint64_t m = max_segno_in(ring + "/seg");
  for (const auto& l : ls(ring + "/lock")) m = std::max(m, max_segno_in(ring + "/lock/" + l));
  return m + 1;
}

uint64_t used_bytes(const std::string& ring) {
  if (is_mount_point(ring)) { struct statvfs v; if (statvfs(ring.c_str(), &v) == 0) return uint64_t(v.f_blocks - v.f_bfree) * v.f_frsize; }
  std::set<std::pair<dev_t, ino_t>> seen; uint64_t sum = 0; walk(ring, seen, sum); return sum;
}

std::vector<Repaired> repair_parts(const std::string& ring) {
  std::vector<Repaired> out; std::string seg = ring + "/seg"; uint64_t s;
  for (const auto& n : ls(seg)) {
    if (n.size() != 22 || !parse_segno(n, s)) continue;
    std::string path = seg + "/" + n; auto data = read_file(path);
    size_t end = sei::last_complete_au_end(data.data(), data.size()); bool keep = false; size_t frames = 0;
    if (end > 0) {
      auto aus = sei::split(data.data(), end); auto head = sei::inspect(data.data(), aus[0]);
      keep = head.idr && head.has_vps; frames = aus.size();
    }
    if (keep) {
      if (truncate(path.c_str(), off_t(end)) == 0) { int fd = open(path.c_str(), O_RDONLY); if (fd >= 0) { fdatasync(fd); close(fd); } rename(path.c_str(), (seg + "/" + seg_name(s)).c_str()); }
    } else unlink(path.c_str());
    out.push_back({s, frames, keep});
  }
  fsync_dir(seg);
  std::sort(out.begin(), out.end(), [](const Repaired& a, const Repaired& b) { return a.segno < b.segno; });
  return out;
}

Writer::Writer(Config cfg, EventSink sink) : cfg_(std::move(cfg)), sink_(std::move(sink)), seg_dir_(cfg_.ring + "/seg") {}
Writer::~Writer() { stop(); }

void Writer::start() {
  mkdir(cfg_.ring.c_str(), 0755); mkdir(seg_dir_.c_str(), 0755);
  for (const auto& r : repair_parts(cfg_.ring))
    if (r.kept) sink_("{\"ev\":\"segment_closed\",\"segno\":" + std::to_string(r.segno) + ",\"partial\":true,\"frames\":" + std::to_string(r.frames) + "}");
    else sink_("{\"ev\":\"part_discarded\",\"segno\":" + std::to_string(r.segno) + "}");
  segno_ = next_segno(cfg_.ring) - 1;
  for (uint64_t s : list_segnos(seg_dir_)) victims_.push_back(s);
  stopping_ = false; th_ = std::thread(&Writer::run, this);
}

void Writer::on_au(const uint8_t* data, size_t len, const Meta& m) {
  std::lock_guard<std::mutex> g(mu_);
  if (q_.size() >= cfg_.queue_max || failed_) { dropped_++; return; }
  q_.push_back(Item{std::vector<uint8_t>(data, data + len), m, mono_now()}); cv_.notify_one();
}

void Writer::stop() {
  { std::lock_guard<std::mutex> g(mu_); if (!th_.joinable()) return; stopping_ = true; cv_.notify_one(); }
  th_.join();
  if (open_) close_segment();
}

void Writer::run() {
  for (;;) {
    Item it;
    { std::unique_lock<std::mutex> lk(mu_);
      cv_.wait_for(lk, std::chrono::seconds(1), [&] { return !q_.empty() || stopping_; });
      if (q_.empty()) { if (stopping_) return; lk.unlock(); if (open_ && mono_now() - last_arrival_ >= cfg_.idle_close_ns) close_segment(); continue; }
      it = std::move(q_.front()); q_.pop_front(); }
    last_arrival_ = it.t_arrival;
    handle(it);
  }
}

void Writer::handle(const Item& it) {
  if (failed_) return;
  if (open_ && it.m.idr && (frames_ >= cfg_.segment_frames || it.m.epoch != epoch_)) close_segment();
  if (!open_) {
    if (!it.m.idr) { if (wrote_any_) dropped_++; else pre_idr_dropped_++; return; }
    if (!reclaim_until_fits(cfg_.expected_bytes)) { dropped_++; return; }
    open_next(); if (failed_) return;
  }
  size_t off = 0;
  while (off < it.au.size()) {
    ssize_t n = write(fd_, it.au.data() + off, it.au.size() - off);
    if (n < 0) { if (errno == EINTR) continue; fail(std::string("write: ") + strerror(errno)); return; }
    off += size_t(n);
  }
  if (frames_ == 0) epoch_ = it.m.epoch;   // the segment's epoch is its first AU's; a change closes it at the next IDR
  frames_++; bytes_total_ += it.au.size(); wrote_any_ = true;
}

bool Writer::reclaim_until_fits(uint64_t need) {
  if (cfg_.budget_bytes == 0) return true;
  while (used_bytes(cfg_.ring) + need > cfg_.budget_bytes) {
    if (victims_.size() <= 1) { if (!full_) sink_("{\"ev\":\"storage_full\"}"); full_ = true; return false; }   // the newest closed segment stays: the tree keeps its highest segno
    uint64_t v = victims_.front(); victims_.pop_front(); int64_t t0 = mono_now();
    unlink((seg_dir_ + "/" + seg_name(v)).c_str());
    sink_("{\"ev\":\"reclaimed\",\"segno\":" + std::to_string(v) + ",\"us\":" + std::to_string(us_since(t0)) + "}");
  }
  full_ = false; return true;
}

void Writer::open_next() {
  segno_++;
  std::string path = seg_dir_ + "/" + seg_name(segno_) + ".part";
  fd_ = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0644);
  if (fd_ < 0) { fail("open " + path + ": " + strerror(errno)); return; }
  open_ = true; frames_ = 0; open_segno_ = segno_;
}

void Writer::close_segment() {
  int64_t t0 = mono_now();
  bool ok = fdatasync(fd_) == 0; close(fd_); fd_ = -1; open_ = false;
  std::string part = seg_dir_ + "/" + seg_name(segno_) + ".part", done = seg_dir_ + "/" + seg_name(segno_);
  if (!ok || rename(part.c_str(), done.c_str()) != 0) { fail("close " + done + ": " + strerror(errno)); return; }
  fsync_dir(seg_dir_);
  victims_.push_back(segno_); closed_++; open_segno_ = 0;
  sink_("{\"ev\":\"segment_closed\",\"segno\":" + std::to_string(segno_) + ",\"partial\":false,\"frames\":" + std::to_string(frames_) + ",\"fsync_us\":" + std::to_string(us_since(t0)) + "}");
}

void Writer::fail(const std::string& what) {
  failed_ = true; if (fd_ >= 0) { close(fd_); fd_ = -1; } open_ = false;
  std::string w; for (char c : what) { if (c == '"' || c == '\\') w += '\\'; w += c; }
  sink_("{\"ev\":\"storage_error\",\"error\":\"" + w + "\"}");
}

}  // namespace segfile
