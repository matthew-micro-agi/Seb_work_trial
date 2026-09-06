// ringfsck — the south contract's checker (DESIGN_RING_CONTROL.md §7): every segment in seg/ and lock/*/
// starts with VPS/SPS/PPS + IDR, every AU carries the camsync SEI, K increases by one inside a file and
// across consecutive files of one epoch, no .part is left behind. Exit 0 when there is nothing to fix.
//
// A step in K that the recording itself explains is reported and not counted as a finding: a frame marked
// K_CORRECTED stepped because a beacon said so, and a file whose first frame is marked K_LOCAL has a head
// the trigger source had not yet anchored (PLAN_K_MATCHING.md §4). Findings are the unexplained steps.
//
//   ringfsck DIR [--repair]      --repair runs the recorder's .part repair; only with the recorder stopped
#include <cstdio>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <vector>
#include <dirent.h>
#include <sys/stat.h>
#include "kmatch/types.hpp"
#include "segfile/segfile.hpp"
#include "sei/sei.hpp"

namespace {
int findings = 0;
void report(const std::string& s) { printf("%s\n", s.c_str()); findings++; }

struct FileSummary { bool ok = false; uint32_t epoch_first = 0, epoch_last = 0; int64_t k_first = -1, k_last = -1; size_t frames = 0; uint16_t flags_first = 0; };

FileSummary check_file(const std::string& path) {
  FileSummary fs; std::ifstream f(path, std::ios::binary); std::vector<uint8_t> d{std::istreambuf_iterator<char>(f), {}};
  auto aus = sei::split(d.data(), d.size());
  if (aus.empty()) { report(path + ": no access unit"); return fs; }
  auto head = sei::inspect(d.data(), aus[0]);
  if (!head.has_vps || !head.idr) report(path + ": does not start with VPS/SPS/PPS + IDR");
  bool have = false; uint32_t epoch = 0; int64_t k = 0; size_t no_sei = 0;
  for (const auto& au : aus) {
    auto info = sei::inspect(d.data(), au);
    if (!info.record) { no_sei++; continue; }
    const auto& r = *info.record;
    if (have && r.epoch == epoch && int64_t(r.k) != k + 1 &&
        (r.flags & (kmatch::FAULT_DUPLICATE | kmatch::FAULT_UNMATCHED | kmatch::FAULT_EPOCH_CHANGE | kmatch::FAULT_K_CORRECTED)) == 0)
      report(path + ": K " + std::to_string(k) + " -> " + std::to_string(r.k) + " (faults " + kmatch::fault_names(r.flags) + ")");
    if (!have) { fs.epoch_first = r.epoch; fs.k_first = r.k; fs.flags_first = r.flags; }
    epoch = r.epoch; k = r.k; have = true;
  }
  if (no_sei) report(path + ": " + std::to_string(no_sei) + " access units without SEI");
  fs.epoch_last = epoch; fs.k_last = k; fs.frames = aus.size(); fs.ok = have && head.has_vps && head.idr;
  return fs;
}

std::vector<std::string> ls(const std::string& dir) {
  std::vector<std::string> v; DIR* d = opendir(dir.c_str()); if (!d) return v;
  while (dirent* e = readdir(d)) { std::string n = e->d_name; if (n != "." && n != "..") v.push_back(n); } closedir(d); return v;
}
}  // namespace

int main(int argc, char** argv) {
  std::string ring; bool repair = false;
  for (int i = 1; i < argc; i++) {                     // --repair before or after the directory
    std::string a = argv[i];
    if (a == "--repair") repair = true;
    else if (a.size() && a[0] == '-') { fprintf(stderr, "unknown option %s\n", a.c_str()); return 2; }
    else if (ring.empty()) ring = a;
    else { fprintf(stderr, "one directory only\n"); return 2; }
  }
  if (ring.empty()) { fprintf(stderr, "usage: ringfsck DIR [--repair]\n"); return 2; }
  if (repair) for (const auto& r : segfile::repair_parts(ring))
    printf("repaired %012llu.h265.part: %s (%zu frames)\n", (unsigned long long)r.segno, r.kept ? "kept" : "no complete AU, unlinked", r.frames);

  for (const auto& n : ls(ring + "/seg")) if (n.size() > 5 && n.compare(n.size() - 5, 5, ".part") == 0) report(ring + "/seg/" + n + ": unfinished segment (ringfsck --repair with the recorder stopped)");

  std::set<ino_t> seen; std::map<uint64_t, FileSummary> segs; size_t files = 0;
  auto visit = [&](const std::string& dir) {
    for (uint64_t s : segfile::list_segnos(dir)) {
      std::string p = dir + "/" + segfile::seg_name(s); struct stat st; if (stat(p.c_str(), &st) != 0) continue;
      files++;
      if (!seen.insert(st.st_ino).second) continue;           // a lock's link to a file already checked
      segs[s] = check_file(p);
    }
  };
  visit(ring + "/seg");
  for (const auto& l : ls(ring + "/lock")) visit(ring + "/lock/" + l);

  // K chain across consecutive segnos of one epoch. K back to 0 at a file boundary is a recorder restart
  // (the K numbering starts over with the recorder, DESIGN §8.4): reported, not a finding.
  const FileSummary* prev = nullptr; uint64_t prev_segno = 0; int restarts = 0, unanchored = 0;
  for (const auto& [s, fs] : segs) {
    if (prev && prev->ok && fs.ok && s == prev_segno + 1 && fs.epoch_first == prev->epoch_last && fs.k_first != prev->k_last + 1) {
      if (fs.k_first == 0) { printf("%s: K restarts at 0 after %s (recorder restart)\n", segfile::seg_name(s).c_str(), segfile::seg_name(prev_segno).c_str()); restarts++; }
      else if (fs.flags_first & kmatch::FAULT_K_LOCAL) { printf("%s: K %lld -> %lld from %s, head not anchored yet (K_LOCAL)\n", segfile::seg_name(s).c_str(), (long long)prev->k_last, (long long)fs.k_first, segfile::seg_name(prev_segno).c_str()); unanchored++; }
      else report(segfile::seg_name(s) + ": K chain " + std::to_string(prev->k_last) + " -> " + std::to_string(fs.k_first) + " from " + segfile::seg_name(prev_segno));
    }
    prev = &fs; prev_segno = s;
  }
  printf("ringfsck %s: %zu segment files, %zu distinct, %d K restarts, %d unanchored heads, %d findings\n", ring.c_str(), files, segs.size(), restarts, unanchored, findings);
  return findings ? 1 : 0;
}
