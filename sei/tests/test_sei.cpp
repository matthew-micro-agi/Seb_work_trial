// The C++ sei against sei/tests/vectors.txt (path as argv[1]), plus the splitter's offsets.
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include "sei/sei.hpp"

static int fails = 0;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static std::vector<uint8_t> unhex(const std::string& h) {
  std::vector<uint8_t> v; for (size_t i = 0; i + 1 < h.size(); i += 2) v.push_back(uint8_t(std::stoul(h.substr(i, 2), nullptr, 16))); return v;
}
static std::string hex(const std::vector<uint8_t>& v) {
  std::string s; char b[3]; for (uint8_t x : v) { snprintf(b, 3, "%02x", x); s += b; } return s;
}
static std::vector<uint8_t> nal_bytes(const std::vector<uint8_t>& d, const sei::Nal& n) { return {d.begin() + long(n.offset), d.begin() + long(n.offset + n.size)}; }

static void check_split(const std::vector<uint8_t>& stream, const std::vector<std::vector<std::string>>& aus, sei::Codec c) {
  auto parts = sei::split(stream.data(), stream.size(), c);
  CHECK(parts.size() == aus.size());
  if (parts.size() != aus.size()) return;
  CHECK(parts.front().begin == 0); CHECK(parts.back().end == stream.size());
  for (size_t a = 0; a < parts.size(); a++) {
    CHECK(parts[a].nals.size() == aus[a].size());
    for (size_t n = 0; n < parts[a].nals.size() && n < aus[a].size(); n++) CHECK(hex(nal_bytes(stream, parts[a].nals[n])) == aus[a][n]);
    if (a + 1 < parts.size()) CHECK(parts[a].end == parts[a + 1].begin);
    // each range re-splits to exactly its own NALs
    auto sub = sei::split(stream.data() + parts[a].begin, parts[a].end - parts[a].begin, c);
    CHECK(sub.size() == 1 && sub[0].nals.size() == parts[a].nals.size());
  }
  if (parts.size() >= 2) CHECK(sei::last_complete_au_end(stream.data(), stream.size(), c) == parts[parts.size() - 2].end);
  CHECK(sei::last_complete_au_end(stream.data(), parts[0].end, c) == 0);
}

int main(int argc, char** argv) {
  if (argc < 2) { fprintf(stderr, "usage: test_sei vectors.txt\n"); return 2; }
  std::ifstream f(argv[1]); std::string line; int records = 0, streams = 0;
  std::vector<uint8_t> stream; std::vector<std::vector<std::string>> aus; bool have_stream = false;
  sei::Codec codec = sei::Codec::H265; int codecs = 0;
  while (std::getline(f, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream in(line); std::string tag; in >> tag;
    if (tag == "C") {
      std::string name; in >> name;
      if (have_stream) { check_split(stream, aus, codec); have_stream = false; }
      codec = name == "h264" ? sei::Codec::H264 : sei::Codec::H265; codecs++;
    } else if (tag == "R") {
      unsigned long long e, k, ts, sc, fl; std::string h; in >> e >> k >> ts >> sc >> fl >> h;
      sei::Record r{uint16_t(e), uint32_t(k), ts, uint16_t(sc), uint16_t(fl)};
      auto nal = sei::build_nal(r, codec); CHECK(hex(nal) == h);
      auto back = sei::parse_nal(nal.data() + 4, nal.size() - 4, codec);
      CHECK(back && back->epoch == r.epoch && back->k == r.k && back->frame_ts_ns == r.frame_ts_ns && back->sensor_count == r.sensor_count && back->flags == r.flags);
      for (size_t i = 4; i + 2 < nal.size(); i++) CHECK(!(nal[i] == 0 && nal[i + 1] == 0 && nal[i + 2] <= 1));
      records++;
    } else if (tag == "S") {
      if (have_stream) check_split(stream, aus, codec);
      std::string h; in >> h; stream = unhex(h); aus.clear(); have_stream = true; streams++;
    } else if (tag == "A") {
      std::vector<std::string> nals; std::string n; while (in >> n) nals.push_back(n); aus.push_back(nals);
    } else if (tag == "I") {
      unsigned idx; unsigned long long e, k, ts, sc, fl; std::string sh, oh; in >> idx >> e >> k >> ts >> sc >> fl >> sh >> oh;
      auto s = unhex(sh); auto parts = sei::split(s.data(), s.size(), codec);
      CHECK(idx < parts.size());
      auto nal = sei::build_nal(sei::Record{uint16_t(e), uint32_t(k), ts, uint16_t(sc), uint16_t(fl)}, codec);
      auto out = sei::insert(s.data() + parts[idx].begin, parts[idx].end - parts[idx].begin, nal, codec);
      CHECK(hex(out) == oh);
      auto again = sei::split(out.data(), out.size(), codec); CHECK(again.size() == 1);
      auto info = sei::inspect(out.data(), again[0], codec);
      CHECK(info.idr && info.has_vps && info.record && info.record->k == k);
    }
  }
  if (have_stream) check_split(stream, aus, codec);
  CHECK(records >= 4 && streams >= 2 && codecs == 2);
  // a NAL parsed as SEI that is not ours, and one codec's SEI never parses as the other's
  uint8_t other[] = {39 << 1, 1, 0x01, 0x02, 0xaa, 0xbb, 0x80}; CHECK(!sei::parse_nal(other, sizeof other));
  uint8_t other4[] = {6, 0x01, 0x02, 0xaa, 0xbb, 0x80}; CHECK(!sei::parse_nal(other4, sizeof other4, sei::Codec::H264));
  auto n5 = sei::build_nal(sei::Record{1, 2, 3, 4, 5});
  auto n4 = sei::build_nal(sei::Record{1, 2, 3, 4, 5}, sei::Codec::H264);
  CHECK(!sei::parse_nal(n5.data() + 4, n5.size() - 4, sei::Codec::H264));
  CHECK(!sei::parse_nal(n4.data() + 4, n4.size() - 4, sei::Codec::H265));
  fprintf(stderr, "test_sei: %d records, %d streams, %d codecs, %d failures\n", records, streams, codecs, fails);
  return fails ? 1 : 0;
}
